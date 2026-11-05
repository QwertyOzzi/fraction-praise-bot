"""Firestore contract for agent-only debit, moderated credit and independent logs."""
import copy
import json
import unittest

from agent_actions import (
    AgentOperationError, commit_agent_debit, create_add_request, get_add_request,
    decide_add_request, list_collection, create_announcement, finish_announcement,
    COLLECTION_ACTIONS, COLLECTION_REQUESTS, COLLECTION_NEWS, MEMORY_FIELD
)


class Response:
    def __init__(self, status, body=None):
        self.status = status
        self.body = copy.deepcopy(body or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return copy.deepcopy(self.body)


class FakeCloud:
    def __init__(self):
        self.rows = [
            {"nick": "Gomez_Addams", "id": "row1", "profileId": "a" * 32,
             "rank": "8", "qty": 6, "status": "На выдаче", "proof": {"text": "", "links": []}},
            {"nick": "Samuel_Norton", "id": "row2", "profileId": "b" * 32,
             "rank": "9", "qty": 4, "status": "На выдаче", "proof": {"text": "", "links": []}}
        ]
        self.docs = {
            "sheets/fractionrp": self._sheet_doc(),
            "sheets/praise": self._sheet_doc()
        }
        self.versions = {"sheets/fractionrp": 1, "sheets/praise": 1}
        self.commits = []

    def _sheet_doc(self):
        return {"rows": {"arrayValue": {"values": [
            {"mapValue": {"fields": copy.deepcopy(row)}} for row in self.rows
        ]}}, "unrelatedField": {"stringValue": "keep"}}

    def doc(self, path):
        if path not in self.docs:
            return Response(404)
        return Response(200, {
            "name": path, "updateTime": f"v{self.versions[path]}",
            "fields": self.docs[path]
        })

    def listing(self, collection):
        matches = [{"name": path, "fields": fields}
                   for path, fields in self.docs.items()
                   if path.startswith(collection + "/")]
        return Response(200, {"documents": matches})

    def commit(self, payload):
        writes = payload["writes"]
        for write in writes:
            update = write["update"]
            path = update["name"].split("/documents/", 1)[1]
            pre = write.get("currentDocument", {})
            if pre.get("exists") is False and path in self.docs:
                return Response(412)
            if "updateTime" in pre:
                if path not in self.docs or pre["updateTime"] != f"v{self.versions[path]}":
                    return Response(412)
        for write in writes:
            update = write["update"]
            path = update["name"].split("/documents/", 1)[1]
            values = copy.deepcopy(update["fields"])
            old = copy.deepcopy(self.docs.get(path, {}))
            for key in write.get("updateMask", {}).get("fieldPaths", values):
                if key in values:
                    old[key] = values[key]
            self.docs[path] = old
            self.versions[path] = self.versions.get(path, 0) + 1
        self.commits.append(copy.deepcopy(payload))
        return Response(200)


class Session:
    def __init__(self, cloud):
        self.cloud = cloud

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, url, **kwargs):
        path = url.split("/documents/", 1)[1]
        if path in (COLLECTION_ACTIONS, COLLECTION_REQUESTS, COLLECTION_NEWS):
            return self.cloud.listing(path)
        return self.cloud.doc(path)

    def post(self, url, **kwargs):
        assert url.endswith(":commit")
        return self.cloud.commit(kwargs["json"])


class Client:
    project_id = "test"

    async def get_token(self):
        return "fake-token"

    @staticmethod
    def _parse_firestore_rows(items):
        return [copy.deepcopy(row["mapValue"]["fields"]) for row in items]

    @staticmethod
    def _row_to_firestore(row):
        return {"mapValue": {"fields": copy.deepcopy(row)}}


