"""Regression checks for permission persistence and public attack surface."""
import pathlib
import unittest

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")


class SecurityHardeningRegressionTests(unittest.TestCase):
    def test_cloud_acl_404_revokes_local_non_owner(self):
        start = SOURCE.index('elif resp.status == 404:')
        body = SOURCE[start: SOURCE.index('else:', start)]
        self.assertIn('admins_db.clear()', body)
        self.assertIn('admin_roles.clear()', body)
        self.assertIn('save_json(ADMINS_FILE, admins_db)', body)

    def test_health_only_constant_state(self):
        section = SOURCE.split('async def handle_ping(request):', 1)[1].split('_support_rate_history', 1)[0]
        self.assertIn('{"status": "online"}', section)
        for item in ('bot_username', 'LEADER_INFO', 'RENDER_GIT_COMMIT'):
            self.assertNotIn(item, section)

    def test_public_ticket_never_reports_success_without_storage(self):
        section = SOURCE.split('async def handle_api_support(request):', 1)[1].split('async def start_web_server():', 1)[0]
        self.assertIn('if not ticket_id:', section)
        self.assertIn('status=503', section)
        self.assertNotIn('{"error": str(e)}', section)
        self.assertIn('safe_support_url(screenshot)', section)

    def test_public_ticket_has_layered_rate_limits(self):
        section = SOURCE.split('async def handle_api_support(request):', 1)[1].split('async def start_web_server():', 1)[0]
        self.assertIn('len(_support_global_attempts) >= 100', section)
        self.assertIn('len(attempts) >= 5', section)
        self.assertIn('request.content_type != "application/json"', section)
        self.assertIn('origin == "https://qwertyozzi.github.io"', section)

    def test_anonymous_support_write_disallowed(self):
        section = SOURCE.split('async def add_support_ticket(', 1)[1].split('\nfirebase_client =', 1)[0]
        self.assertIn('if not token:', section)
        self.assertIn('return None', section)


if __name__ == "__main__":
    unittest.main()
