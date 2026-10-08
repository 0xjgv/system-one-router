import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_hook
import model_provider
import route


def scores(model="opus", effort="high", model_score=0.9, effort_score=0.9):
    probabilities = {item["id"]: 0.1 for item in route.BEHAVIORS}
    probabilities["model_" + model] = model_score
    probabilities["effort_" + effort] = effort_score
    return probabilities


class RouteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "artifacts" / "routing.jsonl"
        log = patch.object(route, "LOG", self.log)
        log.start()
        self.addCleanup(log.stop)

    def test_selection_and_provider_metadata_are_logged(self):
        metadata = {"scoring_provider": "openrouter", "scoring_model": "typesafe/jev-1.13",
                    "attempts": [{"provider": "openrouter", "model": "typesafe/jev-1.13", "elapsed_ms": 1}]}
        with patch("route.model_provider.scores", return_value=(scores(), metadata)) as score:
            model, effort, entry = route.route("Synthetic task", "/workspace", "manual")
        score.assert_called_once_with("Synthetic task", route.BEHAVIORS)
        self.assertEqual((model, effort), ("opus", "high"))
        self.assertEqual(entry["floored"], [])
        for key, value in metadata.items():
            self.assertEqual(entry[key], value)
        self.assertEqual(json.loads(self.log.read_text()), entry)

    def test_floor_applies_independently_to_each_group(self):
        cases = [(0.2, 0.9, ("sonnet", "high"), ["model"]),
                 (0.9, 0.2, ("opus", "xhigh"), ["effort"]),
                 (0.3, 0.3, ("opus", "high"), [])]
        for model_score, effort_score, expected, floored in cases:
            with self.subTest(model_score=model_score, effort_score=effort_score):
                with patch("route.model_provider.scores", return_value=(
                        scores(model_score=model_score, effort_score=effort_score), {})):
                    model, effort, entry = route.route("Synthetic task", "", "agent")
                self.assertEqual((model, effort), expected)
                self.assertEqual(entry["floored"], floored)

    def test_all_provider_failures_keep_spawn_fallback_and_attempts(self):
        attempts = [{"provider": "respan", "model": "span-01-free", "error": "HTTP 503", "elapsed_ms": 1},
                    {"provider": "typesafe", "model": "jev-1.13.0", "error": "TimeoutError", "elapsed_ms": 2}]
        failure = model_provider.ScoreError(attempts)
        with patch("route.model_provider.scores", side_effect=failure):
            model, effort, entry = route.route("Synthetic task", "", "agent")
        self.assertEqual((model, effort), route.FALLBACK)
        self.assertIn("error", entry)
        self.assertEqual(entry["attempts"], attempts)

    def test_log_failure_does_not_block_the_selected_spawn(self):
        with patch("route.model_provider.scores", return_value=(scores(), {})), \
                patch.object(Path, "open", side_effect=PermissionError("unwritable log")):
            model, effort, entry = route.route("Synthetic task", "", "agent")
        self.assertEqual((model, effort), ("opus", "high"))
        self.assertTrue(entry["log_error"])


class HookTests(unittest.TestCase):
    def invoke(self, event):
        output = io.StringIO()
        with patch("agent_hook.sys.stdin", io.StringIO(json.dumps(event))), contextlib.redirect_stdout(output):
            status = agent_hook.main()
        self.assertEqual(status, 0)
        return output.getvalue()

    def test_generic_agents_receive_model_and_routed_effort_type(self):
        for agent_type in (None, "", "general-purpose", "claude"):
            with self.subTest(agent_type=agent_type):
                call = {"prompt": "Synthetic task", "description": "Example", "subagent_type": agent_type}
                with patch("agent_hook.route.route", return_value=("opus", "high", {})) as pick:
                    output = json.loads(self.invoke({"tool_name": "Agent", "cwd": "/workspace", "tool_input": call}))
                pick.assert_called_once_with("Synthetic task", "/workspace", "agent")
                self.assertEqual(output, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {
                    **call, "model": "opus", "subagent_type": "routed-opus-high"}}})

    def test_specialized_agent_preserves_type_and_inputs(self):
        call = {"prompt": "Synthetic task", "subagent_type": "security-reviewer", "model": "sonnet",
                "description": "Example", "run_in_background": True}
        with patch("agent_hook.route.route", return_value=("opus", "xhigh", {})):
            output = json.loads(self.invoke({"tool_name": "Agent", "tool_input": call}))
        self.assertEqual(output["hookSpecificOutput"]["updatedInput"], {**call, "model": "opus"})

    def test_non_agent_and_empty_prompts_emit_nothing(self):
        for event in ({}, {"tool_name": "Read", "tool_input": {"prompt": "Synthetic task"}},
                      {"tool_name": "Agent", "tool_input": {}},
                      {"tool_name": "Agent", "tool_input": {"prompt": ""}}):
            with self.subTest(event=event), patch("agent_hook.route.route") as pick:
                self.assertEqual(self.invoke(event), "")
                pick.assert_not_called()

    def test_hook_subprocess_without_keys_routes_and_logs_provider_failures(self):
        call = {"prompt": "Synthetic task", "description": "Example", "subagent_type": "general-purpose"}
        environment = os.environ.copy()
        for name in ("ROUTER_PROVIDER", "ROUTER_MODEL", "RESPAN_API_KEY",
                     "TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
            environment.pop(name, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parent
            for name in ("route.py", "model_provider.py", "agent_hook.py"):
                shutil.copyfile(source / name, root / name)
            result = subprocess.run(
                [sys.executable, str(root / "agent_hook.py")],
                input=json.dumps({"tool_name": "Agent", "cwd": str(root), "tool_input": call}),
                text=True, capture_output=True, cwd=root, env=environment, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["updatedInput"], {
                **call, "model": "sonnet", "subagent_type": "routed-sonnet-xhigh"})
            entry = json.loads((root / "artifacts" / "beta_log.jsonl").read_text())
        self.assertIn("ScoreError", entry["error"])
        self.assertEqual([attempt["provider"] for attempt in entry["attempts"]], ["respan", "typesafe"])
        self.assertTrue(all(attempt["error"] == "ValueError" for attempt in entry["attempts"]))


if __name__ == "__main__":
    unittest.main()
