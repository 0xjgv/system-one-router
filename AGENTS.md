# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.9, standard-library-only router for Claude Code subagents.

- `route.py`: Respan scoring, model/effort selection, logging, and agent definitions.
- `agent_hook.py`: `PreToolUse` hook that reads JSON from stdin and emits updated Agent input.
- `README.md`: setup, routing behavior, and experiment history.
- `artifacts/beta_log.jsonl`: generated routing records; ignored by Git.

There are no package, asset, or test directories. Keep changes within the existing modules unless a concrete need warrants expansion.

## Build, Test, and Development Commands

No build step or dependency installation is required.

- `python3 route.py agents`: print the 12 `routed-<model>-<effort>` definitions without calling Respan.
- `python3 route.py "Fix the flaky login test"`: score a prompt, print the selection, and append a log record. This sends the prompt to Respan when configured.
- `printf '%s\n' '{"tool_name":"Read","tool_input":{}}' | python3 agent_hook.py`: check pass-through behavior; expect successful exit with no output.
- `python3 -m py_compile route.py agent_hook.py`: check syntax.

See `README.md` for the external `beta()` shell integration.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, and uppercase module constants. Match existing type annotations and concise docstrings. Maintain Python 3.9 compatibility and prefer standard-library tools. No formatter or linter is configured. Keep hook stdout reserved for its JSON protocol.

## Testing Guidelines

No automated test suite or coverage threshold exists. For routing changes, add focused standard-library `unittest` tests named `test_*.py`; run them with `python3 -m unittest discover`.

Mock scoring and redirect logs to temporary paths. Cover low-score and API-failure fallbacks, generic-agent rewriting, specialized-agent type preservation, and empty/non-Agent pass-through. Validate emitted JSON through the hook entry point without sending private prompts to Respan.

## Commit & Pull Request Guidelines

Recent commits use concise imperative subjects such as `Reduce effort-router to the beta router`; historical experiment records use `DESIGN:`. Follow the relevant pattern.

PRs should explain the behavior change, list validation commands and outcomes, and link relevant issues. Include sample input/output for routing or hook changes. Update `README.md` when setup or behavior changes.

## Security & Configuration

Read `RESPAN_API_KEY` from the environment or local `.env`. Never commit credentials or generated logs. Prompts are sent to `api.respan.ai` and recorded locally. Use synthetic prompts for checks. Treat routing quality as experimental, as documented in `README.md`.
