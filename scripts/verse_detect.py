#!/usr/bin/env python3
"""The Attendant (Luke 4:17-20: he hands over the scroll, and takes it back when
the reading is done). Which Bible verse is in focus right now -- cited, alluded
to, or being read? It names the verse (book, chapter, verse); it ships no Bible
text (what it needs it fetches or has recited, and keeps in memory only) and no
per-language rules, so it works in any language the models read.

Built to keep up with the speaker (2026-11-10, validation/results/
2026-11-10-attendant-speed/) and to show less that he is not reading (2026-11-11,
validation/results/2026-11-11-attendant-polish/, kept on branch
claude/attendant-polish-lab; the lab scripts named below live there, not on master):

  packets  -- Soniox confirms a word ~4.7 s after it is spoken (p50 on a live
              capture) but drafts it within ~0.5 s, so the Packetizer reads the
              draft ("wet") words and sends a small packet every few words: every
              IDLE_WORDS at rest, every FAST_WORDS once on alert or with a passage
              in view (and for as long after the gate last heard Scripture), and
              at once at a pause.
  calls    -- run in parallel (Dispatcher); a stale answer never overrides a newer one.
  gate     -- Luna's Decisions API on every packet (~0.13 s): none, about to
              quote, a citation, a reading, or a request to show it. Anything but
              "none" puts the Attendant on ALERT for ALERT_S.
  reader   -- Sol (Claude Haiku with only an Anthropic key) writes an answer only
              to name a new passage: on a number in the words, when the gate hears
              a citation or a request, or when the tracker says "another passage".
              It says which references he names, which verse he is reading if any
              (and how sure it is), and whether he asked for it on the screen.
  text     -- the verses of a passage in view, held in memory only (TextSource):
              getBible with CT_VERSE_BIBLE, else Luna recites them the way he
              speaks. Asked for the moment the verse is named.
  tracker  -- with the text here, one Luna call a packet (~0.2 s) does the gate
              and the tracking: which of the verses in view are the newest words
              from, his own words ("aside"; "wait" before he has begun), or another
              passage -- and is he still reading? A verse he cited and has yet to
              begin takes less (ARMED_TH). Until the text is here Sol tracks
              without it (~1 s).
  follower -- no call: his newest FOLLOW_WORDS words laid against the text in
              memory, as letters only (Follower). A verse they spell out is read at
              once. In a chapter whose text they have followed lately (FIT_S), a
              verse a model names must be borne out by them, or it is his own words
              about the verse. A text they never follow (another translation,
              another language) vouches for nothing: there the models' word stands.
  blind    -- Opus, on the few packets after a citation that he is not heard to
              read: which verse are these words from? Sure of another passage, he
              named the wrong one ("corrected_from").

What goes on the screen, and for how long:

  green    -- only when he brings the text into focus (names the verse and reads
              it, "let us read", a request); a quote from memory stays amber.
  clearing -- CLEAR_S after his last words read: a pause shows as "paused", and the
              clock starts over only when he moves further on (a later verse,
              further into this one, or a tracker sure he is reading). "Please be
              seated" ends a formal reading at once.
  again    -- a passage that just cleared is remembered for REMEMBER_S, and comes
              back when a second packet goes on with it; nothing is shown on the
              first (one answer alone was wrong more often than right).

The only local data is the shape of the Bible: how many verses each chapter has
(66 books, Protestant order; the Romanian Cornilescu and the KJV agree on all
31,102). It rejects verses that do not exist and turns "ultimele 3 versete" into
18-20.

What a listener follows is one stream of "state" events:

  {"type": "state", "state": "idle" | "alert" | "armed" | "active", "was": ...,
   "ref": "1 Timothy 1:18", "why": ..., "paused": bool, "imminent": bool,
   "corrected_from": "Galatians 6:18" (when he named the wrong passage)}

  idle   -- nothing in view: take it down.
  alert  -- he sounds about to quote or cite Scripture; no passage yet (the pill
            shows just the book glyph).
  armed  -- a passage is in view (cited, a chapter named, "the last 3 verses",
            or a few words that sound like a verse): load it, don't show it.
  active -- he is reading it (the ref follows him verse by verse; "paused"
            through an aside), or he asked for it on the screen: show it.

and, for logs and labs, all with "level" ("weak" | "strong"), "seq" and "t":

  {"type": "citation", "how": "full" | "verse" | "chapter", "ref": "John 1:1", ...}
  {"type": "reading", "state": "allusion" | "start" | "verse" | "pause" | "resume" | "end", "ref": ...,
   "by": "read" | "track" | "text"}     (who heard it: the reader, the tracker, the words against the text)
  {"type": "request", "via": "words" | "llm", "ref": "Genesis 26:2", "spoken": "..."}

    python scripts/verse_detect.py "text of a packet" ["next packet" ...]
"""
import collections
import concurrent.futures as cf
import json
import math
import os
import re
import threading
import time
import unicodedata

BOOKS_EN = ["Genesis", "Exodus", "Leviticus", "Numbers", "Deuteronomy", "Joshua", "Judges", "Ruth",
            "1 Samuel", "2 Samuel", "1 Kings", "2 Kings", "1 Chronicles", "2 Chronicles", "Ezra", "Nehemiah",
            "Esther", "Job", "Psalms", "Proverbs", "Ecclesiastes", "Song of Songs", "Isaiah", "Jeremiah",
            "Lamentations", "Ezekiel", "Daniel", "Hosea", "Joel", "Amos", "Obadiah", "Jonah", "Micah", "Nahum",
            "Habakkuk", "Zephaniah", "Haggai", "Zechariah", "Malachi", "Matthew", "Mark", "Luke", "John", "Acts",
            "Romans", "1 Corinthians", "2 Corinthians", "Galatians", "Ephesians", "Philippians", "Colossians",
            "1 Thessalonians", "2 Thessalonians", "1 Timothy", "2 Timothy", "Titus", "Philemon", "Hebrews",
            "James", "1 Peter", "2 Peter", "1 John", "2 John", "3 John", "Jude", "Revelation"]

# Verses in each chapter, book by book.
_VERSES = json.loads(
    "[[31,25,24,26,32,22,24,22,29,32,32,20,18,24,21,16,27,33,38,18,34,24,20,67,34,35,46,22,35,43,55,32,20"
    ",31,29,43,36,30,23,23,57,38,34,34,28,34,31,22,33,26],[22,25,22,31,23,30,25,32,35,29,10,51,22,31,27,3"
    "6,16,27,25,26,36,31,33,18,40,37,21,43,46,38,18,35,23,35,35,38,29,31,43,38],[17,16,17,35,19,30,38,36,"
    "24,20,47,8,59,57,33,34,16,30,37,27,24,33,44,23,55,46,34],[54,34,51,49,31,27,89,26,23,36,35,16,33,45,"
    "41,50,13,32,22,29,35,41,30,25,18,65,23,31,40,16,54,42,56,29,34,13],[46,37,29,49,33,25,26,20,29,22,32"
    ",32,18,29,23,22,20,22,21,20,23,30,25,22,19,19,26,68,29,20,30,52,29,12],[18,24,17,24,15,27,26,35,27,4"
    "3,23,24,33,15,63,10,18,28,51,9,45,34,16,33],[36,23,31,24,31,40,25,35,57,18,40,15,25,20,20,31,13,31,3"
    "0,48,25],[22,23,18,22],[28,36,21,22,12,21,17,22,27,27,15,25,23,52,35,23,58,30,24,42,15,23,29,22,44,2"
    "5,12,25,11,31,13],[27,32,39,12,25,23,29,18,13,19,27,31,39,33,37,23,29,33,43,26,22,51,39,25],[53,46,2"
    "8,34,18,38,51,66,28,29,43,33,34,31,34,34,24,46,21,43,29,53],[18,25,27,44,27,33,20,29,37,36,21,21,25,"
    "29,38,20,41,37,37,21,26,20,37,20,30],[54,55,24,43,26,81,40,40,44,14,47,40,14,17,29,43,27,17,19,8,30,"
    "19,32,31,31,32,34,21,30],[17,18,17,22,14,42,22,18,31,19,23,16,22,15,19,14,19,34,11,37,20,12,21,27,28"
    ",23,9,27,36,27,21,33,25,33,27,23],[11,70,13,24,17,22,28,36,15,44],[11,20,32,23,19,19,73,18,38,39,36,"
    "47,31],[22,23,15,17,14,14,10,17,32,3],[22,13,26,21,27,30,21,22,35,22,20,25,28,22,35,22,16,21,29,29,3"
    "4,30,17,25,6,14,23,28,25,31,40,22,33,37,16,33,24,41,30,24,34,17],[6,12,8,8,12,10,17,9,20,18,7,8,6,7,"
    "5,11,15,50,14,9,13,31,6,10,22,12,14,9,11,12,24,11,22,22,28,12,40,22,13,17,13,11,5,26,17,11,9,14,20,2"
    "3,19,9,6,7,23,13,11,11,17,12,8,12,11,10,13,20,7,35,36,5,24,20,28,23,10,12,20,72,13,19,16,8,18,12,13,"
    "17,7,18,52,17,16,15,5,23,11,13,12,9,9,5,8,28,22,35,45,48,43,13,31,7,10,10,9,8,18,19,2,29,176,7,8,9,4"
    ",8,5,6,5,6,8,8,3,18,3,3,21,26,9,8,24,13,10,7,12,15,21,10,20,14,9,6],[33,22,35,27,23,35,27,36,18,32,3"
    "1,28,25,35,33,33,28,24,29,30,31,29,35,34,28,28,27,28,27,33,31],[18,26,22,16,20,12,29,17,18,20,10,14]"
    ",[17,17,11,16,16,13,13,14],[31,22,26,6,30,13,25,22,21,34,16,6,22,32,9,14,14,7,25,6,17,25,18,23,12,21"
    ",13,29,24,33,9,20,24,17,10,22,38,22,8,31,29,25,28,28,25,13,15,22,26,11,23,15,12,17,13,12,21,14,21,22"
    ",11,12,19,12,25,24],[19,37,25,31,31,30,34,22,26,25,23,17,27,22,21,21,27,23,15,18,14,30,40,10,38,24,2"
    "2,17,32,24,40,44,26,22,19,32,21,28,18,16,18,22,13,30,5,28,7,47,39,46,64,34],[22,22,66,22,22],[28,10,"
    "27,17,17,14,27,18,11,22,25,28,23,23,8,63,24,32,14,49,32,31,49,27,17,21,36,26,21,26,18,32,33,31,15,38"
    ",28,23,29,49,26,20,27,31,25,24,23,35],[21,49,30,37,31,28,28,27,27,21,45,13],[11,23,5,19,15,11,16,14,"
    "17,15,12,14,16,9],[20,32,21],[15,16,15,13,27,14,17,14,15],[21],[17,10,10,11],[16,13,12,13,15,16,20],"
    "[15,13,19],[17,20,19],[18,15,20],[15,23],[21,13,10,14,11,15,14,23,17,12,17,14,9,21],[14,17,18,6],[25"
    ",23,17,25,48,34,29,34,38,42,30,50,58,36,39,28,27,35,30,34,46,46,39,51,46,75,66,20],[45,28,35,41,43,5"
    "6,37,38,50,52,33,44,37,72,47,20],[80,52,38,44,39,49,50,56,62,42,54,59,35,35,32,31,37,43,48,47,38,71,"
    "56,53],[51,25,36,54,47,71,53,59,41,42,57,50,38,31,27,33,26,40,42,31,25],[26,47,26,37,42,15,60,40,43,"
    "48,30,25,52,28,41,40,34,28,41,38,40,30,35,27,27,32,44,31],[32,29,31,25,21,23,25,39,33,21,36,21,14,23"
    ",33,27],[31,16,23,21,13,20,40,13,27,33,34,31,13,40,58,24],[24,17,18,18,21,18,16,24,15,18,33,21,14],["
    "24,21,29,31,26,18],[23,22,21,32,33,24],[30,30,21,23],[29,23,25,18],[10,20,13,18,28],[12,17,18],[20,1"
    "5,16,16,25,21],[18,26,17,22],[16,15,15],[25],[14,18,19,16,14,20,28,13,28,39,40,29,25],[27,26,18,17,2"
    "0],[25,25,22,19,14],[21,22,18],[10,29,24,21,21],[13],[14],[25],[20,29,22,11,14,17,17,13,21,11,19,17,"
    "18,20,8,21,18,24,21,15,27,21]]")

