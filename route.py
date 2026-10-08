"""Use a System One scorer to pick the model and effort for a task or subagent.

    python3 route.py "<prompt>"   print the pick for one prompt and log it
    python3 route.py --target codex --json "<prompt>"   print a Codex model/effort pair
    python3 route.py agents       --agents JSON with one routed-<model>-<effort> agent per pair

One scoring call evaluates all seven options; each group takes the highest probability.
Scoring failures and winners under FLOOR use the target's fallback. Standard library only; Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import model_provider

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "artifacts" / "beta_log.jsonl"
FALLBACK = ("sonnet", "xhigh")
FLOOR = 0.3  # a winning p_present under this means no option fit
MODELS = {  # Fable is wise, Opus smart, Sonnet focused.
    "fable": "The task request calls for wisdom: weighing trade-offs, choosing between several defensible approaches, or working out what is really being asked before acting.",
    "opus": "The task request calls for strong technical skill: hard engineering across several parts of a system, subtle debugging, or careful verification of correctness.",
    "sonnet": "The task request is a focused, well-defined job: one clear goal, a bounded scope, and an obvious way to tell when it is done.",
}
TARGETS = {
    "claude": {"models": MODELS, "fallback": FALLBACK},
    "codex": {
        "models": {
            "gpt-6-astra": MODELS["fable"],
            "gpt-6.1-sol": MODELS["opus"],
            "gpt-6-luna": MODELS["sonnet"],
        },
        "fallback": ("gpt-6.1-sol", "xhigh"),
    },
}
EFFORTS = {
    "xhigh": "The task request can only be done right by hunting hidden edge cases and verifying thoroughly; a missed case would be costly.",
    "high": "The task request needs careful work and some verification, but its risks are visible from the request itself.",
    "medium": "The task request is clear and moderately sized; a direct approach with one quick check is enough.",
    "low": "The task request is simple enough that a fast, direct answer is fine and extra checking adds nothing.",
}
GENERIC_AGENT = ("You are a general-purpose agent. Do the task in the prompt fully: search, read, edit and run "
                 "what it needs. Finish with a short report of what you did and found, with file paths.")


def behavior_definitions(models: dict[str, str]) -> list[dict]:
    return ([{"id": f"model_{k}", "definition": v} for k, v in models.items()]
            + [{"id": f"effort_{k}", "definition": v} for k, v in EFFORTS.items()])


BEHAVIORS = behavior_definitions(MODELS)


def route(text: str, cwd: str, kind: str, target: str = "claude") -> tuple[str, str, dict]:
    """Pick and log one prompt, using the target's fallback if scoring fails."""
    models = TARGETS[target]["models"]
    behaviors = behavior_definitions(models)
    entry: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind,
                   "cwd": cwd, "text": text, "target": target}
    model, effort = TARGETS[target]["fallback"]
    try:
        p, metadata = model_provider.scores(text, behaviors)
        entry.update(metadata)
        best_model = max(models, key=lambda m: p[f"model_{m}"])
        best_effort = max(EFFORTS, key=lambda e: p[f"effort_{e}"])
        floored = [g for g, top in (("model", p[f"model_{best_model}"]), ("effort", p[f"effort_{best_effort}"]))
                   if top < FLOOR]
        model = model if "model" in floored else best_model
        effort = effort if "effort" in floored else best_effort
        entry.update(p_present=p, floored=floored)
    except Exception as err:  # network, HTTP, key, or response shape: never block the spawn
        entry["error"] = type(err).__name__
        if isinstance(err, model_provider.ScoreError):
            entry["attempts"] = err.attempts
    entry.update(model=model, effort=effort)
    try:
        LOG.parent.mkdir(exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        entry["log_error"] = True
    return model, effort, entry


def agents() -> dict:
    """One generic agent per model x effort, since an Agent call can set the model but not the effort."""
    return {f"routed-{m}-{e}": {
        "description": f"General-purpose agent on {m} at {e} effort. Chosen by the beta router hook; do not pick it yourself.",
        "prompt": GENERIC_AGENT, "model": m, "effort": e} for m in MODELS for e in EFFORTS}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, default="claude", help="Execution target (default: claude)")
    parser.add_argument("--json", action="store_true", help="Print a model/effort JSON object")
    parser.add_argument("prompt", help="Task prompt, or 'agents' to print Claude agent definitions")
    args = parser.parse_args(argv)
    if args.prompt == "agents":
        if args.target != "claude":
            parser.error("'agents' is only available for the claude target")
        print(json.dumps(agents()))
        return 0
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    model, effort, entry = route(args.prompt, os.getcwd(), "manual", args.target)
    if args.json:
        print(json.dumps({"model": model, "effort": effort}))
    else:
        note = "Scoring failed" if "error" in entry else ",".join(entry["floored"])
        print(f"{model} {effort}" + (f"  ({note})" if note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
