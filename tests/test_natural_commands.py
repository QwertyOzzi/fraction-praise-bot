"""Gemini parser tests. No API key, aiohttp, or Firebase credentials required."""
import asyncio
import json
import unittest

from natural_commands import (
    MODEL, ParseError, interpret, looks_like_operation, resolve_names,
    simple_parse, validate_plan
)


class TestFastParser(unittest.IsolatedAsyncioTestCase):
    async def test_example_command_needs_zero_api_tokens(self):
        called = []
        async def forbidden_request(*args):
            called.append(args)
            raise AssertionError("Gemini must not be needed")
        command, origin = await interpret(
            "норто,гоммез,кударов -3 фрп", "", forbidden_request
        )
        self.assertEqual(origin, "local")
        self.assertEqual(called, [])
        self.assertEqual(command, {
            "action": "debit", "sheet": "fractionrp",
            "amount": 3, "names": ["норто", "гоммез", "кударов"]
        })

    async def test_screenshot_order_works_without_gemini_or_api_key(self):
        calls = []
        async def no_api(*args):
            calls.append(args)
            raise AssertionError("This syntax must not call Gemini")
        result, source = await interpret(
            "нортон,гоммез,кударов фрп +1", "", no_api
        )
        self.assertEqual(source, "local")
        self.assertEqual(calls, [])
        self.assertEqual(result, {
            "action": "credit", "sheet": "fractionrp", "amount": 1,
            "names": ["нортон", "гоммез", "кударов"]
        })

    def test_reverse_order_with_minus_and_praise(self):
        result = simple_parse("Samuel_Norton; Gomez_Addams похвалы −2")
        self.assertEqual(result["action"], "debit")
        self.assertEqual(result["sheet"], "praise")
        self.assertEqual(result["amount"], 2)
        self.assertEqual(result["names"], ["Samuel_Norton", "Gomez_Addams"])

    def test_plain_message_detection_both_orders(self):
        self.assertTrue(looks_like_operation("норто,гоммез фрп +1"))
        self.assertTrue(looks_like_operation("норто,гоммез +1 фрп"))
        self.assertTrue(looks_like_operation("норто,гоммез фрп −3"))
        self.assertFalse(looks_like_operation("Добрый день!"))

    async def test_repeated_spoken_command_needs_no_gemini(self):
        calls = []
        async def unused_api(*args):
            calls.append(args)
            raise AssertionError("Common spoken commands should parse locally")
        plan, origin = await interpret("дай гоммезу 2 похвалы", "", unused_api)
        self.assertEqual(origin, "local")
        self.assertEqual(calls, [])
        self.assertEqual(plan["action"], "credit")
        self.assertEqual(plan["sheet"], "praise")
        self.assertEqual(plan["amount"], 2)
        self.assertEqual(plan["names"], ["гоммезу"])

    async def test_spoken_plural_and_number_word_parse_locally(self):
        async def unused_api(*args):
            raise AssertionError("No Gemini call expected")
        plan, origin = await interpret(
            "сними у нортона и гоммеза три фрп", "", unused_api
        )
        self.assertEqual(origin, "local")
        self.assertEqual(plan, {
            "action": "debit", "sheet": "fractionrp", "amount": 3,
            "names": ["нортона", "гоммеза"]
        })

    def test_spoken_command_does_not_change_rank(self):
        self.assertFalse(looks_like_operation("повысь нортона на ранг 8"))

    def test_praise_and_plus(self):
        output = simple_parse("Gomez_Addams +5 похвалы")
        self.assertEqual(output["action"], "credit")
        self.assertEqual(output["sheet"], "praise")
        self.assertEqual(output["amount"], 5)

    def test_huge_amount_rejected(self):
        with self.assertRaises(ParseError):
            simple_parse("Gomez_Addams -99 фрп")

    def test_no_injection_into_casual_messages(self):
        self.assertFalse(looks_like_operation("как дела?"))
        self.assertFalse(looks_like_operation("/start"))
        self.assertTrue(looks_like_operation("норто,гоммез -3 фрп"))


