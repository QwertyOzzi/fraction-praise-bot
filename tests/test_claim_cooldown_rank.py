"""Five-minute moderation cooldown: live transitions and rank access guards."""
import ast
import asyncio
import copy
import logging
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
BOT = (ROOT / "bot.py").read_text("utf-8")
PROFILE = (ROOT / "profile_sheets.py").read_text("utf-8")
TREE = ast.parse(BOT)


class Incoming:
    def __init__(self):
        self.from_user = types.SimpleNamespace(id=42)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class Callback(Incoming):
    pass


def make_context(profile, *, cloud_success=True):
    updated = []
    sends = []
    async def sync_cloud():
        updated.append("sync")
        return cloud_success

    env = {
        "Dict": Dict, "Any": Any, "datetime": datetime, "timezone": timezone,
        "asyncio": asyncio, "BaseMiddleware": object, "logger": logging.getLogger("claim-test"),
        "types": types.SimpleNamespace(CallbackQuery=Callback),
        "users_db": {"42": copy.deepcopy(profile)}, "_claim_action_locks": {},
        "CLAIM_COOLDOWN_SECONDS": 300,
        "sync_state_to_cloud": sync_cloud, "USERS_FILE": "test-users.json",
        "save_json": lambda *args: sends.append(args),
    }
    nodes = [
        copy.deepcopy(n) for n in TREE.body
        if ((isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in ("claim_cooldown_remaining",
                            "finish_expired_claim_cooldown"))
            or (isinstance(n, ast.ClassDef)
                and n.name == "DisabledClaimMiddleware"))
    ]
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated claim cooldown>", "exec"), env)
    return env, updated, sends


class TestClaimCooldown(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_blocks_only_for_five_minutes(self):
        profile = {
            "nick": "Former_Agent", "verification_status": "cooldown",
            "claim_id": "stale", "cooldown_until": (
                datetime.now(timezone.utc) + timedelta(minutes=5)
            ).isoformat()
        }
        env, changes, _ = make_context(profile)
        event, handled = Incoming(), []
        async def handler(event, data):
            handled.append(True)
        await env["DisabledClaimMiddleware"]()(handler, event, {})
        self.assertEqual(handled, [])
        self.assertIn("через", event.sent[0][0])
        self.assertEqual(changes, [])
        self.assertEqual(env["users_db"]["42"]["verification_status"], "cooldown")

    async def test_cooldown_expiry_returns_access_to_retry(self):
        profile = {
            "nick": "Former_Agent", "verification_status": "cooldown",
            "claim_id": "stale", "claimed_nick": "Another_Agent",
            "cooldown_until": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
        }
        env, changes, saved = make_context(profile)
        event, handled = Incoming(), []
        async def handler(event, data):
            handled.append(True)
        await env["DisabledClaimMiddleware"]()(handler, event, {})
        self.assertEqual(handled, [True])
        updated = env["users_db"]["42"]
        self.assertEqual(updated["verification_status"], "retry_ready")
        self.assertEqual(updated["nick"], "Former_Agent")
        self.assertNotIn("claim_id", updated)
        self.assertNotIn("cooldown_until", updated)
        self.assertEqual(changes, ["sync"])
        self.assertEqual(len(saved), 1)

    async def test_historic_permanent_denial_can_retry(self):
        env, _, _ = make_context({
            "nick": "", "verification_status": "disabled", "claim_id": "expired"
        })
        event, handled = Incoming(), []
        async def handler(event, data):
            handled.append(True)
        await env["DisabledClaimMiddleware"]()(handler, event, {})
        self.assertEqual(handled, [True])
        self.assertEqual(env["users_db"]["42"]["verification_status"], "retry_ready")

    async def test_cloud_failure_does_not_silently_unlock(self):
        profile = {
            "nick": "", "verification_status": "cooldown",
            "cooldown_until": (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat()
        }
        env, _, _ = make_context(profile, cloud_success=False)
        event, handled = Incoming(), []
        async def handler(event, data):
            handled.append(True)
        await env["DisabledClaimMiddleware"]()(handler, event, {})
        self.assertFalse(handled)
        self.assertEqual(env["users_db"]["42"]["verification_status"], "cooldown")
        self.assertTrue(event.sent)


class TestRankApprovalStatic(unittest.TestCase):
    def test_rank_requests_cannot_bypass_cloud_review(self):
        self.assertIn("requested_rank_change", PROFILE)
        self.assertIn("if (requested_rank_change or", PROFILE)
        self.assertIn('"claimed_rank": rank', PROFILE)
        self.assertIn('rank = str(applicant.get("claimed_rank")', PROFILE)

    def test_ui_has_ten_rank_choices(self):
        self.assertIn('async def cmd_change_rank(', BOT)
        self.assertIn('callback_data="main:rank"', BOT)
        self.assertIn('BotCommand(command="rank"', BOT)
        self.assertIn("range(0, len(ranks), 2)", BOT)

    def test_rejection_invalidates_old_approval_buttons(self):
        node = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "review_claim")
        source = ast.get_source_segment(BOT, node)
        self.assertIn('updated.pop("claim_id", None)', source)
        self.assertIn('timedelta(seconds=CLAIM_COOLDOWN_SECONDS)', source)
        self.assertIn('await sync_state_to_cloud()', source)


if __name__ == "__main__":
    unittest.main()
