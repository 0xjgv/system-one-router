"""Beta router: Span-01 Lite picks the model and effort for each subagent a beta() session spawns.

    python3 route.py "<prompt>"   print the pick for one prompt and log it
    python3 route.py agents       --agents JSON with one routed-<model>-<effort> agent per pair

One Span call scores all seven options; each group takes the option with the highest p_present.
Any failure, or a winner under FLOOR, takes FALLBACK. Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "artifacts" / "beta_log.jsonl"
URL = "https://api.respan.ai/api/v1/scores"
TIMEOUT_S = 10  # the beta() hook timeout is 30 s
FALLBACK = ("sonnet", "xhigh")  # matches omega's delegates
FLOOR = 0.3  # a winning p_present under this means no option fit
MODELS = {  # Fable is wise, Opus smart, Sonnet focused.
    "fable": "The task request calls for wisdom: weighing trade-offs, choosing between several defensible approaches, or working out what is really being asked before acting.",
    "opus": "The task request calls for strong technical skill: hard engineering across several parts of a system, subtle debugging, or careful verification of correctness.",
    "sonnet": "The task request is a focused, well-defined job: one clear goal, a bounded scope, and an obvious way to tell when it is done.",
}
EFFORTS = {
    "xhigh": "The task request can only be done right by hunting hidden edge cases and verifying thoroughly; a missed case would be costly.",
    "high": "The task request needs careful work and some verification, but its risks are visible from the request itself.",
    "medium": "The task request is clear and moderately sized; a direct approach with one quick check is enough.",
    "low": "The task request is simple enough that a fast, direct answer is fine and extra checking adds nothing.",
}
BEHAVIORS = ([{"id": f"model_{k}", "definition": v} for k, v in MODELS.items()]
             + [{"id": f"effort_{k}", "definition": v} for k, v in EFFORTS.items()])
GENERIC_AGENT = ("You are a general-purpose agent. Do the task in the prompt fully: search, read, edit and run "
                 "what it needs. Finish with a short report of what you did and found, with file paths.")


def api_key() -> str:
    """RESPAN_API_KEY from the environment, else from .env next to this file."""
    key = os.environ.get("RESPAN_API_KEY", "").strip()
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "RESPAN_API_KEY":
                key = value.strip().strip("'\"")
    if not key:
        raise RuntimeError("RESPAN_API_KEY not set (environment or .env)")
    return key


def scores(text: str) -> dict[str, float]:
    """p_present per behavior id. The prompt is framed as a user turn the agent has just accepted."""
    body = {"model": "span-01-free", "behaviors": BEHAVIORS,
            "span": {"input": [{"role": "user", "content": text}],
                     "output": {"role": "assistant", "content": "Understood. I will start on this task."}}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return {r["id"]: r["p_present"] for r in json.loads(resp.read())["results"]}


def route(text: str, cwd: str, kind: str) -> tuple[str, str, dict]:
    """Pick and log one prompt. Any failure takes FALLBACK with entry["error"] set."""
    entry: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "kind": kind, "cwd": cwd, "text": text}
    model, effort = FALLBACK
    try:
        p = scores(text)
        best_model = max(MODELS, key=lambda m: p[f"model_{m}"])
        best_effort = max(EFFORTS, key=lambda e: p[f"effort_{e}"])
        floored = [g for g, top in (("model", p[f"model_{best_model}"]), ("effort", p[f"effort_{best_effort}"]))
                   if top < FLOOR]
        model = model if "model" in floored else best_model
        effort = effort if "effort" in floored else best_effort
        entry.update(p_present=p, floored=floored)
    except Exception as err:  # network, HTTP, key, or response shape: never block the spawn
        entry["error"] = f"{type(err).__name__}: {err}"[:300]
    entry.update(model=model, effort=effort)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return model, effort, entry


def agents() -> dict:
    """One generic agent per model x effort, since an Agent call can set the model but not the effort."""
    return {f"routed-{m}-{e}": {
        "description": f"General-purpose agent on {m} at {e} effort. Chosen by the beta router hook; do not pick it yourself.",
        "prompt": GENERIC_AGENT, "model": m, "effort": e} for m in MODELS for e in EFFORTS}


def main(argv: list[str]) -> int:
    if argv == ["agents"]:
        print(json.dumps(agents()))
        return 0
    if len(argv) == 1 and argv[0].strip():
        model, effort, entry = route(argv[0], os.getcwd(), "manual")
        note = "Span failed" if "error" in entry else ",".join(entry["floored"])
        print(f"{model} {effort}" + (f"  ({note})" if note else ""))
        return 0
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
