#!/usr/bin/env python3
"""Any-language -> one chosen target translation via the Anthropic Claude API (cloud).

The sole translator for Church Translate. Loads the editable system prompt
and injects the biblical name glossary + idiom library, so spoken speech is
rendered as clear, reverent text with correct proper-name spelling, whatever
language it was spoken in.

One prompt serves every target: each segment is sent with a target-language
tag (e.g. "->English:", "->Spanish:") so the model knows which language to
translate INTO -- the source language is whatever the speaker used and needs
no tag of its own. This lets a single rolling history (kept per target
language) share the carefully tuned hardening (anti-chat, mis-hear recovery,
faithful scriptural language) across every target.

Built for live captioning: the client uses a short request timeout (a hung
call must not stall the caption feed), and translate() accepts a rolling
`history` of recent (source, target) pairs -- all translated INTO THE SAME
target language, though each pair's source may have been a different spoken
language -- so each segment is translated with the sermon's running context
(pronouns, ongoing metaphors, verse-by-verse readings) instead of in isolation.

Requires:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

Usage:
    python translate_claude.py --text "Dumnezeu este bun."
    python translate_claude.py --target es --text "God is good."
    python translate_claude.py --model claude-haiku-4-5 --text "..."
"""
import argparse
import functools
import os
import re
import sys
import threading
import time
import unicodedata

import languages

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_TARGET_LANG = "en"

# code -> English display name, built from scripts/languages.py. Every language a
# translator model can caption is a TARGET; only the ones Soniox hears are a
# SOURCE (a language hint, or a side of a bilingual pair). Whether a target can
# also be spoken depends on the voice backend and model (languages.can_speak) --
# the target list is no longer cut down to what every voice can say. "auto" is a
# source-only pseudo-code, kept in the same table so the UI builds its menus from
# one server-side list (via /caps).
LANGUAGES = {"auto": "Auto-detect",
             **dict(sorted(languages.CAPTION.items(), key=lambda kv: kv[1]))}
SOURCE_LANGUAGES = {k: v for k, v in LANGUAGES.items() if k == "auto" or k in languages.HEARD}
TARGET_LANGUAGES = {k: v for k, v in LANGUAGES.items() if k != "auto"}

