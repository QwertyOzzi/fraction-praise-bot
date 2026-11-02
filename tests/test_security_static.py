from pathlib import Path
import ast
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")


class BotSecurityChecks(unittest.TestCase):
    def test_no_prefix_owner_key_bypass(self):
        self.assertNotRegex(SOURCE, r"key\.startswith\(")

    def test_health_does_not_expose_access_credentials(self):
        section = SOURCE.split("async def handle_ping(", 1)[1].split("async def handle_api_support(", 1)[0]
        for name in ("deputy_key", "auth_email"):
            self.assertNotIn('"' + name + '"', section)

    def test_no_hardcoded_firebase_password_fallback(self):
        for line in SOURCE.splitlines():
            if "FIREBASE_PASSWORD" in line and "os.getenv" in line:
                self.assertNotIn("saved_cfg", line)
        self.assertNotRegex(SOURCE, r"self\.password\s*=\s*saved_cfg\.get")

    def test_owner_access_bound_to_telegram_identity(self):
        section = SOURCE.split("async def cmd_admin_auth(", 1)[1].split("@dp.callback_query", 1)[0]
        self.assertIn("OWNER_TELEGRAM_IDS", section)

    def test_credit_uses_firestore_precondition(self):
        self.assertIn('"currentDocument.updateTime": revision', SOURCE)
        self.assertIn('async def credit_report(', SOURCE)

    def test_approval_waits_for_credit(self):
        section = SOURCE.split('async def approve_report(', 1)[1].split('async def reject_report(', 1)[0]
        self.assertLess(section.index('await firebase_client.credit_report('), section.index('req["status"] = "approved"'))

    def test_python_syntax(self):
        ast.parse(SOURCE)


if __name__ == "__main__":
    unittest.main()
