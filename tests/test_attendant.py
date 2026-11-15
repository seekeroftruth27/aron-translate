"""The Attendant (scripts/verse_detect.py) with every model answer scripted: the
rules that decide what goes green, what stays amber and when the screen clears.

The "verses" below are made-up sentences, not Bible text: the Attendant compares
words only as letters, so any text will do.

Run from the repo root (no key is read and no provider is called):

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import verse_detect as vd  # noqa: E402

JOHN = vd.BOOKS_EN.index("John")
TEXT = {    # stands in for John 21:16-21
    16: "the fisherman mended seven nets beside the harbour before sunrise",
    17: "children carried baskets of plums along the orchard wall",
    18: "a carpenter measured the oak beam twice and marked it with chalk then sawed slowly",
    19: "the lamplighter climbed his ladder while rain drummed on cobblestones",
    20: "two shepherds counted their flock under a pale winter moon",
    21: "merchants unloaded barrels of salt from the river barge at noon",
}
CITE = {"refs": [{"book": "John", "chapter": 21, "verse": 18}]}
HEARS = {"reading": 0.8, "none": 0.1}               # the gate: Scripture
FOCUS = dict(HEARS, citation=0.6, _focus=0.9)       # ... and he brings a text into focus
QUIET = {"none": 0.95}                              # his own words


class Text:
    """A text source with the chapter already in memory."""

    def get(self, b, c, lo, hi, hint=""):
        got = {v: TEXT[v] for v in range(lo, hi + 1) if (b, c) == (JOHN, 21) and v in TEXT}
        return got if len(got) == hi - lo + 1 else None

    def known(self, b, c):
        return dict(TEXT) if (b, c) == (JOHN, 21) else {}


class Rig:
    """A detector on a clock of its own; say() is one packet and the answers to it."""

    def __init__(self):
        self.now, self.seq, self.words, self.events, self.ans = 1000.0, 0, [], [], {}
        self.det = vd.VerseDetector(
            self.events.append, clock=lambda: self.now, text=Text(), track=None, blind_ask=None,
            ask=lambda *a: self.ans.get("read"), gate=lambda *a: self.ans.get("gate"),
            one=lambda *a: self.ans.get("one"))

    def say(self, text, dt=1.5, gate=QUIET, read=None, track=None, still=None):
        """`track`: the one-call tracker's probabilities (None: it gives no answer)."""
        self.now += dt
        self.seq += 1
        self.ans = {"gate": gate, "read": read,
                    "one": {"gate": gate, "track": track, "still": still} if track else None}
        ctx = " ".join(self.words[-30:])
        self.words += text.split()
        todo = self.det.on_packet(vd.Packet(self.seq, self.now, ctx, text, " ".join(self.words[-14:])))
        while todo:
            call = todo.pop(0)
            todo += self.det.on_result(call, self.det.run_call(call))
        self.det.tick()
        return self.pill()

    def pill(self):
        st = self.det.state
        return st["state"], st.get("ref")

    def cite(self, gate=FOCUS):
        return self.say("John 21:18", gate=gate, read=CITE)

    def by(self):
        return [e.get("by") for e in self.events if e["type"] == "reading" and e.get("level") == "strong"]


