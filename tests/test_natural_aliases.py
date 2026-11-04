"""Confirmed agent aliases must never silently bind to a different account."""
import asyncio
import json
import unittest

from natural_aliases import (
    MEMORY_FIELD, decode_alias_book, encode_alias_book, learn_confirmed_aliases,
    load_alias_memory, merge_alias_books, resolve_with_memory
)

ROWS = [
    {"nick": "Samuel_Norton", "profileId": "a" * 32, "id": "row1",
     "qty": 9, "rank": "8"},
    {"nick": "Gomez_Addams", "profileId": "b" * 32, "id": "row2",
     "qty": 7, "rank": "7"},
]


def learn(aliases=("норто", "гоммез"), rows=None, *, sheet="fractionrp"):
    source = rows if rows is not None else ROWS
    return learn_confirmed_aliases({}, list(aliases), source, sheet,
                                   now="2026-11-04T21:00:00+00:00")


class TestAliases(unittest.TestCase):
    def test_confirmed_short_names_survive_json_reloading(self):
        saved = learn()
        self.assertEqual(set(saved), {"norto", "gommez"})
        data = encode_alias_book(saved)
        restored = decode_alias_book(data, sheet="fractionrp")
        matches, errors = resolve_with_memory(
            ["норто", "гоммез"], ROWS, restored, "fractionrp"
        )
        self.assertEqual(errors, [])
        self.assertEqual([r["nick"] for r in matches],
                         ["Samuel_Norton", "Gomez_Addams"])

    def test_other_sheet_can_reuse_verified_profile_id(self):
        saved = learn()
        praise_rows = [
            {**row, "id": "praise-" + row["id"]} for row in ROWS
        ]
        matches, errors = resolve_with_memory(
            ["норто"], praise_rows, saved, "praise"
        )
        self.assertEqual(errors, [])
        self.assertEqual(matches[0]["nick"], "Samuel_Norton")

    def test_renamed_or_reassigned_agent_must_not_use_old_alias(self):
        saved = learn()
        replaced = [{**ROWS[0], "profileId": "different"}, ROWS[1]]
        matches, errors = resolve_with_memory(
            ["норто"], replaced, saved, "fractionrp"
        )
        self.assertEqual(matches, [])
        self.assertTrue(any("устарела" in text for text in errors))

    def test_reused_full_nick_beats_old_shortcut(self):
        saved = learn()
        reused = [{**ROWS[0], "nick": "Other_Agent"},
                  {**ROWS[1], "nick": "Норто"}]
        matches, errors = resolve_with_memory(["Норто"], reused, saved, "fractionrp")
        self.assertEqual(errors, [])
        self.assertEqual(matches[0]["nick"], "Норто")

    def test_same_alias_confirmed_for_other_agent_blocks_memory(self):
        saved = learn(aliases=["норто"], rows=[ROWS[0]])
        updated = learn_confirmed_aliases(saved, ["норто"], [ROWS[1]], "fractionrp")
        self.assertTrue(updated["norto"]["blocked"])
        matches, errors = resolve_with_memory(
            ["норто"], ROWS, updated, "fractionrp"
        )
        self.assertEqual(matches, [])
        self.assertTrue(any("неоднозначно" in s for s in errors))

    def test_cross_sheet_conflicting_memory_blocks_shortcut(self):
        first = learn(aliases=["норто"], rows=[ROWS[0]])
        second = learn(aliases=["норто"], rows=[ROWS[1]], sheet="praise")
        combined = merge_alias_books(first, second)
        self.assertTrue(combined["norto"]["blocked"])
        matches, errors = resolve_with_memory(
            ["норто"], ROWS, combined, "fractionrp"
        )
        self.assertEqual(matches, [])
        self.assertTrue(errors)

    def test_same_verified_agent_across_sheets_is_not_conflict(self):
        first = learn(aliases=["норто"], rows=[ROWS[0]])
        second = learn(aliases=["норто"], rows=[ROWS[0]], sheet="praise")
        self.assertFalse(merge_alias_books(first, second)["norto"].get("blocked"))

    def test_duplicate_names_in_one_batch_fail_entire_transaction(self):
        saved = learn()
        matches, errors = resolve_with_memory(
            ["норто", "Samuel_Norton"], ROWS, saved, "fractionrp"
        )
        self.assertEqual(matches, [])
        self.assertTrue(errors)

    def test_duplicate_rows_ignored_only_when_not_ambiguous(self):
        saved = learn()
        cloned = [*ROWS, {**ROWS[0], "id": "duplicate"}]
        matches, errors = resolve_with_memory(
            ["норто"], cloned, saved, "fractionrp"
        )
        self.assertEqual(matches, [])
        self.assertTrue(errors)

    def test_empty_or_invalid_profiles_are_not_learned(self):
        assert learn_confirmed_aliases(
            {}, ["норто"], [{"nick": "Samuel_Norton",
                              "profileId": "", "id": ""}],
            "fractionrp"
        ) == {}
        self.assertEqual(decode_alias_book("not-json", sheet="praise"), {})

    def test_document_has_size_limit(self):
        doc = json.dumps({"version": 1, "aliases": {"test": {"blocked": True}},
                          "padding": "x" * 100001})
        self.assertEqual(decode_alias_book(doc, sheet="fractionrp"), {})


class Response:
    def __init__(self, data):
        self.status = 200
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self):
        return self._data


class Session:
    def __init__(self, documents):
        self.docs = documents
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def get(self, url, headers):
        self.requests.append(url)
        sheet = url.rsplit("/", 1)[-1]
        return Response({"fields": {MEMORY_FIELD: {
            "stringValue": encode_alias_book(self.docs.get(sheet, {}))
        }}})


class TestCloudAliasMemory(unittest.IsolatedAsyncioTestCase):
    async def test_firestore_reads_both_sheets(self):
        docs = {
            "fractionrp": learn(["норто"], [ROWS[0]]),
            "praise": learn(["гоммез"], [ROWS[1]], sheet="praise")
        }
        session = Session(docs)
        class Client:
            project_id = "test-project"
            async def get_token(self):
                return "fake-token"

        book = await load_alias_memory(Client(), lambda: session)
        self.assertEqual(set(book), {"norto", "gommez"})
        self.assertEqual(len(session.requests), 2)
        self.assertFalse(any("fake-token" in url for url in session.requests))


if __name__ == "__main__":
    unittest.main()
