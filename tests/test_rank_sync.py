"""Atomic rank changes must not lose FRP, praise, proof, site metadata or ACL."""
import ast
import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from rank_sync import RankSyncError, prepare_rank_sync, update_rank_across_firestore

ROOT = Path(__file__).resolve().parents[1]


def raw_row(nick, rank, amount, profile_id="profile-samuel", marker="frp"):
    return {"mapValue": {"fields": {
        "nick": {"stringValue": nick},
        "rank": {"stringValue": rank},
        "fullName": {"stringValue": f"{nick} ({rank})"},
        "profileId": {"stringValue": profile_id},
        "id": {"stringValue": f"row-{marker}"},
        "qty": {"integerValue": str(amount)},
        "status": {"stringValue": "На выдаче"},
        "proof": {"mapValue": {"fields": {
            "text": {"stringValue": "Ручное начисление"},
            "links": {"arrayValue": {"values": [
                {"mapValue": {"fields": {
                    "text": {"stringValue": "доказательство"},
                    "url": {"stringValue": "https://example.org/proof"}
                }}}
            ]}}
        }}},
        "siteOnlyExtra": {"mapValue": {"fields": {
            "custom": {"integerValue": "777"}
        }}}
    }}}


def state():
    return {
        "users": {
            "123": {
                "nick": "Samuel_Norton", "rank": "8",
                "profileId": "profile-samuel",
                "rank_history": [{"from": "X", "to": "8", "at": "2026-11-09"}],
                "department": "FBI"
            }
        },
        "admins": [99],
        "admin_roles": {"99": "leader"},
        "admin_acl_signature": "trusted-signature",
        "permission_overrides": {"99": {"agents.rank": True}},
        "permission_overrides_signature": "untouched",
        "requests": {"req": {"status": "pending"}},
    }


def signer(admins, roles):
    if admins == [99] and roles == {"99": "leader"}:
        return "trusted-signature"
    return "wrong"


class TestRankPreparation(unittest.TestCase):
    def setUp(self):
        self.state = state()
        self.rows = {
            "fractionrp": [raw_row("Samuel_Norton", "X", 22, marker="frp")],
            "praise": [raw_row("Samuel_Norton", "X", 0, marker="praise")],
        }

    def prepare(self, new_rank="8", expected="8"):
        return prepare_rank_sync(
            self.state, self.rows, uid="123", nick="Samuel_Norton",
            expected_rank=expected, new_rank=new_rank,
            signing_function=signer, actor_id=99, timestamp="2026-11-09T15:00:00Z"
        )

    def test_repairs_existing_bot_8_site_10_without_extra_history_or_data_loss(self):
        before_state = copy.deepcopy(self.state)
        before_rows = copy.deepcopy(self.rows)
        result, sheets, updated = self.prepare()
        self.assertEqual(updated["rank"], "8")
        self.assertEqual(updated["rank_history"], before_state["users"]["123"]["rank_history"])
        self.assertEqual(result["admins"], before_state["admins"])
        self.assertEqual(result["permission_overrides"], before_state["permission_overrides"])
        self.assertEqual(result["requests"], before_state["requests"])
        self.assertEqual(self.rows, before_rows)
        for sheet in ("fractionrp", "praise"):
            old_fields = before_rows[sheet][0]["mapValue"]["fields"]
            new_fields = sheets[sheet][0]["mapValue"]["fields"]
            for key in old_fields:
                if key not in ("rank", "fullName"):
                    self.assertEqual(new_fields[key], old_fields[key], (sheet, key))
            self.assertEqual(new_fields["rank"]["stringValue"], "8")
            self.assertEqual(new_fields["fullName"]["stringValue"], "Samuel_Norton (8)")
        self.assertEqual(sheets["fractionrp"][0]["mapValue"]["fields"]["qty"]["integerValue"], "22")

    def test_real_rank_change_updates_both_sheets_and_history_once(self):
        _, sheets, profile = self.prepare(new_rank="7")
        self.assertEqual(profile["rank"], "7")
        self.assertEqual(len(profile["rank_history"]), 2)
        self.assertEqual(profile["rank_history"][-1]["from"], "8")
        self.assertEqual(profile["rank_history"][-1]["to"], "7")
        self.assertTrue(all(
            items[0]["mapValue"]["fields"]["rank"]["stringValue"] == "7"
            for items in sheets.values()
        ))

    def test_missing_sheet_row_is_not_resurrected(self):
        self.rows["praise"] = []
        _, sheets, _ = self.prepare()
        self.assertIn("fractionrp", sheets)
        self.assertNotIn("praise", sheets)

    def test_conflicting_binding_or_duplicate_row_rejects_all(self):
        self.rows["praise"][0]["mapValue"]["fields"]["profileId"] = {
            "stringValue": "somebody-else"
        }
        with self.assertRaisesRegex(RankSyncError, "Другая привязка"):
            self.prepare()
        self.rows["praise"][0]["mapValue"]["fields"]["profileId"] = {
            "stringValue": "profile-samuel"
        }
        self.rows["fractionrp"].append(copy.deepcopy(self.rows["fractionrp"][0]))
        with self.assertRaisesRegex(RankSyncError, "Дубли"):
            self.prepare()

    def test_stale_rank_or_untrusted_admin_acl_fails_closed(self):
        with self.assertRaisesRegex(RankSyncError, "Ранг уже изменился"):
            self.prepare(expected="X")
        self.state["admin_acl_signature"] = "untrusted"
        with self.assertRaisesRegex(RankSyncError, "Подпись"):
            self.prepare()

    def test_renamed_or_unverified_profile_fails(self):
        self.state["users"]["123"]["nick"] = "Different_Nick"
        with self.assertRaisesRegex(RankSyncError, "Ник изменился"):
            self.prepare()
        self.state["users"]["123"]["nick"] = "Samuel_Norton"
        self.state["users"]["123"]["verification_status"] = "disconnected"
        with self.assertRaisesRegex(RankSyncError, "Регистрация"):
            self.prepare()