# The model that names passages (reader()): CT_VERSE_MODEL, else OpenAI's Sol when
# there is an OpenAI key (as fast as Haiku, ~1.1 s, and it knows more Scripture:
# 73% of 83 uncited readings named exactly, Haiku 65%; recall_lab.py), else Claude
# Haiku with only an Anthropic key.
MODEL = os.environ.get("CT_VERSE_MODEL") or (
    "gpt-6-sol" if os.environ.get("OPENAI_API_KEY") else
    "claude-haiku-5-5" if os.environ.get("ANTHROPIC_API_KEY") else "")
# The blind check (blind()): CT_VERSE_BLIND_MODEL, else Claude Opus with an
# Anthropic key. It is rare (a few packets after a citation) and nobody waits on
# it, and Opus named 82% of those readings exactly (2.4 s). Else MODEL.
BLIND_MODEL = os.environ.get("CT_VERSE_BLIND_MODEL") or (
    "claude-opus-5-5" if os.environ.get("ANTHROPIC_API_KEY") else MODEL)
DECIDE_MODEL = os.environ.get("CT_VERSE_GATE_MODEL", "gpt-6-luna")   # gate and tracker
MEMORY_TTL_S = 45 * 60      # named this long ago, it no longer explains a bare "verse 4"
MEMORY_MAX = 16
ARM_TTL_S = 90              # a cited verse counts as "cited" for a reading this long
# A strong reading that stops is PAUSED, then ended (the screen clears) CLEAR_S
# after the last words he read (Jo: "when the speaker is done, disarm within 10
# seconds"), or at once when he invites the congregation to sit down. Preachers
# break off for an aside and pick the passage up again: for REMEMBER_S after it
# ends, going on with it (from the verse before where he started to three past
# where he was) brings it straight back, with no need to name it again. A
# different passage ends it too. PAUSE_S of silence pauses it.
CLEAR_S, REMEMBER_S, PAUSE_S = 10.0, 60.0, 8.0
ARMED_TTL_S = 90.0          # armed, then nothing about it this long: back to idle
ALERT_S = 10.0              # on alert this long after the gate last heard Scripture coming
PENDING_S = 15.0            # a chapter and verse waits this long for its book
REQUEST_GAP_S = 15.0
REQUEST_WORDS = [w.strip() for w in os.environ.get("CT_VERSE_REQUEST_WORDS", "").split(",") if w.strip()]
# The reader's confidence that it named the verse exactly (recall_lab.py: at 0.8 or
# more, Haiku was within a verse 88% of the time). Below LOW_CONF an uncited
# reading is ignored; at CORRECT_CONF or more, a reading of a different passage
# within CORRECT_S of a citation means he named the wrong one.
LOW_CONF, CORRECT_CONF, CORRECT_S = 0.4, 0.7, 45.0
CORRECT_CHUNKS = 4          # the blind check runs on at most this many packets after a citation
GATE_TH = 0.3               # the gate passes a packet when P(none) < 1 - GATE_TH
TRACK_TH = 0.5              # the tracker's verse counts when its probability is at least this,
ARMED_TH = 0.4              # or this for a verse he cited and has yet to begin (wait_lab.py)
# Packets: new words each (draft words included), the longest a packet waits for
# them, and the words of context before it.
IDLE_WORDS, FAST_WORDS, IDLE_MS, FAST_MS, CTX_WORDS = 6, 3, 3000, 1500, 30
# Idle, a packet rides the Router's cadence (CT_VERSE_IDLE=segment, the default):
# one a clause -- the new words end in punctuation, at least IDLE_WORDS of them --
# or at once on a number ("Efeseni 1:"), at a pause, at SEG_WORDS, or after SEG_MS.
# Draft words, so no wait for Soniox to confirm them. "words": every IDLE_WORDS.
IDLE_BY = (os.environ.get("CT_VERSE_IDLE") or "segment").lower()
SEG_WORDS, SEG_MS = 20, 6000
READ_WORDS = 14             # the reader sees the last this many words as "newest"; three are too few
MAX_READS = 2               # reader calls in flight at once
READ_EARLY = (os.environ.get("CT_VERSE_READ_EARLY") or "digits").lower()   # see _want_read


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def keys(text):
    """Folded words, for matching the church's request phrases."""
    return [p for w in re.findall(r"[\w'-]+", fold(text)) for p in re.split(r"['_-]", w) if p]


class Canon:
    """The shape of the Bible: books, chapters, verse counts. No text."""

    def __init__(self):
        self.chapters = _VERSES
        self.at, self.where = {}, []          # (b, c, v) <-> ordinal
        for b, book in enumerate(self.chapters):
            for c, n in enumerate(book, 1):
                for v in range(1, n + 1):
                    self.at[(b, c, v)] = len(self.where)
                    self.where.append((b, c, v))

    def has(self, b, c, v=None):
        if b is None or not (0 <= b < 66 and c and 1 <= c <= len(self.chapters[b])):
            return False
        return v is None or 1 <= v <= self.chapters[b][c - 1]

    def ref(self, b, c=None, v=None, v2=None):
        if c is None:
            return BOOKS_EN[b]
        if v is None:
            return f"{BOOKS_EN[b]} {c}"
        return f"{BOOKS_EN[b]} {c}:{v}" + (f"-{v2}" if v2 and v2 != v else "")

    def ref_of(self, i):
        return self.ref(*self.where[i])


# ---- the models ---------------------------------------------------------------

_SYSTEM = (
    "You follow a live speech transcript of a church service, a few words at a time, in any language. "
    "For the NEWEST words, report three things.\n"
    "refs: Bible references the speaker says aloud in the newest words: a book with a chapter and verse "
    "(\"John 1:1\", \"Faptele apostolilor, capitolul 27, versetul 20\"), a book and chapter (\"Psalm 23\"), "
    "a verse alone that continues a chapter named before (\"verse 4\", \"versetul întâi\"), or a book named "
    "alone (\"in the Gospel of John\"). Earlier words are context only: a reference split across them "
    "counts when its last part is in the newest words; one already complete before them does not. Write "
    "numbers as digits whether spoken as words or digits. \"verses 16 to 18\" (\"24, la 27\", \"24 până la "
    "27\") gives verse 16 and verse_end 18; \"the first 3 verses\" gives verse 1 and verse_end 3; \"the last 3 verses\" (\"ultimele 3 "
    "versete\") gives last_n 3 with verse null (otherwise last_n is null). Clock times, dates, counts, hymn "
    "numbers and page numbers are not references, and neither is a quotation he does not give a reference "
    "for. A person's name alone (\"Ioan\", \"Iacov\" as people) is not a book. Set book and chapter only "
    "when the speaker says them in these words; for a verse alone leave them null. reading_now: true only "
    "when he is reading that passage aloud or is about to read it right away (\"let us read\", \"vom "
    "citi\", \"it says:\", the reference followed by its text).\n"
    "reading: if in the newest words he is reading or reciting the words of a Bible verse aloud (word for "
    "word or nearly, in any translation), the verse they come from; null if he is speaking in his own "
    "words, retelling, or you cannot tell which verse. The passage in view, if any, is the likeliest "
    "source, and a reading usually goes on to the next verse. But preachers sometimes name the wrong "
    "book or chapter (\"in Galatians\" for words from Ephesians): when the words come from a different "
    "verse than the one he named or the one in view, name the verse they actually come from -- a "
    "mismatch is never a reason for null. confidence: from 0 to 1, that this is exactly the verse.\n"
    "show_request: true when, in the newest words, he asks for a Bible text to be put on the screen or "
    "projected (\"if I could get this verse projected\", \"dacă se poate afișa Geneza 26 cu 2\"), whether or "
    "not he names it.")

_BOOK = {"anyOf": [{"type": "string", "enum": BOOKS_EN}, {"type": "null"}]}
_INT = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
_SCHEMA = {
    "type": "object",
    "properties": {
        "refs": {"type": "array", "items": {
            "type": "object",
            "properties": {"book": _BOOK, "chapter": _INT, "verse": _INT, "verse_end": _INT, "last_n": _INT,
                           "reading_now": {"type": "boolean"}},
            "required": ["book", "chapter", "verse", "verse_end", "last_n", "reading_now"],
            "additionalProperties": False}},
        "reading": {"anyOf": [{"type": "null"}, {
            "type": "object",
            "properties": {"book": {"type": "string", "enum": BOOKS_EN}, "chapter": {"type": "integer"},
                           "verse": {"type": "integer"}, "confidence": {"type": "number"}},
            "required": ["book", "chapter", "verse", "confidence"],
            "additionalProperties": False}]},
        "show_request": {"type": "boolean"}},
    "required": ["refs", "reading", "show_request"],
    "additionalProperties": False}

_client = None
_oa_session = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(timeout=8.0, max_retries=1)
    return _client


def _openai():
    global _oa_session
    if _oa_session is None:
        import requests
        _oa_session = requests.Session()
    return _oa_session


def _user_text(context, newest, note):
    return note + "\n\nEarlier words:\n" + (context or "(none)") + "\n\nNEWEST words:\n" + newest


def _answer(out):
    return {"refs": out.get("refs") or [], "reading": out.get("reading"),
            "show_request": bool(out.get("show_request"))}


def _bill(model, prompt, cached, output, line):
    try:
        import costs
        costs.llm(model, prompt=prompt, cached=cached, output=output, line=line)
    except Exception:
        pass


def _claude_json(model, system, user, schema, max_tokens):
    kw = {"thinking": {"type": "disabled"}} if "haiku" in model else {}    # as recall_lab.py measured them
    r = _anthropic().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}], **kw)
    u = r.usage
    _bill(model, u.input_tokens, u.cache_read_input_tokens or 0, u.output_tokens, "Verses")
    if r.stop_reason == "refusal":
        return None
    return json.loads(next((b.text for b in r.content if b.type == "text"), "null"))