# Models the live UI may pick between. Haiku is the fast default; Sonnet 5 trades
# latency/cost for stronger contextual inference (homophones, mis-segmentation,
# reading intent "between the lines"). The server validates against this set
# (claude-sonnet-5 and claude-sonnet-4-6 kept for back-compat with any saved UI
# choice; the picker offers Haiku + Sonnet 5.5, which replaced Sonnet 5 on
# 2026-10-01: 0.81 s to the first word against Sonnet 5's ~1.4 s caption, and the
# most unaided Romanian of the fast tier -- validation/results/2026-10-01-fast-models).
ALLOWED_MODELS = ("claude-haiku-4-5", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6")
# Thinking pinned OFF, sent explicitly: Haiku 5.5 (released 2026-10-07) accepts
# thinking "disabled" but, like the rest of the 5 family, may think when the
# parameter is left out -- and any thinking lands on time-to-first-word.
# Sonnet 5.5 rejects "disabled" (400: send "between_tools" instead); with no
# tools in the request, "between_tools" means it does not think at all.
# Sonnet 5.5 moved here from THINKING_MODELS on 2026-10-19: thinking off scored
# 56/56 + 24/26 on the context-tangle battery against 53/56 + 24/26 at adaptive
# low, no slower (first word 0.69 / 1.01 s p50/p90 vs 0.73 / 1.04), and low
# had put a caption up blank when the model skipped the caption mark
# (validation/results/2026-10-19-context-tangle/).
NO_THINKING_MODELS = ("claude-haiku-5-5", "claude-sonnet-5-5")
THINKING_OFF = {"claude-sonnet-5-5": {"type": "between_tools"}}
# Models that get Soniox's confidence brackets as plain text instead: the hint
# made Haiku 5.5 worse (12 -> 9 of 22) and it copied bracketed Romanian words
# into the English untranslated (validation/results/2026-10-07-haiku-5-5/).
NO_UNSURE_HINT = ("claude-haiku-5-5",)

# Models steered by adaptive thinking + the effort parameter: the retired Sonnet 5
# (kept for saved settings) and Opus, which the picker does not offer. Adaptive
# thinking reasons only on the segments it judges ambiguous; these models open
# the caption with CAPTION_MARK so reasoning that slips into the reply is held back.
THINKING_MODELS = ("claude-sonnet-5", "claude-opus-5-5", "claude-opus-5-5-fast")

# Picker ids that run a real model in fast mode (Claude API only, 2x price).
FAST_MODELS = {"claude-opus-5-5-fast": "claude-opus-5-5"}

# Reasoning effort for the thinking models: pinned to LOW.
# (The levels below were measured on Sonnet 5.)
# Measured on the validation/scripts/effort_sweep.py + stress corpora: "medium" matched
# "low" on both quality and latency, while "medium"/"high" each produced at
# least one caption that editorialized about a mis-hear instead of just
# translating -- so higher effort buys nothing and carries caption risk.
# (validation/scripts/effort_sweep.py overrides this module attribute to compare levels.)
TRANSLATE_EFFORT = "low"


def _rejects_sampling(model):
    """The Claude 5 family rejects temperature/top_p/top_k with a 400 (Haiku 5.5:
    "`temperature` is deprecated for this model")."""
    return (model or "").startswith(("claude-sonnet-5", "claude-opus-5", "claude-haiku-5"))


def _tag_for(target_lang):
    """Target-language tag prefixed to every segment (and every history user
    turn) so the model translates INTO the right language, e.g. "→Spanish:".
    The prompt explains the tag scheme generically."""
    name = LANGUAGES.get(target_lang, LANGUAGES[DEFAULT_TARGET_LANG])
    return f"→{name}:"


# ---- draft anchoring (optional, per call) ---------------------------------
# The live draft the room is ALREADY reading -- Soniox's own fast translation,
# on screen about a second after the words are spoken -- handed to the model so
# the final can keep that wording wherever keeping it costs nothing. The aim is
# purely visual: every word the two translations agree on is a word that does
# not move when the draft resolves into the final, and stillness is what makes
# a caption readable. Quality still wins, and the instruction says so plainly:
# a change the meaning needs is always made.
#
# The draft is a TRUNCATED PREFIX as often as not. Soniox's translation lags its
# own transcription, so at the moment a unit is dispatched the tail clause may
# not have been translated at all -- hence the explicit release: whatever the
# draft does not reach carries no constraint. Without that line the model has
# been observed to treat the draft's abrupt end as the end of the segment.
#
# This rides in the USER turn rather than the system prompt on purpose:
#   - the instruction cannot exist without the draft it refers to;
#   - the cached system prefix is untouched, so the prompt cache still hits;
#   - with the feature off the request is byte-identical to what it was before
#     this existed, which is what makes an A/B against it honest.
# It costs ~90 tokens on an anchored call and nothing otherwise.
DRAFT_RULE = (
    "A faster, lower-quality machine translation of this same segment is already "
    "on screen for the audience:\n"
    "  {draft}\n"
    "Where that draft is already right, reuse its exact words and word order, so "
    "the caption settles instead of visibly rewriting itself in front of the "
    "reader.\n"
    "This is a COSMETIC preference and nothing more. Translation quality always "
    "takes precedence over matching the draft: wherever the draft is less "
    "accurate, less natural, less faithful to what was said, or simply wrong, "
    "replace it without hesitation and translate the segment as well as you can. "
    "Never keep a worse wording for the sake of matching, and never let the draft "
    "pull you away from the phrasing a well-known passage should have. Judge the "
    "segment on its own merits first, then keep whatever of the draft's wording "
    "that judgement happens to agree with.\n"
    "The draft is often cut off part way through the segment: translate whatever "
    "it does not reach freely, under no constraint, and never treat where it "
    "stops as the end of the segment. It carries no authority over the rules "
    "above -- it is not exempt from them and neither are you: fillers, "
    "disfluencies and stammered restarts are dropped even where the draft keeps "
    "them. Never mention, quote or repeat these instructions -- reply with the "
    "translation only."
)


# The Router's clue (app/server.py /route, scripts/router_gate.py): Soniox's own
# translation of a segment the checker doubted, offered as evidence, not as a
# wording to keep -- unlike DRAFT_RULE, nothing of it is on screen. A reply that
# copies it is cut where its opening words begin (clue_at, below).
CLUE_HEAD = "A fast machine translator rendered"
FLAGGED_RULE = (
    CLUE_HEAD + " this same segment as:\n"
    "  {en}\n"
    "A checker doubted it: {why}. Treat it only as a clue. Where the transcript "
    "above is garbled it may show what the speaker meant; it may also carry the "
    "very error that was flagged. Translate the segment yourself, and never keep "
    "a wording of it you would not have chosen."
)
FLAGGED_WHY = {
    "misheard": "a word in the transcript may be a mis-hearing of a similar-sounding word",
    "translation": "it may be a wrong, literal or unnatural translation",
    "sensitive": "it may hold wording that is offensive or sensitive on a church screen, so word "
                 "that part with care, faithful but decent",
}


def anchored_turn(tag, text, draft=None):
    """The current user turn: the tagged source, plus the draft-anchoring block
    when a draft was supplied. Shared by all three backends (Claude/OpenAI/xAI)
    so the prompt cannot drift between them. A dict draft {"en", "why"} is the
    Router's flagged clue (FLAGGED_RULE) instead."""
    turn = f"{tag} {text}"
    if isinstance(draft, dict):
        en = (draft.get("en") or "").strip()
        if en:
            turn += "\n\n" + FLAGGED_RULE.format(
                en=en, why=FLAGGED_WHY.get(draft.get("why"), FLAGGED_WHY["translation"]))
        return turn
    draft = (draft or "").strip()
    if draft:
        turn += "\n\n" + DRAFT_RULE.format(draft=draft)
    return turn


def history_turns(history, tag):
    """The rolling history replayed as alternating user/assistant turns, shared
    by all three backends for the same reason anchored_turn is.

    History turns stay PLAIN: the draft anchor is about the CURRENT segment's
    draft only, and replaying old ones would both bloat the context and teach the
    model that the block is part of the expected format.

    Entries are (src, tgt) or (src, tgt, target_lang). The 3-tuple form matters
    in a bilingual session, where one pooled history spans BOTH directions:
    stamping the current tag on a turn that was translated the other way would
    show the model a "->English:" turn answered in Romanian -- a worked example
    of ignoring the tag, in the very slot the tag is meant to be learned from.
    A 2-tuple (what the offline harnesses pass) keeps the old meaning: same
    target as this call, so `tag` applies."""
    turns = []
    for entry in (history or []):
        src, tgt = entry[0], entry[1]
        if not (src and tgt):        # the APIs reject empty turns
            continue
        turn_tag = _tag_for(entry[2]) if len(entry) > 2 and entry[2] else tag
        turns.append({"role": "user", "content": f"{turn_tag} {src}"})
        turns.append({"role": "assistant", "content": tgt})
    return turns

_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
PROMPT_FILE = os.path.join(_CONFIG, "translation_prompt.txt")
GLOSSARY_FILE = os.path.join(_CONFIG, "glossary.tsv")   # one file, "## NAMES" / "## IDIOMS" sections

# Fallback if the editable prompt file is missing.
_DEFAULT_PROMPT = (
    "You are a translator for a Christian church service. Each message begins "
    "with a target tag in the form '→Language:' (e.g. '→English:', "
    "'→Spanish:') naming the language to translate the rest of the message "
    "INTO -- the source may be any spoken language. The text is from "
    "speech-to-text and may contain mis-heard words: infer the intended word "
    "from biblical context. Use standard Bible phrasing in the target "
    "language. Render proper names in correct biblical spelling; snap a "
    "close/mis-heard word to a Known name below. "
    "Output ONLY the translation, in the target language.\n\n{glossary}"
)


_GLOSSARY_SECTIONS = {"## NAMES": "names", "## IDIOMS": "idioms"}


def load_glossary(path=GLOSSARY_FILE):
    """Parse the merged congregation glossary into {"names": [...], "idioms":
    [...]} -- each a list of (ROMANIAN, ENGLISH) pairs -- split by "## NAMES" /
    "## IDIOMS" section header lines (skips comments/blank lines within each).

    The names section is authored as Romanian<->English pairs, so it's a
    reliable "this is probably a known biblical name, not a mis-hearing"
    anchor for ANY target language, but the literal spelling snap it
    describes is only guaranteed correct when target_lang is "en" (or the
    source actually is Romanian). For other targets (Spanish, French, ...)
    Claude still has to render the name in that language's own convention
    from its own knowledge -- an accepted limitation, not a bug."""
    sections = {"names": [], "idioms": []}
    current = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                header = line.strip()
                if header in _GLOSSARY_SECTIONS:
                    current = _GLOSSARY_SECTIONS[header]
                    continue
                content = line.split("#", 1)[0].rstrip("\n")
                if current and "\t" in content:
                    ro, en = content.split("\t", 1)
                    if ro.strip() and en.strip():
                        sections[current].append((ro.strip(), en.strip()))
    return sections


# Bilingual-pair note, injected at {pair} when the session is running a language
# PAIR with automatic direction (two hints, e.g. Romanian + English) instead of
# "any source -> one fixed target". Its whole job is to stop the model treating a
# switch between the two as something to repair: the mis-hear rules above tell it
# to silently fix a word that "fits the sounds but not the sense", and without
# this an English sentence dropped into a Romanian sermon is exactly that shape.
# It deliberately does NOT tell the model to flip direction on its own -- the
# direction is decided in the browser from Soniox's per-token language ID and
# arrives as the tag, so the model's job stays "translate into the tagged
# language" whatever the source turns out to be.
_PAIR_NOTE = (
    "This service is bilingual: the speech may be in {a} or {b}, and either can "
    "appear at any moment. A switch between those two languages is normal and "
    "expected -- it is never a transcription error, so never \"correct\" a word "
    "or sentence in one of them into the other on the grounds that it sounds out "
    "of place. The tag still names the single language to translate INTO: follow "
    "it exactly, and where part of the message is already in that tagged "
    "language, reproduce that part verbatim as instructed above rather than "
    "rewriting it."
)


def pair_note(pair):
    """`pair` is a (code_a, code_b) tuple, or None for a one-way session."""
    if not pair:
        return ""
    a, b = (LANGUAGES.get(c, c) for c in pair)
    return _PAIR_NOTE.format(a=a, b=b)


# Delivery tags the translator may lead a reply with when the voice is
# expressive (ElevenLabs v3/v4 audio tags). Voice-quality words only: v4 can
# take a tag as a sound-effect cue ([laughs], [applause]), and a pace tag
# ([slowly], [pause]) would fight the drift controller, so neither is listed.
# The same tuple builds the prompt paragraph below and VoiceTagFilter's list,
# so what the model is offered and what the server accepts cannot drift apart.
VOICE_TAGS = ("gently", "warmly", "solemnly", "reverently", "joyfully",
              "earnestly", "with conviction", "urgently", "sadly", "firmly")

# Appended to the system prompt only when the request asks for it, so with the
# box off the prompt is byte-identical to what it was -- which keeps an A/B
# between the two honest and the prompt cache warm for the ordinary case.
VOICE_TAG_NOTE = (
    "VOICE TAG. This caption is also spoken by an expressive voice that takes a "
    "delivery hint. When the words call for a particular delivery, begin your "
    "reply with exactly one tag in square brackets from this list: "
    + " ".join(f"[{t}]" for t in VOICE_TAGS) + ". Give one to a prayer or a "
    "line spoken to God, a direct appeal to the listeners, a warning or rebuke, "
    "a lament, a burst of praise, a poem or hymn being recited, a tender "
    "personal moment. Plain explanation, storytelling and announcements get "
    "none. Never use any other tag, never put one anywhere but the very start, "
    "and never let it change the translation — the tag is removed before the "
    "caption is shown.")


class VoiceTagFilter:
    """Takes voice tags out of a streamed reply before any of it becomes caption
    text, so the wall, the history, the web relay and the recording never see
    one. Feed it the model's chunks in order; it returns the text to keep.

    A "[" holds the text behind it until its "]" arrives (at most HOLD chars --
    a tag is a word or two), which delays only the rare reply that has one, by
    a few tokens. What counts as a tag:
      * at the very start of the reply, any short bracketed run of letters. An
        unlisted one ([whispering]) is removed too but never spoken: it is
        certainly not caption text, and not a delivery the list allows.
      * anywhere else, only a listed one; any other bracket is left alone.
    `tag` is the listed leading tag, the one the voice gets; `dropped` collects
    every other tag removed, for the log."""
    HOLD = 40
    _PHRASE = re.compile(r"[A-Za-z][A-Za-z ,'-]*")

    def __init__(self):
        self.tag = None
        self.dropped = []
        self._held = ""          # text from an unresolved "[" on
        self._begun = False      # any caption text released yet?
        self._eat_space = False  # drop the space a removed tag leaves behind

    def _release(self, s):
        if self._eat_space and s:
            self._eat_space = False
            if s[0] == " ":
                s = s[1:]
        if s.strip():
            self._begun = True
        return s

    def _take(self, word):
        """Whether the bracketed `word` is a tag (and so removed), noting it."""
        low = word.strip().lower()
        if low in VOICE_TAGS:
            if self._begun or self.tag:
                self.dropped.append(low)
            else:
                self.tag = low
            return True
        if not self._begun and self._PHRASE.fullmatch(word.strip()):
            self.dropped.append(low)
            return True
        return False

    def feed(self, chunk):
        s, self._held, out = self._held + chunk, "", []
        while s:
            i = s.find("[")
            if i < 0:
                out.append(self._release(s))
                break
            if i:
                out.append(self._release(s[:i]))
                s = s[i:]
            j = s.find("]")
            if j < 0:
                if len(s) <= self.HOLD:
                    self._held = s              # wait for the rest of the bracket
                    break
                out.append(self._release("["))  # never closes: ordinary text
                s = s[1:]
            elif self._take(s[1:j]):
                self._eat_space = True
                s = s[j + 1:]
            else:
                out.append(self._release(s[:j + 1]))
                s = s[j + 1:]
        return "".join(out)

    def flush(self):
        """The end of the reply: whatever is still held is either the stub of a
        tag (dropped) or ordinary text (kept)."""
        s, self._held = self._held, ""
        if s and not self._begun and self._PHRASE.fullmatch(s[1:].strip()):
            self.dropped.append(s[1:].strip().lower())
            return ""
        return self._release(s)


# ---- words Soniox was unsure of ----------------------------------------------
# Soniox scores every token; a word's score is its least confident token. The
# low ones are bracketed in the CURRENT user turn (history stays plain, like the
# draft anchor) so the mis-hear rule knows where to look. Low confidence is a
# pointer, not a verdict: on real sermons most words under 0.5 are right, so the
# rule tells the model to keep a bracketed word that fits.
UNSURE_BELOW = float(os.environ.get("CT_UNSURE_BELOW", "0.5"))   # 0 turns it off
UNSURE_OPEN, UNSURE_CLOSE = "⟦", "⟧"
UNSURE_RULE = (
    "Words in ⟦ ⟧ are ones the speech recognizer was unsure of. Each may be "
    "exactly what was said, or a mis-hearing of a similar-sounding word. Keep it "
    "when it fits the sentence and the lines before it; when it does not, "
    "translate the similar-sounding word that does. Unbracketed words can be "
    "mis-heard too. Never output the brackets."
)
_EDGES = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def unsure_turn(text, conf, below=None):
    """`text` with each word scored under `below` bracketed, followed by
    UNSURE_RULE -- or `text` unchanged when no word qualifies. `conf` holds one
    score per whitespace-separated word of `text` (None where unknown); a list
    that doesn't line up is ignored rather than guessed at."""
    below = UNSURE_BELOW if below is None else below
    words = text.split()
    if below <= 0 or not conf or len(conf) != len(words):
        return text
    out, n = [], 0
    for w, c in zip(words, conf):
        lead, core, trail = _EDGES.match(w).groups()
        if isinstance(c, (int, float)) and c < below and core:
            out.append(f"{lead}{UNSURE_OPEN}{core}{UNSURE_CLOSE}{trail}")
            n += 1
        else:
            out.append(w)
    return " ".join(out) + "\n\n" + UNSURE_RULE if n else text


def drop_unsure_marks(s):
    return s.replace(UNSURE_OPEN, "").replace(UNSURE_CLOSE, "")


# ---- caption mark --------------------------------------------------------------
# A thinking model at low effort sometimes skips its thinking and reasons in the
# reply instead, and that reasoning went up on the wall in a service. Those
# models open the caption with CAPTION_MARK; CaptionMarkFilter throws away
# whatever precedes it.
CAPTION_MARK = "⟫"


def caption_mark_note(expressive=False):
    tag = " (a voice tag, when you give one, goes straight after ⟫)" if expressive else ""
    return ("CAPTION MARK. Begin the caption with ⟫. Everything before the first ⟫ "
            "is thrown away; everything after it is shown and read aloud. Do your "
            "working-out in your thinking, not in the reply. If any reasoning does "
            "end up in the reply, it goes before ⟫, never after it. After ⟫ comes "
            "the translation and nothing else" + tag + ".")


class CaptionMarkFilter:
    """Holds a streamed reply until CAPTION_MARK, then passes what follows.
    `before` keeps what preceded the mark (the log shows it); `seen` says
    whether the mark came at all."""

    def __init__(self):
        self.seen = False
        self.before = ""

    def feed(self, chunk):
        if self.seen:
            return chunk.replace(CAPTION_MARK, "")
        self.before += chunk
        i = self.before.find(CAPTION_MARK)
        if i < 0:
            return ""
        self.seen = True
        rest = self.before[i + 1:]
        self.before = self.before[:i]
        return rest.lstrip(" ").replace(CAPTION_MARK, "")

    def flush(self):
        """End of reply with no mark: the held text is dropped. A reply that
        never reached its caption shows nothing rather than its reasoning."""
        return ""


def build_system_prompt(prompt_file=PROMPT_FILE, glossary_file=GLOSSARY_FILE,
                        pair=None, expressive=False):
    """Editable prompt + injected name/idiom glossary -> system prompt.
    `expressive` appends VOICE_TAG_NOTE (the Expression box)."""
    if os.path.exists(prompt_file):
        with open(prompt_file, encoding="utf-8") as f:
            prompt = f.read()
    else:
        prompt = _DEFAULT_PROMPT

    sections = load_glossary(glossary_file)
    names = sections["names"]
    glossary = ""
    if names:
        # The name map is Romanian<->English; for those two target languages use
        # the matching form directly, and snap a close/mis-heard word to the
        # nearest entry. For any OTHER target language, treat these as reference
        # anchors ("this is a known biblical name, not a mis-hearing") and render
        # the name in that language's own standard biblical spelling.
        glossary = ("Known proper names (Romanian ⇄ English reference forms; use "
                    "whichever matches the target language, or -- for a different "
                    "target language -- that language's own standard spelling of "
                    "the same name; snap a close or mis-heard word to the nearest "
                    "one):\n"
                    + "; ".join(f"{ro} ⇄ {en}" for ro, en in names))

    idiom_pairs = sections["idioms"]
    idioms = ""
    if idiom_pairs:
        # Idioms are Romanian-source -> meaning, so they only apply when the
        # SOURCE speech is Romanian, regardless of target language; harmless to
        # keep present otherwise.
        idioms = ("Known Romanian idioms (when the source speech is Romanian, "
                  "render by these meanings, NOT literally):\n"
                  + "; ".join(f"{ro} -> {en}" for ro, en in idiom_pairs)
                  + "\n\nAn expression listed above under known idioms or regional vocabulary "
                  "is NEVER rendered word-for-word. Translate it by the meaning given, using "
                  "the closest natural expression in the target language.")

    # With no pair the placeholder is removed along with the blank line that
    # separated it, so a one-way system prompt is byte-identical to what it was
    # before pair mode existed -- which is what keeps an A/B against it honest.
    note = pair_note(pair)
    prompt = prompt.replace("{pair}", note) if note \
        else re.sub(r"\{pair\}\n{0,2}", "", prompt)
    prompt = prompt.replace("{glossary}", glossary).replace("{idioms}", idioms).strip()
    return prompt + "\n\n" + VOICE_TAG_NOTE if expressive else prompt


# Markers of a broken-role reply: the model talking ABOUT the task, the
# instructions, language identification, or to the user -- instead of just
# translating. Kept high-precision so a genuine translation (including a
# question the preacher asks the congregation, like "Can you confirm?") is
# never suppressed: every pattern references the translation task, these
# instructions, language ID, or the direction tag, none of which occur in real
# sermon content. The model's role-breaks usually trip several at once.
_META_SUPPRESS = re.compile(
    r"\btranslation engine\b"
    r"|\bI (?:do not|don't|cannot|can't) (?:converse|chat)\b"
    r"|→[A-Za-z]+:"                                        # the target-language tag leaking
    r"|\b(?:source|intended) language\b"                   # language-ID meta
    r"|\bfollowing my instructions\b"
    r"|\bthese instructions\b"
    r"|\bappears? to be (?:written )?in \w+,\s*not \w+"    # "appears to be in Spanish, not Romanian"
    r"|\bcannot be certain of the (?:intended )?(?:language|meaning)\b"
    r"|\bconfirm (?:the )?(?:source |intended )?language\b"
    r"|\bprovide (?:the |more )?(?:full )?context\b"
    # Refusals / "the speech-to-text is broken" meta -- a preacher's speech never
    # contains these, but a role-breaking model does (this exact class leaked
    # once: "I cannot translate this ... does not form a coherent Romanian
    # phrase ... provide the full sentence"). Anchored tightly so ordinary
    # sermon words ("God will provide", "I cannot serve two masters") don't trip.
    # ...but NOT "I can't translate that INTO English" -- that's a faithful
    # rendering of a preacher musing about translation ("nu stiu cum s-ar
    # traduce in engleza"), not a role-break. The refusal register is
    # "translate this/it" + stop or a quality complaint, never "into <language>".
    r"|\bI (?:cannot|can't|can not|am unable to|couldn't) translate (?:this|it|that|the\b)(?!\s+into\b)"
    r"|\bcorrupted speech-to-text\b"
    r"|\bdoes not (?:form|appear to be) a (?:coherent|complete|proper|valid|grammatical|meaningful)\b"
    r"|\bprovide (?:the |a )?(?:full|complete|whole|entire) (?:sentence|phrase|message)\b"
    r"|\bI'?ll translate it (?:accurately|correctly|properly)\b"
    r"|\bno translation (?:provided|possible|available|is possible)\b"
    r"|\binput is too (?:brief|short|vague|unclear|incomplete|ambiguous)\b"
    r"|\btoo (?:brief|short|vague|fragmentary|garbled|incomplete) to (?:translate|be translated)\b",
    re.IGNORECASE,
)


def looks_like_meta(text):
    """True if `text` -- even a partial, mid-stream reply -- already carries a
    role-break / refusal marker. _strip_meta only cleans the FINAL caption, but
    the live server streams deltas and speaks whole sentences AS THEY ARRIVE; a
    refusal would be shown and read aloud before the final text exists. This
    lets the streaming path stop the moment a reply turns into meta. Substring
    match (not the bare-parenthetical fullmatch, which needs the closing paren)
    -- refusals lead with a phrase pattern ("I cannot translate this",
    "No translation provided"), so they trip early, before the first sentence
    completes."""
    return bool(_META_SUPPRESS.search(text or ""))


# Letters from another alphabet that stand in for a Latin one. Used ONLY to
# repair a word that mixes scripts -- see _unmix_scripts.
_CONFUSABLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "з": "z",
    "и": "i", "і": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ы": "y", "э": "e", "ј": "j", "ѕ": "s",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "μ": "m", "ν": "n",
    "ο": "o", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "u", "χ": "x",
}
_CONFUSABLE.update({k.upper(): v.upper() for k, v in list(_CONFUSABLE.items())})
_FOREIGN = ("CYRILLIC", "GREEK")