class FakeResponse:
    def __init__(self, status, body=None):
        self.status = status
        self.body = copy.deepcopy(body or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return copy.deepcopy(self.body)


class FakeFirestore:
    def __init__(self, posts):
        self.posts = list(posts)
        self.writes = []
        self.revisions = {"config/bot_state": "v1", "sheets/fractionrp": "v2",
                          "sheets/praise": "v3"}
        self.raw_state = state()
        self.rows = {"fractionrp": [raw_row("Samuel_Norton", "X", 22)],
                     "praise": [raw_row("Samuel_Norton", "X", 0, marker="praise")]}
        self.commits = 0

    def read(self, path):
        if path == "config/bot_state":
            fields = {"data": {"stringValue": json.dumps(self.raw_state)}}
        else:
            fields = {"rows": {"arrayValue": {"values": self.rows[path.split("/")[-1]]}}}
        return FakeResponse(200, {"updateTime": self.revisions[path],
                                  "fields": fields})

    def commit(self, payload):
        self.writes.append(copy.deepcopy(payload["writes"]))
        result = self.posts.pop(0)
        if result == 200:
            for write in payload["writes"]:
                path = write["update"]["name"].split("/documents/")[-1]
                assert write["currentDocument"]["updateTime"] == self.revisions[path]
                assert write["updateMask"]["fieldPaths"] == (
                    ["data", "updatedAt"] if path == "config/bot_state"
                    else ["rows", "updatedAt"]
                )
                fields = write["update"]["fields"]
                if path == "config/bot_state":
                    self.raw_state = json.loads(fields["data"]["stringValue"])
                else:
                    self.rows[path.split("/")[-1]] = copy.deepcopy(
                        fields["rows"]["arrayValue"]["values"]
                    )
            self.commits += 1
        return FakeResponse(result)


class FakeSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def get(self, url, headers=None):
        return self.store.read(url.split("/documents/")[-1])

    def post(self, url, headers=None, json=None):
        self.last_body = json
        return self.store.commit(json)


class Client:
    async def get_token(self):
        return "fake-token"


class TestAtomicCommit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = FakeFirestore([200])
        self.lock = asyncio.Lock()

    async def perform(self, *, new_rank="8"):
        return await update_rank_across_firestore(
            Client(), "test-project", "123", "Samuel_Norton", "8", new_rank,
            99, signer, self.lock, lambda: FakeSession(self.store)
        )

    async def test_three_documents_commit_together(self):
        result = await self.perform()
        self.assertEqual(result["rank"], "8")
        self.assertEqual(self.store.commits, 1)
        self.assertEqual(len(self.store.writes[0]), 3)
        self.assertEqual(self.store.raw_state["users"]["123"]["rank"], "8")
        for sheet in ("fractionrp", "praise"):
            self.assertEqual(
                self.store.rows[sheet][0]["mapValue"]["fields"]["rank"]["stringValue"],
                "8"
            )
        self.assertEqual(
            self.store.rows["fractionrp"][0]["mapValue"]["fields"]["qty"]["integerValue"],
            "22"
        )

    async def test_rejected_transaction_has_zero_partial_changes(self):
        self.store = FakeFirestore([403])
        with self.assertRaises(RankSyncError):
            await self.perform(new_rank="7")
        self.assertEqual(self.store.commits, 0)
        self.assertEqual(self.store.raw_state["users"]["123"]["rank"], "8")
        self.assertEqual(
            self.store.rows["fractionrp"][0]["mapValue"]["fields"]["rank"]["stringValue"],
            "X"
        )

    async def test_revision_conflict_retries_atomically(self):
        self.store = FakeFirestore([412, 200])
        # Avoid sleeping in tests; exercising the real retry loop.
        with mock.patch("rank_sync.asyncio.sleep", new_callable=mock.AsyncMock):
            result = await self.perform(new_rank="7")
        self.assertEqual(result["rank"], "7")
        self.assertEqual(self.store.commits, 1)
        self.assertEqual(len(self.store.writes), 2)


class TestTelegramWiring(unittest.TestCase):
    def test_panel_uses_atomic_helper_not_profile_only_patch(self):
        source = (ROOT / "bot.py").read_text("utf-8")
        tree = ast.parse(source)
        target = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "agent_set_rank")
        handler = ast.get_source_segment(source, target)
        self.assertIn("await update_rank_across_firestore(", handler)
        self.assertNotIn("await sync_state_to_cloud(", handler)
        self.assertIn("if old_rank != new_rank:", handler)
        self.assertIn('callback.message.chat.type != "private"', handler)
        self.assertIn("users_db[agent[\"uid\"]] = updated", handler)


if __name__ == "__main__":
    unittest.main()
