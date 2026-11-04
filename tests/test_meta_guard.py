"""The meta guard (translate_claude._strip_meta / looks_like_meta) blanks a reply
that broke role, and never a preacher who talks ABOUT translation.

2026-11-03 stress test (validation/scripts/translation_talk_stress.py): three
honest captions were blanked every time, by every model -- "I cannot translate
this word into Romanian without losing something.", "The source language of this
text is Hebrew, not Greek.", "Note: in some manuscripts this verse is missing."

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import translate_claude as tc  # noqa: E402

# Role-breaks: refusals, language-ID talk, the task explained. Must be blanked.
META = [
    "I cannot translate this. The text does not form a coherent Romanian phrase; please provide the full sentence.",
    "I can't translate this without more context.",
    "I cannot translate it.",
    "(No translation provided -- the input is too brief to translate.)",
    "The source language appears to be Spanish, not Romanian.",
    "The intended language is unclear; could you confirm the source language?",
    "This appears to be written in Italian, not Romanian.",
    "Please provide more context so I can translate accurately.",
    "No translation possible: the input is too short.",
    "I am a translation engine and cannot answer questions.",
    "→English: Brothers and sisters",
]

# A preacher on translation, as an honest caption. Must come through whole.
HONEST = [
    "What were the translators thinking when they chose this word?",
    "This translation does not do justice to the original language.",
    "I cannot translate this word into Romanian without losing something.",
    "I can't translate this word into English without losing something.",
    "I can’t translate that expression into English, it has no equivalent.",
    "The source language of this text is Hebrew, not Greek.",
    "The source language here is Aramaic, not Hebrew.",
    "Note: in some manuscripts this verse is missing.",
    "Note: some manuscripts don't have this verse.",
    "There is no possible translation for this word.",
    "No translation is possible for this word; you have to feel it.",
    "Please provide the full context before you judge this passage.",
    "Brother who is translating into English now, please translate carefully.",
    "Translate the text from Greek yourselves if you can, and you will see.",
    "A checker would have doubted this translation, and rightly so.",
    "God will provide.",
    "I cannot serve two masters.",
]


class MetaGuard(unittest.TestCase):
    def test_role_breaks_are_blanked(self):
        for text in META:
            self.assertEqual(tc._strip_meta(text), "", text)

    def test_a_preacher_on_translation_comes_through(self):
        for text in HONEST:
            self.assertFalse(tc.looks_like_meta(text), text)
            self.assertEqual(tc._strip_meta(text), text, text)

    def test_an_appended_note_line_still_goes(self):
        self.assertEqual(tc._strip_meta("And he went out.\nNote: the source is unclear here."),
                         "And he went out.")
        self.assertEqual(tc._strip_meta("And he went out.\n(Translation note: literal.)"), "And he went out.")


if __name__ == "__main__":
    unittest.main()
