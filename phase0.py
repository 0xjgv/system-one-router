"""Phase 0 of the effort-router experiment: free Span-01 scores on task requests.

Only span-01-free is ever called. score_free() refuses any other model before a request is
built, and the run loop stops on the daily cap, HTTP 402 or HTTP 403. Protocol: DESIGN.md.

    python3 phase0.py self-test                 offline checks
    python3 phase0.py extract tb3 <clone-dir>   data/tb3.jsonl from a terminal-bench v3.0.0 clone
    python3 phase0.py extract history           artifacts/history_sample.jsonl + review file
    python3 phase0.py dry-run                   counts and one sample request body, no calls
    python3 phase0.py pilot                     framings F1-F3 on 13 TB3 items (39 live calls max)
    python3 phase0.py freeze <F1|F2|F3> "<why>" record the pilot winner
    python3 phase0.py main                      every item once in the frozen framing, plus canary
    python3 phase0.py report                    pilot table, gates, go/no-go

Standard library only; runs on Python 3.9.
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import respan_client as rc

ROOT = Path(__file__).resolve().parent
TB3_FILE = ROOT / "data" / "tb3.jsonl"
ART = ROOT / "artifacts"
SAMPLE = ART / "history_sample.jsonl"
REVIEW = ART / "history_review.md"
APPROVAL = ART / "history_approved.txt"
SCORES = ART / "scores.jsonl"
FROZEN = ROOT / "frozen.json"
DESIGN = ROOT / "DESIGN.md"
HOME = Path.home() / ".claude"

FREE = rc.FREE
STOP_STATUSES = (402, 403)

VERIFICATION = {
    "hidden_edge_cases": "The user's task request describes work where a first, plausible solution would likely miss edge cases, boundary conditions, or unusual inputs that only careful checking would reveal.",
    "brownfield_bug_fix": "The user's task request asks to find and fix a bug or failure in existing code or an existing system, rather than build something new.",
    "correctness_critical": "The user's task request involves work where a subtle mistake would cause serious harm: security holes, data loss or corruption, money, or concurrency and race conditions.",
    "needs_test_verification": "The user's task request can only be judged done after running tests, benchmarks, or other checks against the result; reading the change is not enough.",
}
UNDERSPEC = {
    "ambiguous_goal": "The user's task request states its goal vaguely, so a competent engineer could not tell exactly what outcome the user wants.",
    "missing_acceptance_criteria": "The user's task request gives no clear way to tell when the work is done or correct: no expected output, test, metric, or example.",
    "multiple_valid_readings": "The user's task request can reasonably be read in two or more different ways that would lead to different work.",
}
SCOPE = {
    "trivial_edit": "The user's task request asks for a small, mechanical change, such as a rename, a one-line fix, a config value, or a typo, that needs little thought.",
    "multi_component_change": "The user's task request requires coordinated changes across several files, modules, services, or systems.",
    "exploratory_or_brainstorm": "The user's task request asks for ideas, options, explanations, research, or a discussion rather than a finished change.",
}
DOMAINS = {
    "domain_security": "The user's task request is about computer security: vulnerabilities, exploits, forensics, cryptography, authentication, or malware.",
    "domain_hardware": "The user's task request is about hardware: circuits, embedded systems, firmware, CAD, CPUs, FPGAs, or physical devices.",
    "domain_ml": "The user's task request is about machine learning: training, evaluating, or serving models, or tensors and GPU kernels for ML.",
    "domain_science": "The user's task request is about a scientific field such as physics, chemistry, biology, mathematics, or numerical simulation.",
    "domain_ops": "The user's task request is about operations: servers, deployment, networking, containers, database administration, CI, or system configuration.",
}
BEHAVIORS = [{"id": k, "definition": v} for group in (VERIFICATION, UNDERSPEC, SCOPE, DOMAINS) for k, v in group.items()]
# TB3 metadata category -> domain behaviour. Software and Media have no tag.
CATEGORY_TAG = {"Security": "domain_security", "Hardware": "domain_hardware", "ML": "domain_ml",
                "Science": "domain_science", "Operations": "domain_ops"}

SYSTEM_LINE = "The next message is a task request a user sends to an AI coding agent. Judge the request itself, before any work starts."
ACK = "Understood. I will start on this task."
TB3_CONTEXT = "Context: the agent works in a Linux container with a terminal. Automated tests grade the result."
FRAMINGS = ("F1", "F2", "F3")

# Client projects never leave the machine. Matched against the history `project` path.
EXCLUDE = re.compile(r"corgea", re.I)
MODELS = ("claude-fable-5-1", "claude-opus-5-5")
MIN_CHARS = 15
TEAMMATE = "Another Claude session sent a message"
CORRECTION = re.compile(
    r"^\s*(no\b|nope|wrong|not (quite|what|that)|that'?s (not|wrong)|actually\b|wait\b|stop\b|undo|revert"
    r"|why (did|are|is) (you|it)|you (didn'?t|did not|missed|forgot|broke)|i (said|meant|asked)"
    r"|instead\b|don'?t\b|it (still|doesn'?t)|still (not|broken|fail)|this is wrong)", re.I)
PATH = re.compile(r"[\w.-]*/[\w./-]+|\b[\w-]+\.(py|ts|tsx|js|jsx|go|rs|md|json|sh|toml|ya?ml|sql|java|c|h|cpp)\b")
KEYWORDS = re.compile(r"\b(bug|fix|error|fail\w*|broken|crash\w*|security|vuln\w*|test\w*|why|race|edge)\b", re.I)


# ---------- scoring ----------

def score_free(span: dict, stage: str, model: str = FREE) -> dict:
    if model != FREE:
        raise ValueError(f"phase0 only calls {FREE}; refused {model!r}")
    return rc.score(span, BEHAVIORS, model=model, experiment=f"effort-router/{model}/{stage}")


def span_for(item: dict, framing: str) -> dict:
    text = item["text"]
    if framing == "F1":
        return {"input": [{"role": "system", "content": SYSTEM_LINE}], "output": {"role": "user", "content": text}}
    if framing == "F2":
        return {"input": [{"role": "user", "content": text}], "output": {"role": "assistant", "content": ACK}}
    if framing == "F3":
        context = TB3_CONTEXT if item["source"] == "tb3" else f"Context: the agent works in the user's repository {item['repo']}."
        return {"input": [{"role": "system", "content": SYSTEM_LINE}, {"role": "system", "content": context}],
                "output": {"role": "user", "content": text}}
    raise ValueError(framing)


def run(items: list[dict], framing: str, stage: str) -> str:
    """Score items in order, appending rows to SCORES. Returns 'done' or the stop reason."""
    ART.mkdir(exist_ok=True)
    for n, item in enumerate(items, 1):
        row = {"stage": stage, "framing": framing, "id": item["id"], "source": item["source"]}
        try:
            result = score_free(span_for(item, framing), stage)
            row["probs"] = rc.probs(result)
            row["cached"] = result["_cached"]
        except rc.DailyCapReached as err:
            return f"stopped at item {n}/{len(items)}: daily cap ({err})"
        except rc.ScoreError as err:
            if err.status in STOP_STATUSES:
                return f"stopped at item {n}/{len(items)}: HTTP {err.status}"
            row["error"] = str(err)[:300]
        with SCORES.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(f"{stage} {framing} {n}/{len(items)} {item['id']} {'error' if 'error' in row else 'ok'}", flush=True)
    return "done"


# ---------- data ----------

def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def toml_field(text: str, name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*"?([^"\n]*)"?\s*$', text, re.M)
    return m.group(1).strip() if m else ""


def extract_tb3(clone: Path) -> None:
    rows = []
    for d in sorted((clone / "tasks").iterdir()):
        if not (d / "instruction.md").exists():
            continue
        toml = (d / "task.toml").read_text()
        rows.append({"id": f"tb3/{d.name}", "source": "tb3", "text": (d / "instruction.md").read_text().strip(),
                     "category": toml_field(toml, "category"), "subcategory": toml_field(toml, "subcategory"),
                     "expert_hours": float(toml_field(toml, "expert_time_estimate_hours") or "nan")})
    TB3_FILE.parent.mkdir(exist_ok=True)
    TB3_FILE.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"wrote {len(rows)} tasks to {TB3_FILE.relative_to(ROOT)}")


def human_text(r: dict):
    """The text of a typed user prompt, or None for tool results, meta, commands, interrupts."""
    if not is_boundary(r):
        return None
    content = r.get("message", {}).get("content")
    if isinstance(content, list):
        if any(b.get("type") == "tool_result" for b in content):
            return None
        content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
    content = (content or "").strip()
    if not content or content.startswith("<") or content.startswith("[Request interrupted"):
        return None
    return content


def is_boundary(r: dict) -> bool:
    """Any user action (prompt or command) ends the previous prompt's window."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain"):
        return False
    content = r.get("message", {}).get("content")
    if isinstance(content, str) and content.startswith(TEAMMATE):
        return False  # a teammate's message, not the user; the agent keeps working
    return not (isinstance(content, list) and any(b.get("type") == "tool_result" for b in content))


