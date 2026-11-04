"""Concurrent album delivery must create a single report confirmation."""
import ast
import asyncio
import copy
import html
import types
import unittest
import uuid
from pathlib import Path
from typing import Any, Dict, List

CODE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(CODE)


class Button:
    def __init__(self, **values):
        self.__dict__.update(values)


class Markup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class Media:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeState:
    def __init__(self):
        self.state = "waiting"
        self.data = {
            "report_session": "sessionabc123",
            "rep_type": "fractionrp", "quantity": 1, "reason": "другое",
            "proof_media": [],
        }

    async def get_state(self):
        return self.state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **values):
        self.data.update(values)

    async def set_state(self, new_state):
        self.state = new_state.state


class FakeMessage:
    def __init__(self, message_id, *, media_kind="photo", caption="", text=None):
        self.message_id = message_id
        self.chat = types.SimpleNamespace(id=100)
        self.from_user = types.SimpleNamespace(id=42)
        self.photo = [types.SimpleNamespace(file_id=f"photo-{message_id}")] if media_kind == "photo" else None
        self.video = types.SimpleNamespace(file_id=f"video-{message_id}") if media_kind == "video" else None
        self.caption = caption
        self.text = text
        self.responses = []

    async def answer(self, *args, **kwargs):
        self.responses.append((args, kwargs))


class FakeBot:
    def __init__(self):
        self.edits = []
        self.sent = []
        self.groups = []
        self.photos = []
        self.videos = []
        self.pins = []

    async def edit_message_text(self, *args, **kwargs):
        self.edits.append((args, kwargs))

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return types.SimpleNamespace(message_id=991)

    async def send_media_group(self, *args, **kwargs):
        self.groups.append((args, kwargs))

    async def send_photo(self, *args, **kwargs):
        self.photos.append((args, kwargs))

    async def send_video(self, *args, **kwargs):
        self.videos.append((args, kwargs))

    async def pin_chat_message(self, **kwargs):
        self.pins.append(kwargs)


class Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class FakeCallback:
    def __init__(self, payload):
        self.data = payload
        self.from_user = types.SimpleNamespace(id=42)
        self.message = FakeMessage(991, media_kind="")
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def load(names, extra=None):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            copy_node = copy.deepcopy(node)
            copy_node.decorator_list = []
            nodes.append(copy_node)
    fake_bot = FakeBot()
    sent_cards = []
    edited_cards = []

    async def send_card(message, text, **kwargs):
        sent_cards.append((message, text, kwargs))
        return types.SimpleNamespace(message_id=991)

    async def edit_card(message, text, **kwargs):
        edited_cards.append((message, text, kwargs))

    env = {
        "Dict": Dict, "Any": Any, "List": List,
        "types": types.SimpleNamespace(Message=FakeMessage, CallbackQuery=FakeCallback),
        "FSMContext": FakeState, "html": html, "asyncio": asyncio, "uuid": uuid,
        "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Markup,
        "InputMediaPhoto": Media, "InputMediaVideo": Media,
        "ReportStates": types.SimpleNamespace(
            waiting_for_proof=types.SimpleNamespace(state="waiting"),
            confirm=types.SimpleNamespace(state="confirm")
        ),
        "REPORT_MEDIA_LIMIT": 10, "_report_media_locks": {},
        "users_db": {"42": {"nick": "Gomez_Addams", "rank": "8"}},
        "bot": fake_bot, "logger": Logger(),
        "send_fbi_card": send_card, "edit_fbi_card": edit_card,
    }
    if extra:
        env.update(extra)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<album helper functions>", "exec"), env)
    return env, fake_bot, sent_cards, edited_cards


