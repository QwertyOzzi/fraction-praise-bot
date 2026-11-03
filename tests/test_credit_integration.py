import ast
import asyncio
import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text(encoding="utf-8")
tree = ast.parse(SOURCE)
klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FirebaseClient")
safe = ast.Module(body=[klass], type_ignores=[])

class Response:
    def __init__(self, status, data=None):
        self.status, self.data = status, data or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return copy.deepcopy(self.data)

class Store:
    def __init__(self):
        self.qty = 2
        self.deleted = False
        self.version = 1
        self.ids = []
        self.conflict_once = False
        self.read_count = 0
    def get(self):
        self.read_count += 1
        return Response(200, {"updateTime": "v" + str(self.version), "fields": {
            "rows": {"arrayValue": {"values": ([] if self.deleted else [{"mapValue": {"fields": {
                "nick": {"stringValue": "Alice"}, "qty": {"integerValue": str(self.qty)}
            }}}])}},
            "appliedReportIds": {"arrayValue": {"values": [{"stringValue": v} for v in self.ids]}}
        }})
    def patch(self, params, json):
        if self.conflict_once:
            self.conflict_once = False
            self.version += 1
            self.qty += 1
            return Response(412)
        precondition = dict(params).get("currentDocument.updateTime") if isinstance(params, list) else params.get("currentDocument.updateTime")
        if precondition != "v" + str(self.version):
            return Response(412)
        values = json["fields"]["rows"]["arrayValue"]["values"]
        if values:
            row = values[0]["mapValue"]["fields"]
            self.qty = int(row["qty"]["integerValue"])
            self.deleted = False
        else:
            self.deleted = True
        ledger = json["fields"].get("appliedReportIds")
        if ledger is not None:
            self.ids = [x["stringValue"] for x in ledger["arrayValue"]["values"]]
        self.version += 1
        return Response(200)

class Session:
    def __init__(self, store): self.store = store
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, *args, **kwargs): return self.store.get()
    def patch(self, *args, **kwargs): return self.store.patch(kwargs["params"], kwargs["json"])

class TestCreditIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        ns = {"datetime": datetime, "timezone": timezone, "asyncio": asyncio,
              "List": List, "Dict": Dict, "Any": Any, "Optional": Optional,
              "logger": type("Log", (), {"info": lambda *args: None, "error": lambda *args: None})(),
              "ClientSession": lambda: Session(self.store)}
        exec(compile(safe, "<extracted FirebaseClient>", "exec"), ns)
        self.client = ns["FirebaseClient"]("test-project", "test-api-key", "test-email", "test-password")
        self.client.get_token = self.fake_token
    async def fake_token(self): return "test-token"
    async def test_single_credit(self):
        self.assertTrue(await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id1"))
        self.assertEqual((self.store.qty, self.store.ids), (3, ["id1"]))
    async def test_replay_does_not_double_credit(self):
        await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id1")
        await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id1")
        self.assertEqual(self.store.qty, 3)
    async def test_conflict_retries_latest_state(self):
        self.store.conflict_once = True
        await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id2")
        self.assertEqual(self.store.qty, 4)
        self.assertGreater(self.store.read_count, 1)
    async def test_multiple_praise_credits(self):
        await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id9", quantity=3)
        self.assertEqual(self.store.qty, 5)
        await self.client.credit_report("praise", "Alice", "8", "test", "https://example.org", "id9", quantity=3)
        self.assertEqual(self.store.qty, 5)

    async def test_debit_reduces_queue(self):
        self.assertEqual(await self.client.debit_one("fractionrp", "Alice"), 1)
        self.assertEqual(await self.client.debit_one("fractionrp", "Alice"), 0)
        self.assertEqual(await self.client.debit_one("fractionrp", "Alice"), 0)
        self.assertEqual(self.store.qty, 0)

    async def test_debit_conflict_retries(self):
        self.store.conflict_once = True
        self.assertEqual(await self.client.debit_one("fractionrp", "Alice"), 2)
        self.assertEqual(self.store.qty, 2)
        self.assertGreater(self.store.read_count, 1)

    async def test_remove_only_zero_balance_agent(self):
        self.assertFalse(await self.client.remove_zero_balance_agent("fractionrp", "Alice"))
        self.assertFalse(self.store.deleted)
        self.store.qty = 0
        self.assertTrue(await self.client.remove_zero_balance_agent("fractionrp", "Alice"))
        self.assertTrue(self.store.deleted)
        self.assertTrue(await self.client.remove_zero_balance_agent("fractionrp", "Alice"))

    async def test_remove_conflict_retries(self):
        self.store.qty = 0
        self.store.conflict_once = True
        # Simulated concurrent writer adds 1: no deletion may follow the conflict.
        self.assertFalse(await self.client.remove_zero_balance_agent("fractionrp", "Alice"))
        self.assertFalse(self.store.deleted)

    async def test_reject_unknown_sheet(self):
        with self.assertRaises(ValueError):
            await self.client.credit_report("invalid", "Alice", "8", "test", "https://example.org", "id3")

if __name__ == "__main__":
    unittest.main()
