"""Read-only announcement journal UI and callback security regression tests."""
import ast
import copy
import re
import types
import unittest
from pathlib import Path

from newslog_view import (
    eligible_news, newslog_list, newslog_detail, news_date,
    text_size_utf16, split_telegram_text, NEWSLOG_FETCH_LIMIT,
)

SOURCE = (Path(__file__).resolve().parents[1] / "bot.py").read_text("utf-8")
TREE = ast.parse(SOURCE)
NEWS_ID = "news-" + "a" * 16


def record(ident=NEWS_ID, *, text="Новости с <b>тегами</b>"):
    return {
        "id": ident, "title": "Новости FBI", "category": "news",
        "actor_id": 41, "message": text, "created_at": "2026-11-05T13:02:00+00:00",
        "finished_at": "2026-11-05T13:03:00+00:00",
        "status": "delivered", "sent": 7, "failed": 0, "recipients": 7,
    }


class TestNewslogFormat(unittest.TestCase):
    def test_pages_and_distinguishable_news_items(self):
        rows = [record("news-" + format(i, "016x")) for i in range(19)]
        text, items, page, pages = newslog_list(rows, page=1)
        self.assertEqual((len(items), page, pages), (8, 1, 3))
        self.assertIn("Страница 2/3", text)
        self.assertIn("#000008", text)
        self.assertNotIn("#000000", text)
        self.assertEqual(newslog_list(rows, 999)[2], 2)

    def test_invalid_id_rejected_before_linking(self):
        self.assertEqual(eligible_news([record("fake/collection"), record()]), [record()])
        self.assertEqual(newslog_list([], 0)[3], 1)

    def test_detail_contains_full_text_and_saved_delivery_stats(self):
        result = newslog_detail(record(), {"41": {"nick": "Current_Agent"}})
        self.assertIn("Новости с <b>тегами</b>", result)
        self.assertIn("Telegram ID 41", result)
        self.assertIn("текущий ник: Current_Agent", result)
        self.assertIn("05.11.2026 13:02 UTC", result)
        self.assertIn("✅ Доставлено: 7", result)
        self.assertIn("⚠️ Ошибок доставки: 0", result)
        self.assertNotIn("Новости с", newslog_detail(record(), include_text=False))

    def test_missing_author_and_timestamps_are_not_fabricated(self):
        old = {**record(), "actor_id": None, "created_at": "", "finished_at": None,
               "status": "sending"}
        result = newslog_detail(old)
        self.assertIn("Автор: Не указан", result)
        self.assertIn("Опубликовано: Не указана", result)
        self.assertIn("ещё не подтверждён", result)
        self.assertEqual(news_date("not-a-time"), "Не указана")

    def test_emoji_text_can_be_shown_entirely_in_chunks(self):
        raw = "📰" * 3000 + "\n" + "<b>!"
        chunks = split_telegram_text(raw)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), raw)
        self.assertTrue(all(text_size_utf16(part) <= 3400 for part in chunks))


class Button:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Message:
    def __init__(self, uid=41, private=True):
        self.chat = types.SimpleNamespace(
            id=uid if private else -10001,
            type="private" if private else "supergroup",
        )
        self.edits = []
        self.sent = []

    async def edit_text(self, message, **kwargs):
        self.edits.append((message, kwargs))

    async def answer(self, message, **kwargs):
        self.sent.append((message, kwargs))


class Callback:
    def __init__(self, data, uid=41, private=True):
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = Message(uid, private=private)
        self.data = data
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def build_handlers(env):
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            "newslog_list_keyboard", "show_newslog_list", "newslog_callback"
        ):
            clone = copy.deepcopy(node)
            clone.decorator_list = []
            nodes.append(clone)
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=nodes, type_ignores=[])), "<newslog>", "exec"), env)
    return env


class TestNewslogCallbacks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lookups = []
        self.list_queries = []

        async def lookup(client, session, news_id):
            self.lookups.append(news_id)
            return record(news_id) if news_id == NEWS_ID else None

        async def list_records(client, session, collection, *, limit):
            self.list_queries.append((collection, limit))
            return [record()]

        self.env = build_handlers({
            "types": types.SimpleNamespace(Message=Message, CallbackQuery=Callback),
            "re": re, "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
            "is_leader": lambda uid: uid == 41,
            "firebase_client": object(), "ClientSession": object,
            "get_announcement": lookup, "list_collection": list_records,
            "NEWSLOG_FETCH_LIMIT": NEWSLOG_FETCH_LIMIT,
            "newslog_list": newslog_list, "news_date": news_date,
            "newslog_detail": newslog_detail,
            "text_size_utf16": text_size_utf16,
            "split_telegram_text": split_telegram_text,
            "users_db": {"41": {"nick": "Current_Agent"}},
            "InlineKeyboardButton": Button, "InlineKeyboardMarkup": Button,
        })

    async def test_nonleader_and_group_cannot_open_news(self):
        for callback in (
            Callback(f"newslog:view:{NEWS_ID}:0", uid=99),
            Callback(f"newslog:view:{NEWS_ID}:0", private=False)
        ):
            await self.env["newslog_callback"](callback)
            self.assertEqual(self.lookups, [])
            self.assertTrue(callback.answers[-1][1]["show_alert"])

    async def test_invalid_id_and_page_do_not_query_firestore(self):
        for data in ("newslog:view:../config:0", "newslog:page:-1",
                     f"newslog:view:{NEWS_ID}:9999"):
            callback = Callback(data)
            await self.env["newslog_callback"](callback)
            self.assertTrue(callback.answers[-1][1]["show_alert"])
        self.assertFalse(self.lookups)
        self.assertFalse(self.list_queries)

    async def test_detail_is_plain_text_with_back_and_refresh(self):
        callback = Callback(f"newslog:view:{NEWS_ID}:0")
        await self.env["newslog_callback"](callback)
        self.assertEqual(self.lookups, [NEWS_ID])
        self.assertEqual(len(callback.message.edits), 1)
        body, options = callback.message.edits[0]
        self.assertIn("<b>тегами</b>", body)
        self.assertIsNone(options["parse_mode"])
        buttons = options["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "newslog:page:0")
        self.assertEqual(buttons[0][1].callback_data, callback.data)

    async def test_back_to_list_fetches_fresh_firestore_data(self):
        callback = Callback("newslog:page:0")
        await self.env["newslog_callback"](callback)
        self.assertEqual(self.list_queries, [("fbi_announcements", NEWSLOG_FETCH_LIMIT)])
        markup = callback.message.edits[0][1]["reply_markup"]
        self.assertIn(NEWS_ID, markup.inline_keyboard[0][0].callback_data)


if __name__ == "__main__":
    unittest.main()