class TestAlbumAggregation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env, self.bot, self.cards, self.edited = load({
            "report_preview_text", "report_confirm_keyboard",
            "report_media_progress_text", "process_report_proof",
            "finish_report_proof"
        })
        self.state = FakeState()

    async def test_three_concurrent_photos_have_one_counter_then_one_confirmation(self):
        photos = [FakeMessage(i, caption="Завод" if i == 101 else "")
                  for i in (101, 102, 103)]
        await asyncio.gather(*(self.env["process_report_proof"](m, self.state)
                               for m in photos))
        self.assertEqual(len(self.cards), 1)
        self.assertEqual(len(self.state.data["proof_media"]), 3)
        self.assertEqual(self.state.state, "waiting")
        self.assertEqual(len(self.bot.edits), 2)

        callback = FakeCallback("repfinish:sessionabc123")
        await self.env["finish_report_proof"](callback, self.state)
        self.assertEqual(self.state.state, "confirm")
        self.assertEqual(len(self.edited), 1)
        self.assertIn("Фото (3 шт.)", self.edited[0][1])
        self.assertIn("Завод", self.edited[0][1])
        self.assertEqual(len(self.state.data["proof_media"]), 3)

    async def test_replayed_photo_not_counted_twice(self):
        photo = FakeMessage(101)
        await self.env["process_report_proof"](photo, self.state)
        await self.env["process_report_proof"](photo, self.state)
        self.assertEqual(len(self.state.data["proof_media"]), 1)
        self.assertEqual(len(self.cards), 1)

    async def test_stale_confirmation_button_is_rejected(self):
        await self.env["process_report_proof"](FakeMessage(101), self.state)
        callback = FakeCallback("repfinish:wrong-session")
        await self.env["finish_report_proof"](callback, self.state)
        self.assertEqual(self.state.state, "waiting")
        self.assertEqual(len(self.edited), 0)
        self.assertTrue(callback.answers[-1][1]["show_alert"])

    async def test_mixed_photo_video_one_confirmation(self):
        await self.env["process_report_proof"](FakeMessage(101), self.state)
        await self.env["process_report_proof"](
            FakeMessage(102, media_kind="video"), self.state)
        callback = FakeCallback("repfinish:sessionabc123")
        await self.env["finish_report_proof"](callback, self.state)
        self.assertIn("Фото и видео (2 шт.)", self.edited[0][1])

    async def test_single_url_still_goes_straight_to_review(self):
        await self.env["process_report_proof"](
            FakeMessage(300, media_kind="", text="https://example.org/report"),
            self.state
        )
        self.assertEqual(self.state.state, "confirm")
        self.assertEqual(len(self.cards), 1)
        self.assertIn("https://example.org/report", self.cards[0][1])


class TestModeratorAlbum(unittest.IsolatedAsyncioTestCase):
    async def test_all_album_attachments_forwarded_once(self):
        env, bot, _, _ = load({"pending_report_keyboard", "pending_report_text",
                              "send_pending_report_to_admin"})
        report = {
            "nick": "Gomez_Addams", "rank": "8", "quantity": 1,
            "rep_type": "fractionrp", "reason": "другое", "proof": "Завод",
            "admin_messages": [],
            "proof_media": [
                {"type": "photo", "file_id": "photo-a"},
                {"type": "photo", "file_id": "photo-b"},
                {"type": "photo", "file_id": "photo-c"},
            ],
        }
        await env["send_pending_report_to_admin"](900, "report42", report)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(len(bot.groups), 1)
        self.assertEqual([media.media for media in bot.groups[0][1]["media"]],
                         ["photo-a", "photo-b", "photo-c"])
        self.assertEqual(report["admin_messages"],
                         [{"chat_id": 900, "message_id": 991}])
        self.assertEqual(len(bot.pins), 1)

    async def test_legacy_single_photo_still_forwarded(self):
        env, bot, _, _ = load({"pending_report_keyboard", "pending_report_text",
                              "send_pending_report_to_admin"})
        report = {
            "nick": "Older", "rank": "7", "quantity": 1,
            "rep_type": "praise", "reason": "отчёт", "proof": "Фото",
            "proof_media_type": "photo", "proof_file_id": "old-file",
        }
        await env["send_pending_report_to_admin"](900, "old", report)
        self.assertEqual(len(bot.photos), 1)
        self.assertEqual(len(bot.groups), 0)


if __name__ == "__main__":
    unittest.main()
