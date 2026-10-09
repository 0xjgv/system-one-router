"""Scoring providers and configuration. Run `python3 model_provider.py check` to check keys."""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIMARY = ("respan", "span-01-free")
FALLBACKS = (("typesafe", "jev-1.13.0"), ("openrouter", "typesafe/jev-1.13"))
TIMEOUT_S = 8  # Three attempts leave room within the example hook's 30-second timeout under normal I/O.
PROVIDERS = {
    "respan": ("https://api.respan.ai/api/v1/scores", "RESPAN_API_KEY"),
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "TYPESAFE_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/systemone", "OPENROUTER_API_KEY"),
}
DEFAULT_MODELS = {
    "respan": "span-01-free",  # Use span-01-pro for the paid tier.
    "typesafe": "jev-1.13.0",
    "openrouter": "typesafe/jev-1.13",
}


def setting(name: str, default: str = "") -> str:
    """Read the environment, then this repository's .env. Never execute shell expressions."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip("\"'") or default
    return default


def configuration() -> tuple[str, str]:
    provider = setting("ROUTER_PROVIDER", PRIMARY[0])
    if provider not in PROVIDERS:
        raise ValueError("ROUTER_PROVIDER must be respan, typesafe, or openrouter")
    model = PRIMARY[1] if provider == PRIMARY[0] else DEFAULT_MODELS[provider]
    return provider, setting("ROUTER_MODEL", model)


def provider_chain() -> list[tuple[str, str]]:
    return list(dict.fromkeys((configuration(), *FALLBACKS)))


class ScoreError(RuntimeError):
    def __init__(self, attempts: list[dict]):
        super().__init__("All scoring providers failed")
        self.attempts = attempts


def _score(provider: str, model: str, text: str, behaviors: list[dict]) -> dict[str, float]:
    url, key_name = PROVIDERS[provider]
    key = setting(key_name)
    if not key:
        raise ValueError(f"{key_name} not set (environment or .env)")
    if provider == "respan":
        body = {"model": model, "behaviors": behaviors,
                "span": {"input": [{"role": "user", "content": text}],
                         "output": {"role": "assistant", "content": "Understood. I will start on this task."}}}
    else:
        body = {"model": model, "state": text, "questions": {
            behavior["id"]: {"type": "noul", "instructions": behavior["definition"]}
            for behavior in behaviors}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
        data = json.loads(response.read())
    if provider == "respan":
        results = data["results"]
        probabilities = {result["id"]: result["p_present"] for result in results}
        if len(probabilities) != len(results):
            raise ValueError("Duplicate behavior scores")
    else:
        answers = data["answers"]
        if any(answer["type"] != "noul" for answer in answers.values()):
            raise ValueError("Expected noul answers")
        probabilities = {name: answer["noul"] for name, answer in answers.items()}
    if set(probabilities) != {behavior["id"] for behavior in behaviors}:
        raise ValueError("Response does not match requested behaviors")
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
           for value in probabilities.values()):
        raise ValueError("Scores must be finite probabilities between 0 and 1")
    return probabilities


def scores(text: str, behaviors: list[dict]) -> tuple[dict[str, float], dict]:
    """Try the primary, then Jev directly and via OpenRouter; low scores do not trigger retries."""
    attempts = []
    for provider, model in provider_chain():
        attempt = {"provider": provider, "model": model}
        started = time.monotonic()
        try:
            probabilities = _score(provider, model, text, behaviors)
        except Exception as err:
            # Do not log response bodies, credentials, or arbitrary exception messages.
            attempt["error"] = f"HTTP {err.code}" if isinstance(err, urllib.error.HTTPError) else type(err).__name__
        finally:
            attempt["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            attempts.append(attempt)
        if "error" not in attempt:
            return probabilities, {"scoring_provider": provider, "scoring_model": model, "attempts": attempts}
    raise ScoreError(attempts)


def main(argv: list[str]) -> int:
    if argv != ["check"]:
        print("Usage: python3 model_provider.py check", file=sys.stderr)
        return 1
    try:
        missing = False
        for provider, model in provider_chain():
            key_name = PROVIDERS[provider][1]
            present = bool(setting(key_name))
            missing |= not present
            print(f"{provider}/{model}: {key_name} {'set' if present else 'MISSING'}")
        return int(missing)
    except (ValueError, OSError):
        print("Cannot read provider configuration; check ROUTER_PROVIDER and .env", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
