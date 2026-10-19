import os
import unittest
from unittest.mock import patch

import bot_engine


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def generate_content(self, model, contents):
        self.calls.append(model)
        outcome = self.outcomes.get(model, RuntimeError("simulated quota error"))
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


class GeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "GEMINI_MODEL": "gemini-3.6-flash",
            "GEMINI_FALLBACK_MODELS": "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
        }, clear=False)
        self.env.start()
        self.old_client = bot_engine.client
        self.old_model = bot_engine.LAST_GEMINI_MODEL

    def tearDown(self):
        bot_engine.client = self.old_client
        bot_engine.LAST_GEMINI_MODEL = self.old_model
        self.env.stop()

    def test_primary_success_does_not_call_fallback(self):
        fake = FakeClient({"gemini-3.6-flash": "STATUS: PASS"})
        bot_engine.client = fake
        response = bot_engine.generate_gemini_content("test prompt")
        self.assertEqual(response.text, "STATUS: PASS")
        self.assertEqual(fake.models.calls, ["gemini-3.6-flash"])
        self.assertEqual(bot_engine.LAST_GEMINI_MODEL, "gemini-3.6-flash")

    def test_rate_limit_uses_first_available_fallback(self):
        fake = FakeClient({
            "gemini-3.6-flash": RuntimeError("429 RESOURCE_EXHAUSTED"),
            "gemini-3.5-flash-lite": "STATUS: PASS",
        })
        bot_engine.client = fake
        response = bot_engine.generate_gemini_content("test prompt")
        self.assertEqual(response.text, "STATUS: PASS")
        self.assertEqual(fake.models.calls, ["gemini-3.6-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(bot_engine.LAST_GEMINI_MODEL, "gemini-3.5-flash-lite")

    def test_empty_response_uses_next_fallback(self):
        fake = FakeClient({
            "gemini-3.6-flash": "",
            "gemini-3.5-flash-lite": RuntimeError("503 unavailable"),
            "gemini-3.1-flash-lite": "STATUS: FAIL",
        })
        bot_engine.client = fake
        response = bot_engine.generate_gemini_content("test prompt")
        self.assertEqual(response.text, "STATUS: FAIL")
        self.assertEqual(fake.models.calls, ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

    def test_all_models_failing_raises_sanitized_error(self):
        fake = FakeClient({})
        bot_engine.client = fake
        with self.assertRaisesRegex(RuntimeError, "All configured Gemini models failed"):
            bot_engine.generate_gemini_content("test prompt")
        self.assertEqual(len(fake.models.calls), 3)

    def test_deprecated_primary_does_not_retry_effective_same_model(self):
        with patch.dict(os.environ, {
            "GEMINI_MODEL": "gemini-3.5-flash",
            "GEMINI_FALLBACK_MODELS": "gemini-3.6-flash,gemini-3.5-flash-lite",
        }, clear=False):
            self.assertEqual(bot_engine.gemini_model_chain(), ["gemini-3.5-flash", "gemini-3.5-flash-lite"])


if __name__ == "__main__":
    unittest.main()
