"""PreToolUse hook: a System One scorer picks each subagent's model and effort from its prompt.

The pick goes on the Agent call's `model` and `effort` fields (Claude Code 2.1.292+); every other
input, including the agent type, is kept. systemMessage shows routed-<model>-<effort> in the session
UI only; the model never sees it. Scoring failures get route.FALLBACK; non-Agent calls
pass through unchanged.
"""

import json
import sys

import route


def main() -> int:
    event = json.load(sys.stdin)
    call = event.get("tool_input") or {}
    if event.get("tool_name") != "Agent" or not call.get("prompt"):
        return 0
    model, effort, _ = route.route(call["prompt"], event.get("cwd", ""), "agent")
    routed = {**call, "model": model, "effort": effort}
    print(json.dumps({"systemMessage": f"routed-{model}-{effort}",
                      "hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": routed}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
