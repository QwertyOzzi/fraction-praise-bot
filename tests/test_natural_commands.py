"""Gemini parser tests. No API key, aiohttp, or Firebase credentials required."""
import asyncio
import json
import unittest

from natural_commands import (
    MODEL, ParseError, interpret, looks_like_operation, resolve_names,
    simple_parse, split_operation_reason, natural_phrase_parse, validate_plan
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

    async def test_inline_debit_reason_works_offline(self):
        async def must_not_call_gemini(*args):
            raise AssertionError("Screenshot input should not need Gemini")
        command_text, reason, has_reason = split_operation_reason(
            "кудак -1 фрп выдал в игре"
        )
        self.assertTrue(has_reason)
        self.assertEqual(reason, "выдал в игре")
        plan, origin = await interpret(command_text, "", must_not_call_gemini)
        self.assertEqual(origin, "local")
        self.assertEqual(plan["action"], "debit")
        self.assertEqual(plan["amount"], 1)
        self.assertEqual(plan["names"], ["кудак"])

    def test_explicit_and_reversed_reason_formats(self):
        for raw in (
            "кудак -1 фрп | выдал в игре",
            "кудак фрп -1 выдал в игре",
            "сними у кудака один фрп выдал в игре",
        ):
            command, reason, has_reason = split_operation_reason(raw)
            self.assertTrue(has_reason, raw)
            self.assertEqual(reason, "выдал в игре")
            self.assertIsNotNone(simple_parse(command) or natural_phrase_parse(command))

    def test_ambiguous_command_does_not_extract_reason(self):
        command, reason, has_reason = split_operation_reason(
            "кудак -1 фрп и гоммез -2 фрп выдал в игре"
        )
        self.assertFalse(has_reason)
        self.assertEqual(reason, "")
        self.assertIn("гоммез", command)

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

    async def test_one_praise_accusative_phrase_without_gemini(self):
        calls = []
        async def mock_api(*args):
            calls.append(args)
            raise AssertionError("Похвалу нужно разбирать локально")
        result, origin = await interpret(
            "дай такадо одну похвалу", "", mock_api
        )
        self.assertEqual(origin, "local")
        self.assertEqual(calls, [])
        self.assertEqual(result, {
            "action": "credit", "sheet": "praise", "amount": 1,
            "names": ["такадо"]
        })

    async def test_praise_debit_with_accusative_without_gemini(self):
        async def mock_api(*args):
            raise AssertionError("Похвалы должны разбираться локально")
        result, origin = await interpret(
            "сними у такадо одну похвалу", "", mock_api
        )
        self.assertEqual(origin, "local")
        self.assertEqual(result["action"], "debit")
        self.assertEqual(result["sheet"], "praise")
        self.assertEqual(result["amount"], 1)

    async def test_direct_praise_command_in_both_orders_without_gemini(self):
        async def mock_api(*args):
            raise AssertionError("Похвала не требует API")
        for command in ("такадо +1 похвалу", "такадо похвалу +1",
                        "такадо -1 похвалы", "такадо похвал -1"):
            plan, source = await interpret(command, "", mock_api)
            self.assertEqual(source, "local", command)
            self.assertEqual(plan["sheet"], "praise", command)
            self.assertEqual(plan["amount"], 1, command)

    def test_praise_message_detection(self):
        for command in ("дай такадо одну похвалу", "такадо +1 похвалу",
                        "такадо похвалу -1"):
            self.assertTrue(looks_like_operation(command), command)

    def test_unknown_praise_word_does_not_match_local_grammar(self):
        self.assertIsNone(simple_parse("такадо +1 похвальбу"))

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
            "нортону и гоммезу оформи корректировку на три фрп", "mock-key", api
        )
        self.assertEqual(origin, "gemini")
        self.assertEqual(command["amount"], 3)
        self.assertEqual(len(called), 1)
        self.assertIn(MODEL, called[0][0])
        self.assertEqual(
            called[0][2]["generationConfig"]["thinkingConfig"]["thinkingLevel"],
            "high"
        )
        self.assertGreaterEqual(called[0][2]["generationConfig"]["maxOutputTokens"], 8192)
        self.assertNotIn("temperature", called[0][2]["generationConfig"])
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
            await interpret("пожалуйста, оформи Нортону три фрп", "", no_request)


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

    def test_russian_clyde_carbon_pronunciation_matches_latin(self):
        rows = [
            {"id": "c1", "nick": "Clyde_Carbon", "profileId": "person-clyde",
             "rank": "8", "qty": 3},
            {"id": "g1", "nick": "Gomez_Addams", "profileId": "person-gomez",
             "rank": "8", "qty": 5},
        ]
        for alias in ("Клайд Карбон", "клайду карбону",
                      "Клайду_Карбону", "клайд", "карбон",
                      "Clyde_Carbon", "Клайд_Carbon"):
            matches, errors = resolve_names([alias], rows)
            self.assertEqual(errors, [], alias)
            self.assertEqual(matches[0]["nick"], "Clyde_Carbon", alias)

    def test_full_phonetic_name_is_more_specific_than_first_name(self):
        rows = [
            {"id": "c1", "nick": "Clyde_Carbon", "qty": 4},
            {"id": "c2", "nick": "Clyde_Smith", "qty": 5},
        ]
        matches, errors = resolve_names(["Клайду Карбону"], rows)
        self.assertEqual(errors, [])
        self.assertEqual(matches[0]["id"], "c1")
        matches, errors = resolve_names(["клайду"], rows)
        self.assertEqual(matches, [])
        self.assertTrue(any("несколько" in err for err in errors))

    def test_full_name_is_not_guessed_from_only_one_matching_token(self):
        rows = [{"id": "c1", "nick": "Clyde_Carbon", "qty": 4}]
        for alias in ("клайду соснову", "незнакомый карбону",
                      "клайду карбону неизвестный"):
            matches, errors = resolve_names([alias], rows)
            self.assertEqual(matches, [], alias)
            self.assertTrue(errors, alias)

    async def test_screenshot_input_with_reason_no_api(self):
        async def no_gemini(*args):
            raise AssertionError("Name spelling and reason do not require Gemini")
        for raw in ("клайду +1 фрп проверка",
                    "клайду карбону +1 фрп причина тест",
                    "Клайд_Карбон +1 фрп причина тест",
                    "Clyde_Carbon +1 фрп причина тест"):
            command, reason, delimiter = split_operation_reason(raw)
            self.assertTrue(delimiter, raw)
            self.assertTrue(reason, raw)
            plan, origin = await interpret(command, "", no_gemini)
            self.assertEqual(plan["sheet"], "fractionrp")
            self.assertEqual(plan["amount"], 1)
            self.assertEqual(origin, "local")

    def test_duplicate_nick_in_sheet_is_unsafe(self):
        rows = self.rows + [{"nick": "Gomez_Addams", "id": "copy", "qty": 5}]
        matched, errors = resolve_names(["гоммез"], rows)
        self.assertFalse(matched)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
