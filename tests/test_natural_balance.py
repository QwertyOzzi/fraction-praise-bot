"""Atomic Firestore group operations: no partial balances, no duplicate debits."""
import copy
import unittest

from natural_balance import apply_batch, BalanceConflict
from natural_aliases import MEMORY_FIELD, decode_alias_book


class FakeResponse:
    def __init__(self, code, body=None):
        self.status = code
        self.body = copy.deepcopy(body or {})
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return copy.deepcopy(self.body)


class FirestoreFake:
    def __init__(self):
        self.rows = [
            {"nick": "Samuel_Norton", "id": "1", "profileId": "a",
             "rank": "8", "qty": 9, "status": "На выдаче"},
            {"nick": "Gomez_Addams", "id": "2", "profileId": "b",
             "rank": "7", "qty": 7, "status": "На выдаче"},
        ]
        self.ids = []
        self.aliases = ""
        self.agent_actions = []
        self.rev = 1
        self.writes = 0

    def get(self):
        return FakeResponse(200, {
            "updateTime": f"v{self.rev}",
            "fields": {
                "rows": {"arrayValue": {"values": [
                    {"mapValue": {"fields": row}} for row in self.rows
                ]}},
                "appliedNaturalIds": {"arrayValue": {"values": [
                    {"stringValue": value} for value in self.ids
                ]}},
                MEMORY_FIELD: {"stringValue": self.aliases},
                "appliedAgentActions": {"arrayValue": {
                    "values": copy.deepcopy(self.agent_actions)
                }}
            }
        })

    def patch(self, payload, params):
        if ("currentDocument.updateTime", f"v{self.rev}") not in params:
            return FakeResponse(412)
        fields = payload["fields"]
        self.rows = [
            copy.deepcopy(item["mapValue"]["fields"])
            for item in fields["rows"]["arrayValue"]["values"]
        ]
        self.ids = [
            item["stringValue"]
            for item in fields["appliedNaturalIds"]["arrayValue"]["values"]
        ]
        if MEMORY_FIELD in fields:
            self.aliases = fields[MEMORY_FIELD]["stringValue"]
            assert ("updateMask.fieldPaths", MEMORY_FIELD) in params
        if "appliedAgentActions" in fields:
            assert ("updateMask.fieldPaths", "appliedAgentActions") in params
            self.agent_actions = copy.deepcopy(
                fields["appliedAgentActions"]["arrayValue"]["values"]
            )
        self.rev += 1
        self.writes += 1
        return FakeResponse(200)


class Session:
    def __init__(self, store):
        self.store = store
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def get(self, url, **kwargs):
        return self.store.get()
    def patch(self, url, **kwargs):
        return self.store.patch(kwargs["json"], kwargs["params"])


class Client:
    project_id = "test-project"
    async def get_token(self):
        return "fake-token"
    @staticmethod
    def _parse_firestore_rows(items):
        return [copy.deepcopy(item["mapValue"]["fields"]) for item in items]
    @staticmethod
    def _row_to_firestore(row):
        return {"mapValue": {"fields": copy.deepcopy(row)}}


