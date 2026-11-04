"""Occupied nicknames must create a safe provisional claim, not steal profiles."""
import ast
import asyncio
import copy
import hmac
import json
import logging
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_SOURCE = (ROOT / "profile_sheets.py").read_text("utf-8")
PROFILE_TREE = ast.parse(PROFILE_SOURCE)
NODES = [node for node in PROFILE_TREE.body
         if isinstance(node, ast.AsyncFunctionDef)
         and node.name in ("register_profile", "resolve_nickname_claim")]
PROFILE_ENV = {
    "asyncio": asyncio, "hmac": hmac, "json": json, "re": re, "uuid": uuid,
    "datetime": datetime, "timezone": timezone,
    "logger": logging.getLogger("test-claims"),
    "ClientTimeout": lambda **kwargs: None,
    "SHEETS": ("fractionrp", "praise")
}
exec(compile(ast.fix_missing_locations(ast.Module(body=NODES, type_ignores=[])),
             "isolated profile helpers", "exec"), PROFILE_ENV)
register_profile = PROFILE_ENV["register_profile"]
resolve_nickname_claim = PROFILE_ENV["resolve_nickname_claim"]


class Response:
    def __init__(self, status, data=None):
        self.status = status
        self.data = copy.deepcopy(data or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return copy.deepcopy(self.data)


class Firestore:
    def __init__(self, *, owner_admin=False):
        old_id = "a" * 32
        self.users = {
            "111": {"nick": "Samuel_Norton", "rank": "9",
                    "telegram_username": "oldowner", "profileId": old_id}
        }
        self.admins = [111] if owner_admin else []
        self.rows = {
            "fractionrp": [{"nick": "Samuel_Norton", "rank": "9",
                            "profileId": old_id, "qty": 8,
                            "status": "На выдаче",
                            "proof": {"text": "reason", "links": []}}],
            "praise": [{"nick": "Samuel_Norton", "rank": "9",
                       "profileId": old_id, "qty": 3,
                       "status": "На выдаче",
                       "proof": {"text": "reason", "links": []}}]
        }
        self.versions = {"config/bot_state": 1,
                         "sheets/fractionrp": 1,
                         "sheets/praise": 1}
        self.sheet_writes = 0

    def read(self, url):
        path = url.rsplit("/documents/", 1)[1]
        revision = self.versions[path]
        if path == "config/bot_state":
            state = {
                "users": self.users, "admins": self.admins,
                "admin_roles": {str(uid): "deputy" for uid in self.admins},
                "admin_acl_signature": "signed-acl",
                "requests": {}
            }
            data = {"fields": {"data": {"stringValue": json.dumps(state)}}}
        else:
            rows = self.rows[path.split("/")[-1]]
            data = {"fields": {"rows": {"arrayValue": {
                "values": [self.pack(row) for row in rows]
            }}}}
        return Response(200, {"updateTime": "v" + str(revision), **data})

    @staticmethod
    def pack(row):
        return {"mapValue": {"fields": copy.deepcopy(row)}}

    def commit(self, payload):
        writes = payload["writes"]
        for write in writes:
            path = write["update"]["name"].rsplit("/documents/", 1)[1]
            if write["currentDocument"]["updateTime"] != "v" + str(self.versions[path]):
                return Response(412)
        for write in writes:
            path = write["update"]["name"].rsplit("/documents/", 1)[1]
            fields = write["update"]["fields"]
            if path == "config/bot_state":
                state = json.loads(fields["data"]["stringValue"])
                self.users = state["users"]
                self.admins = state["admins"]
            else:
                sheet = path.split("/")[-1]
                self.rows[sheet] = [
                    copy.deepcopy(value["mapValue"]["fields"])
                    for value in fields["rows"]["arrayValue"]["values"]
                ]
                self.sheet_writes += 1
            self.versions[path] += 1
        return Response(200)


class Session:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        return self.store.read(url)

    def post(self, url, **kwargs):
        return self.store.commit(kwargs["json"])


class Client:
    def __init__(self, store):
        self.store = store

    async def get_token(self):
        return "test-token"

    def _parse_firestore_rows(self, values):
        return [copy.deepcopy(row["mapValue"]["fields"]) for row in values]

    def _row_to_firestore(self, row):
        return Firestore.pack(row)


class TestNicknameClaimTransaction(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Firestore()
        self.client = Client(self.store)
        self.lock = asyncio.Lock()
        self.signer = lambda admins, roles: "signed-acl"
        # CI's static-test environment does not install aiohttp.
        PROFILE_ENV["ClientSession"] = lambda **kwargs: Session(self.store)

    async def claim(self, nick="Samuel_Norton"):
        return await register_profile(
            self.client, "test", 222, nick, "8", "candidate",
            "", self.signer, self.lock
        )

    async def test_taken_nick_is_provisionally_accepted_without_sheet_write(self):
        ok, message, old_nick, pending = await self.claim()
        self.assertTrue(ok, message)
        self.assertEqual(old_nick, "")
        self.assertEqual(pending["verification_status"], "pending")
        self.assertEqual(pending["claimed_nick"], "Samuel_Norton")
        self.assertFalse(pending["nick"])
        self.assertEqual(self.store.users["111"]["nick"], "Samuel_Norton")
        self.assertEqual(self.store.rows["fractionrp"][0]["qty"], 8)
        self.assertEqual(self.store.rows["praise"][0]["qty"], 3)
        self.assertEqual(self.store.sheet_writes, 0)

    async def test_review_transfers_binding_atomically_with_balances_unchanged(self):
        ok, _, _, pending = await self.claim()
        self.assertTrue(ok)
        local = copy.deepcopy(self.store.users)
        approved, reason, holder, displaced = await resolve_nickname_claim(
            self.client, "test", 222, pending["claim_id"],
            self.signer, self.lock, {1}, local_users=local
        )
        self.assertTrue(approved, reason)
        self.assertEqual(holder["nick"], "Samuel_Norton")
        self.assertEqual(holder["verification_status"], "verified")
        self.assertEqual(displaced["111"]["verification_status"], "disconnected")
        self.assertEqual(displaced["111"]["nick"], "")
        self.assertNotEqual(displaced["111"]["profileId"], holder["profileId"])
        self.assertEqual(self.store.rows["fractionrp"][0]["qty"], 8)
        self.assertEqual(self.store.rows["praise"][0]["qty"], 3)
        self.assertEqual(self.store.rows["fractionrp"][0]["profileId"],
                         holder["profileId"])
        self.assertEqual(local["222"]["nick"], "Samuel_Norton")
        self.assertEqual(local["111"]["nick"], "")

    async def test_stale_or_replayed_claim_rejected(self):
        _, _, _, pending = await self.claim()
        ok, _, _, _ = await resolve_nickname_claim(
            self.client, "test", 222, "bad-nonce",
            self.signer, self.lock, {1}
        )
        self.assertFalse(ok)
        ok, _, _, _ = await resolve_nickname_claim(
            self.client, "test", 222, pending["claim_id"],
            self.signer, self.lock, {1}
        )
        self.assertTrue(ok)
        again, _, _, _ = await resolve_nickname_claim(
            self.client, "test", 222, pending["claim_id"],
            self.signer, self.lock, {1}
        )
        self.assertFalse(again)

    async def test_cannot_displace_approved_admin(self):
        self.store = Firestore(owner_admin=True)
        self.client = Client(self.store)
        _, _, _, pending = await self.claim()
        ok, reason, _, _ = await resolve_nickname_claim(
            self.client, "test", 222, pending["claim_id"],
            self.signer, self.lock, {1}
        )
        self.assertFalse(ok)
        self.assertIn("руководителя", reason)
        self.assertEqual(self.store.users["111"]["nick"], "Samuel_Norton")

    async def test_can_choose_free_nickname_after_conflict(self):
        _, _, _, pending = await self.claim()
        result = await register_profile(
            self.client, "test", 222, "New_Agent", "8",
            "candidate", "", self.signer, self.lock
        )
        self.assertTrue(result[0], result[1])
        user = result[3]
        self.assertEqual(user["nick"], "New_Agent")
        self.assertNotIn("claim_id", user)
        self.assertNotIn("claimed_nick", user)
        self.assertEqual(self.store.rows["fractionrp"][0]["qty"], 8)
        self.assertEqual(len(self.store.rows["fractionrp"]), 2)


class TestClaimSecurity(unittest.TestCase):
    def test_claims_never_grant_admin_authority(self):
        source = (ROOT / "bot.py").read_text("utf-8")
        self.assertIn('not is_admin(callback.from_user.id)', source)
        self.assertIn('if not _cloud_state_hydrated:', source)
        self.assertIn("DisabledClaimMiddleware", source)
        self.assertIn('"verification_status") == "disabled"', source)
        self.assertIn('await notify_claim_reviewers(user_key, saved_profile)', source)
        self.assertIn("copy_message(", source)
        self.assertIn("await sync_state_to_cloud()", source)

    def test_registration_is_atomic_on_collision(self):
        source = (ROOT / "profile_sheets.py").read_text("utf-8")
        self.assertIn('"currentDocument": {"updateTime": docs["config/bot_state"]["updateTime"]}', source)
        self.assertIn('"writes": [write]', source)
        self.assertIn("if any(uid in protected for uid, _ in holders):", source)
        self.assertIn('"verification_status": "pending"', source)


if __name__ == "__main__":
    unittest.main()
