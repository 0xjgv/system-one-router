# system-one-router

Choose a Claude Code subagent's model and effort with a small decision model.
A `PreToolUse` hook scores the task prompt and updates the Agent call before it runs.
Supports Respan Lite, Respan Pro, direct TypeSafe Jev, and OpenRouter System One models.

Python 3.9+ and the standard library are sufficient. The shell integration also
requires [Claude Code](https://code.claude.com/docs/en/overview) and access to the
configured execution models (`fable`, `opus`, and `sonnet`).

## Setup

```sh
git clone https://github.com/0xjgv/system-one-router.git
cd system-one-router
```

Set credentials in your environment or create a `.env` file in the repository:

```dotenv
RESPAN_API_KEY=your-respan-key
TYPESAFE_API_KEY=your-typesafe-key
# Required only when selecting OpenRouter:
# OPENROUTER_API_KEY=your-openrouter-key
```

Environment values take precedence. The `.env` reader accepts `NAME=value` lines
with optional quotes; it does not execute shell expressions. Keep this file private;
Git ignores it.

```sh
python3 model_provider.py check
```

This checks that keys are present for the primary provider and direct Jev fallback.
It prints key names, never values, and exits nonzero if a required key is missing.
It does not contact providers or validate credentials. A direct TypeSafe setup needs
only `TYPESAFE_API_KEY`.

## Choose a scoring provider

Set `ROUTER_PROVIDER` and `ROUTER_MODEL` in the environment or `.env`:

| Provider | `ROUTER_PROVIDER` | `ROUTER_MODEL` | Credential |
|---|---|---|---|
| Respan Lite (default) | `respan` | `span-01-free` | `RESPAN_API_KEY` |
| Respan Pro | `respan` | `span-01-pro` | `RESPAN_API_KEY` |
| TypeSafe Jev | `typesafe` | `jev-1.13.0` | `TYPESAFE_API_KEY` |
| OpenRouter | `openrouter` | `typesafe/jev-1.13`, or another compatible System One model ID | `OPENROUTER_API_KEY` |

Omitting `ROUTER_MODEL` selects the provider's default from `model_provider.py`.
Set both variables when switching providers if a model is already configured.
The `PRIMARY` and `FALLBACK` constants in that file define the default provider chain.

```sh
# Respan Pro
ROUTER_PROVIDER=respan ROUTER_MODEL=span-01-pro \
  python3 route.py "Fix the flaky login test"

# Direct Jev
ROUTER_PROVIDER=typesafe ROUTER_MODEL=jev-1.13.0 \
  python3 route.py "Fix the flaky login test"

# Jev through OpenRouter; replace the model ID to try another System One model
ROUTER_PROVIDER=openrouter ROUTER_MODEL=typesafe/jev-1.13 \
  python3 route.py "Fix the flaky login test"
```

Respan Pro requires Respan credits. OpenRouter uses its
`/api/v1/systemone` endpoint and models that support `noul` questions, rather than
the chat-completions API. See [Respan's models](https://www.respan.ai/docs/documentation/span-01/concept#models-and-pricing),
[TypeSafe's API](https://docs.typesafe.ai/api), and
[OpenRouter's System One API](https://openrouter.ai/docs/guides/community/typesafe-sdk).

## How routing works

1. Score seven definitions from `route.py`: three execution models (`fable`, `opus`,
   `sonnet`) and four efforts (`xhigh`, `high`, `medium`, `low`). Respan returns
   `p_present`; TypeSafe and OpenRouter return one `noul` probability per definition.
2. Take the highest score in each group. A winner below `FLOOR` (`0.3`) uses that
   group's default: `sonnet` or `xhigh`. Low scores do not trigger a provider retry.
3. Missing credentials, request errors, or malformed scores trigger direct Jev
   fallback. If both attempts fail, use `sonnet`/`xhigh`. Selecting the same direct
   Jev model as the fallback makes only one attempt.

Requests use a 10-second socket timeout per attempt with no retries within a
provider. The shell example gives the whole hook 30 seconds. Invalid configuration
also produces the static default; use `check` to catch it before starting a session.

Generic agents (`general-purpose`, `claude`, or no type) become
`routed-<model>-<effort>` agents, whose definitions carry the effort. Specialized
agents retain their type and instructions and receive only the selected model.
Non-Agent calls and empty prompts pass through. Provider and log-write failures
do not block spawning.

The scoring model chooses the execution model; these are separate settings.
The shared probability format does not establish equal calibration across providers.
Treat routing quality and the current threshold as experimental.

## Try it with a shell function

From the cloned repository, set its absolute path:

```sh
export EFFORT_ROUTER_DIR="$(pwd)"
```

Paste this function into a POSIX-compatible shell, Bash, or Zsh, or add it to your
shell configuration. It uses the exported path so hooks work from other projects.

```sh
beta() (
  : "${EFFORT_ROUTER_DIR:?Set EFFORT_ROUTER_DIR to the cloned repository}"
  export EFFORT_ROUTER_DIR
  unset CLAUDE_CODE_SUBAGENT_MODEL_FORCE
  python3 "$EFFORT_ROUTER_DIR/model_provider.py" check || exit
  routed_agents=$(python3 "$EFFORT_ROUTER_DIR/route.py" agents) || exit
  claude --model opus \
    --settings '{"hooks":{"PreToolUse":[{"matcher":"Agent","hooks":[{"type":"command","command":"python3 \"$EFFORT_ROUTER_DIR/agent_hook.py\"","timeout":30}]}]}}' \
    --agents "$routed_agents" "$@"
)
```

Run a session from the project you want to work on:

```sh
beta

# Request a small read-only delegation to exercise the hook
beta -p "Use a general-purpose subagent to summarize this project's README. Do not modify files."

# Test the same flow using Respan Pro
ROUTER_PROVIDER=respan ROUTER_MODEL=span-01-pro \
  beta -p "Use a general-purpose subagent to summarize this project's README. Do not modify files."
```

Avoid forcing a global subagent model or effort override when testing routing.
The function preserves normal Claude Code permission checks. See the
[subagent](https://code.claude.com/docs/en/sub-agents) and
[hook](https://code.claude.com/docs/en/hooks) documentation for integration details.

## Tests and logs

```sh
python3 -m unittest discover -v                 # Offline tests; no API keys needed
python3 route.py agents                        # Print all 12 agent definitions
printf '%s\n' '{"tool_name":"Read","tool_input":{}}' | python3 agent_hook.py
```

The last command should exit successfully without output. To test an Agent call
directly, use a synthetic prompt; this makes a live scoring request:

```sh
printf '%s\n' '{"tool_name":"Agent","tool_input":{"prompt":"Explain what a Python list is","subagent_type":"general-purpose"}}' \
  | python3 agent_hook.py
tail -n 1 "$EFFORT_ROUTER_DIR/artifacts/beta_log.jsonl"
```

Each routing record includes the prompt, working directory, selected model/effort,
scores, and provider attempts with timing and error categories. Successful scoring
also records `scoring_provider` and `scoring_model`. Logs stay in the repository's
ignored `artifacts/beta_log.jsonl`; logging failure does not discard a decision.

Prompts are sent to the selected scoring service and, on failure, TypeSafe. Keep
credentials and logs out of commits and use synthetic prompts for integration tests.

## Files

| File | Purpose |
|---|---|
| `model_provider.py` | Provider configuration, credentials, API adapters, and fallback |
| `route.py` | Behavior definitions, model/effort selection, agent definitions, and logs |
| `agent_hook.py` | Claude Code `PreToolUse` JSON interface |
| `test_model_provider.py`, `test_route.py` | Offline provider, routing, and hook regression tests |