def _script_of(ch):
    name = unicodedata.name(ch, "")
    for script in ("LATIN",) + _FOREIGN:
        if script in name:
            return script
    return None


def _unmix_scripts(text):
    """Repair a word that mixes alphabets, e.g. Romanian "întărите" carrying a
    Cyrillic и, т and е where i, t and e belong.

    The model does this occasionally and it is invisible in a transcript: the
    word reads normally here, shows as corrupted text on the caption wall, and
    is mis-voiced by text-to-speech. Asking the prompt to stop was measured and
    did not work (the substitution survived the instruction), so it is repaired
    here instead.

    The safety property is that only a word MIXING scripts is touched, and only
    when Latin is the majority: a wholly Cyrillic or Greek word is a legitimate
    Russian, Ukrainian or Greek translation and is never altered. A mixed word
    whose intruders are not all in the table is left exactly as it came, since a
    half-repair would be a different wrong word rather than a visible one."""
    def fix(match):
        word = match.group(0)
        scripts = [s for s in (_script_of(c) for c in word) if s]
        if "LATIN" not in scripts or not any(s in _FOREIGN for s in scripts):
            return word
        if scripts.count("LATIN") < sum(s in _FOREIGN for s in scripts):
            return word                      # not predominantly Latin: leave it
        fixed = "".join(_CONFUSABLE.get(c, c) if _script_of(c) in _FOREIGN else c
                        for c in word)
        if any(_script_of(c) in _FOREIGN for c in fixed):
            return word                      # cannot fully repair
        print(f"[claude] repaired mixed-script word {word!r} -> {fixed!r}",
              file=sys.stderr)
        return fixed
    return re.sub(r"\w+", fix, text)


