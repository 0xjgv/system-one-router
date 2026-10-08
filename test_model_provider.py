import contextlib
import io
import json
import math
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import model_provider


BEHAVIORS = [
    {"id": "one", "definition": "A focused task."},
    {"id": "two", "definition": "A complex task."},
]


def response(body):
    return contextlib.closing(io.BytesIO(json.dumps(body).encode()))


def respan_body(one=0.8, two=0.2):
    return {"results": [{"id": "one", "p_present": one},
                        {"id": "two", "p_present": two}]}


def typesafe_body(one=0.8, two=0.2):
    return {"answers": {"one": {"type": "noul", "noul": one},
                        "two": {"type": "noul", "noul": two}}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = {
            "RESPAN_API_KEY": "synthetic-respan-key",
            "TYPESAFE_API_KEY": "synthetic-typesafe-key",
            "OPENROUTER_API_KEY": "synthetic-openrouter-key",
        }
        env = patch.dict(os.environ, self.environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        root = patch.object(model_provider, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)

    def test_respan_model_override_and_transport(self):
        os.environ["ROUTER_MODEL"] = "span-01-pro"
        with patch("model_provider.urllib.request.urlopen", return_value=response(respan_body())) as send:
            probabilities, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
        request = send.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, model_provider.PROVIDERS["respan"][0])
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-respan-key")
        self.assertEqual(send.call_args.kwargs["timeout"], model_provider.TIMEOUT_S)
        self.assertEqual(body["model"], "span-01-pro")
        self.assertEqual(body["behaviors"], BEHAVIORS)
        self.assertEqual(body["span"]["input"], [{"role": "user", "content": "Synthetic task"}])
        self.assertEqual(probabilities, {"one": 0.8, "two": 0.2})
        self.assertEqual(metadata["scoring_provider"], "respan")
        self.assertEqual(metadata["scoring_model"], "span-01-pro")

    def test_typesafe_and_openrouter_transport(self):
        for provider, model, key in (
                ("typesafe", "jev-1.13.0", "synthetic-typesafe-key"),
                ("openrouter", "typesafe/jev-1.13", "synthetic-openrouter-key")):
            with self.subTest(provider=provider):
                os.environ["ROUTER_PROVIDER"] = provider
                with patch("model_provider.urllib.request.urlopen", return_value=response(typesafe_body())) as send:
                    probabilities, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
                request = send.call_args.args[0]
                self.assertEqual(request.full_url, model_provider.PROVIDERS[provider][0])
                self.assertEqual(request.get_header("Authorization"), "Bearer " + key)
                self.assertEqual(json.loads(request.data), {
                    "model": model, "state": "Synthetic task",
                    "questions": {item["id"]: {"type": "noul", "instructions": item["definition"]}
                                  for item in BEHAVIORS},
                })
                self.assertEqual(probabilities, {"one": 0.8, "two": 0.2})
                self.assertEqual(metadata["scoring_provider"], provider)

    def test_invalid_primary_scores_use_fallback(self):
        invalid = [
            {}, {"results": []}, respan_body(True), respan_body("0.8"),
            respan_body(math.nan), respan_body(math.inf), respan_body(-0.1), respan_body(1.1),
            {"results": [{"id": "one", "p_present": 0.8}, {"id": "one", "p_present": 0.2},
                         {"id": "two", "p_present": 0.2}]},
        ]
        for body in invalid:
            with self.subTest(body=body):
                with patch("model_provider.urllib.request.urlopen", side_effect=[
                        response(body), response(typesafe_body())]) as send:
                    probabilities, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(probabilities, {"one": 0.8, "two": 0.2})
                self.assertEqual(metadata["scoring_provider"], "typesafe")
                self.assertIn("error", metadata["attempts"][0])

    def test_low_scores_do_not_call_fallback(self):
        with patch("model_provider.urllib.request.urlopen", return_value=response(respan_body(0.1, 0.2))) as send:
            probabilities, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(probabilities, {"one": 0.1, "two": 0.2})
        self.assertEqual(metadata["scoring_provider"], "respan")

    def test_missing_primary_key_uses_fallback(self):
        del os.environ["RESPAN_API_KEY"]
        with patch("model_provider.urllib.request.urlopen", return_value=response(typesafe_body())) as send:
            _, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(metadata["scoring_provider"], "typesafe")
        self.assertIn("error", metadata["attempts"][0])

    def test_invalid_typesafe_primary_uses_fallback(self):
        os.environ["ROUTER_PROVIDER"] = "openrouter"
        for body in ({"answers": {}}, typesafe_body(False),
                     {"answers": {"one": {"type": "noul", "noul": 0.8}}}):
            with self.subTest(body=body):
                with patch("model_provider.urllib.request.urlopen", side_effect=[
                        response(body), response(typesafe_body())]):
                    _, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
                self.assertEqual(metadata["scoring_provider"], "typesafe")
                self.assertIn("error", metadata["attempts"][0])

    def test_http_primary_failure_uses_fallback(self):
        error = urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)
        with patch("model_provider.urllib.request.urlopen", side_effect=[error, response(typesafe_body())]):
            _, metadata = model_provider.scores("Synthetic task", BEHAVIORS)
        self.assertEqual(metadata["scoring_provider"], "typesafe")
        self.assertIn("503", metadata["attempts"][0]["error"])

    def test_http_failure_attempts_do_not_disclose_response_or_credentials(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "sensitive response text", {}, None)
        with patch("model_provider.urllib.request.urlopen", side_effect=[error, ValueError("sensitive response text")]):
            with self.assertRaises(model_provider.ScoreError) as caught:
                model_provider.scores("Synthetic task", BEHAVIORS)
        attempts = caught.exception.attempts
        self.assertEqual([attempt["provider"] for attempt in attempts], ["respan", "typesafe"])
        for attempt in attempts:
            self.assertIn("error", attempt)
            self.assertGreaterEqual(attempt["elapsed_ms"], 0)
        diagnostic = str(caught.exception) + json.dumps(attempts)
        self.assertIn("401", diagnostic)
        self.assertNotIn("sensitive response text", diagnostic)
        self.assertNotIn("synthetic-", diagnostic)

    def test_duplicate_fallback_is_attempted_once(self):
        os.environ["ROUTER_PROVIDER"] = "typesafe"
        with patch("model_provider.urllib.request.urlopen", side_effect=OSError("offline")) as send:
            with self.assertRaises(model_provider.ScoreError) as caught:
                model_provider.scores("Synthetic task", BEHAVIORS)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(caught.exception.attempts), 1)

    def test_env_settings_override_dotenv_without_evaluation(self):
        sentinel = self.root / "sentinel"
        command = "$(touch " + str(sentinel) + ")"
        (self.root / ".env").write_text(
            "ROUTER_PROVIDER='openrouter'\nROUTER_MODEL=custom-model\n"
            "RESPAN_API_KEY=from-file\nUNTRUSTED=" + command + "\n")
        self.assertEqual(model_provider.configuration(), ("openrouter", "custom-model"))
        self.assertEqual(model_provider.setting("RESPAN_API_KEY"), "synthetic-respan-key")
        self.assertEqual(model_provider.setting("UNTRUSTED"), command)
        self.assertFalse(sentinel.exists())
        os.environ["ROUTER_PROVIDER"] = "respan"
        os.environ["ROUTER_MODEL"] = "override-model"
        self.assertEqual(model_provider.configuration(), ("respan", "override-model"))

    def test_unknown_provider_is_rejected(self):
        os.environ["ROUTER_PROVIDER"] = "unknown"
        with self.assertRaises(ValueError):
            model_provider.configuration()

    def test_check_requires_primary_and_fallback_keys_without_network(self):
        os.environ["ROUTER_PROVIDER"] = "openrouter"
        for missing, expected in ((None, 0), ("OPENROUTER_API_KEY", 1), ("TYPESAFE_API_KEY", 1)):
            with self.subTest(missing=missing), patch.dict(os.environ, self.environment):
                if missing:
                    del os.environ[missing]
                output = io.StringIO()
                with patch("model_provider.urllib.request.urlopen") as send, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = model_provider.main(["check"])
                self.assertEqual(result, expected)
                send.assert_not_called()
                self.assertNotIn("synthetic-", output.getvalue())


if __name__ == "__main__":
    unittest.main()
