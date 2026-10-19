import os
import unittest
from unittest.mock import patch, Mock

import pandas as pd

import bot_engine


class TestUpstoxProvider(unittest.TestCase):
    def test_analytics_token_preferred_over_legacy(self):
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "analytics-test", "UPSTOX_ACCESS_TOKEN": "legacy-test"}):
            self.assertEqual(bot_engine._upstox_token(), "analytics-test")

    def test_legacy_token_supported(self):
        env = {k: v for k, v in os.environ.items() if k not in {"UPSTOX_ANALYTICS_TOKEN", "UPSTOX_ACCESS_TOKEN"}}
        env["UPSTOX_ACCESS_TOKEN"] = "legacy-test"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(bot_engine._upstox_token(), "legacy-test")

    def test_upstox_success_does_not_call_yahoo(self):
        result = {"price": 123.4, "provider": "Upstox Analytics", "quote_timestamp": "2026-10-19T10:00:00+05:30",
                  "quote_timestamp_source": "exchange_timestamp", "instrument_key": "NSE_EQ|INE000000001"}
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "test", "UPSTOX_YAHOO_FALLBACK_ENABLED": "true"}), \
             patch.object(bot_engine, "_fetch_upstox_data", return_value=result), \
             patch.object(bot_engine, "_fetch_yahoo_data") as yahoo:
            got = bot_engine.fetch_stock_data_with_fallback("TEST")
        self.assertEqual(got["provider"], "Upstox Analytics")
        yahoo.assert_not_called()
        status = bot_engine.get_provider_status()
        self.assertEqual(status["last_quote_provider"], "Upstox Analytics")
        self.assertEqual(status["last_quote_timestamp_source"], "exchange_timestamp")

    def test_yahoo_fallback_only_when_enabled(self):
        yahoo_result = {"price": 100.0, "provider": "Yahoo Finance (fallback)", "quote_timestamp": "retrieval",
                        "quote_timestamp_source": "retrieval_time_only", "instrument_key": None, "fallback_used": True}
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "test", "UPSTOX_YAHOO_FALLBACK_ENABLED": "true"}), \
             patch.object(bot_engine, "_fetch_upstox_data", side_effect=RuntimeError("API down")), \
             patch.object(bot_engine, "_fetch_yahoo_data", return_value=yahoo_result) as yahoo:
            got = bot_engine.fetch_stock_data_with_fallback("TEST")
        self.assertEqual(got["provider"], "Yahoo Finance (fallback)")
        self.assertTrue(got["fallback_used"])
        yahoo.assert_called_once_with("TEST")

    def test_disabled_fallback_fails_closed(self):
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "test", "UPSTOX_YAHOO_FALLBACK_ENABLED": "false"}), \
             patch.object(bot_engine, "_fetch_upstox_data", side_effect=RuntimeError("API down")), \
             patch.object(bot_engine, "_fetch_yahoo_data") as yahoo:
            got = bot_engine.fetch_stock_data_with_fallback("TEST")
        self.assertIsNone(got)
        yahoo.assert_not_called()

    def test_upstox_get_uses_bearer_auth_and_checks_http(self):
        fake = Mock()
        fake.raise_for_status.return_value = None
        fake.json.return_value = {"status": "success", "data": {}}
        with patch.dict(os.environ, {"UPSTOX_ANALYTICS_TOKEN": "secret-for-test"}), \
             patch.object(bot_engine.requests, "get", return_value=fake) as get:
            payload = bot_engine._upstox_get("https://example.test", {"q": "x"})
        self.assertEqual(payload["status"], "success")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret-for-test")
        self.assertNotIn("secret-for-test", repr(payload))


if __name__ == "__main__":
    unittest.main()