def _strip_meta(text):
    """Drop trailing meta-commentary the model sometimes appends despite the
    'output only the translation' instruction (it would get read aloud / shown)."""
    # The Router's clue goes first (see clue_at), so the checks below judge only
    # the caption.
    text = text or ""
    end = clue_at(text)
    if end < len(text):
        print(f"[claude] WARNING: cut the Router's clue from the reply: "
              f"{text[end:].strip()[:80]!r}", file=sys.stderr)
        text = text[:end]
    # A reply that is ONE bare parenthetical is always a model annotation like
    # "(silence)", "(no translatable content)", or a whole refusal note like
    # "(No translation provided -- the input is too brief ...)" -- never a
    # translation, since the server strips input parentheticals before
    # translating. Bound is generous (a refusal note runs long) but finite, so a
    # genuine short aside that happens to be parenthesized isn't swallowed whole.
    text = _unmix_scripts(text)
    if re.fullmatch(r"\s*[(\[][^)\]]{0,200}[)\]]\s*", text):
        return ""
    lines = [ln for ln in text.splitlines()]
    while lines and re.match(r"^\s*[\(\[]?\s*(note|translation note|n\.?b\.?|explanation)\b",
                             lines[-1], re.IGNORECASE):
        lines.pop()
    text = "\n".join(lines).strip()
    # Safety net: if the model broke role and talked ABOUT the task/itself
    # (parroting the system prompt, naming the language, asking for context),
    # suppress the reply entirely -- meta-text must never be shown as a caption,
    # spoken aloud, or stored as translation context.
    if _META_SUPPRESS.search(text):
        print(f"[claude] WARNING: suppressed meta-response: {text[:80]!r}",
              file=sys.stderr)
        return ""
    return text


