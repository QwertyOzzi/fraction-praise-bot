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

    def test_report_idempotency_ledger(self):
        self.assertIn('if report_id in applied_ids:', SOURCE)
        self.assertIn('"appliedReportIds"', SOURCE)
        self.assertIn('req["proof"], req_id', SOURCE)

    def test_report_quantities_and_pins(self):
        self.assertIn("waiting_for_quantity = State()", SOURCE)
        self.assertIn("quantity=quantity", SOURCE)
        self.assertIn("pin_chat_message", SOURCE)
        self.assertIn("unpin_chat_message", SOURCE)
        self.assertIn("async def adjust_report_quantity(", SOURCE)

    def test_manual_grants_are_restricted(self):
        section = SOURCE.split("async def perform_manual_grant(", 1)[1].split("@dp.message(Command", 1)[0]
        self.assertIn("if not is_admin(", section)
        self.assertIn("firebase_client.credit_report(", section)

    def test_issue_queue_reloads_after_debit(self):
        section = SOURCE.split("async def process_issue_one(", 1)[1].split("# ╔", 1)[0]
        self.assertIn('firebase_client.debit_one("fractionrp", nick)', section)
        self.assertIn("render_queue(rows)", section)
        self.assertIn("callback.message.edit_text", section)

    def test_both_report_types_have_quantity_selection(self):
        section = SOURCE.split("async def process_report_type(", 1)[1].split("async def process_report_quantity_button(", 1)[0]
        self.assertIn("await state.set_state(ReportStates.waiting_for_quantity)", section)
        self.assertNotIn('if rep_type == "praise":', section)
        adjustment = SOURCE.split("async def adjust_report_quantity(", 1)[1].split("async def send_report_confirmed(", 1)[0]
        self.assertIn('("fractionrp", "praise")', adjustment)

    def test_http_port_before_bounded_cloud_sync(self):
        section = SOURCE.split("async def main():", 1)[1]
        self.assertLess(section.index("await start_web_server()"), section.index("await asyncio.wait_for(sync_state_from_cloud(), timeout=20)"))
        self.assertIn('headers = {"Authorization": f"Bearer {token}"}', SOURCE)
        self.assertIn('RELEASE_TAG = "leader-deputy-roles-v4"', SOURCE)

    def test_python_syntax(self):
        ast.parse(SOURCE)


if __name__ == "__main__":
    unittest.main()
