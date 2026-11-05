"""Guard the low-noise Render heartbeat from silently failing."""
import re
import unittest
from pathlib import Path

WORKFLOW = (Path(__file__).resolve().parents[1] /
            ".github/workflows/keep_alive.yml").read_text(encoding="utf-8")


class TestRenderKeepAliveWorkflow(unittest.TestCase):
    def test_runs_six_times_hourly_without_busy_zero_minute(self):
        match = re.search(r"cron:\s*['\"]([^'\"]+)['\"]", WORKFLOW)
        self.assertIsNotNone(match)
        fields = match.group(1).split()
        self.assertEqual(len(fields), 5)
        minutes = [int(x) for x in fields[0].split(",")]
        self.assertEqual(minutes, [7, 17, 27, 37, 47, 57])

    def test_real_public_health_route(self):
        self.assertIn(
            "https://fraction-praise-bot-1.onrender.com/health",
            WORKFLOW
        )

    def test_http_failures_are_visible(self):
        self.assertIn("--fail", WORKFLOW)
        self.assertIn("--show-error", WORKFLOW)
        self.assertIn("--retry 2", WORKFLOW)
        self.assertIn("set -euo pipefail", WORKFLOW)
        self.assertNotIn("|| echo", WORKFLOW)

    def test_no_telegram_spam_or_credentials(self):
        self.assertNotIn("api.telegram.org", WORKFLOW)
        self.assertNotIn("sendMessage", WORKFLOW)
        self.assertNotIn("BOT_TOKEN", WORKFLOW)
        self.assertIn("contents: read", WORKFLOW)


if __name__ == "__main__":
    unittest.main()
