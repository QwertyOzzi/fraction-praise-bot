"""UI-only regression checks. No Telegram network or Firebase is needed."""
import ast
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parents[1].joinpath("bot.py").read_text("utf-8")
DESIGN = Path(__file__).resolve().parents[1].joinpath("telegram_design.py").read_text("utf-8")
TREE = ast.parse(CODE)


class TestTelegramUi(unittest.TestCase):
    def test_lea_only_on_noncritical_screens(self):
        self.assertEqual(DESIGN.count("https://lea-script.space/"), 1)
        self.assertEqual(CODE.count("lea=True"), 5)
        for name in ("pending_report_text", "approve_report", "reject_report",
                     "cmd_admin_auth", "cmd_security_overview", "perform_manual_grant"):
            fn = next(n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == name)
            self.assertNotIn("lea=True", ast.get_source_segment(CODE, fn))

    def test_buttons_have_supported_colors(self):
        for style in ("primary", "success", "danger"):
            self.assertIn('style="' + style + '"', CODE)
        for node in ast.walk(TREE):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id not in ("InlineKeyboardButton", "KeyboardButton"):
                    continue
                for kw in node.keywords:
                    if kw.arg == "style":
                        self.assertIsInstance(kw.value, ast.Constant)
                        self.assertIn(kw.value.value, ("primary", "success", "danger"))

    def test_roles_and_sensitive_handlers_remain(self):
        self.assertIn("_cloud_state_hydrated and user_id in admins_db", CODE)
        for name in ("cmd_start", "approve_report", "reject_report", "cmd_admin_auth",
                     "agent_do_action", "agent_delete_confirm"):
            self.assertTrue(any(isinstance(n, ast.AsyncFunctionDef) and n.name == name
                                for n in TREE.body))


if __name__ == "__main__":
    unittest.main()
