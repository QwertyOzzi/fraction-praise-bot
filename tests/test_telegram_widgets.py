"""Tests for the isolated Bot API 10.4 widget implementation."""
import json
import sys
import unittest
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

# CI intentionally runs dependency-free tests without installing aiohttp.
# Mock only the external networking module; widget logic stays real.
try:
    import aiohttp  # noqa: F401
except ImportError:
    sys.modules["aiohttp"] = MagicMock()

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
    def test_safe_error_details_for_diagnostics(self):
        self.assertEqual(
            w.safe_telegram_description({"description": "Bad Request: INPUT_MEDIA_WIDGET_INVALID"}),
            "Bad Request: INPUT_MEDIA_WIDGET_INVALID"
        )
        masked = w.safe_telegram_description({
            "description": "Bad Request: id 123456789012 unavailable"
        })
        self.assertNotIn("123456789012", masked)
        self.assertIn("[id]", masked)
        hidden = w.safe_telegram_description({
            "description": "Bad Request: https://api.telegram.org/bot123:SECRET"
        })
        self.assertEqual(hidden, "Telegram rejected the request")

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

    def test_native_html_fallback_escapes_profile_fields(self):
        data = roster({
            "nick": "Agent_Test",
            "rank": "<script>",
            "verification_status": "verified"
        })
        state = w._state_data(w.profile_snapshot(data, 123))
        rendered = w.fallback_card_html(state)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("FRP", rendered)
        self.assertIn("Обновить", str(w.refresh_keyboard()))

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
    async def test_unchanged_widget_edit_is_not_a_failure(self):
        class Response:
            status = 400
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def json(self, **kwargs):
                return {"ok": False, "error_code": 400,
                        "description": "Bad Request: message is not modified"}

        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            def post(self, url, json):
                return Response()

        with patch.object(w, "ClientSession", return_value=Session()):
            done = await w.telegram_api("fake_token", "editMessageWidgetData", {})
        self.assertTrue(done)

    async def test_denied_widget_becomes_native_auto_updated_card(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value=None)),
              patch.object(w, "send_profile_widget",
                           AsyncMock(side_effect=w.WidgetUnavailable(
                               "Telegram sendWidget: Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED"
                           ))),
              patch.object(w, "send_fallback_card", AsyncMock(return_value=77)) as send,
              patch.object(w, "save_ref", AsyncMock()) as save):
            result = await w.open_profile_widget(object(), "fake_token", 123)
            self.assertEqual(result, "fallback")
            send.assert_awaited_once()
            self.assertEqual(save.await_args.kwargs["kind"], "text")
            self.assertEqual(save.await_args.args[2], 77)

    async def test_existing_native_card_is_edited_in_place(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value={
                  "message_id": 77, "last_data": "{}", "kind": "text"
              })),
              patch.object(w, "edit_fallback_card", AsyncMock()) as edit,
              patch.object(w, "send_profile_widget", AsyncMock()) as html_send,
              patch.object(w, "send_fallback_card", AsyncMock()) as text_send,
              patch.object(w, "save_ref", AsyncMock()) as save):
            result = await w.open_profile_widget(object(), "fake_token", 123)
            self.assertEqual(result, "updated")
            self.assertEqual(edit.await_args.args[2], 77)
            self.assertEqual(save.await_args.kwargs["kind"], "text")
            html_send.assert_not_awaited()
            text_send.assert_not_awaited()

    async def test_native_manual_refresh_uses_text_edit(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value={
                  "message_id": 77, "last_data": "{}", "kind": "text"
              })),
              patch.object(w, "edit_fallback_card", AsyncMock()) as edit,
              patch.object(w, "update_profile_widget", AsyncMock()) as widget):
            await w.refresh_profile_widget(object(), "fake_token", 123, 77)
            edit.assert_awaited_once()
            widget.assert_not_awaited()

    async def test_other_widget_errors_do_not_trigger_fallback(self):
        data = roster({"nick": "Agent_Test", "rank": "7", "verification_status": "verified"})
        with (patch.object(w, "read_roster", AsyncMock(return_value=data)),
              patch.object(w, "read_ref", AsyncMock(return_value=None)),
              patch.object(w, "send_profile_widget", AsyncMock(
                  side_effect=w.WidgetUnavailable("Telegram sendWidget: HTML_INVALID")
              )),
              patch.object(w, "send_fallback_card", AsyncMock()) as fallback):
            with self.assertRaises(w.WidgetUnavailable):
                await w.open_profile_widget(object(), "fake_token", 123)
            fallback.assert_not_awaited()

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
            self.assertEqual(edit.await_args.args[1:3], (123, 789))

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