def _openai_json(model, system, user, schema, max_tokens):
    r = _openai().post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={"model": model, "reasoning_effort": "none", "max_completion_tokens": max_tokens,
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
              "response_format": {"type": "json_schema", "json_schema": {
                  "name": "bible_focus", "strict": True, "schema": schema}}},
        timeout=(5, 8))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    _bill(model, u.get("prompt_tokens") or 0, (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
          u.get("completion_tokens") or 0, "Verses")
    msg = j["choices"][0]["message"]
    return None if msg.get("refusal") else json.loads(msg.get("content") or "null")


def _json_call(model=None):
    """fn(system, user, schema, max_tokens) for `model` (default MODEL), or None (no key)."""
    model = model or MODEL
    if model.startswith("claude") and os.environ.get("ANTHROPIC_API_KEY"):
        return lambda *a: _claude_json(model, *a)
    if model.startswith("gpt") and os.environ.get("OPENAI_API_KEY"):
        return lambda *a: _openai_json(model, *a)
    return None


def reader():
    """The reader for MODEL -> fn(context, newest, note) -> answer, or None (no key)."""
    call = _json_call()
    if call is None:
        return None
    return lambda context, newest, note: _answer(
        call(_SYSTEM, _user_text(context, newest, note), _SCHEMA, 400) or {})


# The blind check: just the spoken words, no citation and nothing in view. With a
# citation in view, the reader tends to abstain rather than contradict him when he
# reads from another book than he named ("Galateni 6:18", then Ephesians 6:18:
# Haiku named nothing 4 times of 4); asked blind, the models name such verses
# readily (recall_lab.py).
_BLIND = ("A preacher is reading aloud from the Bible in a church service. You get a speech recogniser's "
          "transcript of what he said (it may contain mis-heard words), in any language, with no reference "
          "given. If the words are Bible text, name the verse they come from: book, chapter and verse (where "
          "they run over two verses, the first), and your confidence from 0 to 1 that it is exactly right. If "
          "they are not Bible text, give your best guess with confidence 0.")
_BLIND_SCHEMA = {"type": "object", "properties": {
    "book": {"type": "string", "enum": BOOKS_EN}, "chapter": {"type": "integer"},
    "verse": {"type": "integer"}, "confidence": {"type": "number"}},
    "required": ["book", "chapter", "verse", "confidence"], "additionalProperties": False}


def blind():
    """The blind check with BLIND_MODEL -> fn(text) -> {book, chapter, verse, confidence}, or None."""
    call = _json_call(BLIND_MODEL) or _json_call()
    if call is None:
        return None
    return lambda text: call(_BLIND, "Transcript:\n" + text, _BLIND_SCHEMA, 200)


def _decide(text, question):
    """One choice question to Luna's Decisions API -> {choice: probability}."""
    r = _openai().post("https://api.openai.com/v1/decisions",
                       headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                       json={"model": DECIDE_MODEL, "input": text, "questions": [question]}, timeout=(5, 3))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    _bill(DECIDE_MODEL, u.get("input_tokens") or 0, (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
          u.get("output_tokens") or 0, "Verses gate")
    return {p["value"]: p["probability"] for p in j["answers"][0].get("probabilities") or []}


def _lines(context, newest):
    return (f"[earlier] {context}\n" if context else "") + f"[NEWEST] {newest}"


# The gate: Jo's ALERT question folded into the earlier one (none / citation /
# reading / request), so it stays one ~0.13 s call a packet.
GATE = (os.environ.get("CT_VERSE_GATE") or ("luna" if os.environ.get("OPENAI_API_KEY") else "off")).lower()
GATE_Q = {"type": "choice", "name": "s", "instructions":
          "A sermon, transcribed live by a speech recogniser, a few words at a time, in any language. "
          "Judge the NEWEST words (the earlier ones are context).",
          "choices": [
              {"value": "none", "description": "none: no Bible reference, no Bible text, nothing leading into one"},
              {"value": "about", "description": "about: he is about to quote or read Scripture -- \"Biblia "
               "spune:\", \"scrie așa:\", \"Pavel zice:\", \"să citim\", \"the Lord says:\""},
              {"value": "citation", "description": "citation: it names or points to a Bible passage -- a book, "
               "chapter or verse, whole or in part (\"John 3:16\", \"capitolul 5\", \"versetul 4\", \"the last "
               "three verses\", \"from the same book\"), including the end of a reference begun earlier"},
              {"value": "reading", "description": "reading: he reads or recites Bible text, word for word or "
               "nearly"},
              {"value": "request", "description": "request: he asks for a Bible text to be put on the screen "
               "or projected"}]}


# Jo's question, asked in the same Decisions call as the gate (no extra time): on
# ALERT, the reader is asked to name a verse only once there is enough to go on.
ENOUGH_Q = {"type": "predicate", "name": "enough", "instructions":
            "A sermon, transcribed live, a few words at a time. Taken together, do the newest words and the "
            "earlier ones hold enough Bible text (or a clear enough reference) to tell which verse it is?"}


# Jo's magic words: green (active) is for a text he deliberately brings into focus
# -- "versetul 4", "să citim", "I will read", "scrie așa:" -- or asks to have shown.
# A passage named or quoted in passing stays amber. Asked in the same call.
FOCUS = (os.environ.get("CT_VERSE_FOCUS") or "on").lower() in ("on", "1", "yes")
FOCUS_TH, FOCUS_S = 0.5, 30.0       # how sure, and how long a cue keeps a reading green
FOCUS_Q = {"type": "predicate", "name": "focus", "instructions":
           "A sermon, transcribed live by a speech recogniser, a few words at a time, in any language. In the "
           "NEWEST words or just before them, does he deliberately bring a particular Bible text into focus? Yes "
           "when he names the verse or verses (a verse number, or chapter and verse: \"versetul 4\", \"Ioan "
           "8:47\", \"Romans 12 verse 1\") and goes on to read or quote it, when he announces that he will read "
           "(\"să citim\", \"citesc din\", \"I will read\", \"let us look at verse 12\"), or when he asks for "
           "it to be shown. No when he quotes Scripture from memory without saying which verse (\"Biblia spune: "
           "...\", \"the Lord said: ...\"), or names a book or chapter in passing as part of his own sentence."}


# Jo: a formal reading (everyone standing) clears the screen when they sit down.
SEATED = (os.environ.get("CT_VERSE_SEATED") or "on").lower() in ("on", "1", "yes")
SEATED_Q = {"type": "predicate", "name": "seated", "instructions":
            "A sermon, transcribed live by a speech recogniser, a few words at a time, in any language. In the "
            "NEWEST words, does he invite the congregation to sit down, or tell them they may be seated "
            "(\"vă invit să vă reașezați\", \"puteți lua loc\", \"you may be seated\")?"}


ALL_ASKED = frozenset(("enough", "focus", "seated"))


def _questions(asked=ALL_ASKED):
    """The per-packet questions, each only where its answer is used (VerseDetector._asked)."""
    return [GATE_Q] + ([ENOUGH_Q] if "enough" in asked else []) + \
        ([FOCUS_Q] if FOCUS and "focus" in asked else []) + ([SEATED_Q] if SEATED and "seated" in asked else [])


def gate_luna(context, newest, asked=ALL_ASKED):
    r = _openai().post("https://api.openai.com/v1/decisions",
                       headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                       json={"model": DECIDE_MODEL, "input": _lines(context, newest),
                             "questions": _questions(asked)}, timeout=(5, 3))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    _bill(DECIDE_MODEL, u.get("input_tokens") or 0, (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
          u.get("output_tokens") or 0, "Verses gate")
    out = {}
    for a in j["answers"]:
        if a["type"] == "choice":
            out.update({p["value"]: p["probability"] for p in a.get("probabilities") or []})
        elif a["type"] == "predicate" and "probability" in a:    # not a refusal
            out["_" + a["name"]] = a["probability"]
    return out


# The tracker, once a passage is known: which verse are the newest words from?
# A written answer, because the speed lab's tracker test (tracker_lab.py: 355
# three-word packets of 16 readings) found Luna's Decisions API fast (0.13 s) but
# unsure where Romanian verses end -- 12-23 of 33 verse changes caught, 26-101
# early moves -- while Sol caught 26 of 33 on the packet with no early move
# (0.95 s; Terra the same, Haiku 6 of 33, Luna written 12). CT_VERSE_TRACK_MODEL.
TRACK_MODEL = os.environ.get("CT_VERSE_TRACK_MODEL") or (
    "gpt-6-sol" if os.environ.get("OPENAI_API_KEY") else MODEL)
_TRACK_SCHEMA = {"type": "object", "properties": {"verse": {"anyOf": [{"type": "integer"}, {"type": "null"}]}},
                 "required": ["verse"], "additionalProperties": False}


def track_written(context, newest, intro, ref):
    """-> {"verse": n | None (his own words)}."""
    system = ("You follow a live speech transcript of a preacher in church, a few words at a time, in any "
              f"language. {intro} Which verse of {ref} are the NEWEST words from? null if they are his own words "
              "(a comment, an explanation), not Bible text.")
    if TRACK_MODEL.startswith("claude"):
        r = _anthropic().messages.create(
            model=TRACK_MODEL, max_tokens=50, system=system, thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _TRACK_SCHEMA}},
            messages=[{"role": "user", "content": _lines(context, newest)}])
        u = r.usage
        _bill(TRACK_MODEL, u.input_tokens, u.cache_read_input_tokens or 0, u.output_tokens, "Verses")
        return json.loads(next((b.text for b in r.content if b.type == "text"), "null"))
    r = _openai().post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        json={"model": TRACK_MODEL, "reasoning_effort": "none", "max_completion_tokens": 50,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": _lines(context, newest)}],
              "response_format": {"type": "json_schema", "json_schema": {
                  "name": "verse", "strict": True, "schema": _TRACK_SCHEMA}}},
        timeout=(5, 6))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    _bill(TRACK_MODEL, u.get("prompt_tokens") or 0, 0, u.get("completion_tokens") or 0, "Verses")
    return json.loads(j["choices"][0]["message"].get("content") or "null")


def tracker():
    if TRACK_MODEL.startswith("claude") and os.environ.get("ANTHROPIC_API_KEY"):
        return track_written
    if TRACK_MODEL.startswith("gpt") and os.environ.get("OPENAI_API_KEY"):
        return track_written
    return None


# ---- the verse text, for the one-call tracker ------------------------------------
# Given the verses' text as its choices, Luna's Decisions API tracks verse changes
# as well as Sol from memory (tracker_lab: 26-28 of 33 caught on the packet,
# against Sol's 26) in 0.15 s instead of ~1 s. The text is kept in memory for the
# service only. Where it comes from (recite_lab.py):
#   getBible (CT_VERSE_BIBLE, a getBible translation such as "cornilescu"): a
#     chapter as static JSON in 0.3-1.6 s, no key;
#   Luna reciting a few verses from memory (the fallback, or the only source
#     with CT_VERSE_BIBLE unset): ~3 s for four verses, and on 14 chapters 462 of
#     475 verses came out at least 80% the same as getBible's Cornilescu (Sol
#     471, Opus 391, Haiku 154). Tracking on recited text did as well (27 of 33).
# The default (CT_VERSE_TEXT=off: Sol tracks from memory). With "is he still
# reading?" and the focus rule, on the three-sermon replay: 25 of 32 verse changes,
# p50 1.7 s, p90 2.4 s (Sol: 24, 2.3 / 3.3 s), with almost no Sol calls.
TEXT = (os.environ.get("CT_VERSE_TEXT") or "on").lower()
BIBLE = os.environ.get("CT_VERSE_BIBLE", "").strip()
RECITE_MODEL = os.environ.get("CT_VERSE_RECITE_MODEL", "gpt-6-luna")
TRANSLATION = os.environ.get("CT_VERSE_TRANSLATION", "").strip()   # e.g. "Cornilescu"
RECITE_AHEAD = 4            # verses recited past where he is
GATE_STOP = float(os.environ.get("CT_VERSE_GATE_STOP", "0.5"))  # the gate's P(none) that means he stopped,
ONE_SURE = float(os.environ.get("CT_VERSE_ONE_SURE", "0.8"))    # unless the tracker is this sure of a verse
# Jo's "is he still reading?" asked in the same call while he reads (still_lab.py:
# a third of the gate's false stops). "text" puts the verses in the question.
STILL = (os.environ.get("CT_VERSE_STILL") or "text").lower()
STILL_TH = float(os.environ.get("CT_VERSE_STILL_TH", "0.5"))    # below this, he has stopped
_RECITE_SCHEMA = {"type": "object", "properties": {"verses": {"type": "array", "items": {
    "type": "object", "properties": {"verse": {"type": "integer"}, "text": {"type": "string"}},
    "required": ["verse", "text"], "additionalProperties": False}}},
    "required": ["verses"], "additionalProperties": False}


