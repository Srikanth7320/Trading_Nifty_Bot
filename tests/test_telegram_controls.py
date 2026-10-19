import unittest
import sys
import types
import importlib.util

if importlib.util.find_spec("flask") is None:
    raise unittest.SkipTest("Flask is not installed in this test environment")
# The control-state tests do not run the scheduler; allow import in minimal test envs.
sys.modules.setdefault("schedule", types.ModuleType("schedule"))
import app as web_app


class TelegramControlTests(unittest.TestCase):
    def tearDown(self):
        web_app.BOT_ENABLED.clear()

    def test_start_and_stop_toggle_bot_state(self):
        web_app.control_stop()
        self.assertEqual(web_app.bot_control_snapshot()["status"], "paused")
        web_app.control_start()
        self.assertEqual(web_app.bot_control_snapshot()["status"], "running")
        web_app.control_stop()
        self.assertEqual(web_app.bot_control_snapshot()["status"], "paused")

    def test_about_page_exposes_model_and_telegram_controls(self):
        web_app.control_start()
        client = web_app.app.test_client()
        response = client.get("/about")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gemini AI model fallback", response.data)
        self.assertIn(b"Telegram controls", response.data)
        self.assertIn(b"/start", response.data)
        self.assertIn(b"/stop", response.data)


if __name__ == "__main__":
    unittest.main()