# ---- replayed captions --------------------------------------------------------
# The rolling history is the model's own earlier captions, replayed to it as its
# assistant turns. A reply that RE-TRANSLATES captions already on screen in front
# of its new words -- "stitching" an open fragment by restating it, or giving a
# bare predicate back its subject -- is wrong once; stored as it came, it becomes
# a worked example of "each reply restates the thought so far", and the model
# follows it. Observed 2026-09-25 (Grok 4.20, en->ro): two restating replies
# reached the six-turn history, and every reply after them restated everything
# since, longer each time.
#
# So a reply's head is judged against those same captions before anything sees
# it -- screen, projector, voice or history. It is cut only when it is a VERBATIM
# replay (every REPLAY_N consecutive words of it occur in the recent captions,
# read as one running text so a replay may span several), at least
# REPLAY_MIN_WORDS long, ends exactly where one of those captions ended, AND what
# is left after it still translates the whole source (at least REPLAY_KEEP of the
# source's word count) -- i.e. the head was surplus. That last test is what
# separates a restatement from a speaker who genuinely says the same thing again
# in other words: there the repeated caption IS this source's translation, and
# cutting it would leave too little. A source that opens with words recently
# translated is exempt outright.
#
# Measured over 2,421 recorded Haiku captions (two Romanian sermons and the LXX
# interview) with a six-turn history: nothing is cut. Without the last two tests
# three were, all genuine repeats ("They are all from Transylvania." said twice
# in different Romanian). On the 2026-09-25 session every restatement is cut to
# exactly its new words.
REPLAY_N = 3             # words per n-gram: "the same words", not the same topic
REPLAY_MIN_WORDS = 6     # a replayed head shorter than this is left alone
REPLAY_KEEP = 0.5        # what remains must be at least this share of the source's
                         # words; legit replies run ~1.0x, p1 0.4-0.6x
