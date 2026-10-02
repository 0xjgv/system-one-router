# effort-router

Picks the model and effort for each subagent a `beta()` Claude Code session spawns. A
PreToolUse hook sends the Agent call's prompt to Respan's Span-01 Lite (`span-01-free`), takes
the best-scoring model and effort, and rewrites the call before Claude Code runs it.

## Files

| File | Role |
|---|---|
| `route.py` | Span call, pick, log; `route.py agents` prints the `--agents` JSON |
| `agent_hook.py` | PreToolUse hook on `Agent`: rewrites the call from the pick |

Standard library only; runs on Python 3.9.

## How a pick is made

1. One Span call scores 7 behaviors: one per model (`fable`, `opus`, `sonnet`) and one per
   effort (`xhigh`, `high`, `medium`, `low`). Definitions are in `route.py` (`MODELS`,
   `EFFORTS`).
2. Each group takes the option with the highest `p_present`.
3. A group whose winner scores under `FLOOR` (0.3) takes the fallback, `sonnet`/`xhigh`. So does
   the whole pick when the call fails for any reason (no key, network, HTTP error, timeout after
   10 s). The hook never blocks a spawn.

The hook sets the call's `model` to the pick. An Agent call cannot set effort, so a generic spawn
(`general-purpose`, `claude`, or no type) is moved to the agent `routed-<model>-<effort>`, whose
definition carries the effort. A specialised agent (`Explore`, `engineer`, …) keeps its type and
instructions and gets only the model. Non-Agent calls and empty prompts pass through.

Every prompt goes to `api.respan.ai`, Corgea work included.

## Setup

The key comes from `RESPAN_API_KEY`, else from `.env` in this directory:

```
RESPAN_API_KEY=...
```

`beta()` in `~/.utils/functions.sh` wires it in:

```
--settings '{..., "hooks": {"PreToolUse": [{"matcher": "Agent",
  "hooks": [{"type": "command", "command": "python3 ~/Code/effort-router/agent_hook.py", "timeout": 30}]}]}}'
--agents "$(python3 ~/Code/effort-router/route.py agents)"
```

Without `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`; it would override the routed model.

## Use

```
python3 route.py "Fix the flaky login test"    # prints e.g. "sonnet medium"
python3 route.py agents                        # the 12 routed-* agent definitions
```

Every pick, from the hook or the CLI, is appended to `artifacts/beta_log.jsonl` with the prompt,
the scores, and the result. The log is git-ignored.

## Evidence

The Phase 0 experiment (commits `eeef7f8`..`a470454`, removed from the tree) ended NO-GO: on
revealed cost, Span's cost score ranked tasks worse than a prompt-length-and-keyword baseline
(Spearman 0.200 vs 0.297). Treat the
router as an online experiment, not a validated one. `git show a470454:DESIGN.md` has the record.