class TextSource:
    """Verse text for the tracker, fetched when a passage comes into view and kept
    in memory: getBible first (when a translation is set), else Luna reciting."""

    def __init__(self, bible=BIBLE, recite_model=RECITE_MODEL, translation=TRANSLATION, clock=time.time):
        self.bible, self.recite_model, self.translation, self.clock = bible, recite_model, translation, clock
        self.verses = {}          # (b, c) -> {v: text}
        self.asked = {}           # (b, c, lo) -> when (a fetch in flight or done)
        self.bad = {}             # (b, c) -> when getBible failed
        self.lock = threading.Lock()
        self.pool = cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="attendant-text")
        self.stats = collections.Counter()

    def get(self, b, c, lo, hi, hint=""):
        """{v: text} for verses lo..hi when all are here; else None (and they are fetched)."""
        with self.lock:
            have = self.verses.get((b, c), {})
            if all(v in have for v in range(lo, hi + 1)):
                return {v: have[v] for v in range(lo, hi + 1)}
            key = (b, c, lo)
            if self.clock() - self.asked.get(key, -1e9) < 20:
                return None
            self.asked[key] = self.clock()
        self.pool.submit(self._fetch, b, c, lo, hi, hint)
        return None

    def known(self, b, c):
        """Whatever of the chapter is in memory, {v: text}: nothing is fetched."""
        with self.lock:
            return dict(self.verses.get((b, c)) or {})

    def _put(self, b, c, got):
        with self.lock:
            self.verses.setdefault((b, c), {}).update(got)
            if len(self.verses) > 40:
                self.verses.pop(next(iter(self.verses)))

    def _fetch(self, b, c, lo, hi, hint):
        if self.bible and self.clock() - self.bad.get((b, c), -1e9) > 600:
            try:
                r = _openai().get(f"https://api.getbible.net/v2/{self.bible}/{b + 1}/{c}.json", timeout=(3, 5))
                r.raise_for_status()
                self._put(b, c, {int(v["verse"]): v["text"].strip() for v in r.json()["verses"]})
                self.stats["getbible"] += 1
                return
            except Exception:
                self.bad[(b, c)] = self.clock()
                self.stats["getbible failed"] += 1
        if not self.recite_model or not os.environ.get("OPENAI_API_KEY"):
            return
        try:
            self._put(b, c, self.recite(b, c, lo, hi + RECITE_AHEAD, hint))
            self.stats["recited"] += 1
        except Exception:
            self.stats["recite failed"] += 1

    def recite(self, b, c, lo, hi, hint=""):
        hi = min(hi, _VERSES[b][c - 1])
        which = f"the {self.translation} translation" if self.translation else \
            "the translation he seems to read from, in his language"
        system = ("You help a church's live Scripture display follow a preacher reading the Bible aloud. Give "
                  f"the text of the verses asked for, verse by verse, from memory, as in {which}. Keep each "
                  "verse's own words as closely as you can.")
        user = f"Give {BOOKS_EN[b]} {c}:{lo}-{hi}." + (f"\nHe speaks like this: \"{hint}\"" if hint else "")
        r = _openai().post("https://api.openai.com/v1/chat/completions",
                           headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                           json={"model": self.recite_model, "reasoning_effort": "none",
                                 "max_completion_tokens": 2000,
                                 "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                                 "response_format": {"type": "json_schema", "json_schema": {
                                     "name": "passage", "strict": True, "schema": _RECITE_SCHEMA}}},
                           timeout=(5, 20))
        r.raise_for_status()
        j = r.json()
        u = j.get("usage") or {}
        _bill(self.recite_model, u.get("prompt_tokens") or 0, 0, u.get("completion_tokens") or 0, "Verses")
        out = json.loads(j["choices"][0]["message"].get("content") or "{}")
        return {int(v["verse"]): v["text"].strip() for v in out.get("verses") or [] if lo <= v["verse"] <= hi}


def text_source():
    if TEXT not in ("on", "1", "yes") or not os.environ.get("OPENAI_API_KEY"):
        return None
    return TextSource()


def still_q(intro, texts=None):
    """Jo's "is he still reading?": his own words about a verse reuse its words, which
    the verse choices alone can't tell from reading it."""
    s = ("A preacher in church, transcribed live by a speech recogniser, a few words at a time, in any "
         "language. " + intro + " Are the NEWEST words him still reading the Bible text aloud, word for word "
         "or nearly? No if they are his own words -- explaining, applying or retelling it -- even when they "
         "repeat some of its words.")
    if texts:
        s += " The text he was reading (he may read a slightly different translation): " + \
             " ".join(f"[{v}] {t}" for v, t in sorted(texts.items()))
    return {"type": "predicate", "name": "still", "instructions": s}