class TestGeminiFallback(unittest.IsolatedAsyncioTestCase):
    async def test_mock_gemini38_structured_command(self):
        called = []
        async def api(url, headers, payload):
            called.append((url, headers, payload))
            return {"candidates": [{"content": {"parts": [
                {"text": json.dumps({
                    "action": "debit", "sheet": "fractionrp", "amount": 3,
                    "names": ["нортона", "гоммеза"]
                })}
            ]}}]}
        command, origin = await interpret(
            "сними у нортона и гоммеза три фрп", "mock-key", api
        )
        self.assertEqual(origin, "gemini")
        self.assertEqual(command["amount"], 3)
        self.assertEqual(len(called), 1)
        self.assertIn(MODEL, called[0][0])
        self.assertEqual(
            called[0][2]["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "low"
        )
        self.assertGreaterEqual(called[0][2]["generationConfig"]["maxOutputTokens"], 1000)
        self.assertEqual(called[0][1]["x-goog-api-key"], "mock-key")
        self.assertNotIn("Telegram ID", called[0][2]["contents"][0]["parts"][0]["text"])

    async def test_explicit_sign_cannot_be_reversed(self):
        async def bad_response(*args):
            return {"candidates": [{"content": {"parts": [{
                "text": json.dumps({"action": "credit", "sheet": "fractionrp",
                                    "amount": 3, "names": ["норто"]})
            }]}}]}
        with self.assertRaises(ParseError):
            await interpret("сними у норто -3 фрп", "mock-key", bad_response)

    def test_wrong_coin_type_rejected(self):
        with self.assertRaises(ParseError):
            validate_plan(
                {"action": "debit", "sheet": "praise",
                 "amount": 3, "names": ["норто"]},
                original="сними у норто 3 фрп"
            )

    async def test_no_key_fails_cleanly(self):
        async def no_request(*args):
            raise AssertionError
        with self.assertRaises(ParseError):
            await interpret("сними у Нортона три фрп", "", no_request)


class TestNickResolver(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": "a", "nick": "Samuel_Norton", "rank": "8",
             "profileId": "bind1", "qty": 7},
            {"id": "b", "nick": "Gomez_Addams", "rank": "8",
             "profileId": "bind2", "qty": 4},
        ]

    def test_russian_declensions_are_found_without_api(self):
        matched, errors = resolve_names(["нортона", "гоммезу"], self.rows)
        self.assertEqual(errors, [])
        self.assertEqual([row["nick"] for row in matched],
                         ["Samuel_Norton", "Gomez_Addams"])

    def test_russian_prefix_and_typo(self):
        matched, errors = resolve_names(["норто", "гоммез"], self.rows)
        self.assertEqual(errors, [])
        self.assertEqual([r["nick"] for r in matched],
                         ["Samuel_Norton", "Gomez_Addams"])

    def test_unknown_agent_blocks_entire_batch(self):
        matched, errors = resolve_names(
            ["норто", "гоммез", "кударов"], self.rows
        )
        self.assertEqual(matched, [])
        self.assertTrue(any("кударов" in e for e in errors))

    def test_ambiguous_prefix_must_be_clarified(self):
        rows = self.rows + [{"nick": "Other_Norton", "qty": 1, "id": "c"}]
        matched, errors = resolve_names(["норто"], rows)
        self.assertEqual(matched, [])
        self.assertTrue(errors)

    def test_same_target_twice_is_not_two_debits(self):
        matched, errors = resolve_names(["норто", "Samuel_Norton"], self.rows)
        self.assertFalse(matched)
        self.assertTrue(errors)

    def test_duplicate_nick_in_sheet_is_unsafe(self):
        rows = self.rows + [{"nick": "Gomez_Addams", "id": "copy", "qty": 5}]
        matched, errors = resolve_names(["гоммез"], rows)
        self.assertFalse(matched)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
