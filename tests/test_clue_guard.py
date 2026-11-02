"""The Router's clue never reaches a caption (translate_claude.clue_at).

A segment the Router sends on carries FLAGGED_RULE in its user turn; a model
that copies the block into its reply must not put any of it on the wall, in the
voice, on the website or in the history -- not even its first word, mid-stream.

Run from the repo root, with the app's Python (the /translate tests need Flask
and are skipped without it; no key is read and no provider is called):

    python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import translate_claude as tc  # noqa: E402

# Live: recordings/session-2026-10-28_18-04-17/session.json, row 26 -- "Nu.",
# sent on as "misheard", answered by gpt-6.1-terra; this was the caption.
LIVE = {"text": "Nu.", "flagged": {"en": "No.", "why": "misheard"}, "caption": "No.",
        "reply": "No. A fast machine translator rendered this same segment as:\nNo.\n"
                 "A checker doubted it: a word in the transcript may be a mis-hearing of a "
                 "similar-sounding word. Treat it only as a clue. Where the transcript above "
                 "is garbled it may show what the speaker meant; it may also carry the very "
                 "error that was flagged. Translate the segment yourself, and never keep a "
                 "wording of it you would not have chosen."}

# Reproduced 2026-10-29 on a fresh server with no history, gpt-6.1-terra-fast:
# the translation, then the whole clue block as the user turn carried it.
_EN = "Brothers, the Lord is my shepherd, I shall not want."
REPRO = {"text": "Fraților, Domnul este păstorul meu, nu voi duce lipsă de nimic.",
         "flagged": {"en": _EN, "why": "translation"},
         "caption": "Brothers, the Lord is my shepherd; I shall not want.",
         "reply": "Brothers, the Lord is my shepherd; I shall not want.\n\n"
                  + tc.FLAGGED_RULE.format(en=_EN, why=tc.FLAGGED_WHY["translation"])}

# Captions that must come through exactly as they are.
ORDINARY = [
    "A fast car stopped outside the church.",
    "A man said: no.",
    "And the angel said to her: A",
    "A fast, a machine, a translator.",
    "He was a fast runner; the machine rendered nothing.",
    "Translator's note is not this. A fast machine",
    "Iar Domnul a fost cu el.",
    "a fast machine translated it",
    "No.",
    "",
]


def chunkings(text):
    """The reply as a stream might deliver it: a character at a time, in
    fixed-size pieces, and a word (with its space) at a time."""
    yield list(text)
    for n in (2, 3, 5, 8, 13):
        yield [text[i:i + n] for i in range(0, len(text), n)]
    words = text.split(" ")
    yield [w + " " for w in words[:-1]] + words[-1:]


def streamed(chunks):
    """What the server would show after each chunk: the reply, stopped where
    the clue begins, with any tail that may be its first words held back."""
    full, shown = "", []
    for c in chunks:
        full += c
        shown.append(full[:tc.clue_at(full, final=False)])
    shown.append(full[:tc.clue_at(full)])
    return shown


class ClueAt(unittest.TestCase):
    def test_the_clue_starts_with_the_words_cut_at(self):
        self.assertTrue(tc.FLAGGED_RULE.startswith(tc.CLUE_HEAD))
        turn = tc.anchored_turn("→English:", LIVE["text"], LIVE["flagged"])
        self.assertIn("\n\n" + tc.CLUE_HEAD + " this same segment as:\n  No.\n", turn)

    def test_final_reply_keeps_only_the_translation(self):
        for case in (LIVE, REPRO):
            reply = case["reply"]
            self.assertEqual(reply[:tc.clue_at(reply)].strip(), case["caption"])

    def test_strip_meta_cuts_it_for_every_backend(self):
        # every backend's non-streaming translate() finishes in _strip_meta
        for case in (LIVE, REPRO):
            self.assertEqual(tc._strip_meta(case["reply"]), case["caption"])

    def test_no_streamed_prefix_shows_any_of_the_clue(self):
        for case in (LIVE, REPRO):
            for chunks in chunkings(case["reply"]):
                shown = streamed(chunks)
                for a, b in zip(shown, shown[1:]):
                    self.assertTrue(b.startswith(a), (a, b))      # only ever grows
                for s in shown:                                   # ...and never past the caption
                    self.assertTrue(case["caption"].startswith(s.rstrip()), s)
                self.assertEqual(shown[-1].strip(), case["caption"])

    def test_case_and_spacing(self):
        reply = "no.\n\na  fast machine\ttranslator Rendered this same segment as: no."
        self.assertEqual(reply[:tc.clue_at(reply)].strip(), "no.")

    def test_a_clue_that_opens_the_reply_leaves_nothing(self):
        reply = tc.FLAGGED_RULE.format(en="No.", why="x") + "\n\nNo."
        self.assertEqual(tc.clue_at(reply), 0)

    def test_ordinary_captions_pass_untouched(self):
        for text in ORDINARY:
            self.assertEqual(tc.clue_at(text), len(text), text)
            self.assertEqual(tc._strip_meta(text), text.strip(), text)
            for chunks in chunkings(text):
                shown = streamed(chunks)
                for a, b in zip(shown, shown[1:]):
                    self.assertTrue(b.startswith(a), (text, a, b))
                self.assertEqual(shown[-1], text)

    def test_a_tail_like_the_clue_waits_a_word_at_most(self):
        self.assertEqual("No. A"[:tc.clue_at("No. A", final=False)], "No. ")
        self.assertEqual("No. A fast "[:tc.clue_at("No. A fast ", final=False)], "No. ")
        self.assertEqual(tc.clue_at("No. A fast car", final=False), len("No. A fast car"))
        self.assertEqual(tc.clue_at("No. A", final=True), len("No. A"))


@unittest.skipUnless(importlib.util.find_spec("flask"), "needs the app's Python (Flask)")
class TranslateEndpoint(unittest.TestCase):
    """POST /translate with a stand-in translator that copies the clue: the
    stream, the final, the history and the session record."""

    @classmethod
    def setUpClass(cls):
        # No keys, so nothing is warmed or billed; no browser, no files.
        env = {k: "" for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
                               "GEMINI_API_KEY", "SONIOX_API_KEY", "ELEVENLABS_API_KEY")}
        env.update(CT_OPEN="0", CT_SESSION_SAVE="0")
        with mock.patch.dict(os.environ, env):
            sys.path.insert(0, os.path.join(ROOT, "app"))
            import server
        cls.server = server
        cls.client = server.app.test_client()

    def post(self, case, reply, chunk=3, flagged=True):
        sent = {}

        def model(text, draft=None, **_):
            sent["draft"] = draft
            for i in range(0, len(reply), chunk):
                yield reply[i:i + chunk]

        s = self.server
        s.HISTORY.clear()
        with mock.patch.object(s, "_provider_for", lambda m: model), \
                mock.patch.object(s, "HAS_OPENAI_KEY", True):
            r = self.client.post("/translate", json={
                "text": case["text"], "target_lang": "en", "model": "gpt-6.1-terra-fast",
                "tts": False, **({"flagged": case["flagged"]} if flagged else {})})
            lines = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
        return lines, sent

    def test_the_clue_never_reaches_the_caption(self):
        s = self.server
        for case in (LIVE, REPRO):
            for chunk in (1, 3, 7):
                n0 = len(s.SERVER_WARNINGS)
                lines, sent = self.post(case, case["reply"], chunk)
                self.assertEqual(sent["draft"]["en"], case["flagged"]["en"])   # the clue went out
                deltas = "".join(m["text"] for m in lines if m["type"] == "delta")
                final = [m for m in lines if m["type"] == "final"][-1]
                self.assertEqual(deltas.rstrip(), case["caption"])   # at most the space before it
                self.assertEqual(final["text"], case["caption"])
                self.assertEqual(final["clue_cut"], len(case["reply"].split()) - len(case["caption"].split()))
                (_, src, tgt, lang), = s.HISTORY["en"]
                self.assertEqual((src, tgt, lang), (case["text"], case["caption"], "en"))
                self.assertEqual(len(s.SERVER_WARNINGS), n0 + 1)
                w = s.SERVER_WARNINGS[-1]
                self.assertEqual((w["level"], w["kind"]), ("warn", "clue"))
                self.assertIn("gpt-6.1-terra-fast", w["msg"])

    def test_the_session_record_carries_the_warning(self):
        s = self.server
        lines, _ = self.post(LIVE, LIVE["reply"])
        at = s.SERVER_WARNINGS[-1]["at"]
        doc = json.loads(s._session_files({"id": "session-2026-10-29_00-00-00",
                                           "opened": at - 1})["session.json"])
        self.assertIn(s.SERVER_WARNINGS[-1], doc["server"]["warnings"])
        later = json.loads(s._session_files({"opened": at + 1})["session.json"])
        self.assertEqual(later["server"]["warnings"], [])         # an earlier page's cut

    def test_a_reply_without_the_clue_is_unchanged(self):
        for flagged in (True, False):
            for text in ORDINARY[:-1]:
                lines, _ = self.post(LIVE, text, chunk=2, flagged=flagged)
                final = [m for m in lines if m["type"] == "final"][-1]
                self.assertEqual("".join(m["text"] for m in lines if m["type"] == "delta"), text)
                self.assertEqual(final["text"], text.strip())
                self.assertNotIn("clue_cut", final)


if __name__ == "__main__":
    unittest.main()