class TestSeparateAgentTransactions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cloud = FakeCloud()
        self.client = Client()
        self.session_factory = lambda: Session(self.cloud)
        self.target = copy.deepcopy(self.cloud.rows[0])

    def sheet_rows(self, sheet="fractionrp"):
        values = self.cloud.docs["sheets/" + sheet]["rows"]["arrayValue"]["values"]
        return self.client._parse_firestore_rows(values)

    def all_logs(self):
        return [json.loads(doc["payload"]["stringValue"])
                for path, doc in self.cloud.docs.items()
                if path.startswith(COLLECTION_ACTIONS + "/")]

    async def test_agent_debit_and_separate_audit_are_one_commit(self):
        result, log = await commit_agent_debit(
            self.client, self.session_factory,
            op_id="agent-unique-001", actor_id=501,
            actor_nick="Other_Agent", sheet="fractionrp",
            target=self.target, amount=2, alias="гомер", raw="гомер -2 фрп"
        )
        self.assertEqual(result, "applied")
        self.assertEqual(log["action"], "debit")
        self.assertEqual(log["actor_id"], 501)
        self.assertEqual(log["before"], 6)
        self.assertEqual(log["after"], 4)
        self.assertEqual(self.sheet_rows()[0]["qty"], 4)
        self.assertEqual(len(self.all_logs()), 1)
        self.assertEqual(len(self.cloud.commits), 1)
        self.assertEqual(len(self.cloud.commits[0]["writes"]), 2)
        self.assertTrue(all(
            "admin_activity" not in write["update"]["name"]
            for write in self.cloud.commits[0]["writes"]
        ))
        self.assertEqual(self.cloud.docs["sheets/fractionrp"]["unrelatedField"]["stringValue"],
                         "keep")
        self.assertIn(MEMORY_FIELD, self.cloud.docs["sheets/fractionrp"])

    async def test_repeated_confirm_cannot_debit_twice(self):
        kwargs = dict(op_id="agent-unique-002", actor_id=501,
                      actor_nick="Other_Agent", sheet="fractionrp",
                      target=self.target, amount=2, alias="гомер", raw="гомер -2 фрп")
        first, _ = await commit_agent_debit(self.client, self.session_factory, **kwargs)
        second, _ = await commit_agent_debit(self.client, self.session_factory, **kwargs)
        self.assertEqual((first, second), ("applied", "already_applied"))
        self.assertEqual(self.sheet_rows()[0]["qty"], 4)
        self.assertEqual(len(self.cloud.commits), 1)

    async def test_negative_balance_blocks_all_writes(self):
        with self.assertRaises(AgentOperationError):
            await commit_agent_debit(
                self.client, self.session_factory,
                op_id="agent-insufficient", actor_id=501,
                actor_nick="Other_Agent", sheet="fractionrp",
                target=self.target, amount=7
            )
        self.assertEqual(self.sheet_rows()[0]["qty"], 6)
        self.assertEqual(self.all_logs(), [])

    async def test_changed_binding_blocks_all_writes(self):
        stale = dict(self.target)
        stale["profileId"] = "old-binding"
        with self.assertRaises(AgentOperationError):
            await commit_agent_debit(
                self.client, self.session_factory,
                op_id="agent-stale", actor_id=501, actor_nick="Other_Agent",
                sheet="praise", target=stale, amount=1
            )
        self.assertFalse(self.all_logs())

    async def test_pending_credit_requires_evidence_and_does_not_change_balance(self):
        with self.assertRaises(AgentOperationError):
            await create_add_request(
                self.client, self.session_factory,
                actor_id=501, actor_nick="Other_Agent", sheet="praise",
                target=self.target, amount=1
            )
        self.assertFalse(self.all_logs())
        req = await create_add_request(
            self.client, self.session_factory,
            actor_id=501, actor_nick="Other_Agent", sheet="praise",
            target=self.target, amount=1, reason="За службу",
            raw="Гомер +1 похвала", target_alias="Гомер"
        )
        self.assertEqual(req["status"], "pending")
        self.assertEqual(self.sheet_rows("praise")[0]["qty"], 6)
        self.assertEqual(len(self.all_logs()), 1)
        self.assertEqual(self.all_logs()[0]["action"], "credit_requested")
        fetched = await get_add_request(self.client, self.session_factory, req["id"])
        self.assertEqual(fetched, req)

    async def test_approval_atomically_updates_sheet_request_and_log(self):
        req = await create_add_request(
            self.client, self.session_factory,
            actor_id=501, actor_nick="Other_Agent", sheet="praise",
            target=self.target, amount=3, evidence_kind="photo",
            evidence_id="tg-file-id", raw="Гомер +3 похвалы", target_alias="Гомер"
        )
        self.assertEqual(self.sheet_rows("praise")[0]["qty"], 6)
        result, approved = await decide_add_request(
            self.client, self.session_factory,
            request_id=req["id"], reviewer_id=777, decision="approve"
        )
        self.assertEqual(result, "applied")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.sheet_rows("praise")[0]["qty"], 9)
        self.assertEqual(len(self.cloud.commits[-1]["writes"]), 3)
        logs = self.all_logs()
        self.assertEqual({item["action"] for item in logs},
                         {"credit_requested", "credit_approved"})
        repeated, _ = await decide_add_request(
            self.client, self.session_factory,
            request_id=req["id"], reviewer_id=888, decision="approve"
        )
        self.assertEqual(repeated, "already_decided")
        self.assertEqual(self.sheet_rows("praise")[0]["qty"], 9)

    async def test_rejection_leaves_sheet_unchanged_and_is_logged(self):
        req = await create_add_request(
            self.client, self.session_factory,
            actor_id=501, actor_nick="Other_Agent", sheet="fractionrp",
            target=self.target, amount=1, reason="Служба"
        )
        result, rejected = await decide_add_request(
            self.client, self.session_factory,
            request_id=req["id"], reviewer_id=777, decision="reject"
        )
        self.assertEqual(result, "applied")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.sheet_rows()[0]["qty"], 6)
        self.assertEqual({item["action"] for item in self.all_logs()},
                         {"credit_requested", "credit_rejected"})

    async def test_logs_are_listed_from_independent_collection(self):
        req = await create_add_request(
            self.client, self.session_factory,
            actor_id=501, actor_nick="Other_Agent", sheet="fractionrp",
            target=self.target, amount=1, reason="Служба"
        )
        pending = await list_collection(
            self.client, self.session_factory, COLLECTION_REQUESTS
        )
        logs = await list_collection(
            self.client, self.session_factory, COLLECTION_ACTIONS
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], req["id"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "credit_requested")

    async def test_manual_news_created_and_finalized_in_separate_collection(self):
        notice = await create_announcement(
            self.client, self.session_factory,
            actor_id=777, title="Обновление", message="Новая версия",
            category="update", recipients=5
        )
        self.assertEqual(notice["status"], "sending")
        self.assertEqual(len(self.all_logs()), 0)
        saved = await finish_announcement(
            self.client, self.session_factory, notice["id"],
            sent=4, failed=1
        )
        self.assertEqual(saved["status"], "partial")
        self.assertEqual((saved["sent"], saved["failed"]), (4, 1))
        docs = await list_collection(
            self.client, self.session_factory, COLLECTION_NEWS
        )
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], notice["id"])
        self.assertFalse(any(
            path.startswith(COLLECTION_ACTIONS + "/") for path in self.cloud.docs
        ))


if __name__ == "__main__":
    unittest.main()