class TestAtomicBalanceChanges(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FirestoreFake()
        self.client = Client()
        self.session_factory = lambda: Session(self.store)

    def targets(self):
        return [copy.deepcopy(row) for row in self.store.rows]

    async def run_batch(self, action, amount, transaction="ai-test-1", targets=None):
        return await apply_batch(
            self.client, "fractionrp", self.targets() if targets is None else targets,
            action, amount, transaction, self.session_factory
        )

    async def test_multiple_agents_debited_in_one_commit(self):
        result = await self.run_batch("debit", 3)
        self.assertEqual(result, "applied")
        self.assertEqual([r["qty"] for r in self.store.rows], [6, 4])
        self.assertEqual(self.store.writes, 1)

    async def test_credit_is_one_atomic_commit(self):
        result = await self.run_batch("credit", 5)
        self.assertEqual(result, "applied")
        self.assertEqual([r["qty"] for r in self.store.rows], [14, 12])
        self.assertEqual(self.store.writes, 1)

    async def test_no_partial_debit_if_one_agent_lacks_balance(self):
        originals = self.targets()
        with self.assertRaises(BalanceConflict):
            await self.run_batch("debit", 8)
        self.assertEqual(self.store.rows, originals)
        self.assertEqual(self.store.writes, 0)

    async def test_modified_balance_since_preview_cancels_all(self):
        originals = self.targets()
        self.store.rows[0]["qty"] += 1
        with self.assertRaises(BalanceConflict):
            await self.run_batch("debit", 2, targets=originals)
        self.assertEqual([r["qty"] for r in self.store.rows], [10, 7])
        self.assertEqual(self.store.writes, 0)

    async def test_changed_profile_binding_cancels(self):
        originals = self.targets()
        self.store.rows[1]["profileId"] = "not-original"
        with self.assertRaises(BalanceConflict):
            await self.run_batch("credit", 1, targets=originals)
        self.assertEqual(self.store.writes, 0)

    async def test_idempotency_survives_new_requests(self):
        prepared = self.targets()
        first = await self.run_batch("debit", 2, targets=prepared)
        repeat = await self.run_batch("debit", 2, targets=prepared)
        self.assertEqual(first, "applied")
        self.assertEqual(repeat, "already_applied")
        self.assertEqual(self.store.writes, 1)
        self.assertEqual([row["qty"] for row in self.store.rows], [7, 5])

    async def test_learning_occurs_only_in_successful_firestore_commit(self):
        prepared = self.targets()
        result = await apply_batch(
            self.client, "fractionrp", prepared, "credit", 1,
            "ai-memory-001", self.session_factory,
            aliases=["норто", "гоммез"],
            original_text="норто,гоммез фрп +1"
        )
        self.assertEqual(result, "applied")
        learned = decode_alias_book(self.store.aliases, sheet="fractionrp")
        self.assertEqual(learned["norto"]["nick"], "Samuel_Norton")
        self.assertEqual(learned["gommez"]["nick"], "Gomez_Addams")
        self.assertEqual(self.store.writes, 1)
        self.assertEqual([row["qty"] for row in self.store.rows], [10, 8])

    async def test_failed_transaction_does_not_learn_any_alias(self):
        prepared = self.targets()
        with self.assertRaises(BalanceConflict):
            await apply_batch(
                self.client, "fractionrp", prepared, "debit", 8,
                "ai-memory-002", self.session_factory,
                aliases=["норто", "гоммез"],
                original_text="норто,гоммез фрп -8"
            )
        self.assertEqual(self.store.writes, 0)
        self.assertFalse(self.store.aliases)

    async def test_duplicate_callback_never_relearns_or_debits_again(self):
        prepared = self.targets()
        for _ in range(2):
            result = await apply_batch(
                self.client, "fractionrp", prepared, "debit", 1,
                "ai-memory-003", self.session_factory,
                aliases=["норто", "гоммез"],
                original_text="норто,гоммез фрп -1"
            )
        self.assertEqual(result, "already_applied")
        self.assertEqual(self.store.writes, 1)
        self.assertEqual([row["qty"] for row in self.store.rows], [8, 6])
        self.assertEqual(len(decode_alias_book(
            self.store.aliases, sheet="fractionrp"
        )), 2)

    async def test_invented_alias_is_not_learned(self):
        prepared = self.targets()
        await apply_batch(
            self.client, "fractionrp", prepared, "credit", 1,
            "ai-memory-004", self.session_factory,
            aliases=["придумано", "гоммез"],
            original_text="Samuel_Norton,гоммез +1 фрп"
        )
        learned = decode_alias_book(self.store.aliases, sheet="fractionrp")
        self.assertNotIn("pridumano", learned)
        self.assertIn("gommez", learned)

    async def test_duplicate_targets_rejected(self):
        prepared = self.targets()
        with self.assertRaises(ValueError):
            await self.run_batch("debit", 2, targets=[prepared[0], prepared[0]])
        self.assertEqual(self.store.writes, 0)

    async def test_agent_debit_writes_atomic_separate_journal(self):
        targets = self.targets()[:1]
        result = await apply_batch(
            self.client, "fractionrp", targets, "debit", 2,
            "ai-agent-audit-01", self.session_factory,
            agent_audit={
                "actor_id": 42, "actor_nick": "Gomez_Addams",
                "reason": "Самостоятельное списание агентом"
            }
        )
        self.assertEqual(result, "applied")
        self.assertEqual(self.store.rows[0]["qty"], 7)
        self.assertEqual(len(self.store.agent_actions), 1)
        import json
        note = json.loads(self.store.agent_actions[0]["stringValue"])
        self.assertEqual(note["actor_id"], 42)
        self.assertEqual(note["action"], "debit")
        self.assertEqual(note["targets"], ["Samuel_Norton"])
        self.assertEqual(self.store.writes, 1)

    async def test_failed_agent_debit_has_no_audit_entry(self):
        with self.assertRaises(BalanceConflict):
            await apply_batch(
                self.client, "fractionrp", self.targets(), "debit", 10,
                "ai-agent-audit-02", self.session_factory,
                agent_audit={
                    "actor_id": 42, "actor_nick": "Gomez_Addams"
                }
            )
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.store.agent_actions, [])

    async def test_repeat_agent_callback_keeps_single_audit(self):
        prepared = self.targets()[:1]
        for _ in range(2):
            await apply_batch(
                self.client, "fractionrp", prepared, "debit", 1,
                "ai-agent-audit-03", self.session_factory,
                agent_audit={
                    "actor_id": 42, "actor_nick": "Gomez_Addams"
                }
            )
        self.assertEqual(self.store.rows[0]["qty"], 8)
        self.assertEqual(len(self.store.agent_actions), 1)
        self.assertEqual(self.store.writes, 1)

    async def test_invalid_amount_rejected(self):
        with self.assertRaises(ValueError):
            await self.run_batch("debit", 21)
        self.assertEqual(self.store.writes, 0)


if __name__ == "__main__":
    unittest.main()
