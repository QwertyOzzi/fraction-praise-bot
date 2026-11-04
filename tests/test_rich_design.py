"""Unit checks for real Telegram Rich Messages without Telegram credentials."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESIGN = (ROOT / "telegram_design.py").read_text("utf-8")
BOT = (ROOT / "bot.py").read_text("utf-8")
design_tree = ast.parse(DESIGN)
helpers = [node for node in design_tree.body
           if isinstance(node, ast.FunctionDef)
           and node.name in {"rich_card_html", "plain_card_html"}]
env = {
    "re": re,
    "TITLE_RE": re.compile(r"^([^\n<]*)<b>([^<\n]+)</b>\n(?:━+\n)?\n*"),
    "LEA_URL": "https://lea-script.space/",
    "LEA_LABEL": "🧩 ЛЕА ↗"
}
exec(compile(ast.Module(body=helpers, type_ignores=[]), "isolated rich design", "exec"), env)


class TestRichCards(unittest.TestCase):
    def test_real_heading_and_compact_lea_button(self):
        sample = ("🛡️ <b>FBI · Личный кабинет</b>\n\n"
                  "👤 <b>Агент:</b> <code>QA_TestAgent2</code>\n"
                  "🎖️ <b>Ранг:</b> 8\n\nВыберите действие.")
        output = env["rich_card_html"](sample, lea=True)
        self.assertIn("<h2>🛡️ FBI · Личный кабинет</h2>", output)
        self.assertIn('<tg-button type="url" url="https://lea-script.space/">', output)
        self.assertIn("🧩 ЛЕА ↗</tg-button>", output)
        self.assertIn("<code>QA_TestAgent2</code>", output)
        self.assertNotIn("━", output)

    def test_no_ad_by_default(self):
        sample = "📋 <b>FBI · Проверка отчёта</b>\n\nДанные заявки"
        self.assertNotIn("ЛЕА", env["rich_card_html"](sample))

    def test_fallback_readable_without_line_art(self):
        source = "🛡️ <b>FBI · Личный кабинет</b>\n━━━━━━━━━━━━━━\n\nТекст"
        fallback = env["plain_card_html"](source, lea=False)
        self.assertNotIn("━", fallback)
        self.assertNotIn("lea-script", fallback)

    def test_only_home_and_status_include_promo(self):
        self.assertEqual(BOT.count("lea=True"), 2)
        for protected in ("approve_report", "reject_report", "cmd_admin_auth",
                          "agent_do_action", "cmd_list_admins"):
            node = next(n for n in ast.parse(BOT).body
                        if isinstance(n, ast.AsyncFunctionDef) and n.name == protected)
            self.assertNotIn("lea=True", ast.get_source_segment(BOT, node))


if __name__ == "__main__":
    unittest.main()
