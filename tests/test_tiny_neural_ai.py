"""Offline regression for tiny neural fallback; no API keys or Firebase needed."""
import unittest
from tiny_neural_ai import parse, predict


class TinyNeuralTests(unittest.TestCase):
    def test_model_present_and_trained(self):
        result = predict("начисли Нортону 2 фрп")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "credit")
        self.assertGreater(result[1], 0.90)

    def test_new_word_orders(self):
        cases = [
            ("Гоммезу добавь 2 похвалы", "credit", "praise", 2, ["Гоммезу"]),
            ("для нортона нужно вычесть 3 фрп", "debit", "fractionrp", 3, ["нортона"]),
            ("пожалуйста, сними у нортона и гоммеза три фрп",
             "debit", "fractionrp", 3, ["нортона", "гоммеза"]),
            ("уменьши Гоммезу 2 фрп", "debit", "fractionrp", 2, ["Гоммезу"]),
            ("для Нортона зачисли 2 фрп", "credit", "fractionrp", 2, ["Нортона"]),
        ]
        for phrase, action, sheet, amount, names in cases:
            with self.subTest(phrase=phrase):
                self.assertEqual(parse(phrase), {"action": action,
                    "sheet": sheet, "amount": amount, "names": names})

    def test_fail_closed(self):
        for phrase in (
            "Привет, как дела?", "Гоммезу 2 фрп", "Гоммезу начисли 2",
            "Гоммезу начисли 2 фрп и 2 похвалы", "Гоммезу добавь -2 фрп",
            "Гоммезу сними +2 фрп", "сними и добавь Нортону 3 фрп",
            "сними Нортону 30 фрп", "сними Нортону 0 фрп",
            "сними Нортону 2 фрп | причина",
            "сними Нортону 2 фрп и Кударову 3 фрп",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(parse(phrase))

    def test_no_permissions_or_db_side_effects(self):
        result = parse("Нортону добавь 1 фрп")
        self.assertEqual(set(result), {"action", "sheet", "amount", "names"})


if __name__ == "__main__":
    unittest.main()