_WORD_RE = re.compile(r"\w+")
_REPLAY_TAIL = re.compile(r"[\s,.;:!?…\-–—”’»)\]]*")   # what closes a replayed head


def _word_keys(text):
    return [m.group().lower() for m in _WORD_RE.finditer(text or "")]


def _ngrams(texts, n=REPLAY_N):
    keys = [k for t in texts for k in _word_keys(t)]
    return {tuple(keys[i:i + n]) for i in range(len(keys) - n + 1)}


class ReplayGuard:
    """Judges one reply's head against the captions that preceded it.

    `prev_targets` / `prev_sources`: the history turns in THIS call's direction,
    oldest first; `source` is what this call translates. cut(text, final)
    returns how many leading characters of `text` merely replay earlier
    captions -- 0 when none -- or None while a streaming reply cannot be judged
    yet: every word so far could still belong to a replay, there are fewer than
    REPLAY_N whole words, or a replayed head has been found but not yet enough
    of what follows it. A streaming caller holds its deltas and its voice until
    it gets a number, which for an ordinary reply takes its first REPLAY_N
    words."""

    def __init__(self, prev_targets, source="", prev_sources=()):
        self.grams = _ngrams(prev_targets)
        self.ends = {tuple(k[-2:]) for k in map(_word_keys, prev_targets) if len(k) >= 2}
        src = _word_keys(source)
        self.need = REPLAY_KEEP * len(src)        # words that must follow a cut
        head = tuple(src[:REPLAY_N])
        self.armed = bool(self.grams) and not (
            len(head) == REPLAY_N and head in _ngrams(prev_sources))

    def cut(self, text, final=True):
        if not self.armed:
            return 0
        words = list(_WORD_RE.finditer(text or ""))
        if not final and words and words[-1].end() == len(text):
            words.pop()                           # the last word may still be growing
        keys = [m.group().lower() for m in words]
        k = 0
        while k + REPLAY_N <= len(keys) and tuple(keys[k:k + REPLAY_N]) in self.grams:
            k += 1
        if not final and k + REPLAY_N > len(keys):
            return None
        covered = k + REPLAY_N - 1 if k else 0    # leading words inside the replay
        for c in range(covered, REPLAY_MIN_WORDS - 1, -1):
            if tuple(keys[c - 2:c]) in self.ends:     # ...cut where a caption ended
                if len(keys) - c >= self.need:        # ...and the rest translates the source
                    return _REPLAY_TAIL.match(text, words[c - 1].end()).end()
                return 0 if final else None
        return 0


# ---- the Router's clue, copied into the reply ------------------------------------
# FLAGGED_RULE rides in the user turn after the source, and a model has copied it
# into its reply: on 2026-10-28 gpt-6.1-terra answered "Nu." with "No. A fast
# machine translator rendered this same segment as: No. A checker doubted it: ...",
# the whole block, and the wall, the voice, the website and the history all
# carried it (1 row of 523; reproduced the next day on a fresh server with no
# history). No part of the block is ever caption text, so a reply is cut where
# its opening words begin and the rest is dropped -- before the caption is shown,
# spoken, published or kept as context. Matched whatever the case and spacing; a
# clue the model translates into the target language instead is not caught.
_CLUE = re.compile(r"\b" + r"\s+".join(map(re.escape, CLUE_HEAD.split())) + r"\b", re.I)
_CLUE_KEY = " ".join(CLUE_HEAD.lower().split()) + " "
_CLUE_WORDS = len(CLUE_HEAD.split())
_WORD_START = re.compile(r"\b\w")


def clue_at(text, final=True):
    """How much of `text` may be shown: up to where CLUE_HEAD begins, or all of
    it. (The whitespace before the clue is left for the caller's strip.)

    While the reply is still streaming (final=False), a tail that could yet grow
    into CLUE_HEAD is held back too -- "A", "A fast", "a fast machine" -- so the
    clue's first words never reach the wall ahead of the rest. Ordinary text
    that only starts like it ("A fast car") is held for a word at most. What is
    shown only ever grows: a held tail either becomes the clue, and is cut
    there, or turns out not to be, and is released whole."""
    m = _CLUE.search(text)
    if m:
        return m.start()
    if not final:
        for start in [w.start() for w in _WORD_START.finditer(text)][-_CLUE_WORDS:]:
            if _CLUE_KEY.startswith(re.sub(r"\s+", " ", text[start:].lower())):
                return start
    return len(text)


