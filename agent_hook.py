"""PreToolUse hook for beta(): Span-01 Lite picks each subagent's model and effort from its prompt.

Model goes on the Agent call's `model` field. Effort can only come from an agent definition, so a
generic spawn (general-purpose, claude, or no type) is sent to routed-<model>-<effort>, defined by
`route.py agents`. A specialised agent keeps its type and instructions and gets only the model.
Span failures get route.FALLBACK; non-Agent calls pass through unchanged.
"""

import json
import sys

import route

GENERIC = (None, "", "general-purpose", "claude")


def main() -> int:
    event = json.load(sys.stdin)
    call = event.get("tool_input") or {}
    if event.get("tool_name") != "Agent" or not call.get("prompt"):
        return 0
    model, effort, _ = route.route(call["prompt"], event.get("cwd", ""), "agent")  # Span failed: FALLBACK
    routed = {**call, "model": model}
    if call.get("subagent_type") in GENERIC:
        routed["subagent_type"] = f"routed-{model}-{effort}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": routed}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