def prompt_windows(records: list[dict]) -> list[dict]:
    """One entry per human prompt: main-thread output tokens and tool calls until the next user action."""
    out = []
    for i, r in enumerate(records):
        text = human_text(r)
        if text is None:
            continue
        usage, tools, first = {}, set(), None
        j = i + 1
        while j < len(records) and not is_boundary(records[j]):
            a = records[j]
            if a.get("type") == "assistant" and not a.get("isSidechain"):
                m = a["message"]
                first = first or a
                usage[m.get("id")] = (m.get("usage") or {}).get("output_tokens") or 0  # stream records repeat usage
                tools.update(b.get("id") for b in m.get("content", []) if b.get("type") == "tool_use")
            j += 1
        nxt = next((human_text(x) for x in records[j:j + 1] if human_text(x)), None)
        if first is None:
            continue
        out.append({"text": text, "ts": r.get("timestamp", ""), "model": first["message"].get("model"),
                    "effort": first.get("effort"), "output_tokens": sum(usage.values()), "tool_calls": len(tools),
                    "next_prompt": nxt, "correction": bool(nxt and CORRECTION.search(nxt))})
    return out


def norm(s: str) -> str:
    return " ".join(s.split())[:80]


def extract_history(n: int = 50, seed: int = 1) -> None:
    by_session: dict[str, list[dict]] = {}
    excluded = 0
    seen = set()
    for line in (HOME / "history.jsonl").read_text().splitlines():
        h = json.loads(line)
        d = h.get("display", "").strip()
        if not h.get("sessionId") or d.startswith(("!", "/")) or len(d) < MIN_CHARS or h.get("pastedContents"):
            continue
        if EXCLUDE.search(h.get("project", "")):
            excluded += 1
            continue
        if d in seen:
            continue
        seen.add(d)
        by_session.setdefault(h["sessionId"], []).append(h)
    pool = []
    for sid, hs in by_session.items():
        paths = glob.glob(str(HOME / "projects" / "*" / f"{sid}.jsonl"))
        if not paths:
            continue
        records = [json.loads(l) for l in Path(paths[0]).read_text().splitlines() if l.strip()]
        windows = {}
        for w in prompt_windows(records):
            windows.setdefault(norm(w["text"]), w)
        for h in hs:
            w = windows.get(norm(h["display"]))
            if w and w["model"] in MODELS:
                pool.append({**w, "text": h["display"].strip(), "project": h["project"], "session": sid})
    # Revealed cost is relative to its (model, effort) stratum: effort alone moves tokens.
    strata: dict[tuple, list[dict]] = {}
    for p in pool:
        strata.setdefault((p["model"], p["effort"]), []).append(p)
    for group in strata.values():
        logs = sorted(math.log1p(p["output_tokens"]) for p in group)
        med = logs[len(logs) // 2]
        for p in group:
            p["cost_resid"] = math.log1p(p["output_tokens"]) - med
    rng = random.Random(seed)
    queues = [rng.sample(g, len(g)) for _, g in sorted(strata.items(), key=lambda kv: str(kv[0]))]
    sample = []
    while len(sample) < n and any(queues):
        for q in queues:
            if q and len(sample) < n:
                sample.append(q.pop())
    for i, s in enumerate(sample):
        s.update(id=f"hist/{i:02d}", source="history", repo=Path(s["project"]).name)
    ART.mkdir(exist_ok=True)
    SAMPLE.write_text("".join(json.dumps(s) + "\n" for s in sample))
    lines = ["# History sample for review", "", "Nothing here is sent until you approve it (see DESIGN.md).", ""]
    for s in sample:
        lines += [f"## {s['id']} · {s['project']} · {s['model']} {s['effort']}", "", s["text"], ""]
    REVIEW.write_text("\n".join(lines))
    print(f"pool {len(pool)} prompts in {len(strata)} strata; excluded {excluded} client-project entries")
    for k, g in sorted(strata.items(), key=lambda kv: -len(kv[1])):
        print(f"  {k}: pool {len(g)}, sampled {sum(1 for s in sample if (s['model'], s['effort']) == k)}")
    projects: dict[str, int] = {}
    for s in sample:
        projects[s["project"]] = projects.get(s["project"], 0) + 1
    print("sample projects:", json.dumps(projects, indent=1))
    print(f"wrote {len(sample)} to {SAMPLE.relative_to(ROOT)}; review {REVIEW.relative_to(ROOT)}, then approve with:")
    print(f"  shasum -a 256 {SAMPLE.relative_to(ROOT)} | cut -d' ' -f1 > {APPROVAL.relative_to(ROOT)}")


def history_items() -> list[dict]:
    """The history sample, only if APPROVAL holds its current sha256."""
    if not SAMPLE.exists() or not APPROVAL.exists():
        return []
    digest = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    return load_jsonl(SAMPLE) if APPROVAL.read_text().strip() == digest else []


def lexical(text: str) -> dict:
    return {"log_len": math.log1p(len(text)), "paths": len(PATH.findall(text)), "keywords": len(KEYWORDS.findall(text))}


def stratified(items: list[dict], k: int = 5) -> list[dict]:
    """Round-robin over (source, lexical-length quintile) so a truncated run still spans the data."""
    buckets: dict[tuple, list[dict]] = {}
    for source in sorted({i["source"] for i in items}):
        group = sorted((i for i in items if i["source"] == source), key=lambda i: (len(i["text"]), i["id"]))
        for rank, item in enumerate(group):
            buckets.setdefault((source, rank * k // len(group)), []).append(item)
    queues = [buckets[key] for key in sorted(buckets, key=lambda kv: (kv[1], kv[0]))]
    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def pilot_items(tb3: list[dict]) -> list[dict]:
    """13 TB3 items from the five tagged categories: 3 each from the largest three, 2 from the rest."""
    by_cat = {c: sorted((t for t in tb3 if t["category"] == c), key=lambda t: t["id"]) for c in CATEGORY_TAG}
    order = sorted(by_cat, key=lambda c: (-len(by_cat[c]), c))
    rng = random.Random(0)
    out = []
    for i, c in enumerate(order):
        out += rng.sample(by_cat[c], 3 if i < 3 else 2)
    return out


# ---------- stats ----------

def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def std(xs):
    if len(xs) < 2:
        return float("nan")
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a, b):
    ma, mb = mean(a), mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else float("nan")


def spearman(a, b):
    return pearson(ranks(a), ranks(b))


def auc(scores, labels):
    """Rank AUC; nan when a class is empty."""
    pos = sum(labels)
    neg = len(labels) - pos
    if not pos or not neg:
        return float("nan")
    r = ranks(scores)
    return (sum(ri for ri, y in zip(r, labels) if y) - pos * (pos + 1) / 2) / (pos * neg)


def zsum(rows: list[dict], keys) -> list[float]:
    out = [0.0] * len(rows)
    for k in keys:
        xs = [r[k] for r in rows]
        m, s = mean(xs), std(xs) or 1.0
        out = [o + (x - m) / (s if s == s else 1.0) for o, x in zip(out, xs)]
    return out


def bootstrap_diff(metric, a, b, y, n=1000, seed=0):
    """90% interval of metric(a, y) - metric(b, y) over item resamples."""
    rng = random.Random(seed)
    idx = range(len(y))
    diffs = []
    for _ in range(n):
        s = [rng.choice(idx) for _ in idx]
        d = metric([a[i] for i in s], [y[i] for i in s]) - metric([b[i] for i in s], [y[i] for i in s])
        if d == d:
            diffs.append(d)
    diffs.sort()
    return (diffs[int(0.05 * len(diffs))], diffs[int(0.95 * len(diffs)) - 1]) if diffs else (float("nan"),) * 2


def axes(probs: dict) -> dict:
    p = {k: v["present"] for k, v in probs.items()}
    verif = mean([p[k] for k in VERIFICATION])
    return {"verif": verif, "under": mean([p[k] for k in UNDERSPEC]),
            "span_cost": verif + p["multi_component_change"] - p["trivial_edit"]}


def gates(rows: list[dict], hist: list[dict]) -> dict:
    """rows: scored items with probs; hist: history items (labels) keyed by id."""
    ok = [r for r in rows if "probs" in r]
    ax = [axes(r["probs"]) for r in ok]
    no_obs = [v["not_observable"] for r in ok for v in r["probs"].values()]
    g1 = {"verif_std": std([a["verif"] for a in ax]), "under_std": std([a["under"] for a in ax]),
          "p_not_observable_std": std(no_obs), "per_behavior_std": {
              b["id"]: std([r["probs"][b["id"]]["present"] for r in ok]) for b in BEHAVIORS}}
    g1["pass"] = g1["verif_std"] > 0.05 and g1["under_std"] > 0.05 and g1["p_not_observable_std"] >= 0.02
    g3 = {"spearman_verif_under": spearman([a["verif"] for a in ax], [a["under"] for a in ax])}
    g3["pass"] = abs(g3["spearman_verif_under"]) < 0.7

    labels = {h["id"]: h for h in hist}
    hs = [(r, labels[r["id"]]) for r in ok if r["id"] in labels]
    g2: dict = {"n": len(hs)}
    if len(hs) < 10:
        g2.update({"pass": None, "note": "fewer than 10 scored history items: no verdict"})
    else:
        feats = [{**lexical(h["text"]), **axes(r["probs"])} for r, h in hs]
        lex = {"lex_zsum": zsum(feats, ("log_len", "paths", "keywords")), "lex_len": [f["log_len"] for f in feats]}
        cost = [h["cost_resid"] for _, h in hs]
        corr = [1 if h["correction"] else 0 for _, h in hs]
        span_cost = [f["span_cost"] for f in feats]
        span_under = [f["under"] for f in feats]
        best_c = max(lex, key=lambda k: spearman(lex[k], cost))
        g2["cost"] = {"span": spearman(span_cost, cost), "best_lexical": best_c, "lexical": spearman(lex[best_c], cost),
                      "ci90_diff": bootstrap_diff(spearman, span_cost, lex[best_c], cost)}
        g2["cost"]["pass"] = g2["cost"]["span"] - g2["cost"]["lexical"] >= 0.05
        g2["correction_positives"] = sum(corr)
        if sum(corr) >= 5 and len(corr) - sum(corr) >= 5:
            best_k = max(lex, key=lambda k: auc(lex[k], corr))
            g2["correction"] = {"span": auc(span_under, corr), "best_lexical": best_k, "lexical": auc(lex[best_k], corr),
                                "ci90_diff": bootstrap_diff(auc, span_under, lex[best_k], corr)}
            g2["correction"]["pass"] = g2["correction"]["span"] - g2["correction"]["lexical"] >= 0.05
        else:
            g2["correction"] = {"pass": None, "note": "fewer than 5 positives or negatives: underpowered, not gated"}
        g2["pass"] = g2["cost"]["pass"] and g2["correction"]["pass"] is not False
    verdict = "GO" if g1["pass"] and g3["pass"] and g2.get("pass") else "NO-GO"
    if g1["pass"] and g3["pass"] and g2.get("pass") is None:
        verdict = "NO VERDICT (gate 2 not evaluable)"
    return {"gate1_non_degenerate": g1, "gate2_beats_lexical": g2, "gate3_distinct_axes": g3, "verdict": verdict}


def tb3_checks(rows: list[dict], tb3: list[dict]) -> dict:
    """Reported, not gated: domain tags vs TB3 category, span cost vs expert hours."""
    meta = {t["id"]: t for t in tb3}
    ok = [r for r in rows if "probs" in r and r["id"] in meta]
    out = {"n": len(ok), "domain_auc": {}}
    for cat, tag in CATEGORY_TAG.items():
        out["domain_auc"][cat] = auc([r["probs"][tag]["present"] for r in ok], [meta[r["id"]]["category"] == cat for r in ok])
    hours = [(axes(r["probs"])["span_cost"], lexical(meta[r["id"]]["text"])["log_len"], meta[r["id"]]["expert_hours"])
             for r in ok if meta[r["id"]]["expert_hours"] == meta[r["id"]]["expert_hours"]]
    if len(hours) >= 10:
        out["expert_hours_spearman"] = {"span_cost": spearman([h[0] for h in hours], [h[2] for h in hours]),
                                        "lex_len": spearman([h[1] for h in hours], [h[2] for h in hours]), "n": len(hours)}
    return out


def top_domain_accuracy(rows: list[dict], tb3: list[dict]) -> float:
    meta = {t["id"]: t for t in tb3}
    hits = [max(DOMAINS, key=lambda d: r["probs"][d]["present"]) == CATEGORY_TAG[meta[r["id"]]["category"]]
            for r in rows if "probs" in r and meta.get(r["id"], {}).get("category") in CATEGORY_TAG]
    return mean([1.0 if h else 0.0 for h in hits])


# ---------- commands ----------

def latest(stage: str) -> list[dict]:
    rows = {}
    for r in load_jsonl(SCORES):
        if r["stage"] == stage:
            rows[(r["framing"], r["id"])] = r
    return list(rows.values())


def frozen_framing() -> str:
    if not FROZEN.exists():
        sys.exit("no frozen framing: run pilot, then freeze")
    framing = json.loads(FROZEN.read_text())["framing"]
    if f"Frozen framing: {framing}" not in DESIGN.read_text():
        sys.exit(f"write 'Frozen framing: {framing}' and the reason into DESIGN.md before the main run")
    return framing


def cmd_dry_run() -> None:
    tb3, hist = load_jsonl(TB3_FILE), history_items()
    print(f"tb3 {len(tb3)}; history sample {len(load_jsonl(SAMPLE))} (approved: {bool(hist)}); behaviours {len(BEHAVIORS)}")
    print(f"pilot calls: {len(pilot_items(tb3)) * len(FRAMINGS)} max; main calls: {len(tb3) + len(hist)} max + 3 canary")
    print("usage today:", rc.usage_today())
    body = rc.request_body(span_for(tb3[0], "F1"), BEHAVIORS[:2], FREE)
    print("sample body (first 2 behaviours):", json.dumps(body, indent=1)[:1500])


def cmd_pilot() -> None:
    items = pilot_items(load_jsonl(TB3_FILE))
    for framing in FRAMINGS:
        status = run(items, framing, "pilot")
        print(framing, status)
        if status != "done":
            return


def cmd_freeze(framing: str, why: str) -> None:
    assert framing in FRAMINGS, framing
    digest = hashlib.sha256(json.dumps(BEHAVIORS, sort_keys=True).encode()).hexdigest()[:12]
    FROZEN.write_text(json.dumps({"framing": framing, "why": why, "behaviors_sha": digest,
                                  "date": datetime.now(timezone.utc).date().isoformat()}, indent=1) + "\n")
    print(f"frozen {framing}; now add 'Frozen framing: {framing}' and the reason to DESIGN.md")


def cmd_main() -> None:
    framing = frozen_framing()
    tb3, hist = load_jsonl(TB3_FILE), history_items()
    if not hist:
        print("history sample not approved: scoring TB3 only; gate 2 will have no verdict")
    items = stratified(tb3 + hist)
    status = run(items, framing, "main")
    print("main", status)
    if status != "done":
        return
    # Canary: rescore 3 items outside the cache to catch a served-model change during the run.
    os.environ["RESPAN_CACHE_EPOCH"] = "canary-" + datetime.now(timezone.utc).isoformat(timespec="minutes")
    print("canary", run(items[:3], framing, "canary"))


def cmd_report() -> None:
    tb3 = load_jsonl(TB3_FILE)
    pilot = latest("pilot")
    if pilot:
        print("PILOT (13 TB3 items per framing)")
        for f in FRAMINGS:
            rows = [r for r in pilot if r["framing"] == f]
            ok = [r for r in rows if "probs" in r]
            if not ok:
                print(f"  {f}: {len(rows) - len(ok)} errors, 0 scored", *(r.get("error", "")[:120] for r in rows[:1]))
                continue
            ax = [axes(r["probs"]) for r in ok]
            no_obs = [v["not_observable"] for r in ok for v in r["probs"].values()]
            print(f"  {f}: scored {len(ok)}/{len(rows)}; top-domain acc {top_domain_accuracy(ok, tb3):.2f}; "
                  f"verif mean {mean([a['verif'] for a in ax]):.3f} std {std([a['verif'] for a in ax]):.3f}; "
                  f"under mean {mean([a['under'] for a in ax]):.3f} std {std([a['under'] for a in ax]):.3f}; "
                  f"p_not_obs mean {mean(no_obs):.3f} std {std(no_obs):.3f}")
    main = latest("main")
    if not main:
        return
    pilot_ids = {r["id"] for r in pilot}
    hist = load_jsonl(SAMPLE)
    # Gate 1 and 3 exclude pilot items: the framing was chosen on them.
    result = gates([r for r in main if r["id"] not in pilot_ids], hist)
    result["errors"] = sum(1 for r in main if "error" in r)
    result["tb3_checks"] = tb3_checks(main, tb3)
    canary = {r["id"]: r for r in latest("canary")}
    drift = [abs(canary[r["id"]]["probs"][b]["present"] - r["probs"][b]["present"])
             for r in main if r["id"] in canary and "probs" in r and "probs" in canary[r["id"]] for b in r["probs"]]
    result["canary_max_abs_diff"] = max(drift) if drift else None
    print(json.dumps(result, indent=1))


# ---------- self-test ----------

def self_test() -> None:
    import io
    import tempfile
    import urllib.error

    global ART, SCORES
    tmp = Path(tempfile.mkdtemp())
    rc.CACHE_DIR, rc.STATE_DIR, rc.CALL_LOG = tmp / "cache", tmp / "state", tmp / "state" / "calls.jsonl"
    ART, SCORES = tmp / "art", tmp / "art" / "scores.jsonl"
    rc.MIN_INTERVAL_S = 0
    os.environ["RESPAN_API_KEY"] = "test-key-not-real"
    items = [{"id": f"t{i}", "source": "tb3", "text": f"task {i}"} for i in range(3)]
    calls = []

    def http_error(status, body):
        def post(body_):
            calls.append(body_)
            raise urllib.error.HTTPError(rc.URL, status, "x", {}, io.BytesIO(body.encode()))
        return post

    # 1. A non-free model is refused before any request.
    rc._post = http_error(500, "must not be reached")
    try:
        score_free(span_for(items[0], "F1"), "t", model="span-01-pro")
    except ValueError:
        pass
    else:
        raise AssertionError("span-01-pro not refused")
    assert not calls
    # 2. HTTP 402 and 403 stop the run after one request.
    for status in STOP_STATUSES:
        calls.clear()
        rc._post = http_error(status, "payment required")
        assert run(items, "F1", "t").startswith("stopped at item 1/3"), status
        assert len(calls) == 1
    # 3. The daily cap stops the run.
    calls.clear()
    rc._post = http_error(429, '{"error":"behavior_scorer_tier_limit: daily limit reached"}')
    assert "daily cap" in run(items, "F1", "t") and len(calls) == 1
    # 4. A good response is logged with the model in the experiment field, and the body is free-only.
    def ok(body):
        calls.append(body)
        return {"results": [{"id": b["id"], "p_present": 0.5, "p_absent": 0.4, "p_not_observable": 0.1}
                            for b in body["behaviors"]]}
    calls.clear()
    rc._post = ok
    assert run(items, "F2", "t") == "done"
    assert all(c["model"] == FREE for c in calls) and len(calls) == 3
    log = load_jsonl(rc.CALL_LOG)
    assert all(e["experiment"] == f"effort-router/{FREE}/t" for e in log if e["status"] == 200)
    assert "test-key-not-real" not in rc.CALL_LOG.read_text() + SCORES.read_text()
    # 5. Request shapes.
    hist_item = {"id": "h", "source": "history", "text": "fix it", "repo": "demo"}
    assert span_for(hist_item, "F1")["output"] == {"role": "user", "content": "fix it"}
    assert span_for(hist_item, "F2")["input"] == [{"role": "user", "content": "fix it"}]
    assert "demo" in span_for(hist_item, "F3")["input"][1]["content"]
    assert len({b["id"] for b in BEHAVIORS}) == 15
    # 6. Transcript windows: stream records deduped, tool calls counted, correction flagged, commands skipped.
    a = lambda mid, out, tools=(): {"type": "assistant", "effort": "high", "message": {
        "id": mid, "model": "claude-fable-5-1", "usage": {"output_tokens": out},
        "content": [{"type": "tool_use", "id": t} for t in tools]}}
    u = lambda text: {"type": "user", "message": {"role": "user", "content": text}}
    tr = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]}}
    records = [u("<command-name>/clear</command-name>"), u("add a retry to the fetcher"), a("m1", 100, ["t1"]),
               a("m1", 100), tr, u(TEAMMATE + ": done"), a("m2", 50, ["t2", "t3"]),
               u("no, that's not what I asked"), a("m3", 10)]
    w = prompt_windows(records)
    assert [x["text"] for x in w] == ["add a retry to the fetcher", "no, that's not what I asked"]
    assert w[0]["output_tokens"] == 150 and w[0]["tool_calls"] == 3 and w[0]["correction"] and not w[1]["correction"]
    assert w[0]["model"] == "claude-fable-5-1" and w[0]["effort"] == "high"
    # 7. TOML fields and stats.
    toml = '[metadata]\ncategory = "Hardware"\nexpert_time_estimate_hours = 2\n'
    assert toml_field(toml, "category") == "Hardware" and toml_field(toml, "expert_time_estimate_hours") == "2"
    assert auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert abs(spearman([1, 2, 3, 4], [10, 20, 30, 40]) - 1) < 1e-9
    assert abs(spearman([1, 2, 3, 4], [4, 3, 2, 1]) + 1) < 1e-9
    assert ranks([3, 1, 3]) == [2.5, 1.0, 2.5]
    # 8. Gates on synthetic data: correlated axes fail gate 3; flat scores fail gate 1.
    def row(i, v, un):
        probs = {b["id"]: {"present": 0.5, "absent": 0.4, "not_observable": 0.1} for b in BEHAVIORS}
        for k in VERIFICATION:
            probs[k] = {"present": v, "absent": 1 - v, "not_observable": 0.0 + 0.05 * (i % 2)}
        for k in UNDERSPEC:
            probs[k] = {"present": un, "absent": 1 - un, "not_observable": 0.0}
        return {"id": f"r{i}", "probs": probs}
    same = gates([row(i, i / 20, i / 20) for i in range(20)], [])
    assert same["gate1_non_degenerate"]["pass"] and not same["gate3_distinct_axes"]["pass"]
    assert same["verdict"] == "NO-GO"
    flat = gates([row(i, 0.5, (i * 7 % 20) / 20) for i in range(20)], [])
    assert not flat["gate1_non_degenerate"]["pass"]
    # 9. Stratified order interleaves sources; the pilot draws 13 tagged items.
    mixed = [{"id": f"a{i}", "source": "tb3", "text": "x" * i} for i in range(10)] + \
            [{"id": f"b{i}", "source": "history", "text": "y" * i} for i in range(10)]
    order = stratified(mixed)
    assert len(order) == 20 and {order[0]["source"], order[1]["source"]} == {"history", "tb3"}
    tb3 = [{"id": f"{c}{i}", "category": c} for c in list(CATEGORY_TAG) + ["Software"] for i in range(4)]
    assert len(pilot_items(tb3)) == 13 and all(p["category"] in CATEGORY_TAG for p in pilot_items(tb3))
    print("self-test ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["self-test"]:
        self_test()
    elif args[:2] == ["extract", "tb3"] and len(args) == 3:
        extract_tb3(Path(args[2]))
    elif args == ["extract", "history"]:
        extract_history()
    elif args == ["dry-run"]:
        cmd_dry_run()
    elif args == ["pilot"]:
        cmd_pilot()
    elif args[:1] == ["freeze"] and len(args) == 3:
        cmd_freeze(args[1], args[2])
    elif args == ["main"]:
        cmd_main()
    elif args == ["report"]:
        cmd_report()
    else:
        print(__doc__)
