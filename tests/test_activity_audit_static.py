"""Regression guards for owner alerts and privilege-scoped action logging."""
import pathlib
import unittest

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")


class ActivityAuditTests(unittest.TestCase):
    def test_owner_notification_command_is_private_and_owner_only(self):
        section = SOURCE.split('async def cmd_owner_notifications(', 1)[1].split('@dp.message(Command("version"))', 1)[0]
        self.assertIn('is_owner(message.from_user.id)', section)
        self.assertIn('message.chat.type != "private"', section)
        self.assertIn('updateMask.fieldPaths', section)
        self.assertIn('auditTelegramEnabled', section)

    def test_auditor_never_notifies_as_unauthorized_actor(self):
        section = SOURCE.split('async def record_admin_activity(', 1)[1].split('@dp.message(Command("notifications"))', 1)[0]
        self.assertIn('if not is_admin(actor_id):', section)
        self.assertIn('if is_owner(actor_id):', section)
        self.assertIn('for owner_id in OWNER_TELEGRAM_IDS:', section)
        self.assertIn('ClientTimeout(total=8)', section)

    def test_all_core_actions_emit_events(self):
        for action in (
            'manual_grant', 'report_approved', 'report_rejected',
            'point_issued', 'rank_changed', 'admin_revoked',
            'admin_role_assigned', 'agent_deleted',
        ):
            self.assertIn('record_admin_activity("' + action + '"', SOURCE)

    def test_no_secrets_in_activity_payload(self):
        section = SOURCE.split('async def record_admin_activity(', 1)[1].split('@dp.message(Command("notifications"))', 1)[0]
        self.assertNotIn('BOT_TOKEN', section)
        self.assertNotIn('DEPUTY_KEY', section)
        self.assertNotIn('FIREBASE_PASSWORD', section)


if __name__ == '__main__':
    unittest.main()
