"""The voice's delivery tags (scripts/voice_director.py): which model is asked,
how its answer is read, and that a slow or failing call never holds the voice.

    python -m unittest discover -s tests -v
"""
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import voice_director as vd   # noqa: E402


class ParseTest(unittest.TestCase):
    def test_json(self):
        self.assertEqual(vd.parse('{"tag": "reverently", "why": "Scripture"}'), "reverently")

    def test_none_is_no_tag(self):
        self.assertIsNone(vd.parse('{"tag": "none", "why": "plain narration"}'))

    def test_an_unlisted_tag_is_no_tag(self):
        self.assertIsNone(vd.parse('{"tag": "whispering", "why": "?"}'))

    def test_plain_text_naming_one_tag(self):
        self.assertEqual(vd.parse("I would say: with conviction."), "with conviction")

    def test_plain_text_naming_two_tags_is_no_tag(self):
        self.assertIsNone(vd.parse("gently, or perhaps warmly"))


class CheckStyledTest(unittest.TestCase):
    CLIP = "Are we determined to give things up?"

    def test_capitals_a_pause_and_a_tag(self):
        self.assertEqual(vd.check_styled(self.CLIP, "[earnestly] Are we DETERMINED… to give things up?", True),
                         ("earnestly", "Are we DETERMINED… to give things up?"))

    def test_unchanged_is_fine(self):
        self.assertEqual(vd.check_styled(self.CLIP, self.CLIP, True), (None, self.CLIP))

    def test_a_changed_word_is_turned_away(self):
        self.assertIsNone(vd.check_styled(self.CLIP, "Are we ready to give things up?", True))

    def test_changed_punctuation_is_turned_away(self):
        self.assertIsNone(vd.check_styled(self.CLIP, "Are we determined to give things up!", True))

    def test_three_words_in_capitals_is_too_much(self):
        self.assertIsNone(vd.check_styled(self.CLIP, "ARE WE DETERMINED to give things up?", True))

    def test_three_pauses_is_too_much(self):
        self.assertIsNone(vd.check_styled(self.CLIP, "Are we… determined… to give… things up?", True))

    def test_a_tag_inside_the_clip_is_turned_away(self):
        self.assertIsNone(vd.check_styled(self.CLIP, "Are we [earnestly] determined to give things up?", True))

    def test_no_tag_where_a_sentence_does_not_begin(self):
        self.assertEqual(vd.check_styled("or only on feast days?", "[earnestly] or only on FEAST days?", False),
                         (None, "or only on FEAST days?"))

    def test_an_unlisted_tag_is_dropped_and_the_rest_kept(self):
        self.assertEqual(vd.check_styled(self.CLIP, "[shouting] Are we determined to give things UP?", True),
                         (None, "Are we determined to give things UP?"))

    def test_words_already_in_capitals_do_not_count(self):
        clip = "The LORD is my shepherd, the LORD of hosts."
        self.assertEqual(vd.check_styled(clip, "The LORD is MY shepherd, the LORD of hosts.", True)[1],
                         "The LORD is MY shepherd, the LORD of hosts.")

    def test_romanian_letters(self):
        clip = "Suntem hotărâți să renunțăm?"
        self.assertEqual(vd.check_styled(clip, "Suntem HOTĂRÂȚI să renunțăm?", True)[1],
                         "Suntem HOTĂRÂȚI să renunțăm?")

    def test_quotation_marks_around_scripture_are_let_through(self):
        clip = "I have been crucified with Christ,"
        self.assertEqual(vd.check_styled(clip, '[reverently] "I have been crucified with Christ,"', True),
                         ("reverently", '"I have been crucified with Christ,"'))

    def test_nothing_back(self):
        self.assertIsNone(vd.check_styled(self.CLIP, None, True))


class SayTest(unittest.TestCase):
    """Respellings for the voice: only of words in the clip, only in letters."""
    CLIP = "Job had such a beautiful expression, saying:"

    def say(self, *pairs, styled=None):
        return vd.apply_say(self.CLIP, styled or self.CLIP, [{"word": w, "say": s} for w, s in pairs])

    def test_a_name_is_respelled_for_the_voice(self):
        self.assertEqual(self.say(("Job", "Jobe")), "Jobe had such a beautiful expression, saying:")

    def test_a_word_not_in_the_clip_is_ignored(self):
        self.assertEqual(self.say(("Elimelech", "Eh-LIM-eh-lek")), self.CLIP)

    def test_only_whole_words(self):
        clip = "a job well done"
        self.assertEqual(vd.apply_say(clip, clip, [{"word": "job", "say": "jobe"}]), "a jobe well done")
        self.assertEqual(vd.apply_say(clip, clip, [{"word": "jo", "say": "joe"}]), clip)

    def test_no_digits_brackets_or_tags_in_a_respelling(self):
        for bad in ("Job 19", "[shouting] Job", "Jobe…", ""):
            self.assertEqual(self.say(("Job", bad)), self.CLIP, bad)

    def test_a_scripture_reference_read_out(self):
        clip = "as John 3:16 says,"
        self.assertEqual(vd.apply_say(clip, clip, [{"word": "John 3:16", "say": "John three sixteen"}]),
                         "as John three sixteen says,")

    def test_a_word_the_director_put_in_capitals(self):
        self.assertEqual(self.say(("Job", "Jobe"), styled="JOB had such a beautiful expression, saying:"),
                         "JOBE had such a beautiful expression, saying:")

    def test_no_more_than_four(self):
        clip = "Ana Bob Cid Dan Eve"
        got = vd.apply_say(clip, clip, [{"word": w, "say": w + "x"} for w in clip.split()])
        self.assertEqual(got, "Anax Bobx Cidx Danx Eve")

    def test_nothing_or_junk_leaves_it_alone(self):
        self.assertEqual(vd.apply_say(self.CLIP, self.CLIP, None), self.CLIP)
        self.assertEqual(vd.apply_say(self.CLIP, self.CLIP, ["Job"]), self.CLIP)

    def test_a_style_answer_with_a_respelling(self):
        answer = '{"text": "[reverently] Job had such a beautiful expression, saying:", ' \
                 '"say": [{"word": "Job", "say": "Jobe"}], "why": "name"}'
        self.assertEqual(vd._styled(self.CLIP, answer, True),
                         ("reverently", "Jobe had such a beautiful expression, saying:"))

    def test_a_changed_word_in_the_text_still_turns_the_answer_away(self):
        answer = '{"text": "Jobe had such a beautiful expression, saying:", "say": [], "why": ""}'
        self.assertIsNone(vd._styled(self.CLIP, answer, True))


