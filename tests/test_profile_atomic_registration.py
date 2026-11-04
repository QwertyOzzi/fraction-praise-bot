import ast
import unittest
from pathlib import Path

class TestProfile(unittest.TestCase):
    def test_syntax(self):
        ast.parse((Path(__file__).resolve().parents[1] / 'profile_sheets.py').read_text())