SYSTEM_PROMPT = build_system_prompt()


@functools.lru_cache(maxsize=8)
def system_prompt_for(pair=None, expressive=False, marked=False):
    base = _system_prompt_for(pair, expressive)
    return base + "\n\n" + caption_mark_note(expressive) if marked else base


def _system_prompt_for(pair=None, expressive=False):
    """The system prompt for a session, memoized per language pair and
    Expression setting.

    Both are constant for a whole service, so this is a handful of distinct
    strings at most -- and memoizing matters: the prompt is the CACHED PREFIX of
    every request, so rebuilding it per call would be pointless churn, and a
    prompt whose bytes wobbled between calls would miss the prompt cache
    entirely. pair=None, expressive=False returns the module-level one-way
    prompt unchanged."""
    if not pair and not expressive:
        return SYSTEM_PROMPT
    return build_system_prompt(pair=pair, expressive=expressive)

# Live captions can't wait on a wedged request (the SDK default timeout is
# 10 minutes). Connect fails fast (network blips are the common case); read
# is roomier because a non-streaming response only arrives once the model
# finishes generating, and a coalesced catch-up batch can take a while.
# 1 SDK retry on top; the browser queue re-batches and retries the rest.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 25.0
MAX_RETRIES = 1

def ascii_key_problem(name, value):
    """Return a human-readable complaint if `value` (an API key) contains a
    non-ASCII character, else None.

    Such a character is almost always a curly quote or en/em-dash that got
    substituted for a straight quote or hyphen when the key was copied through
    Word / Outlook / Slack / a PDF. httpx refuses to put a non-ASCII byte in a
    request header, so the key surfaces only much later as an opaque
    UnicodeEncodeError 500 deep inside the SDK (httpx's _normalize_header_value)
    -- with no hint that the key itself is at fault. Catch it early and say so.
    """
    if not value:
        return None
    try:
        value.encode("ascii")
        return None
    except UnicodeEncodeError as exc:
        bad = value[exc.start:exc.end]
        return (f"{name} contains a non-ASCII character ({bad!r}) at position "
                f"{exc.start} -- usually a smart-quote or en/em-dash pasted in "
                f"place of a straight quote or hyphen. Re-enter the key exactly "
                f"as shown in the provider console (plain ASCII, no surrounding "
                f"quotes).")


# The SDK's default connection pool expires idle connections after just 5s
# (httpx.Limits keepalive_expiry) -- but caption segments typically arrive
# 5-20s apart, so nearly EVERY translation would silently pay a fresh TCP+TLS
# handshake. Keep pooled connections for 2 minutes instead: httpx discards a
# server-dropped socket at checkout, and the SDK's retry covers the rare
# closed-in-flight race, so a long expiry is safe.
KEEPALIVE_EXPIRY_S = 120.0

_client = None
_http_client = None

# Prompt-cache result of the LAST Claude call ON THIS THREAD, for the Translation
# panel's cache light. Thread-local, not a module global: /translate is served
# concurrently, and a shared slot would report another request's call.
#
# Why a side channel rather than a return value: translate_stream() yields plain
# text chunks, and app/server.py calls it through the SAME contract as the OpenAI
# and xAI translators. Threading usage through the yields would change that
# contract for all three providers to serve one provider's diagnostic.
_cache_stats = threading.local()


def last_cache_stats():
    """{"read": int, "write": int, "input": int} for this thread's last Claude
    call, or None if it hasn't made one. `read` > 0 means the system prefix was
    served from cache; `write` > 0 means this call created the entry; both zero
    means the prefix is under the model's minimum cacheable size and the
    cache_control marker was silently ignored (see _build_request)."""
    return getattr(_cache_stats, "last", None)


def _note_cache(usage):
    """Record one response's cache counters. Best-effort: a provider or SDK that
    doesn't report them must never break a translation."""
    try:
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        inp = getattr(usage, "input_tokens", 0) or 0
        # Anthropic's input_tokens is the UNCACHED part only; the other three
        # backends report the whole prompt. `prompt` is the whole prompt for
        # all four, so "read of prompt" reads the same on every provider.
        _cache_stats.last = {"read": read, "write": write, "input": inp,
                             "prompt": inp + read + write,
                             "output": getattr(usage, "output_tokens", 0) or 0}
    except Exception:
        _cache_stats.last = None


def _get_client():
    """One shared client (connection reuse) with live-caption timeouts."""
    global _client, _http_client
    if _client is None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "anthropic package not installed -- run: pip install anthropic"
            )
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        problem = ascii_key_problem("ANTHROPIC_API_KEY", key)
        if problem:
            # A clean RuntimeError instead of the opaque UnicodeEncodeError 500;
            # the caption server maps it to a readable error line for the browser.
            raise RuntimeError(problem)
        import httpx
        _http_client = httpx.Client(
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10,
                                keepalive_expiry=KEEPALIVE_EXPIRY_S),
        )
        _client = anthropic.Anthropic(
            http_client=_http_client,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            max_retries=MAX_RETRIES,
        )
    return _client


def warm():
    """Pre-build the shared client and complete the TCP+TLS handshake to the
    API host, so the FIRST caption of a service doesn't pay for either. The
    GET is unauthenticated and unbilled -- any HTTP response (401/404) means
    the connection is up and sitting in the pool. Best-effort: on any failure
    the first real call simply does its own handshake as before."""
    try:
        client = _get_client()
        _http_client.get(str(client.base_url))
    except Exception:
        pass


