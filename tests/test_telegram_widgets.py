"""Tests for the isolated Bot API 10.4 widget implementation."""
import json
import unittest
import zlib
from unittest.mock import AsyncMock, patch

import telegram_widgets as w


def roster(profile=None, frp=None, praise=None):
    return {
        "users": {"123": profile} if profile is not None else {},
        "sheets": {
            "fractionrp": frp if frp is not None else [],
            "praise": praise if praise is not None else [],
        },
    }


class WidgetDataTests(unittest.TestCase):
    def test_html_stays_within_bot_api_limits(self):
        data = w.PROFILE_WIDGET_HTML.encode("utf-8")
        self.assertLessEqual(len(data), 32768)
        self.assertLessEqual(len(zlib.compress(data, 9)), 4096)
        self.assertNotIn(b"\x00", data)
        self.assertIn("chatDataChanged", w.PROFILE_WIDGET_HTML)
        self.assertIn("textContent", w.PROFILE_WIDGET_HTML)

    def test_verified_agent_balance_by_uid_nick(self):
        data = roster(
            {"nick": "Agent_Test", "rank": "8", "verification_status": "verified"},
            [{"nick": "agent_test", "qty": 12}, {"nick": "Other_Test", "qty": 900}],
            [{"nick": "Agent_Test", "qty": 4}],
        )
        snap = w.profile_snapshot(data, 123)
        self.assertEqual((snap["frp"], snap["praise"], snap["rank"]), ("12", "4", "8"))
        self.assertEqual(w.profile_snapshot(data, 999)["frp"], "—")

    def test_unverified_and_disabled_cannot_see_old_balances(self):
        for status in ["pending", "needs_proof", "disabled", "disconnected"]:
            data = roster(
                {"nick": "Agent_Test", "rank": "9", "verification_status": status},
                [{"nick": "Agent_Test", "qty": 999}],
            )
            snap = w.profile_snapshot(data, 123)
            self.assertEqual((snap["nick"], snap["frp"], snap["praise"]), ("—", "—", "—"))

    def test_missing_or_duplicate_rows_are_not_fake_zeroes(self):
        data = roster(
            {"nick": "Agent_Test", "rank": "5", "verification_status": "verified"},
            [{"nick": "Agent_Test", "qty": 1}, {"nick": "agent_test", "qty": 2}],
        )
        snap = w.profile_snapshot(data, 123)
        self.assertEqual(snap["frp"], "—")
        self.assertEqual(snap["praise"], "—")
        self.assertIn("Конфликт", snap["status"])

    def test_state_fits_size_and_does_not_include_secrets(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified",
                       "telegram_username": "private", "auth_token": "do_not_expose"})
        state = w._state_data(w.profile_snapshot(data, 123))
        parsed = json.loads(state)
        self.assertLessEqual(len(state.encode("utf-8")), 1024)
        self.assertNotIn("private", state)
        self.assertNotIn("do_not_expose", state)
        self.assertEqual(parsed["nick"], "Agent_Test")


class WidgetOperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_duplicate_if_existing_widget_edit_fails(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value={
                  "message_id": 888, "last_data": "{}"
              })),
              patch.object(w, "update_profile_widget",
                           AsyncMock(side_effect=w.WidgetUnavailable("edit failed"))),
              patch.object(w, "send_profile_widget", AsyncMock()) as new_widget):
            with self.assertRaises(w.WidgetUnavailable):
                await w.open_profile_widget(object(), "fake_token", 123)
            new_widget.assert_not_awaited()

    async def test_manual_widget_refresh_only_edits_message(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value=None)),
              patch.object(w, "update_profile_widget", AsyncMock()) as edit):
            await w.refresh_profile_widget(object(), "fake_token", 123, 789)
            edit.assert_awaited_once()
            self.assertEqual(edit.await_args.args[2:4], (123, 789))

    async def test_registry_downtime_still_allows_a_manual_card(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref",
                           AsyncMock(side_effect=w.WidgetUnavailable("firestore denied"))),
              patch.object(w, "send_profile_widget", AsyncMock(return_value=42)) as send,
              patch.object(w, "save_ref",
                           AsyncMock(side_effect=w.WidgetUnavailable("firestore denied")))):
            result = await w.open_profile_widget(object(), "fake_token", 123)
            self.assertEqual(result, "untracked")
            send.assert_awaited_once()
