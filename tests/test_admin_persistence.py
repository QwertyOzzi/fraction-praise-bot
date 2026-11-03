import ast
import asyncio
import hashlib
import hmac
import json
import os
import types
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SOURCE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


class Response:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload or {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def json(self):
        return self.payload


class Client:
    async def get_token(self):
        return "test-token"


class Session:
    def __init__(self, cloud):
        self.cloud = cloud
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    def get(self, url, **kwargs):
        if "/config/bot_state" in url:
            return Response(200, {"fields": {"data": {
                "stringValue": json.dumps(self.cloud)
            }}})
        return Response(404)


class Logger:
    def __init__(self):
        self.errors = []
    def info(self, *args):
        pass
    def warning(self, *args):
        pass
    def error(self, *args):
        self.errors.append(args)


def build_context(cloud):
    funcs = [
        node for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
        node.name in ("sign_admin_permissions", "sync_state_from_cloud")
    ]
    saved = []
    logger = Logger()
    env = {
        "Dict": Dict, "List": List, "Any": Any,
        "datetime": datetime, "timezone": timezone,
        "os": os, "json": json, "hmac": hmac, "hashlib": hashlib,
        "uuid": uuid, "BOT_TOKEN": "test-telegram-secret",
        "OWNER_TELEGRAM_IDS": {6060562962},
        "FIREBASE_PROJECT_ID": "fraction-praise",
        "firebase_client": Client(),
        "ClientSession": lambda: Session(cloud),
        "users_db": {},
        "admins_db": [],
        "admin_roles": {},
        "owner_cfg": {"owner_ids": [6060562962]},
        "requests_db": {},
        "completed_cleanup_ids": set(),
        "_pending_legacy_admins": {},
        "_pending_legacy_nonce": "",
        "_cloud_state_hydrated": False,
        "logger": logger,
        "USERS_FILE": "users.json",
        "ADMINS_FILE": "admins.json",
        "OWNER_FILE": "owner.json",
        "REQUESTS_FILE": "requests.json",
        "save_json": lambda *args: saved.append(args),
    }
    exec(compile(ast.Module(body=funcs, type_ignores=[]), "<state sync>", "exec"), env)
    return env


class TestAdminRestoration(unittest.IsolatedAsyncioTestCase):
    async def test_valid_signed_admins_and_roles_restored(self):
        cloud = {
            "users": {
                "101": {"nick": "A_Leader", "rank": "10"},
                "102": {"nick": "B_Deputy", "rank": "9"}
            },
            "admins": [101, 102],
            "admin_roles": {"101": "leader", "102": "deputy"},
            "requests": {}
        }
        ctx = build_context(cloud)
        cloud["admin_acl_signature"] = ctx["sign_admin_permissions"](
            cloud["admins"], cloud["admin_roles"]
        )
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["admins_db"], [101, 102])
        self.assertEqual(ctx["admin_roles"], cloud["admin_roles"])
        self.assertTrue(ctx["_cloud_state_hydrated"])

    async def test_deleted_cloud_profile_is_not_resurrected_from_local_cache(self):
        cloud = {
            "users": {"222": {"nick": "Still_Registered", "rank": "8"}},
            "admins": [],
            "admin_roles": {},
            "requests": {}
        }
        ctx = build_context(cloud)
        ctx["users_db"].update({
            "222": {"nick": "Outdated_Nick", "rank": "8"},
            "999": {"nick": "Already_Deleted", "rank": "7"}
        })
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["users_db"], cloud["users"])
        self.assertNotIn("999", ctx["users_db"])
        self.assertTrue(ctx["_cloud_state_hydrated"])

    async def test_stale_local_admin_not_restored_without_signed_grant(self):
        cloud = {"users": {}, "admins": [], "admin_roles": {}, "requests": {}}
        ctx = build_context(cloud)
        ctx["admins_db"].append(123456)
        ctx["admin_roles"]["123456"] = "leader"
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["admins_db"], [])
        self.assertEqual(ctx["admin_roles"], {})
        self.assertTrue(ctx["_cloud_state_hydrated"])

    async def test_unsigned_legacy_requires_owner_approval(self):
        cloud = {"users": {"101": {"nick": "Old_Leader"}},
                 "admins": [101], "admin_roles": {"101": "leader"}}
        ctx = build_context(cloud)
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["admins_db"], [])
        self.assertFalse(ctx["_cloud_state_hydrated"])
        self.assertEqual(ctx["_pending_legacy_admins"], {101: "leader"})

    async def test_forged_signature_does_not_elevate(self):
        cloud = {"users": {"101": {"nick": "Forged_User"}},
                 "admins": [101], "admin_roles": {"101": "leader"},
                 "admin_acl_signature": "fake"}
        ctx = build_context(cloud)
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["admins_db"], [])
        self.assertFalse(ctx["_cloud_state_hydrated"])

    async def test_empty_existing_state_initializes_safely(self):
        ctx = build_context({"users": {}, "admins": [], "admin_roles": {}})
        await ctx["sync_state_from_cloud"]()
        self.assertEqual(ctx["admins_db"], [])
        self.assertTrue(ctx["_cloud_state_hydrated"])


if __name__ == "__main__":
    unittest.main()
