"""Select a Codex model and effort, then run one task with `codex exec`."""

import argparse
import os
import subprocess
import sys

import route


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="Task to route and send to Codex")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER, help="Additional codex exec options")
    args = parser.parse_args(argv)
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    model, effort, entry = route.route(args.prompt, os.getcwd(), "codex", "codex")
    note = "; scoring failed, using fallback" if "error" in entry else ""
    print(f"Routing to {model} ({effort}{note})", file=sys.stderr)
    options = args.codex_args
    if options[:1] == ["--"]:
        options = options[1:]
    command = ["codex", "exec", *options, "--model", model,
               "-c", f'model_reasoning_effort="{effort}"', "--", args.prompt]
    try:
        status = subprocess.run(command).returncode
        return status if status >= 0 else 128 - status
    except FileNotFoundError:
        print("codex executable not found; install Codex CLI and add it to PATH", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