class Attendant(unittest.TestCase):
    def setUp(self):
        for k, v in dict(FOLLOW=True, FOCUS=True, GATE_STOP=0.5, ONE_SURE=0.8, STILL_TH=0.5,
                         READ_EARLY="digits").items():
            p = mock.patch.object(vd, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.rig = Rig()

    def test_a_citation_arms_the_verse(self):
        self.assertEqual(self.rig.cite(), ("armed", "John 21:18"))

    def test_his_words_alone_turn_a_cited_verse_green(self):
        """The follower: the verse's text spelled out, no model answer needed."""
        self.rig.cite()
        self.assertEqual(self.rig.say("a carpenter measured the oak beam", gate=HEARS), ("active", "John 21:18"))
        self.assertEqual(self.rig.by(), ["text"])

    def test_a_few_common_words_do_not(self):
        self.rig.cite()
        self.assertEqual(self.rig.say("and then the", gate=HEARS)[0], "armed")

    def test_the_next_verse_follows_on_his_words(self):
        self.rig.cite()
        self.rig.say("a carpenter measured the oak beam", gate=HEARS)
        self.rig.say("the lamplighter climbed", gate=HEARS)     # (the end of verse 18 is still in his newest words)
        self.assertEqual(self.rig.say("his ladder while rain", gate=HEARS), ("active", "John 21:19"))

    def test_a_cited_verse_waiting_takes_less_from_the_tracker(self):
        """ARMED_TH: his first words in another translation leave the tracker under
        "wait"; for the verse he cited that is enough."""
        self.rig.cite()
        pill = self.rig.say("a woodworker sized up", gate=HEARS, track={"wait": 0.52, "v18": 0.45})
        self.assertEqual(pill, ("active", "John 21:18"))

    def test_but_not_while_the_gate_hears_no_scripture(self):
        self.rig.cite()
        pill = self.rig.say("a woodworker sized up", gate={"none": 0.7, "reading": 0.2},
                            track={"wait": 0.52, "v18": 0.45})
        self.assertEqual(pill, ("armed", "John 21:18"))

    def test_nor_for_a_verse_he_did_not_cite(self):
        self.rig.cite()
        pill = self.rig.say("a woodworker sized up", gate=HEARS, track={"wait": 0.52, "v19": 0.45})
        self.assertEqual(pill, ("armed", "John 21:18"))

    def test_nor_for_the_rest_of_a_passage_he_has_stopped_reading(self):
        """After a reading the tracker's first answer is "aside": the usual bar stands."""
        rig = self.rig
        rig.say("John 21:18 to 20", gate=FOCUS, read={"refs": [{"book": "John", "chapter": 21, "verse": 18,
                                                                 "verse_end": 20}]})
        rig.say("a woodworker sized up", gate=HEARS, track={"wait": 0.1, "v18": 0.85})
        for _ in range(12):
            if rig.say("so my friends I", track={"aside": 0.9, "v18": 0.05}, still=0.1)[0] != "active":
                break
        self.assertIn(rig.det.canon.at[(JOHN, 21, 19)], rig.det.armed)
        for _ in range(2):
            pill = rig.say("some lamp man went", gate=HEARS, track={"aside": 0.5, "v19": 0.45}, still=0.6)
        self.assertNotEqual(pill[0], "active")

    def test_without_a_focus_cue_a_cited_verse_stays_amber(self):
        """Jo's rule: green only when he brings the text into focus."""
        self.rig.cite(gate=dict(HEARS, citation=0.6))
        pill = self.rig.say("a woodworker sized up", gate=HEARS, track={"wait": 0.1, "v18": 0.85})
        self.assertEqual(pill, ("armed", "John 21:18"))

    def test_a_named_verse_his_words_do_not_bear_out_is_turned_down(self):
        """In a chapter whose text his words have followed, the tracker naming the next
        verse over words that are none of it is his comment, not a reading."""
        self.rig.cite()
        self.rig.say("a carpenter measured the oak beam", gate=HEARS)
        pill = self.rig.say("which reminds me of", gate=HEARS, track={"aside": 0.1, "v19": 0.85}, still=0.9)
        self.assertEqual(pill, ("active", "John 21:18"))
        self.assertEqual(self.rig.det.stats["not borne out"], 1)

    def read_then_stop(self):
        """v18 read, then his own words until the screen clears -> seconds it took."""
        rig = self.rig
        rig.cite()
        rig.say("a carpenter measured the oak beam", gate=HEARS)
        t0 = rig.now
        for _ in range(12):
            if rig.say("so my friends I", track={"aside": 0.9, "v18": 0.05}, still=0.1)[0] != "active":
                break
        return rig.now - t0

    def test_the_screen_clears_within_ten_seconds_or_so_of_his_last_words(self):
        took = self.read_then_stop()
        self.assertNotEqual(self.rig.pill()[0], "active")
        self.assertLessEqual(took, vd.CLEAR_S + 3)
        self.assertGreater(took, vd.CLEAR_S - 3)

    def test_a_pause_shows_as_paused_not_cleared(self):
        self.rig.cite()
        self.rig.say("a carpenter measured the oak beam", gate=HEARS)
        self.rig.say("so my friends I", track={"aside": 0.9, "v18": 0.05}, still=0.1)
        self.assertEqual(self.rig.pill(), ("active", "John 21:18"))
        self.assertTrue(self.rig.det.state.get("paused"))

    def test_a_cleared_passage_comes_back_only_on_a_second_packet(self):
        """One answer alone brought a cleared passage back wrongly more often than rightly."""
        self.read_then_stop()
        sure = {"aside": 0.05, "v19": 0.9}
        first = self.rig.say("the lamplighter climbed", gate=HEARS, track=sure, still=0.9)
        self.assertNotEqual(first[0], "active")
        second = self.rig.say("his ladder while rain", gate=HEARS, track=sure, still=0.9)
        self.assertEqual(second, ("active", "John 21:19"))

    def test_and_not_on_words_that_are_none_of_its_text(self):
        self.read_then_stop()
        for _ in range(3):
            pill = self.rig.say("some lamp man went up", gate=HEARS, track={"aside": 0.05, "v19": 0.9}, still=0.9)
        self.assertNotEqual(pill[0], "active")

    def test_a_sure_tracker_picking_the_verse_up_restarts_the_clear_clock(self):
        """A long verse read in another translation: nothing but the tracker says he is
        still in it, and a pause mid-verse must not end it while he reads on."""
        rig = self.rig
        rig.cite()
        sure = {"aside": 0.05, "v18": 0.9}
        rig.say("a woodworker sized up", gate=HEARS, track=sure, still=0.9)
        rig.say("hm", track={"aside": 0.9, "v18": 0.05}, still=0.1)         # a breath: paused
        for _ in range(10):                                                 # 15 s more of the same verse
            pill = rig.say("the timber two times over", gate=HEARS, track=sure, still=0.9)
        self.assertEqual(pill, ("active", "John 21:18"))
        self.assertFalse(rig.det.state.get("paused"))


class Follower(unittest.TestCase):
    def match(self, *packets):
        fo = vd.Follower()
        for seq, text in enumerate(packets, 1):
            fo.hear(vd.Packet(seq, 0, "", text))
        return fo.match((JOHN, 21), TEXT)

    def test_words_in_order_name_the_verse_and_how_far_in(self):
        score, pos = self.match("a carpenter measured", "the oak beam twice")[18]
        self.assertGreaterEqual(score, vd.FOLLOW_SURE)
        self.assertAlmostEqual(pos, 7 / 15, places=1)

    def test_words_out_of_order_count_once(self):
        got = self.match("chalk beam carpenter")
        self.assertLess(got.get(18, (0, 0))[0], vd.FOLLOW_ON)

    def test_a_word_every_verse_has_weighs_next_to_nothing(self):
        got = self.match("the the the")
        self.assertTrue(all(score < vd.FOLLOW_SOME for score, _ in got.values()))

    def test_only_words_up_to_the_newest_packet_count(self):
        """A verse he finished two packets ago is not the one he is reading now."""
        got = self.match("a carpenter measured the oak beam", "so my friends", "I think that")
        self.assertNotIn(18, got)


if __name__ == "__main__":
    unittest.main()
