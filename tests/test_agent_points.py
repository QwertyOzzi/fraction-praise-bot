"""Agent point commands and isolated logging must not bypass leadership review."""
import unittest

from agent_tools import (
    parse_min_args, suggest_nicks, active_profile, audit_fields, audit_preview
)

ROWS = [
    {"nick": "Gomez_Addams", "id": "abc", "profileId": "id-1", "qty": 9},
    {"nick": "Samuel_Norton", "id": "def", "profileId": "id-2", "qty": 8},
    {"nick": "Homer_Adams", "id": "ghi", "profileId": "id-3", "qty": 1}
]


class TestAgentPoints(unittest.TestCase):
    def test_minus_frp_and_praise_command(self):
        p = parse_min_args("frp Gomez_Addams -2")
        self.assertEqual(p, {
            "action": "debit", "sheet": "fractionrp",
            "amount": 2, "names": ["Gomez_Addams"]
        })
        self.assertEqual(parse_min_args("praise Gomez_Addams −3")["sheet"], "praise")
        self.assertEqual(parse_min_args("фрп Gomez_Addams –1")["action"], "debit")

    def test_plus_is_approval_request_not_unrestricted_debit(self):
        p = parse_min_args("praise Gomez_Addams +2")
        self.assertEqual(p["action"], "credit")
        self.assertEqual(p["amount"], 2)

    def test_cannot_modify_multiple_agents_with_one_agent_command(self):
        with self.assertRaises(ValueError):
            parse_min_args("frp Gomez_Addams,Samuel_Norton -5")

    def test_bad_amounts_and_text_are_rejected(self):
        for command in ("frp Gomez_Addams -0", "frp Gomez_Addams -25",
                        "frp -2", "banana Gomez_Addams -2", "frp Gomez_Addams 5"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    parse_min_args(command)

    def test_suggests_a_nickname_but_never_applies_one(self):
        rows = suggest_nicks("Гомер", ROWS)
        self.assertTrue(rows)
        self.assertTrue(any(r["nick"] == "Homer_Adams" for r in rows))
        self.assertLessEqual(len(rows), 4)

    def test_unauthorized_profiles_cannot_submit(self):
        self.assertFalse(active_profile({}))
        for status in ("pending", "needs_proof", "disabled",
                       "disconnected", "cooldown", "retry_ready"):
            self.assertFalse(active_profile(
                {"nick": "Test_Agent", "verification_status": status}
            ))
        self.assertTrue(active_profile({"nick": "Test_Agent"}))
        self.assertTrue(active_profile({
            "nick": "Test_Agent", "verification_status": "verified"
        }))

    def test_agent_log_has_an_independent_source_field(self):
        values = audit_fields(42, "Gomez_Addams", "debit", "fractionrp",
                              "Samuel_Norton", 2, "agent-12345678")
        self.assertEqual(values["source"]["stringValue"], "agent_points")
        self.assertEqual(values["actorTelegramId"]["integerValue"], "42")
        self.assertEqual(values["sheet"]["stringValue"], "fractionrp")
        self.assertIn("Gomez_Addams", audit_preview({"fields": values}))

    def test_agent_logs_never_use_admin_audit_name(self):
        from pathlib import Path
        directory = Path(__file__).resolve().parents[1]
        agent_module = (directory / "agent_audit.py").read_text("utf-8")
        self.assertIn("/agent_action_log/", agent_module)
        self.assertIn("/news_dispatch_log/", agent_module)
        self.assertNotIn("/audit_log", agent_module)


if __name__ == "__main__":
    unittest.main()