class BackendTest(unittest.TestCase):
    def test_claude_with_an_anthropic_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}):
            self.assertEqual(vd.backend(), "claude")

    def test_luna_without_one(self):
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["OPENAI_API_KEY"] = "o"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(vd.backend(), "luna")

    def test_nobody_without_keys(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(vd.backend())

    def test_off(self):
        with mock.patch.object(vd, "ON", False), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a"}):
            self.assertIsNone(vd.backend())


class DirectorTest(unittest.TestCase):
    def test_the_answer_for_the_sentence_asked(self):
        d = vd.VoiceDirector()
        d.said("Sunday at church is beautiful.")
        seen = {}

        def ask(said, first):
            seen["said"], seen["first"] = said, first
            return "earnestly"
        with mock.patch.object(vd, "backend", return_value="claude"), mock.patch.object(vd, "_ask_claude", ask):
            d.begin("s1", "To whom do we give")
            self.assertEqual(d.tag("s1", 2.0), "earnestly")
        self.assertEqual(seen, {"said": ["Sunday at church is beautiful."], "first": "To whom do we give"})

    def test_luna_is_asked_without_an_anthropic_key(self):
        d = vd.VoiceDirector()
        with mock.patch.object(vd, "backend", return_value="luna"), \
                mock.patch.object(vd, "_ask_luna", return_value="warmly") as luna, \
                mock.patch.object(vd, "_ask_claude") as claude:
            d.begin("s1", "Dear candidates,")
            self.assertEqual(d.tag("s1", 2.0), "warmly")
        luna.assert_called_once()
        claude.assert_not_called()

    def test_a_slow_answer_gives_no_tag(self):
        d = vd.VoiceDirector()
        gate = threading.Event()

        def slow(said, first):
            gate.wait(2.0)
            return "solemnly"
        with mock.patch.object(vd, "backend", return_value="claude"), mock.patch.object(vd, "_ask_claude", slow):
            d.begin("s1", "But then comes Monday")
            self.assertIsNone(d.tag("s1", 0.05))
            gate.set()

    def test_a_failing_call_gives_no_tag(self):
        d = vd.VoiceDirector()
        with mock.patch.object(vd, "backend", return_value="claude"), \
                mock.patch.object(vd, "_ask_claude", side_effect=RuntimeError("overloaded")):
            d.begin("s1", "Is Jesus in our hearts?")
            self.assertIsNone(d.tag("s1", 2.0))

    def test_a_clip_is_styled_with_the_engines_notes(self):
        d = vd.VoiceDirector()
        d.said("Sunday at church is beautiful.")
        seen = {}

        def claude(system, user, schema):
            seen["system"], seen["user"] = system, user
            return '{"text": "To whom do we give FIRST place?", "why": "the key word"}'
        with mock.patch.object(vd, "backend", return_value="claude"), mock.patch.object(vd, "_claude", claude):
            d.begin_style("c1", "To whom do we give first place?", "soniox", True)
            self.assertEqual(d.styled("c1", 2.0), (None, "To whom do we give FIRST place?"))
        self.assertIn("Soniox v2", seen["system"])
        self.assertIn("Sunday at church is beautiful.", seen["user"])
        self.assertIn("it begins a sentence", seen["user"])

    def test_a_styling_that_changes_the_words_is_turned_away(self):
        d = vd.VoiceDirector()
        with mock.patch.object(vd, "backend", return_value="claude"), \
                mock.patch.object(vd, "_claude", return_value='{"text": "Who comes first?", "why": "shorter"}'):
            d.begin_style("c1", "To whom do we give first place?", "elevenlabs", True)
            self.assertIsNone(d.styled("c1", 2.0))

    def test_luna_styles_without_an_anthropic_key(self):
        d = vd.VoiceDirector()
        with mock.patch.object(vd, "backend", return_value="luna"), \
                mock.patch.object(vd, "_luna", return_value='{"text": "[warmly] Dear candidates,", "why": "x"}') as luna:
            d.begin_style("c1", "Dear candidates,", "elevenlabs", True)
            self.assertEqual(d.styled("c1", 2.0), ("warmly", "Dear candidates,"))
        self.assertIn("ElevenLabs v4", luna.call_args[0][0])

    def test_never_asked_is_no_tag(self):
        self.assertIsNone(vd.VoiceDirector().tag("nobody", 0.0))

    def test_no_backend_asks_nobody(self):
        d = vd.VoiceDirector()
        with mock.patch.object(vd, "backend", return_value=None), mock.patch.object(vd, "_ask_claude") as c:
            d.begin("s1", "It began on a Sunday,")
            self.assertIsNone(d.tag("s1", 0.1))
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
