"""In CI the tests must really run: a missing Flask or JavaScript engine would
otherwise skip most of the suite and still show green.

    python -m unittest discover -s tests -v
"""
import os
import unittest

from support import HAVE_FLASK

try:
    import py_mini_racer  # noqa: F401
    HAVE_JS = True
except ImportError:
    HAVE_JS = False


@unittest.skipUnless(os.environ.get("CI"), "only enforced in CI (GitHub sets CI=true)")
class SuiteCanRun(unittest.TestCase):
    def test_flask_is_installed(self):
        self.assertTrue(HAVE_FLASK, "pip install -r requirements-dev.txt")

    def test_the_javascript_engine_is_installed(self):
        self.assertTrue(HAVE_JS, "pip install -r requirements-dev.txt")


if __name__ == "__main__":
    unittest.main()
