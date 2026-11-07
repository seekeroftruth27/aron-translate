"""The .env reader: how a church's settings file is turned into environment
variables (server._load_dotenv).

    python -m unittest discover -s tests -v
"""
import os
import tempfile
import unittest
from unittest import mock

from support import load_server, need_flask


@need_flask
class DotEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def load(self, text, existing=None):
        """Run the reader on a .env holding `text`; return the variables it set."""
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "app"))
            with open(os.path.join(root, ".env"), "wb") as f:
                f.write(text if isinstance(text, bytes) else text.encode("utf-8"))
            with mock.patch.dict(os.environ, existing or {}), \
                 mock.patch.object(self.server, "HERE", os.path.join(root, "app")):
                before = dict(os.environ)
                self.server._load_dotenv()
                return {k: v for k, v in os.environ.items() if before.get(k) != v}

    def test_plain_lines(self):
        self.assertEqual(self.load("T_A=1\nT_B = two words \n"), {"T_A": "1", "T_B": "two words"})

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertEqual(self.load("# note\n\n   \nT_A=1\nnot a setting\n"), {"T_A": "1"})

    def test_an_inline_comment_is_not_part_of_the_value(self):
        got = self.load("T_URL=http://10.0.0.5:1025/   # the ProPresenter machine\n")
        self.assertEqual(got, {"T_URL": "http://10.0.0.5:1025/"})

    def test_a_hash_inside_a_value_is_kept(self):
        self.assertEqual(self.load("T_A=ab#cd\n"), {"T_A": "ab#cd"})

    def test_a_blank_value_with_a_comment_stays_blank(self):
        got = self.load("T_A=   # http://example.com\n", existing={"T_A": "unset"})
        self.assertNotIn("T_A", got)                       # nothing overrides the real variable

    def test_a_blank_value_is_empty_not_the_comment(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("T_BLANK", None)
            got = self.load("T_BLANK=   # http://example.com\n")
            self.assertEqual(got.get("T_BLANK", ""), "")

    def test_quoted_values_are_taken_verbatim(self):
        got = self.load('T_A="x # y"\nT_B=\'p q\'\n')
        self.assertEqual(got, {"T_A": "x # y", "T_B": "p q"})

    def test_a_real_variable_wins(self):
        got = self.load("T_A=file\n", existing={"T_A": "shell"})
        self.assertNotIn("T_A", got)

    def test_a_byte_order_mark_does_not_hide_the_first_key(self):
        self.assertEqual(self.load(b"\xef\xbb\xbfT_FIRST=1\nT_B=2\n"), {"T_FIRST": "1", "T_B": "2"})

    def test_windows_line_endings(self):
        self.assertEqual(self.load("T_A=1\r\nT_B=2\r\n"), {"T_A": "1", "T_B": "2"})

    def test_a_missing_file_is_fine(self):
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(self.server, "HERE", os.path.join(root, "app")):
            self.server._load_dotenv()


if __name__ == "__main__":
    unittest.main()
