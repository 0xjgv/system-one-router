"""Shared Span-01 client for lab/respan experiments.

One module so parallel experiments share one throttle, one disk cache, and one call log.

- Key: RESPAN_API_KEY from the environment, else lab/respan/.env.
- Cache: identical requests (model + span + behaviors) are answered from disk and never
  count against the free tier's daily cap. Delete a cache file to force a re-call.
- Throttle: a cross-process file lock spaces live calls >= MIN_INTERVAL_S apart, so all
  experiments together stay under the 60 requests/minute org limit.
- Retries: 429 capacity/minute limits, 424, 503, 504 back off exponentially. The free
  tier's daily cap raises DailyCapReached; rerun after 00:00 UTC and the cache resumes.
- 400/402/403/413/422 raise ScoreError immediately.

Standard library only. `python3 respan_client.py self-test` runs offline checks.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "https://api.respan.ai/api/v1/scores"
FREE = "span-01-free"
MIN_INTERVAL_S = 1.1
MAX_ATTEMPTS = 6
TIMEOUT_S = 120
CACHE_DIR = ROOT / "cache"
STATE_DIR = ROOT / ".state"
CALL_LOG = STATE_DIR / "calls.jsonl"


class ScoreError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class DailyCapReached(ScoreError):
    pass


def api_key() -> str:
    key = os.environ.get("RESPAN_API_KEY", "").strip()
    if key:
        return key
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "RESPAN_API_KEY":
                return value.strip().strip("'\"")
    raise RuntimeError("RESPAN_API_KEY not set (environment or lab/respan/.env)")


def has_key() -> bool:
    try:
        return bool(api_key())
    except RuntimeError:
        return False


def request_body(span: dict, behaviors: list[dict], model: str = FREE) -> dict:
    return {"span": span, "behaviors": behaviors, "model": model}


def cache_key(body: dict) -> str:
    # RESPAN_CACHE_EPOCH separates answers from different served versions of the same model id:
    # span-01-free changed between 2026-09-26 and 2026-09-28 (drift/RESULTS.md). Unset keeps the
    # original keys, so earlier analyses still replay from the Sep 25-26 cache.
    epoch = os.environ.get("RESPAN_CACHE_EPOCH", "").strip()
    keyed = {**body, "_cache_epoch": epoch} if epoch else body
    canonical = json.dumps(keyed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / key[:2] / f"{key}.json"


def _log(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with CALL_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _wait_for_slot() -> None:
    """Block until MIN_INTERVAL_S has passed since the last live call from any process."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "throttle.lock").open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        last = float(f.read().strip() or 0)
        delay = last + MIN_INTERVAL_S - time.time()
        if delay > 0:
            time.sleep(delay)
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))


def _post(body: dict) -> dict:
    data = json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    req = urllib.request.Request(URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return json.loads(resp.read())


def score(span: dict, behaviors: list[dict], *, model: str = FREE, experiment: str = "") -> dict:
    """Return the Span-01 response dict. Adds `_cached` and `_latency_s` keys."""
    body = request_body(span, behaviors, model)
    key = cache_key(body)
    path = _cache_path(key)
    if path.exists():
        cached = json.loads(path.read_text())
        cached["_cached"] = True
        return cached

    tier_limits = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _wait_for_slot()
        started = time.time()
        try:
            result = _post(body)
        except urllib.error.HTTPError as err:
            text = err.read().decode(errors="replace")
            latency = time.time() - started
            _log({"ts": started, "experiment": experiment, "key": key, "status": err.code,
                  "attempt": attempt, "latency_s": latency, "body": text[:300]})
            if err.code == 429 and "behavior_scorer_tier_limit" in text:
                # The daily-cap message wording is not guaranteed; three tier-limit
                # refusals in a row for one request are treated as the daily cap.
                tier_limits += 1
                if "daily" in text.lower() or tier_limits >= 3:
                    raise DailyCapReached(err.code, text) from err
            if err.code in (424, 429, 503, 504) and attempt < MAX_ATTEMPTS:
                retry_after = float(err.headers.get("Retry-After") or 0)
                time.sleep(max(retry_after, 2 ** attempt))
                continue
            raise ScoreError(err.code, text) from err
        except (urllib.error.URLError, TimeoutError) as err:
            _log({"ts": started, "experiment": experiment, "key": key, "status": "network",
                  "attempt": attempt, "error": str(err)[:300]})
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** attempt)
                continue
            raise
        latency = time.time() - started
        _validate(result, behaviors)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**result, "_latency_s": latency}))
        _log({"ts": started, "experiment": experiment, "key": key, "status": 200,
              "attempt": attempt, "latency_s": latency,
              "input_tokens": (result.get("usage") or {}).get("input_tokens")})
        result["_cached"] = False
        result["_latency_s"] = latency
        return result
    raise AssertionError("unreachable")


def _validate(result: dict, behaviors: list[dict]) -> None:
    ids = [b["id"] for b in behaviors]
    got = [r["id"] for r in result.get("results", [])]
    if got != ids:
        raise ScoreError(200, f"result ids {got} != requested {ids}")


def probs(result: dict) -> dict[str, dict[str, float]]:
    """{behavior_id: {"present", "absent", "not_observable"}}."""
    return {
        r["id"]: {"present": r["p_present"], "absent": r["p_absent"],
                  "not_observable": r["p_not_observable"]}
        for r in result["results"]
    }


def usage_today() -> dict:
    """Live calls logged today (UTC), for budgeting against the unknown daily cap."""
    if not CALL_LOG.exists():
        return {"ok": 0, "errors": 0}
    today = datetime.now(timezone.utc).date()
    ok = errors = 0
    for line in CALL_LOG.read_text().splitlines():
        entry = json.loads(line)
        if datetime.fromtimestamp(entry["ts"], timezone.utc).date() != today:
            continue
        if entry["status"] == 200:
            ok += 1
        else:
            errors += 1
    return {"ok": ok, "errors": errors}


def _self_test() -> None:
    a = request_body({"input": [], "output": {"role": "assistant", "content": "x"}},
                     [{"id": "b", "definition": "abc"}])
    b = json.loads(json.dumps(a))
    assert cache_key(a) == cache_key(b)
    b["behaviors"][0]["definition"] = "abd"
    assert cache_key(a) != cache_key(b)
    fake = {"results": [{"id": "b", "p_present": 0.7, "p_absent": 0.2, "p_not_observable": 0.1}]}
    _validate(fake, [{"id": "b", "definition": "abc"}])
    assert probs(fake)["b"]["present"] == 0.7
    try:
        _validate(fake, [{"id": "c", "definition": "abc"}])
    except ScoreError:
        pass
    else:
        raise AssertionError("id mismatch not caught")
    print("self-test ok; key present:", has_key(), "; usage today:", usage_today())


if __name__ == "__main__":
    if sys.argv[1:] == ["self-test"]:
        _self_test()
    elif sys.argv[1:] == ["ping"]:
        r = score({"input": [{"role": "user", "content": "This is the third time my order is late."}],
                   "output": {"role": "assistant", "content": "I am sorry, let me check on that for you."}},
                  [{"id": "apology", "definition": "The assistant apologizes for a problem."}],
                  experiment="ping")
        print(json.dumps(r, indent=2))
    else:
        print("usage: python3 respan_client.py self-test|ping")