def _build_request(text, history, model, temperature, target_lang, draft=None,
                   pair=None, expressive=False):
    """Shared setup for translate()/translate_stream(): the message list (with
    rolling history) + the request kwargs (system prompt, thinking/effort
    for Sonnet 5, sampling params where accepted). Split out so the streaming
    and non-streaming call paths can't drift apart."""
    client = _get_client()
    tag = _tag_for(target_lang)
    messages = history_turns(history, tag)
    messages.append({"role": "user", "content": anchored_turn(tag, text, draft)})

    # With adaptive thinking on, max_tokens caps thinking + output together, so a
    # short translation can truncate under 1024 -- give the thinking models headroom.
    max_tokens = 2048 if model in THINKING_MODELS else 1024
    # The system prompt is marked for prompt caching, and it is large enough to
    # cache on EVERY model in ALLOWED_MODELS -- measured with count_tokens and
    # confirmed live (a cache read on the second call) on 2026-10-19, after the
    # idioms left config/glossary.tsv:
    #
    #   Haiku 4.5        one-way 4631 tok | bilingual 4744   (floor 4096)
    #   Haiku / Sonnet 5.5  one-way 6353  | bilingual 6504
    #
    # Haiku 4.5 once fell under its 4096 floor and the marker was silently
    # ignored (no cost, no error, just no cache); it now clears it by ~530
    # tokens, so TRIMMING config/translation_prompt.txt or config/glossary.tsv
    # much further can silently put it back under and quietly restore full input
    # price. The Translation panel's cache light exists to make that visible:
    # see last_cache_stats() above.
    system = [{"type": "text",
               "text": system_prompt_for(pair, expressive, marked=model in THINKING_MODELS),
               "cache_control": {"type": "ephemeral"}}]
    kwargs = dict(model=FAST_MODELS.get(model, model), max_tokens=max_tokens,
                  system=system, messages=messages)
    if model in FAST_MODELS:
        kwargs["speed"] = "fast"
        kwargs["betas"] = ["fast-mode-2026-02-01"]
    if not _rejects_sampling(model):
        kwargs["temperature"] = temperature   # Sonnet 5 rejects sampling params (400)
    if model in THINKING_MODELS:
        # Adaptive thinking reasons only where it helps (ambiguous segments), capped
        # at the effort level; disabling thinking would forfeit the very contextual
        # inference Sonnet 5 is chosen for. effort is GA (no beta header).
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": TRANSLATE_EFFORT}
    elif model in NO_THINKING_MODELS:
        kwargs["thinking"] = THINKING_OFF.get(model, {"type": "disabled"})
    return client, kwargs


def translate(text: str, history=None, model: str = DEFAULT_MODEL,
              temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
              draft: str = None, pair=None) -> str:
    """Translate one segment INTO `target_lang` (a code from LANGUAGES, e.g.
    "en", "es", "ro"). The source language is whatever the speaker used --
    Claude infers it directly, no source tag needed.

    `history` is a list of recent (source, target) pairs replayed as prior
    turns so the model keeps the sermon's context. Each pair's source may have
    been a DIFFERENT spoken language, and in a bilingual session the TARGETS
    differ too -- one pooled history spans both directions, so the context
    survives a speaker handover instead of being split in half. Every user turn
    (history and current) carries its own target-language tag, so a replayed
    turn translated the other way still reads correctly as an example.

    `pair` is the (code_a, code_b) tuple of a bilingual session, or None for a
    one-way one; it only selects the system prompt (see pair_note) and never
    decides direction -- target_lang alone does that."""
    import anthropic
    client, kwargs = _build_request(text, history, model, temperature, target_lang,
                                    draft, pair)
    api = client.beta.messages if "betas" in kwargs else client.messages
    try:
        response = api.create(**kwargs)
    except anthropic.APIError as exc:
        # Normalize to RuntimeError: the caption server maps that to a 503
        # the browser retries, instead of an opaque 500.
        raise RuntimeError(f"Claude API: {exc}") from exc

    _note_cache(response.usage)
    if response.stop_reason == "max_tokens":
        print("[claude] WARNING: translation truncated at max_tokens",
              file=sys.stderr)
    result = "".join(b.text for b in response.content if b.type == "text").strip()
    if model in THINKING_MODELS:
        mark = CaptionMarkFilter()
        result = (mark.feed(result) + mark.flush()).strip()
    return _strip_meta(result)


def translate_stream(text: str, history=None, model: str = DEFAULT_MODEL,
                     temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
                     draft: str = None, pair=None, expressive=False):
    """Like translate(), but yields the response text incrementally (as Claude
    generates it) instead of returning the complete string at once -- lets the
    caller start showing/using partial output (live captions, per-sentence TTS)
    before the model finishes the whole segment.

    Deliberately does NOT do meta-suppression or condense-marker splitting --
    those need the FULL text, so the caller (app/server.py) does that once the
    stream ends. This only streams raw model output -- including, when
    `expressive` is set, a leading voice tag for VoiceTagFilter to take off."""
    import anthropic
    client, kwargs = _build_request(text, history, model, temperature, target_lang,
                                    draft, pair, expressive)
    api = client.beta.messages if "betas" in kwargs else client.messages
    mark = CaptionMarkFilter() if model in THINKING_MODELS else None
    try:
        with api.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                out = mark.feed(chunk) if mark else chunk
                if out:
                    yield out
            final = stream.get_final_message()
    except anthropic.APIError as exc:
        raise RuntimeError(f"Claude API: {exc}") from exc

    if mark and mark.before.strip():
        print(f"[claude] dropped {len(mark.before)} chars before the caption mark: "
              f"{mark.before[:120]!r}", file=sys.stderr)
    if mark and not mark.seen:
        print("[claude] WARNING: reply had no caption mark; nothing shown", file=sys.stderr)
    _note_cache(final.usage)
    if final.stop_reason == "max_tokens":
        print("[claude] WARNING: translation truncated at max_tokens", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=None)
    ap.add_argument("--infile", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--target", default=DEFAULT_TARGET_LANG,
                    choices=sorted(TARGET_LANGUAGES),
                    help="language code to translate INTO (default: en); "
                         "source language is auto-inferred by Claude")
    args = ap.parse_args()

    if args.text is not None:
        src = args.text
    elif args.infile is not None:
        with open(args.infile, encoding="utf-8") as f:
            src = f.read()
    else:
        src = sys.stdin.read()
    src = src.strip()
    if not src:
        print("no input text", file=sys.stderr)
        return 1

    print(f"[claude] {args.model} (-> {LANGUAGES[args.target]}, Anthropic API)", file=sys.stderr)
    t0 = time.time()
    try:
        out = translate(src, model=args.model, temperature=args.temperature,
                        target_lang=args.target)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    dt = time.time() - t0

    print("\n=== source ===")
    print(src)
    print(f"\n=== {LANGUAGES[args.target]} (Claude translation) ===")
    print(out)
    print(f"\n[timing] {dt:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
