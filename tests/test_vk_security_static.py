"""Static checks for VK bridge architecture without service credentials."""
import ast
import hashlib
import hmac
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VkBridgeTests(unittest.TestCase):
    def test_modules_parse_and_keep_private_chat_gate(self):
        src = (ROOT / "vk_bot" / "main.py").read_text(encoding="utf-8")
        ast.parse(src)
        ast.parse((ROOT / "vk_bot" / "backend.py").read_text(encoding="utf-8"))
        self.assertIn("peer != uid", src)
        self.assertIn("is_staff(actor)", src)
        self.assertIn("commit_agent_debit(", src)
        self.assertIn("decide_report(", src)

    def test_shared_identity_never_rewrites_bot_state(self):
        src = (ROOT / "vk_bot" / "backend.py").read_text(encoding="utf-8")
        self.assertIn("vk_accounts/", src)
        self.assertIn("tg_vk_links/", src)
        self.assertIn("vk_link_codes/", src)
        self.assertNotIn('_replace(client, "config/bot_state"', src)
        self.assertIn('"currentDocument": {"updateTime":', src)

    def test_acl_signature_denies_tampering(self):
        text = (ROOT / "vk_bot" / "backend.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        fn = next(x for x in tree.body
                  if isinstance(x, ast.FunctionDef) and x.name == "_acl_valid")
        sandbox = {"os": os, "json": json, "hmac": hmac, "hashlib": hashlib,
                   "OWNERS": {6060562962}}
        compiled = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
        exec(compile(compiled, "vk_acl_test", "exec"), sandbox)
        prev = os.environ.get("VK_ACL_SIGNING_KEY")
        os.environ["VK_ACL_SIGNING_KEY"] = "test-secret-key-is-at-least-thirty-two-characters"
        try:
            admins = [12345]
            roles = {"12345": "leader"}
            payload = json.dumps({"admins": admins, "roles": roles},
                                 ensure_ascii=True, sort_keys=True,
                                 separators=(",", ":")).encode()
            sig = hmac.new(os.environ["VK_ACL_SIGNING_KEY"].encode(),
                           payload, hashlib.sha256).hexdigest()
            state = {"admins": admins, "admin_roles": roles,
                     "vk_acl_signature": sig}
            self.assertTrue(sandbox["_acl_valid"](state))
            state["admin_roles"]["12345"] = "deputy"
            self.assertFalse(sandbox["_acl_valid"](state))
            state["vk_acl_signature"] = ""
            self.assertFalse(sandbox["_acl_valid"](state))
        finally:
            if prev is None:
                os.environ.pop("VK_ACL_SIGNING_KEY", None)
            else:
                os.environ["VK_ACL_SIGNING_KEY"] = prev

    def test_bot_registers_link_and_reviewer_commands(self):
        text = (ROOT / "bot.py").read_text(encoding="utf-8")
        for cmd in ('Command("vklink")', 'Command("vkunlink")',
                    'Command("vkpending")', 'Command("vkreview")',
                    'Command("vkacl")'):
            self.assertIn(cmd, text)
        self.assertIn("vk_acl_signature", text)


if __name__ == "__main__":
    unittest.main()
