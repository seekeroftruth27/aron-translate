"""The clauses voice (scripts/voice_clauses.py): where the voice is cut, who
speaks each clip, and which clip carries a sentence's tag.

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import voice_clauses as vc   # noqa: E402

KEY = ("voice", "eleven_v4", "en")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Director:
    """Records what it is asked; answers a tag per sentence, and styles a clip
    only when told what to answer."""

    def __init__(self):
        self.begun, self.heard, self.styling, self.forgot = [], [], [], []
        self.styled_answer = None

    def begin(self, key, first):
        self.begun.append((key, first))

    def begin_style(self, key, clip, engine, starts):
        self.styling.append((key, clip, engine, starts))

    def said(self, text):
        self.heard.append(text)

    def tag(self, key, wait):
        return "earnestly" if key[1] % 2 else None

    def styled(self, key, wait):
        self.waited = wait
        return self.styled_answer

    def forget(self, key):
        self.forgot.append(key)


def resolved(p):
    return p._ev.is_set()


class ClauseVoiceTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.director = Director()
        self.v = vc.ClauseVoice(self.director, clock=self.clock, ticker=False)

    def push(self, text, lag=5.0, tags=True):
        self.clock.t += 2.0
        return self.v.push(text, KEY, lag, tags=tags)

    def test_full_stop_cuts_and_the_first_part_speaks_the_clip(self):
        a = self.push("To whom do we")
        b = self.push("give first place?")
        self.assertTrue(resolved(a) and resolved(b))
        self.assertEqual(a.wait(), "To whom do we give first place?")
        self.assertIsNone(b.wait())          # rode along in a's clip
        self.assertEqual(self.v.cuts["full stop"], 1)

    def test_comma_waits_while_the_voice_has_audio_queued(self):
        a = self.push("Are we determined to follow Him,", lag=3.0)
        self.assertFalse(resolved(a))        # behind: pile up to the full stop
        self.push("or only on feast days?", lag=3.0)
        self.assertEqual(a.wait(), "Are we determined to follow Him, or only on feast days?")

    def test_comma_cuts_when_the_voice_is_about_to_run_dry(self):
        a = self.push("Are we determined to follow Him,", lag=0.4)
        self.assertEqual(a.wait(), "Are we determined to follow Him,")
        self.assertEqual(self.v.cuts["comma"], 1)

    def test_a_short_clause_never_cuts_at_its_comma(self):
        a = self.push("Dear candidates,", lag=0.0)    # two words: too short to stand alone
        self.assertFalse(resolved(a))

    def test_a_very_long_sentence_is_cut_at_a_comma_even_when_behind(self):
        words = " ".join(["word"] * (vc.LONG_WORDS - 1))
        a = self.push(words, lag=9.0)
        self.assertFalse(resolved(a))
        self.push("and more,", lag=9.0)
        self.assertTrue(resolved(a))
        self.assertEqual(self.v.cuts["long"], 1)

    def test_a_pause_sends_what_is_waiting(self):
        a = self.push("I have been")
        self.v.flush_idle()
        self.assertFalse(resolved(a))
        self.clock.t += vc.IDLE_S
        self.v.flush_idle()
        self.assertEqual(a.wait(), "I have been")
        self.assertEqual(self.v.cuts["pause"], 1)

    def test_a_comma_waits_while_the_clip_is_not_due_yet(self):
        # The voice has nothing queued, but the page holds this clip 6 s more:
        # cutting now would gain nothing, so it waits for the full stop.
        a = self.v.push("Are we determined to follow Him,", KEY, 0.0, due=6.0)
        self.assertFalse(resolved(a))
        self.clock.t += 2.0
        self.v.push("or only on feast days?", KEY, 0.0, due=6.0)
        self.assertEqual(a.wait(), "Are we determined to follow Him, or only on feast days?")
        self.assertEqual(self.v.cuts["comma"], 0)

    def test_when_the_clip_falls_due_it_goes_at_its_last_comma(self):
        a = self.v.push("Are we determined to follow Him,", KEY, 0.0, due=4.0)
        self.clock.t += 1.0
        b = self.v.push("or only on", KEY, 0.0, due=5.0)
        self.v.flush_idle()
        self.assertFalse(resolved(a))                 # 3 s to go: still waiting
        self.clock.t = 3.0                            # 1 s to go
        self.v.flush_idle()
        self.assertEqual(a.wait(), "Are we determined to follow Him,")
        self.assertFalse(resolved(b))                 # the rest waits for its own end
        self.assertEqual(self.v.cuts["comma"], 1)

    def test_a_pause_waits_while_the_clip_is_not_due_yet(self):
        a = self.v.push("This means that in our temple", KEY, 0.0, due=9.0)
        self.clock.t += vc.IDLE_S
        self.v.flush_idle()
        self.assertFalse(resolved(a))                 # he paused, but the clip is 5.5 s off
        self.v.push("there must be prayer.", KEY, 0.0, due=9.0)
        self.assertEqual(a.wait(), "This means that in our temple there must be prayer.")
        self.assertEqual(self.v.cuts["pause"], 0)

    def test_a_pause_still_sends_the_words_once_the_clip_is_due(self):
        a = self.v.push("I have been", KEY, 0.0, due=5.0)
        self.clock.t = 4.0
        self.v.flush_idle()
        self.assertEqual(a.wait(), "I have been")
        self.assertEqual(self.v.cuts["pause"], 1)

    def test_the_clip_just_cut_keeps_the_voice_busy(self):
        # Overdue, so it goes at its comma; the next comma then waits while the
        # voice speaks those ten words, though the page has not counted them yet.
        a = self.v.push("And I'd like to ask you one more question now,", KEY, 0.0, due=0.5)
        self.assertEqual(a.wait(), "And I'd like to ask you one more question now,")
        self.clock.t += 1.0
        b = self.v.push("beyond a life of obedience,", KEY, 0.0, due=0.0)
        self.assertFalse(resolved(b))
        self.v.push("is our life also a life of prayer?", KEY, 0.0, due=0.0)
        self.assertEqual(b.wait(), "beyond a life of obedience, is our life also a life of prayer?")

    def test_a_queued_voice_counts_even_once_the_clip_is_due(self):
        a = self.v.push("Are we determined to follow Him,", KEY, 6.0, due=0.5)
        self.assertFalse(resolved(a))                 # 6 s of speech ahead of it

    def test_another_voice_finishes_what_is_waiting(self):
        a = self.push("I have been")
        b = self.v.push("crucified with Christ.", ("other", "eleven_v4", "en"), 5.0)
        self.assertEqual(a.wait(), "I have been")
        self.assertEqual(b.wait(), "crucified with Christ.")
        self.assertEqual(self.v.cuts["switch"], 1)

    def test_only_a_sentences_first_clip_carries_its_tag(self):
        a = self.push("Are we determined to follow Him,", lag=0.0)   # sentence 1, cut at the comma
        b = self.push("or only on feast days?", lag=0.0)
        c = self.push("Is Jesus in our hearts?")                     # sentence 2
        self.assertEqual(a.wait(), "Are we determined to follow Him,")
        self.assertEqual(self.v.speech(a), ("Are we determined to follow Him,", "earnestly"))
        self.assertEqual(b.wait(), "or only on feast days?")
        self.assertIsNone(b.tag_key)                                  # a later clause goes plain
        self.assertEqual(self.v.speech(b), ("or only on feast days?", None))
        self.assertEqual(c.tag_key, (KEY, 2))
        self.assertEqual([k[1] for k, _ in self.director.begun], [1, 2])
        self.assertEqual(self.director.begun[1][1], "Is Jesus in our hearts?")

    def test_with_expression_tags_off_the_director_is_never_asked(self):
        a = self.push("To whom do we give first place?", tags=False)
        self.assertEqual(self.director.begun, [])
        self.assertEqual(self.director.styling, [])
        self.assertIsNone(a.style_key)

    def test_a_cut_clip_is_styled_and_its_styling_is_what_the_voice_reads(self):
        self.director.styled_answer = ("earnestly", "To whom do we give FIRST place?")
        a = self.push("To whom do we give first place?")
        (key, clip, engine, starts), = self.director.styling
        self.assertEqual((clip, engine, starts), ("To whom do we give first place?", "voice", True))
        self.assertEqual(self.v.speech(a), ("To whom do we give FIRST place?", "earnestly"))
        self.assertEqual(self.director.forgot, [a.tag_key])         # the quick tag is not needed

    def test_styling_is_waited_for_only_while_the_voice_has_speech_queued(self):
        self.director.styled_answer = None
        a = self.push("To whom do we give first place?", lag=9.0)
        self.v.speech(a)
        self.assertEqual(self.director.waited, vc.STYLE_WAIT_S)       # plenty queued: the full wait
        b = self.push("Is Jesus in our hearts?", lag=0.0)
        self.v.speech(b)
        self.assertEqual(self.director.waited, vc.MIN_WAIT_S)         # about to run dry: hardly any
        self.clock.t += 2.0
        c = self.v.push("Dear candidates, have we truly received Jesus into our hearts?", KEY, 0.0,
                        tags=True, due=3.0)
        self.v.speech(c)                                               # the page holds it 3 s anyway
        self.assertAlmostEqual(self.director.waited, 3.0 - vc.FIRST_AUDIO_S)

    def test_without_styling_back_in_time_the_quick_tag_stands_in(self):
        self.director.styled_answer = None
        a = self.push("To whom do we give first place?")
        self.assertEqual(self.v.speech(a), ("To whom do we give first place?", "earnestly"))

    def test_a_clause_that_continues_a_sentence_is_styled_without_a_tag(self):
        self.push("Are we determined to follow Him,", lag=0.0)
        b = self.push("or only on feast days?", lag=0.0)
        self.assertEqual(self.director.styling[-1][3], False)        # starts=False
        self.assertIsNone(b.tag_key)

    def test_the_director_hears_every_clip(self):
        self.push("It began on a Sunday.")
        self.push("Sunday at church is beautiful.")
        self.assertEqual(self.director.heard, ["It began on a Sunday.", "Sunday at church is beautiful."])

    def test_empty_text_is_resolved_at_once(self):
        p = self.v.push("  ", KEY, 0.0)
        self.assertIsNone(p.wait(0.1))

    def test_closing_quote_after_a_full_stop_still_ends_the_sentence(self):
        a = self.push('and I live, yet it is no longer I who live."')
        self.assertEqual(a.wait(), 'and I live, yet it is no longer I who live."')


if __name__ == "__main__":
    unittest.main()