def decide_one(context, newest, intro, texts, first, asked=ALL_ASKED):
    """One Decisions call for a packet with a passage in view: the gate's two
    questions and which verse (texts: {v: text}) -> {"gate": {...}, "track": {...}}."""
    ch = [first] + [(f"v{v}", f"verse {v}: \"{t}\"") for v, t in sorted(texts.items())]
    ch.append(("other", "other: Bible text from a different passage"))
    qs = _questions(asked)
    if first[0] == "aside" and STILL in ("text", "plain"):
        qs.append(still_q(intro, texts if STILL == "text" else None))
    q = {"type": "choice", "name": "v", "instructions":
         "A preacher in church, transcribed live by a speech recogniser, a few words at a time, in any "
         "language. " + intro + " The verses' text is given in the choices; he may read a slightly different "
         "translation. Which verse are the NEWEST words from?",
         "choices": [{"value": v, "description": d} for v, d in ch]}
    r = _openai().post("https://api.openai.com/v1/decisions",
                       headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                       json={"model": DECIDE_MODEL, "input": _lines(context, newest),
                             "questions": qs + [q]}, timeout=(5, 3))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    _bill(DECIDE_MODEL, u.get("input_tokens") or 0, (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
          u.get("output_tokens") or 0, "Verses gate")
    gate, track, still = {}, {}, None
    for a in j["answers"]:
        if a["name"] == "v":
            track = {p["value"]: p["probability"] for p in a.get("probabilities") or []}
        elif a["name"] == "still":
            still = a.get("probability")     # None when Luna refuses it: the gate's "none" decides
        elif a["type"] == "choice":
            gate.update({p["value"]: p["probability"] for p in a.get("probabilities") or []})
        elif a["type"] == "predicate" and "probability" in a:
            gate["_" + a["name"]] = a["probability"]
    return {"gate": gate, "track": track, "still": still}


# ---- the follower -------------------------------------------------------------
# Luna says which verse his newest words are from, a second or so after them, and
# is sometimes sure of a verse he only talks about. With the verses' text already
# in memory the words themselves say more, at once: the follower lays his newest
# words against the text and reports how much of each verse they spell out, in
# order. It compares words only as letters (no language's rules). A verse they
# spell out is read at once; and in a chapter whose text they have spelled out
# lately (FIT_S), a verse a model names must be borne out by them -- or it is his
# own words about the verse. A text they have never followed (another translation,
# another language) vouches for nothing: there the models' word stands, as before.
FOLLOW = (os.environ.get("CT_VERSE_FOLLOW") or "on").lower() in ("on", "1", "yes")
FOLLOW_WORDS = 10           # his newest words laid against the text
FOLLOW_SURE = 3.0           # this much of a verse in them, in order (about 3 telling words), and
FOLLOW_CLEAR = 1.0          # this much more than of any other verse: he is reading it
FOLLOW_ON = 1.5             # this much, further into the verse being read: he is still reading it
FOLLOW_SOME = 0.5           # this much bears out a verse a model names (FOLLOW_ON to pick a reading up again)
ARM_AHEAD = 2               # of a passage he named, the verses past its first that are in view before he reads
FOLLOW_WATCH = 4            # besides the chapter in view, the newest this many he named or read
FIT_S = 120.0               # his words spelled a verse of the chapter out (FOLLOW_ON) this lately: its text is
                            # the one he reads (trust_lab.py: of the verses the models named, turned down
                            # wrongly / rightly: 6 / 75 on the tuned sermons, 13 / 69 on the others)


def stem(w):
    """A folded word as the follower compares it: doubled letters once, the first
    five ("Iisus" is "Isus", "cetățile" is "cetăți")."""
    return re.sub(r"(.)\1+", r"\1", w)[:5]


class Follower:
    """match() -> {verse: (score, pos)} for the verses his newest words spell out:
    the weight of the verse's words found in them in order (a word counts for less
    the more of the verses have it), and how far into the verse (0..1) they reach.
    Only words in order up to his newest packet count."""

    def __init__(self):
        self.said = collections.deque(maxlen=FOLLOW_WORDS)    # (stem, packet seq)
        self.index = {}                                         # (chapter, its text) -> its index

    def hear(self, pkt):
        for w in keys(pkt.text):
            self.said.append((stem(w), pkt.seq))

    def _index(self, key, texts):
        key = (key, hash(frozenset(texts.items())))
        if key not in self.index:
            if len(self.index) > 12:
                self.index.clear()
            words = {v: [stem(w) for w in keys(t)] for v, t in texts.items()}
            df = collections.Counter(w for ws in words.values() for w in set(ws))
            n = max(2, len(words))
            where = collections.defaultdict(list)               # stem -> [(verse, position)]
            for v, ws in words.items():
                for j, w in enumerate(ws):
                    where[w].append((v, j))
            weight = {w: max(0.05, 1 - math.log(d) / math.log(n)) for w, d in df.items()}
            self.index[key] = (texts, where, weight, {v: len(ws) for v, ws in words.items()})
        return self.index[key][1:]

    def match(self, key, texts, seq=None, said=None):
        """`key`: which text this is (its chapter); `seq`: the newest packet; `said`:
        his words as they were (a copy of self.said) in place of the newest."""
        said = self.said if said is None else said
        if not texts or not said:
            return {}
        where, weight, length = self._index(key, texts)
        seq = said[-1][1] if seq is None else seq
        pairs = collections.defaultdict(list)                   # verse -> [(spoken index, position, weight)]
        for i, (w, _) in enumerate(said):
            for v, j in where.get(w, ()):
                pairs[v].append((i, j, weight[w]))
        fresh = next(i for i, (_, s) in enumerate(said) if s >= seq) if said[-1][1] >= seq else 1e9
        out = {}
        for v, ps in pairs.items():
            best = []                                           # per pair: the weight of the words in order up to it
            top = None
            for k, (i, j, w) in enumerate(ps):
                sc = w + max((best[m] for m, (i0, j0, _) in enumerate(ps[:k])
                              if i0 < i and j0 < j and i - i0 <= 3 and j - j0 <= 4), default=0)
                best.append(sc)
                if i >= fresh - 1 and (top is None or sc > top[0]):
                    top = (sc, (j + 1) / length[v])
            if top:
                out[v] = (round(top[0], 2), round(top[1], 2))
        return out


# ---- packets ------------------------------------------------------------------

class Packet:
    """`text`: the new words; `context`: the words before them; `recent`: the last
    READ_WORDS words, new ones included (enough for the reader to know a verse)."""

    def __init__(self, seq, t, context, text, recent=None):
        self.seq, self.t, self.context, self.text = seq, t, context, text
        self.recent = recent or text


class Packetizer:
    """Soniox messages -> packets of the newest words. It reads the draft words as
    well as the confirmed ones: on a live capture Soniox confirmed a word ~4.7 s
    after it was spoken (p50; 6.1 s p90) but drafted it within ~0.5 s."""

    def __init__(self, clock=time.time):
        self.final, self.tail = "", ""     # confirmed text; the current draft tail
        self.sent, self.seq = 0, 0          # words already sent; packets so far
        self.clock = clock
        self.last = clock()

    def tokens(self, toks):
        """One Soniox message -> True if it carried an endpoint (a pause)."""
        tail, end = [], False
        for t in toks:
            if t.get("translation_status") == "translation":
                continue
            w = t.get("text") or ""
            if w.startswith("<"):
                end = end or w == "<end>"
                continue
            if t.get("is_final"):
                self.final += w
            else:
                tail.append(w)
        self.tail = "".join(tail)
        if len(self.final) > 8000:          # keep the confirmed text bounded
            cut = self.final.find(" ", 4000)
            if cut > 0:
                self.sent = max(0, self.sent - len(self.final[:cut].split()))
                self.final = self.final[cut:]
        return end

    def wet(self, n=40):
        """The last `n` words, draft included (for the church's request phrases)."""
        return " ".join((self.final + self.tail).split()[-n:])

    def take(self, fast, end=False, now=None):
        """A packet, if one is due: FAST_WORDS (fast) or IDLE_WORDS new words, the
        longest wait for them, or a pause."""
        now = self.clock() if now is None else now
        ws = (self.final + self.tail).split()
        if self.tail and not end:
            ws = ws[:-1]           # the last draft word may still be growing ("versetul 3" -> "35")
        self.sent = min(self.sent, len(ws))   # a draft can shrink
        new = len(ws) - self.sent
        if new <= 0:
            return None
        if not fast and IDLE_BY == "segment":
            news = ws[self.sent:]
            due = end or new >= SEG_WORDS or (now - self.last) * 1000 >= SEG_MS or \
                any(c.isdigit() for w in news for c in w) or \
                (new >= IDLE_WORDS and unicodedata.category(news[-1][-1]).startswith("P"))
            if not due:
                return None
        else:
            need, wait = (FAST_WORDS, FAST_MS) if fast else (IDLE_WORDS, IDLE_MS)
            if new < need and not end and (now - self.last) * 1000 < wait:
                return None
        self.seq += 1
        pkt = Packet(self.seq, now, " ".join(ws[max(0, self.sent - CTX_WORDS):self.sent]), " ".join(ws[self.sent:]),
                     " ".join(ws[-READ_WORDS:]))
        self.sent, self.last = len(ws), now
        return pkt


# ---- the scratchpad -----------------------------------------------------------

class Scratchpad:
    """Bubble 1: what the speaker has named, alluded to or read in this service
    -- a book alone, a chapter, or a verse -- newest first, kept in a JSON file so
    a restart mid-sermon keeps it. Never shown: it resolves a bare "versetul 4" and
    tells the reader what is in view."""

    def __init__(self, path=None, clock=time.time):
        self.path, self.clock, self.items = path, clock, []   # [{"book","chapter","verse","at","via"}]
        if path and os.path.exists(path):
            try:
                items = json.load(open(path, encoding="utf-8")).get("items") or []
                now = clock()
                self.items = [x for x in items if now - x["at"] < MEMORY_TTL_S][:MEMORY_MAX]
            except Exception:
                self.items = []

    def note(self, b, c=None, v=None, via="citation"):
        """via: citation (he named it) | allusion | reading."""
        key = (b, c, v)
        self.items = [{"book": b, "chapter": c, "verse": v, "at": self.clock(), "via": via}] + [
            x for x in self.items if (x["book"], x["chapter"], x["verse"]) != key]
        self.items = self.items[:MEMORY_MAX]
        self._save()

    def live(self):
        now = self.clock()
        return [x for x in self.items if now - x["at"] < MEMORY_TTL_S]

    def chapters(self, via=("citation", "reading")):
        """[(book, chapter)] newest first, each once, noted one of the ways in `via`
        (an allusion alone does not make a chapter the one a bare "verse 4" means)."""
        out = []
        for x in self.live():
            if x["chapter"] is not None and x["via"] in via and (x["book"], x["chapter"]) not in out:
                out.append((x["book"], x["chapter"]))
        return out

    def describe(self, canon):
        return ", ".join(canon.ref(x["book"], x["chapter"], x["verse"]) for x in self.live())

    def clear(self):
        self.items = []
        self._save()

    def _save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": self.items}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---- the detector -------------------------------------------------------------

class Call:
    """One model call a packet needs: kind "gate" | "track" | "read" | "blind"."""

    def __init__(self, kind, pkt, extra=None):
        self.kind, self.pkt, self.extra = kind, pkt, extra or {}


class VerseDetector:
    """The Attendant's state, driven by packets and the answers to the calls they
    need. Event-driven so the calls can run in parallel (Dispatcher) or in a
    simulated clock (the speed lab):

        calls = det.on_packet(pkt)            # under det.lock
        answer = det.run_call(call)           # the network; no lock
        more = det.on_result(call, answer)    # under det.lock

    `ask`: the reader, reader() by default (None: requests by phrase only).
    `gate`, `track`: Luna's Decisions API and Sol by default when there is an OpenAI
    key (None: without them the reader hears every packet with a digit, or while a
    passage is in view, and carries the readings).
    `text`: a TextSource; once a passage's text is here, one Luna call a packet
    (`one`, decide_one) does the gate and the tracking, and Sol stands down."""

    def __init__(self, emit, memory=None, ask="auto", gate="auto", track="auto", blind_ask="auto",
                 clock=time.time, text="auto", one=decide_one):
        decide = GATE == "luna" and bool(os.environ.get("OPENAI_API_KEY"))
        self.ask = reader() if ask == "auto" else ask
        self.gate = (gate_luna if decide else None) if gate == "auto" else gate
        self.track = tracker() if track == "auto" else track
        self.blind = blind() if blind_ask == "auto" else blind_ask
        self.text = text_source() if text == "auto" else text
        self.one = one if self.gate else None
        self.canon = Canon()
        self.emit_cb, self.clock = emit, clock
        self.memory = memory or Scratchpad(clock=clock)
        self.lock = threading.RLock()
        self.armed = {}            # ordinal -> (until, ref, named while he was reading elsewhere): verses he cited
        self.reading = None        # {"i", "start", "last_t", "heard", "paused", "cited", "strong", ...}
        self.seen = {}             # (b, c, v) -> when last cited (one reference split over packets)
        self.focus_t = -1e9         # when Luna last heard him bring a text into focus
        self.recent = None          # the reading that just ended, for REMEMBER_S: {"start", "i", "cited", "until"}
        self.pending = None        # (c, v, v2, when): "capitolul 15, versetul 21," with the book still to come
        self.seq = 0
        self.errors = 0
        self.last_error = ""
        self.request_words = [" ".join(keys(w)) for w in REQUEST_WORDS]
        self.last_request = (-1e9, None)   # (when, ref)
        self.state = {"state": "idle", "ref": None, "since": clock()}
        self.alert_until = 0.0
        self.last_packet = clock()
        self.applied = collections.Counter()   # kind -> newest packet whose answer was applied
        self.reads = set()                     # packet seqs with a read call
        self.inflight = collections.Counter()
        self.since_cite = 0
        self.stats = collections.Counter()
        self._recent = collections.deque(maxlen=12)   # feed(): the words before each packet
        self.follow = Follower() if FOLLOW and self.text else None
        self.fit = {}               # (b, c) -> when his words last spelled out a verse of its text (FIT_S)
        self.jump = None            # (ordinal, packet seq, how far in): a verse elsewhere, heard once mid-reading
        self.said = ""              # his newest words (the hint for reciting a passage)
        self.heard = collections.deque(maxlen=READ_WORDS)   # (packet seq, its words): what the reader may be shown
        self.cut = 0                # packets up to this one were heard before the last reading ended

    # -- events
    def _emit(self, ev):
        self.seq += 1
        ev = dict(ev, seq=self.seq, t=round(self.clock(), 2))
        try:
            self.emit_cb(ev)
        except Exception:
            pass
        if ev["type"] != "state":
            self._advance(ev)

    def _verse_ev(self, i, **kw):
        b, c, v = self.canon.where[i]
        return dict({"type": "reading", "ref": self.canon.ref(b, c, v), "book": b, "chapter": c, "verse": v}, **kw)

    def fast(self):
        """Packets come small and quick while on alert or with a passage in view, and
        for as long after the gate last heard Scripture."""
        return self.state["state"] != "idle" or self.reading is not None or self.clock() < self.alert_until

    # -- packets in, calls out
    def on_packet(self, pkt):
        now = self.clock()
        self.stats["packets"] += 1
        self.last_packet = now
        self._expire(now)
        self.hear(pkt.text)
        self.heard.append((pkt.seq, len(pkt.text.split())))
        self.said = pkt.recent
        calls = []
        spec = self._track_spec() if (self.track or self.text) else None
        if self.follow:
            self.follow.hear(pkt)
            self._prove(now)
            if self._follow(spec, pkt, now):    # the words alone named a verse: what is in view has changed
                spec = self._track_spec()
        texts = None
        if spec and self.text and self.one:
            texts = self.text.get(spec["b"], spec["c"], spec["tlo"], spec["thi"], pkt.recent)
            for j in spec["jumps"] if texts else []:
                texts.update(self.text.get(spec["b"], spec["c"], j, j, pkt.recent) or {})  # a verse he named
        asked = self._asked()
        if texts:
            # the verses' text is here: one Luna call does the gate and the tracker
            calls.append(Call("one", pkt, dict(spec, texts=texts, asked=asked)))
        else:
            if self.gate:
                calls.append(Call("gate", pkt, {"asked": asked}))
            if spec and self.track:
                calls.append(Call("track", pkt, spec))
        if self.ask and self._want_read(pkt, spec):
            calls.append(self._read_call(pkt))
        return calls

    def _want_read(self, pkt, spec):
        if not self.gate:          # no gate: every packet with a digit, or while something is in view
            return bool(re.search(r"\d", pkt.text)) or self.state["state"] != "idle" or \
                self.reading is not None or bool(self.memory.chapters())
        # With a gate, the reader starts alongside it -- not after it -- when a passage
        # may be named right now: a number in the words ("Efeseni 1:"). Waiting for the
        # gate cost ~0.3-0.5 s of zero-to-sixty; otherwise the gate decides (a citation
        # or request, or enough to name a verse). READ_EARLY=alert also starts it on
        # every alert packet, as up to 2.21 -- the costliest line on a live sermon.
        st = self.state["state"]
        idle = spec is None or (self.reading is None and st != "armed")   # (a passage just ended: still idle)
        soon = (READ_EARLY == "alert" and st == "alert") or \
            ((idle or st == "alert") and bool(re.search(r"\d", pkt.text)))
        return (soon or (st == "armed" and spec is None)) and self.inflight["read"] < MAX_READS

    def _read_call(self, pkt):
        self.reads.add(pkt.seq)
        if len(self.reads) > 200:
            self.reads = set(sorted(self.reads)[-100:])
        self.inflight["read"] += 1
        # its "newest" words: the last READ_WORDS, but none from before a reading ended
        # (a verse he has finished is not one he is reading now)
        since = sum(n for seq, n in self.heard if self.cut < seq <= pkt.seq)
        return Call("read", pkt, {"note": self._note(), "words": since})

    @staticmethod
    def read_args(call):
        """(context, newest, note) as the reader is asked."""
        p = call.pkt
        words = (p.context + " " + p.text).split()
        k = max(len(p.text.split()), min(READ_WORDS, call.extra.get("words") or READ_WORDS))
        return " ".join(words[:-k]), " ".join(words[-k:]), call.extra["note"]

    def _note(self):
        lines = ["Named or read earlier in this service, newest first: "
                 + (self.memory.describe(self.canon) or "nothing")]
        r = self.reading
        if r and r["strong"]:
            lines.append(f"Being read now: {self.canon.ref_of(r['i'])}"
                         + (" (he paused for an aside)" if r["paused"] else ""))
        elif self.state["state"] == "armed" and self.state.get("ref"):
            lines.append(f"In view: {self.state['ref']}")
        return "\n".join(lines)

    def _track_spec(self):
        """The tracker's question for what is in view, or None (nothing to track)."""
        r, st = self.reading, self.state
        jumps = []
        if r:
            b, c, v = self.canon.where[r["i"]]
            intro = f"He is reading {self.canon.ref(b, c)} aloud, last heard at verse {v}."
            # a move counts to the next verse; a jump only to a verse he cited
            lo, hi = max(1, v - 1), v + 1
            # verses he named since this reading began ("Versetul 35."), newest first
            named = sorted(((a[0], self.canon.where[i][2]) for i, a in self.armed.items()
                            if self.canon.where[i][:2] == (b, c) and a[2] and a[0] - ARM_TTL_S >= r["t0"]
                            and not lo <= self.canon.where[i][2] <= hi), reverse=True)
            jumps = sorted({v for _, v in named[:2]})
            if jumps:
                intro += " He has just named verse " + " and ".join(map(str, jumps)) + "."
        elif st["state"] != "armed" and self.recent and self.clock() < self.recent["until"]:
            b, c, v = self.canon.where[self.recent["i"]]
            intro = (f"He was reading {self.canon.ref(b, c)} aloud, up to verse {v}, and stopped; he may go on "
                     "with it.")
            lo, hi = max(1, v - 1), v + 1
        elif st["state"] == "armed" and st.get("book") is not None and st.get("chapter"):
            b, c, v, v2 = st["book"], st["chapter"], st.get("verse"), st.get("verse_end")
            intro = (f"He has just named {st['ref']} and may be about to read it; null while he is still "
                     "speaking in his own words.")
            lo, hi = (max(1, v - 1), min(v2 or v, v + ARM_AHEAD) + 1) if v else (1, self.canon.chapters[b][c - 1])
        else:
            return None
        n = self.canon.chapters[b][c - 1]
        hi = min(hi, n)
        # the text window for the one-call tracker: where he is and the next verse
        # (a bare chapter: its first verses); anything further is "other"
        tlo, thi = (lo, hi) if (r or v) else (1, min(n, 4))
        first = ("wait", "wait: he is still speaking in his own words -- not reading yet") \
            if st["state"] == "armed" and not r else \
            ("aside", "aside: his own words (a comment, an explanation, a story) -- not Bible text")
        return {"intro": intro, "ref": self.canon.ref(b, c), "b": b, "c": c, "lo": lo, "hi": hi,
                "tlo": tlo, "thi": thi, "first": first, "focus": self._focus(), "jumps": jumps}

    def _follow(self, spec, pkt, now):
        """His newest words against the text in memory: the chapter in view, the
        passage he just read, the chapters he named or read lately. A verse they
        spell out is read at once, without waiting for Luna (-> True); out of view
        it takes more of it."""
        view = (spec["b"], spec["c"]) if spec else None
        watch = [view] if view else []
        if self.recent and now < self.recent["until"]:
            watch.append(self.canon.where[self.recent["i"]][:2])
        best = []
        for bc in dict.fromkeys(watch + self.memory.chapters(("citation", "reading", "allusion"))[:FOLLOW_WATCH]):
            best += [(m[0], bc, v, m[1]) for v, m in self.follow.match(bc, self.text.known(*bc), pkt.seq).items()]
        if not best:
            return False
        best.sort(reverse=True)
        r = self.reading
        score, bc, v, pos = best[0]
        if score < FOLLOW_SURE + (0 if bc in watch else 1) or \
                len(best) > 1 and score - best[1][0] < FOLLOW_CLEAR:
            return False
        self.fit[bc] = now
        i = self.canon.at[(*bc, v)]
        last = r or (self.recent if self.recent and now < self.recent["until"] else None)
        if last and i == last["i"] and pos <= last["pos"]:
            return False            # words of the verse again, no further into it: Luna says if he is reading
        if r and r["strong"] and r["paused"] is None and i not in self.armed \
                and not self._lo(r) <= i <= r["i"] + 3:
            # another verse, uncited, in the middle of a reading: only if he goes on with it
            j, self.jump = self.jump, (i, pkt.seq, pos)
            if not (j and pkt.seq - j[1] <= 2 and (j[0] == i and pos > j[2] or j[0] == i - 1)):
                return False
        self.stats["followed"] += 1
        self.applied["text"] = pkt.seq
        return self._read(i, 0.95, via="text", pkt=pkt)

    def _prove(self, now):
        """A verse named before its text was here (a quote from memory: the text is
        fetched once he is heard): did the words that named it spell it out?"""
        r = self.reading
        if not r or r["proof"] is None:
            return
        i, seq, said = r["proof"]
        b, c, v = self.canon.where[i]
        known = self.text.known(b, c) if self.text else {}
        if v in known:
            if self.follow.match((b, c), known, seq, said).get(v, (0, 0))[0] >= FOLLOW_ON:
                self.fit[(b, c)] = now
            r["proof"] = None

    def _spelled(self, i, pkt):
        """(score, pos) of verse i in his words from packet `pkt` on (Follower.match),
        or None without its text in memory."""
        b, c, v = self.canon.where[i]
        known = self.text.known(b, c) if self.follow and pkt else {}
        return self.follow.match((b, c), known, pkt.seq).get(v, (0, 0)) if v in known else None

    def _asked(self):
        """Which of the per-packet questions this state uses: "enough?" only with no
        passage known (it starts the reader), "be seated?" only while a reading is
        active; focus always (the cue often comes before the citation)."""
        r, st = self.reading, self.state["state"]
        known = (r is not None and r["paused"] is None) or (r is None and st == "armed")
        return frozenset(["focus"] + ([] if known else ["enough"]) + (["seated"] if r and r["strong"] else []))

    def _focus(self):
        """What is in view, to tell a late answer about an earlier passage."""
        r, st, k = self.reading, self.state, self.recent
        if r:
            return ("r", r["start"])
        if st["state"] == "armed":
            return ("a", st.get("ref"))
        # idle or on alert: the passage that just ended, if any (not the pill's ref: the
        # gate's half of an answer puts him on alert, and the tracker's half must stand)
        return ("k", k["start"] if k else None)

    def run_call(self, call):
        """The network part: no lock held."""
        self.stats[call.kind] += 1
        p = call.pkt
        if call.kind == "gate":
            return self.gate(p.context, p.text, call.extra["asked"]) if call.extra else self.gate(p.context, p.text)
        if call.kind == "track":
            return self.track(p.context, p.text, call.extra["intro"], call.extra["ref"])
        if call.kind == "one":
            e = call.extra
            return self.one(p.context, p.text, e["intro"].replace("; null while", "; wait while"), e["texts"], e["first"],
                            e["asked"])
        if call.kind == "read":
            return self.ask(*self.read_args(call))
        if call.kind == "blind":
            return self.blind(p.recent)
        return None

    def on_result(self, call, res):
        """An answer back -> more calls it calls for."""
        more = self._on_result(call, res)
        spec = self._track_spec() if self.text and self.one else None
        if spec:        # the text of what has come into view: asked for now, not at the next packet
            self.text.get(spec["b"], spec["c"], spec["tlo"], spec["thi"], self.said)
        return more

    def _on_result(self, call, res):
        now = self.clock()
        if call.kind == "read":
            self.inflight["read"] = max(0, self.inflight["read"] - 1)
        if res is None:
            return []
        if call.kind == "gate":
            return self._on_gate(call, res, now)
        if call.kind == "one":
            more = self._on_gate(call, res.get("gate") or {}, now)
            return more + self._on_one_track(call, res, now)
        if call.kind == "track":
            return self._on_track(call, res, now)
        if call.kind == "read":
            return self._on_read(call, res, now)
        if call.kind == "blind" and (res.get("confidence") or 0) >= CORRECT_CONF:
            b = BOOKS_EN.index(res["book"]) if res.get("book") in BOOKS_EN else None
            if b is not None and self.canon.has(b, res.get("chapter"), res.get("verse")):
                self._read(self.canon.at[(b, res["chapter"], res["verse"])], res["confidence"])
        return []

    def _on_gate(self, call, p, now):
        if p.get("_focus", 0) >= FOCUS_TH:
            self.focus_t = max(self.focus_t, call.pkt.t)
        if p.get("_seated", 0) >= 0.5 and self.reading and self.reading["strong"]:
            self.stats["seated"] += 1
            self._end_reading(remember=False)       # "please be seated": the formal reading is over,
            self.focus_t = -1e9                     # and so is the focus he read it under
        if p.get("none", 1.0) >= 1 - GATE_TH and p.get("_enough", 0) < 0.5:
            return []
        top = max((k for k in p if k != "none"), key=lambda k: p[k], default="about")
        self.alert_until = now + ALERT_S    # (in any state: a verse may follow the reading that is ending)
        if self.state["state"] == "idle":
            self._set_state("alert", None, why=top)
        r = self.reading
        known = (r is not None and r["paused"] is None) or (r is None and self.state["state"] == "armed")
        # "Fapte 19:9." followed by its text is mostly "reading" to the gate: a fair
        # share for citation or request is enough to have the reader parse it.
        # Otherwise, with nothing in view, the reader is asked only once there is
        # enough to name a verse (Jo's question, ENOUGH_Q).
        enough = p.get("_enough", 1.0 if "_enough" not in p else 0) >= 0.5
        want = p.get("citation", 0) + p.get("request", 0) >= GATE_TH or (not known and enough)
        if want and self.ask and call.pkt.seq not in self.reads and self.inflight["read"] < MAX_READS:
            return [self._read_call(call.pkt)]
        return []

    def _on_track(self, call, p, now):
        if call.pkt.seq < self.applied["track"] or not isinstance(p, dict):
            return []
        self.applied["track"] = call.pkt.seq
        spec, v = call.extra, p.get("verse")
        if spec.get("focus") != self._focus():
            return []               # asked about a passage no longer in view
        if v is None:
            if self.reading:
                self._miss(now)     # his own words: pause a reading (or drop an allusion)
            return []
        b, c = spec["b"], spec["c"]
        if not self.canon.has(b, c, v):
            return []
        i = self.canon.at[(b, c, v)]
        if spec["lo"] <= v <= spec["hi"] or i in self.armed:
            self.stats["tracked"] += 1
            self._read(i, 0.9, via="track", pkt=call.pkt)
            return []
        # further than the next verse, and not one he cited: let the reader say where
        if self.ask and call.pkt.seq not in self.reads and self.inflight["read"] < MAX_READS:
            return [self._read_call(call.pkt)]
        return []

    def _on_one_track(self, call, res, now):
        """The tracker's half of a one-call answer: a sure verse moves him; "is he
        still reading?" answered no (else the gate hearing no Scripture), or "aside",
        means he has stopped."""
        p, gate = res.get("track") or {}, res.get("gate") or {}
        if call.pkt.seq < self.applied["track"] or not p:
            return []
        self.applied["track"] = call.pkt.seq
        spec = call.extra
        if spec.get("focus") != self._focus():
            return []
        at = lambda k: self.canon.at.get((spec["b"], spec["c"], int(k[1:])))
        top, bar = max(p, key=lambda k: p[k]), TRACK_TH
        if spec["first"][0] == "wait":      # waiting for a verse he cited: its first words count sooner,
            v = max((k for k in p if k.startswith("v")), key=lambda k: p[k], default=None)
            if v and p[v] >= ARMED_TH and at(v) in self.armed:      # even under "wait"
                top, bar = v, ARMED_TH
        still = res.get("still")
        quiet = still < STILL_TH if still is not None else gate.get("none", 0) >= GATE_STOP
        stop = quiet and p[top] < ONE_SURE
        i = at(top) if top.startswith("v") else None
        if i is not None and p[top] >= bar and not stop:
            self.stats["tracked"] += 1
            was = self.reading if self.reading and self.reading["paused"] is not None else None
            if self._read(i, 0.9, via="track", pkt=call.pkt):
                # Picked up again, sure of the verse and that he is still reading: the clear
                # clock starts over even on the verse he was on (with a text in another
                # language nothing else tells a long verse from his comment on it).
                if was and was is self.reading and was["paused"] is None and \
                        min(p[top], still if still is not None else 0) >= ONE_SURE:
                    was["stop_t"] = None
                return []
        r = self.reading
        if r and (top == "aside" or quiet):
            m = self._spelled(r["i"], call.pkt)
            if m and m[0] >= FOLLOW_ON and m[1] > r["pos"]:
                self._read(r["i"], 0.9, via="text", pkt=call.pkt)   # further into the verse: he is reading
            elif call.pkt.seq > self.applied["text"]:  # (not of words the follower heard him read)
                self._miss(now)
            return []
        if top == "other" and self.ask and call.pkt.seq not in self.reads and self.inflight["read"] < MAX_READS:
            return [self._read_call(call.pkt)]
        return []

    def _on_read(self, call, out, now):
        said = call.pkt.context + " " + call.pkt.text
        named, was = None, self.reading
        for ref in out.get("refs") or []:
            named = self._take(ref, said) or named
        if out.get("show_request"):
            self._request("llm", call.pkt.text, named)
        rd = out.get("reading")
        b = BOOKS_EN.index(rd["book"]) if rd and rd.get("book") in BOOKS_EN else None
        if b is not None and self.canon.has(b, rd.get("chapter"), rd.get("verse")):
            i = self.canon.at[(b, rd["chapter"], rd["verse"])]
            if was and self.reading is None and i not in self.armed:
                return []           # naming another verse ended the reading: the words before it are not read now
            if call.pkt.seq >= self.applied["read"]:
                self.applied["read"] = call.pkt.seq
                self._read(i, rd.get("confidence"), pkt=call.pkt)
            return []
        if self.reading and not self.track:
            self._miss(now)             # no tracker: the reader carries the misses
        if not out.get("refs") and self._just_cited():
            self.since_cite += 1
            return [Call("blind", call.pkt)]
        return []

    def _just_cited(self):
        """A citation armed lately, nothing read yet: the time he reads what he cited
        -- or, when he named the wrong book, something else."""
        st = self.state
        return (self.blind is not None and st["state"] == "armed" and st.get("why") == "citation"
                and self.reading is None and self.clock() - st["since"] <= CORRECT_S
                and self.since_cite < CORRECT_CHUNKS)

    # -- simple, synchronous use (the command line, POST /verse {text}, tests)
    def feed(self, text):
        text = (text or "").strip()
        if not text:
            return
        with self.lock:
            ctx = " ".join(self._recent)
            self._recent.append(text)
            todo = self.on_packet(Packet(self.stats["packets"] + 1, self.clock(), ctx[-400:], text))
        while todo:
            call = todo.pop(0)
            try:
                res = self.run_call(call)
            except Exception as e:
                self.errors += 1
                self.last_error = f"{call.kind}: {str(e)[:200]}"
                res = None
            with self.lock:
                todo += self.on_result(call, res)

    # -- readings
    @staticmethod
    def _lo(r):
        """The lowest verse that still counts as the same passage: a strong reading may
        go back over the verse before where he started; an allusion only forward."""
        return max(r["start"] - 1, 0) if r["strong"] else r["i"]

    def _read(self, i, conf=None, via="read", pkt=None):
        """He is reading verse i (an ordinal) in packet `pkt`, the answer says, this sure
        -> False if his words do not bear it out. via "track": the tracker, which only
        follows what is in view (never a correction); "text": the follower."""
        now = self.clock()
        r = self.reading
        m = self._spelled(i, pkt)
        if via != "text" and m:
            # A model names a verse whose text is in memory: do his words bear it out?
            # Picking a reading up again (paused, or just cleared) takes more of the verse
            # than going on with one.
            b, c, v = self.canon.where[i]
            again = r["paused"] is not None if r else bool(self.recent and now < self.recent["until"])
            if m[0] >= FOLLOW_ON:
                self.fit[(b, c)] = now
            elif m[0] < (FOLLOW_ON if again else FOLLOW_SOME) and now - self.fit.get((b, c), -1e9) <= FIT_S:
                self.stats["not borne out"] += 1    # a text his words have followed: his own words about it
                return False
        if via != "text" and r and pkt and pkt.seq <= self.applied["text"] and self._lo(r) <= i < r["i"]:
            return True             # the follower has heard him go on since: an answer about older words
        pos = m[1] if m and m[0] >= FOLLOW_ON else 0        # how far into the verse his words reach
        cited = i in self.armed
        conf = 0.5 if conf is None else conf
        if r and self._lo(r) <= i <= r["i"] + 3:   # the same passage, going on (or picked up again)
            paused = r["paused"]
            if i > r["i"] or (i == r["i"] and pos > r["pos"]):
                r["stop_t"] = None      # he moved on: reading again (a verse already read may be his comment)
            r.update(last_t=now, heard=pkt.t if pkt else now, paused=None, cited=r["cited"] or cited,
                     pos=max(pos, r["pos"]) if i == r["i"] else pos)
            back = r["back"]
            if not r["strong"] and not (back and pkt and pkt.t > back["pt"]) and (back or not self._focused(now)):
                if i != r["i"]:                 # amber, following the verse (nothing shown of a passage
                    r["i"] = i                  # that just cleared until a later packet goes on with it)
                    if not back:
                        self._emit(self._verse_ev(i, state="allusion", level="weak", cited=r["cited"], by=via))
                return True
            if not r["strong"] and back:        # he does go on with the passage that just cleared
                self.recent = None
                r.update(strong=True, i=i, start=min(back["start"], r["start"]), cited=r["cited"] or back["cited"])
                self._emit(self._verse_ev(i, state="start", level="strong", cited=r["cited"], resumed=True, by=via))
            elif not r["strong"]:               # an allusion that kept going: he is reading
                r["strong"] = True
                r["i"] = i
                self._emit(self._verse_ev(i, state="start", level="strong", cited=r["cited"], by=via,
                                          **({"began": self.canon.ref_of(r["start"])} if i != r["start"] else {})))
            elif paused or i != r["i"]:
                r["i"] = i
                self._emit(self._verse_ev(i, state="resume" if paused else "verse", level="strong",
                                          cited=r["cited"], by=via,
                                          **({"away_s": round(now - paused, 1)} if paused else {})))
            return True
        if not cited and conf < LOW_CONF:
            return True             # a guess: not worth arming (recall_lab: wrong more often than not)
        # He cited one passage and is now, a moment later, reading a different one
        # the reader is sure of: he named the wrong book or chapter. Follow the words.
        st = self.state
        corrected = None
        if via == "read" and not cited and conf >= CORRECT_CONF and st["state"] == "armed" \
                and st.get("why") == "citation" and now - st["since"] <= CORRECT_S \
                and (st.get("book"), st.get("chapter")) != self.canon.where[i][:2]:
            corrected = st.get("ref")
        if r and r["strong"]:
            self._end_reading()
        # A verse he cited (or asked for) is read at once; anything else is an
        # allusion until he goes on with it under a focus cue. The passage that just
        # cleared ("back") comes back when a later packet goes on with it, and nothing
        # is shown until then: one answer alone brought it back wrongly more often
        # than rightly, for CLEAR_S on the screen each time.
        strong = (cited or corrected is not None) and self._focused(now)
        k = self.recent
        back = dict(k, pt=pkt.t if pkt else now) if not strong and k and now < k["until"] and \
            k["start"] - 1 <= i <= k["i"] + 3 and self.canon.where[i][:2] == self.canon.where[k["i"]][:2] else None
        if strong:
            self.recent = None
        self.reading = {"i": i, "start": i, "last_t": now, "heard": pkt.t if pkt else now, "paused": None,
                        "cited": cited, "strong": strong, "t0": now, "stop_t": None, "pos": pos, "back": back,
                        "proof": (i, pkt.seq, list(self.follow.said)) if m is None and pkt and self.follow else None}
        if back:
            return True
        b, c, v = self.canon.where[i]
        self.memory.note(b, c, v, "reading" if strong else "allusion")
        self._emit(self._verse_ev(i, state="start" if strong else "allusion",
                                  level="strong" if strong else "weak", cited=cited, confidence=round(conf, 2),
                                  by=via, **({"corrected_from": corrected} if corrected else {})))
        return True

    def _focused(self, now):
        """Has he brought a text into focus lately (Jo's magic words, or a request)?"""
        return not (FOCUS and self.gate) or now - self.focus_t <= FOCUS_S or \
            now - self.last_request[0] <= FOCUS_S

    def _miss(self, now):
        """A packet without the passage: drop an allusion; pause a strong reading, or
        end one paused for too long."""
        r = self.reading
        if not r["strong"]:
            self.reading = None
        elif r["stop_t"] is not None and now - r["stop_t"] > CLEAR_S:
            self._end_reading()
        elif r["paused"] is None:
            r["paused"] = now
            if r["stop_t"] is None:
                r["stop_t"] = r["heard"]    # the clear clock runs from his last words read
            self._emit(self._verse_ev(r["i"], state="pause", level="strong", cited=r["cited"]))

    def tick(self):
        """Call every second or so: alerts and armed passages lapse, silence pauses a
        reading, and a paused one ends."""
        with self.lock:
            r, now = self.reading, self.clock()
            self._expire(now)
            if r and r["strong"]:
                if r["paused"] is None and now - r["last_t"] > PAUSE_S or \
                        r["stop_t"] is not None and now - r["stop_t"] > CLEAR_S:
                    self._miss(now)
            elif r and now - r["last_t"] > PAUSE_S:
                self.reading = None

    def _end_reading(self, remember=True):
        r, self.reading = self.reading, None
        self.cut = self.heard[-1][0] if self.heard else 0
        if r["strong"] and remember:
            self.recent = {"start": r["start"], "i": r["i"], "cited": r["cited"], "pos": r["pos"],
                           "until": self.clock() + REMEMBER_S}
        for i in range(r["start"], r["i"] + 1):        # he has read it: retelling it is not reading it again
            self.armed.pop(i, None)
        b, c, v = self.canon.where[r["i"]]
        b0, c0, v0 = self.canon.where[r["start"]]
        self._emit({"type": "reading", "state": "end", "level": "strong",
                    "ref": self.canon.ref(b0, c0, v0, v if c == c0 else None),
                    "book": b0, "chapter": c0, "verse": v0, "verse_end": v if c == c0 else None,
                    "cited": r["cited"]})

    def _arm(self, b, c, v, v2, ref):
        until = self.clock() + ARM_TTL_S
        n = self.canon.chapters[b][c - 1]
        at = self.reading["i"] if self.reading else None
        for vv in range(v, min(v2 or v, n) + 1):
            i = self.canon.at[(b, c, vv)]
            self.armed[i] = (until, ref, at is not None and not at - 1 <= i <= at + 1)
        if self.text and not self.reading:      # its text, while he is still naming it
            self.text.get(b, c, max(1, v - 1), min(min(v2 or v, v + ARM_AHEAD) + 1, n), self.said)

    # -- the state a listener follows: idle -> alert -> armed -> active -> idle
    def _set_state(self, state, ev=None, **kw):
        ref = ev.get("ref") if ev else None
        old = self.state
        if old["state"] == state and old.get("ref") == ref and all(old.get(k) == v for k, v in kw.items()):
            return
        where = {k: ev.get(k) for k in ("book", "chapter", "verse", "verse_end")} if ev else {}
        self.state = dict({"state": state, "ref": ref}, since=self.clock(), **where, **kw)
        out = {"type": "state", "state": state, "level": "strong" if state == "active" else "weak",
               "ref": ref, "was": old["state"]}
        if ev:
            for k in ("book", "chapter", "verse", "verse_end", "cited", "via", "spoken", "corrected_from"):
                if ev.get(k) is not None:
                    out[k] = ev[k]
        out.update(kw)
        self._emit(out)

    def _advance(self, ev):
        st, kind, state = self.state["state"], ev["type"], ev.get("state")
        if kind == "request":
            self._set_state("active", ev, why="request")
        elif kind == "reading" and state == "start":
            self._set_state("active", ev, why="reading")
        elif kind == "reading" and state in ("verse", "resume") and st == "active":
            self._set_state("active", ev, why=self.state.get("why", "reading"), paused=False)
        elif kind == "reading" and state == "pause" and st == "active":
            self._set_state("active", dict(ev, ref=self.state["ref"]), why=self.state.get("why", "reading"),
                            paused=True)
        elif kind == "reading" and state == "end":
            if st == "active":
                self._set_state("idle", ev, why="finished")
        elif st != "active" and (kind == "citation" or state == "allusion"):
            if kind == "citation":
                self.since_cite = 0
            self._set_state("armed", ev, why="allusion" if state == "allusion" else "citation",
                            imminent=bool(ev.get("reading_now")))

    def _expire(self, now):
        self.armed = {i: a for i, a in self.armed.items() if a[0] > now}
        st = self.state
        if st["state"] == "alert" and now > self.alert_until:
            self._set_state("idle", None, why="quiet")
        elif st["state"] == "armed" and now - st["since"] > ARMED_TTL_S and self.reading is None:
            self._set_state("idle", None, why="quiet")
        elif st["state"] == "active" and st.get("why") == "request" and \
                self.reading is None and now - st["since"] > ARMED_TTL_S:
            self._set_state("idle", None, why="quiet")      # asked for, never read: clear after a while

    # -- citations
    def _cite_ev(self, how, b, c, v, v2, ref, **kw):
        strong = bool(ref.get("reading_now")) and v is not None
        r = self.reading
        if strong and r and r["strong"] and not self._lo(r) <= self.canon.at[(b, c, v)] <= r["i"] + 3:
            self._end_reading()                 # he has moved on to another passage
        return dict({"type": "citation", "how": how, "level": "strong" if strong else "weak",
                     "ref": self.canon.ref(b, c, v, v2), "book": b, "chapter": c, "verse": v, "verse_end": v2,
                     "reading_now": bool(ref.get("reading_now"))}, **kw)

    def _once(self, key):
        now = self.clock()
        if now - self.seen.get(key, -1e9) > 60:
            self.seen[key] = now
            return True
        return False

    def _take(self, ref, said):
        """One reference the reader heard -> (b, c, v, v2) it settled on, or None."""
        b = BOOKS_EN.index(ref["book"]) if ref.get("book") in BOOKS_EN else None
        c, v, v2 = ref.get("chapter"), ref.get("verse"), ref.get("verse_end")
        last_n = ref.get("last_n") if v is None else None
        if v2 is not None and v is not None and v2 <= v:
            v2 = None
        # Its numbers must be in the words: in testing the model once gave a quote he
        # never referenced ("A fost silit să ia crucea" -> Matthew 27:32). Soniox
        # writes spoken numbers as digits; with none in the words, only a verse
        # alone passes (a spelled-out "versetul întâi").
        digits = set(int(d) for d in re.findall(r"\d+", said))
        if digits and any(n is not None and n not in digits for n in (c, v)):
            return None
        if not digits and (c is not None or (v is not None and b is not None)):
            return None
        if b is not None and len(self.canon.chapters[b]) == 1:
            # a book of one chapter is cited by verse: "3 John, verse 4", "Jude 3"
            if c is None and v is not None:
                c = 1
            elif v is None and c is not None and c > 1:
                c, v = 1, c
        if last_n and last_n > 0:
            # "ultimele 3 versete": the last N of the chapter he named (here, or just before)
            if c is None:
                fit = [(xb, xc) for xb, xc in self.memory.chapters() if b is None or xb == b]
                if not fit:
                    return None
                b, c = fit[0]
            if not self.canon.has(b, c):
                return None
            n = self.canon.chapters[b][c - 1]
            v, v2 = max(1, n - last_n + 1), n
            if v2 == v:
                v2 = None
        if b is not None and c is None and v is None:
            p, self.pending = self.pending, None
            if p and self.clock() - p[3] < PENDING_S and self.canon.has(b, p[0], p[1]):
                # "Capitolul 15, versetul 21," then "în Marcu": the book completes it
                return self._take(dict(ref, book=BOOKS_EN[b], chapter=p[0], verse=p[1], verse_end=p[2],
                                       last_n=None), said + " " + " ".join(str(n) for n in p[:3] if n))
            self.memory.note(b, via="citation")            # a book alone: the scratchpad only
            return None
        if b is not None and c is not None:
            if not self.canon.has(b, c, v):
                return None                                # no such chapter or verse: not a reference
            st = self.state
            if v is None and st["state"] == "armed" and st.get("verse") and self.clock() - st["since"] < 15 \
                    and (st.get("book"), st.get("chapter")) != (b, c):
                return None         # a bare chapter does not replace a verse he named moments ago
            r = self.reading
            if r and v is not None:
                rb, rc, rv = self.canon.where[r["i"]]
                if (rb, rc) == (b, c) and self.armed.get(self.canon.at[(b, c, v)], (0, None))[1] == \
                        self.canon.ref(b, c, v, v2):
                    return b, c, v, v2      # the passage he is reading, as he named it: those words, heard again
                if (rb, rc) != (b, c) or not rv - 1 <= v <= rv + 1:
                    self._end_reading()     # he names another passage (or jumps within this one)
            elif r and r["paused"] is not None and self.canon.where[r["i"]][:2] != (b, c):
                self._end_reading()     # he paused one passage and names another: it is over
            self.memory.note(b, c, v, "citation")
            if v is None:                                  # a chapter alone
                self._arm(b, c, 1, 3, self.canon.ref(b, c))
                if self._once((b, c, None)):
                    self._emit(self._cite_ev("chapter", b, c, None, None, ref))
                return b, c, None, None
            self._arm(b, c, v, v2, self.canon.ref(b, c, v, v2))
            if self._once((b, c, v)):
                self._emit(self._cite_ev("full", b, c, v, v2, ref))
            return b, c, v, v2
        if v is None:
            return None
        # a verse alone (or with only its book, or only its chapter: "capitolul 15,
        # versetul 21" before "Marcu"): the chapters in the scratchpad that fit. None
        # fits: wait, the book usually follows in the next packet.
        fit = [(xb, xc) for xb, xc in self.memory.chapters() if (b is None or xb == b)
               and (c is None or xc == c) and self.canon.has(xb, xc, v)]
        if not fit:
            if c is not None:
                self.pending = (c, v, v2, self.clock())
            return None
        for xb, xc in fit:
            self._arm(xb, xc, v, v2, self.canon.ref(xb, xc, v, v2))
        tb, tc = fit[0]
        self.memory.note(tb, tc, v, "citation")
        if self._once((tb, tc, v)):
            self._emit(self._cite_ev("verse", tb, tc, v, v2, ref,
                                     candidates=[self.canon.ref(xb, xc, v, v2) for xb, xc in fit[1:]]))
        return tb, tc, v, v2

    # -- "could we have this on the screen?"
    def hear(self, text):
        """Cheap, any time (a packet, or Soniox's draft words): do the church's own
        request phrases occur? Then dispatch at once."""
        if not self.request_words:
            return
        k = " ".join(keys(text))
        if any(w and w in k for w in self.request_words):
            with self.lock:
                self._request("words", text)

    def _request(self, via, spoken, named=None):
        """Emit a request: `named` (b, c, v, v2) if the request said which, else the
        newest verse (or chapter) in the scratchpad."""
        now = self.clock()
        if named is None:
            live = self.memory.live()
            x = next((x for x in live if x["verse"] is not None), None) or \
                next((x for x in live if x["chapter"] is not None), None)
            named = (x["book"], x["chapter"], x["verse"], None) if x else None
        ref = self.canon.ref(*named) if named else None
        when, last = self.last_request
        if now - when < REQUEST_GAP_S and (ref == last or via == "words" or ref is None):
            return                  # the same request heard again (words, then the reader)
        self.last_request = (now, ref)
        ev = {"type": "request", "level": "strong", "via": via, "spoken": (spoken or "")[:160], "ref": ref}
        if named:
            b, c, v, v2 = named
            ev.update(book=b, chapter=c, verse=v, verse_end=v2)
            if v is not None:
                self._arm(b, c, v, v2, ref)
        self._emit(ev)


class Dispatcher:
    """Runs a detector's calls on a few threads, so a slow reader never holds up the
    gate or the tracker; answers are applied as they come back."""

    def __init__(self, det, workers=6):
        self.det = det
        self.pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="attendant")

    def packet(self, pkt):
        with self.det.lock:
            calls = self.det.on_packet(pkt)
        for call in calls:
            self.pool.submit(self._run, call)

    def _run(self, call):
        try:
            res = self.det.run_call(call)
        except Exception as e:
            self.det.errors += 1
            self.det.last_error = f"{call.kind}: {str(e)[:200]}"
            res = None
        with self.det.lock:
            more = self.det.on_result(call, res)
        for c in more:
            self.pool.submit(self._run, c)


if __name__ == "__main__":
    import sys
    det = VerseDetector(lambda ev: print(json.dumps(ev, ensure_ascii=False)))
    for chunk in sys.argv[1:]:
        det.feed(chunk)
