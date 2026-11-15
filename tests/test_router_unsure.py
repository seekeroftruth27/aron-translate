"""Soniox's per-word confidence stands in for the Router's mis-hear check when
that check gives no answer (router_gate.decide).

2026-11-08: "pe Domnul cu o cântare" heard as "pe domnul Cocritan" became
"we will praise Mr. Cocritan again". The check flags it (sl ~0.75); a low word
should still send it on the day the check fails, and only then.

Run from the repo root (no key is read and no provider is called):

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import router_gate as rg  # noqa: E402

RO = "Vom reocupa locurile și îl vom mai lăuda pe domnul Coc."
EN = "We will reoccupy the seats and we will praise Mr. Coc again."
CONF = [0.98, 0.99, 0.98, 1.0, 1.0, 0.85, 1.0, 1.0, 1.0, 0.99, 0.42]   # stt-rt-v6 on that audio


def decide(answers, **kw):
    with mock.patch.object(rg, "_ask", lambda text, qs, timeout: answers):
        return rg.decide(RO, EN, [], timeout=1.0, th=0.3, **kw)


class Unsure(unittest.TestCase):
    PASS = {"v": 0.1, "sens": 0.0, "sl": 0.1}   # the checks see nothing wrong

    def test_check_answered_wins_over_a_low_word(self):
        out = decide(self.PASS, conf=CONF, unsure=0.5)
        self.assertEqual((out["route"], out["why"], out["low"]), (False, "", 0.42))

    def test_no_misheard_answer_low_word_sends_on(self):
        out = decide({"v": 0.1, "sens": 0.0, "sl": None}, conf=CONF, unsure=0.5)
        self.assertEqual((out["route"], out["why"]), (True, "misheard"))

    def test_no_answer_above_the_bar_keeps_soniox(self):
        out = decide({"v": 0.1, "sens": 0.0, "sl": None}, conf=CONF, unsure=0.3)
        self.assertEqual((out["route"], out["why"], out["low"]), (False, "", 0.42))

    def test_off_and_absent(self):
        self.assertFalse(decide({"sl": None}, conf=CONF, unsure=0.0)["route"])
        out = decide({"sl": None})
        self.assertEqual((out["route"], out["low"]), (False, None))

    def test_unknown_words_ignored(self):
        out = decide({"sl": None}, conf=[None, 0.9, None], unsure=0.5)
        self.assertEqual((out["route"], out["low"]), (False, 0.9))

    def test_checks_failing_still_route_a_low_word(self):
        def boom(text, qs, timeout):
            raise TimeoutError
        with mock.patch.object(rg, "_ask", boom):
            out = rg.decide(RO, EN, [], timeout=1.0, th=0.3, conf=CONF, unsure=0.5)
        self.assertEqual((out["route"], out["why"]), (True, "misheard"))


if __name__ == "__main__":
    unittest.main()
