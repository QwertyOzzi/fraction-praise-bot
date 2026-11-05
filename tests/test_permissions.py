"""Permission regression tests without cloud access."""
import unittest

from access_control import (
    AGENT_DELEGATABLE, PERMISSIONS, normalize_overrides, sign_overrides,
    has_permission, max_debit_for,
)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = dict(owners={1}, admins=[2, 3],
                        roles={"2": "leader", "3": "deputy"},
                        overrides={}, hydrated=True)

    def can(self, uid, action):
        return has_permission(uid, action, **self.ctx)

    def test_default_leadership_scope(self):
        self.assertTrue(self.can(1, "agents.delete"))
        self.assertFalse(self.can(2, "agents.delete"))
        self.assertTrue(self.can(2, "agents.rank"))
        self.assertTrue(self.can(2, "events.manage"))
        self.assertTrue(self.can(2, "messages.schedule"))
        self.assertFalse(self.can(3, "events.manage"))
        self.assertFalse(self.can(3, "messages.schedule"))
        self.assertFalse(self.can(3, "agents.rank"))
        self.assertTrue(self.can(3, "reports.review"))
        self.assertFalse(self.can(4, "points.debit"))
        self.assertFalse(self.can(4, "agents.view"))

    def test_owner_can_delegate_safe_debit_only(self):
        self.ctx["overrides"] = {"4": {"points.debit": True, "agents.view": True}}
        self.assertTrue(self.can(4, "points.debit"))
        self.assertFalse(self.can(4, "agents.view"))
        self.assertEqual(max_debit_for(4, **self.ctx), 1)
        self.assertEqual(max_debit_for(3, **self.ctx), 5)
        self.assertEqual(max_debit_for(2, **self.ctx), 20)
        self.assertEqual(max_debit_for(1, **self.ctx), 20)

    def test_explicit_deny_and_unhydrated(self):
        self.ctx["overrides"] = {"3": {"points.grant": False},
                                 "2": {"agents.rank": False}}
        self.assertFalse(self.can(3, "points.grant"))
        self.assertFalse(self.can(2, "agents.rank"))
        self.ctx["hydrated"] = False
        self.assertFalse(self.can(3, "reports.review"))
        self.assertTrue(self.can(1, "agents.delete"))

    def test_signed_overrides_tampering(self):
        trusted = {"3": {"reports.review": True}}
        expected = sign_overrides(trusted, "test-key")
        self.assertEqual(expected, sign_overrides(trusted, "test-key"))
        self.assertNotEqual(expected, sign_overrides(
            {"3": {"reports.review": False}}, "test-key"))

    def test_malformed_overrides_fail_closed(self):
        for bad in ({"abc": {}}, {"0": {}},
                    {"3": {"reports.review": "yes"}},
                    {"3": {"root.admin": True}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_overrides(bad)

    def test_known_permission_set(self):
        self.assertIn("points.debit", AGENT_DELEGATABLE)
        self.assertIn("agents.rank", PERMISSIONS)


if __name__ == "__main__":
    unittest.main()
