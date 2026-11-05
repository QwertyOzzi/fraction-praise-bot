import unittest
from datetime import datetime, timezone, timedelta

from personnel import as_utc, report_summary, ranked_reporters, personnel_details


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class PersonnelTests(unittest.TestCase):
    def setUp(self):
        self.requests = {
            "a": {"user_id": "123", "status": "approved", "rep_type": "fractionrp",
                  "quantity": 2, "created_at": (NOW - timedelta(days=2)).isoformat()},
            "b": {"user_id": "123", "status": "pending", "rep_type": "praise",
                  "quantity": 1, "created_at": (NOW - timedelta(days=3)).isoformat()},
            "c": {"user_id": "123", "status": "approved", "rep_type": "praise",
                  "quantity": 1, "created_at": (NOW - timedelta(days=20)).isoformat()},
            "d": {"user_id": "999", "status": "rejected", "rep_type": "praise",
                  "created_at": (NOW - timedelta(days=1)).isoformat()},
            "e": {"user_id": "123", "status": "approved",
                  "created_at": "invalid-date"}
        }

    def test_report_summary_identifies_exact_uid_and_date(self):
        week = report_summary(self.requests, 123, 7, now=NOW)
        self.assertEqual((week["total"], week["approved"], week["pending"]), (2, 1, 1))
        self.assertEqual((week["earned_frp"], week["earned_praise"]), (2, 0))
        self.assertEqual(report_summary(self.requests, 123, 30, now=NOW)["approved"], 2)
        self.assertEqual(report_summary(self.requests, 12, 30, now=NOW)["total"], 0)

    def test_html_escaped_and_missing_history_not_invented(self):
        card = personnel_details({"department": "<Test & 1>", "rank": "8"},
                                 self.requests, 123, now=NOW)
        self.assertIn("&lt;Test &amp; 1&gt;", card)
        self.assertIn("Нет данных", card)
        self.assertIn("не время в игре", card)

    def test_report_leaderboard_only_verified(self):
        profiles = {"123": {"nick": "Agent_A", "rank": "8"},
                    "999": {"nick": "Agent_B", "rank": "7", "verification_status": "pending"}}
        results = ranked_reporters(self.requests, profiles, now=NOW)
        self.assertEqual([r["nick"] for r in results], ["Agent_A"])

    def test_naive_and_utc_timestamps(self):
        self.assertEqual(as_utc("2026-11-05T12:00:00").tzinfo, timezone.utc)
        self.assertIsNone(as_utc("not a timestamp"))


if __name__ == "__main__":
    unittest.main()
