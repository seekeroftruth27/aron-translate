"""The voice's director: how each clip should be read, from a model of its own.

Two calls, both to a language model that is not the translator (the
translation prompt stays as it is, and this works whichever engine translated
the caption: Soniox's own line, or a translator model the Router sent it to):

  tag    the moment a sentence's first words arrive: one delivery tag from the
         Expression list (translate_claude.VOICE_TAGS) or none, from what has
         been said and those first words. Quick, and the fallback.
  style  the moment a clip is cut (voice_clauses): the clip as the voice
         should read it, written in the speech model's own markup -- at most
         one tag (only where a sentence begins), a word or two in CAPITALS
         where the sentence leans on it, an ellipsis or two where a weighty
         pause fits. The words themselves never change: check_styled() turns
         away any answer whose words, order or punctuation differ, or that
         styles more than that, and the clip is then read with the tag alone.
         Apart from the text, it may say how the voice should pronounce a word
         it would say wrong ("say": Job as "Jobe", 2026-11-20); apply_say()
         takes only respellings of words in the clip, in letters, and the
         caption on the wall keeps its own spelling.

Measured in the voice lab (validation/results/2026-11-17-voice-lab, Jo rating
blind, 2026-11-17/18): Claude Haiku 5.5 with the tag prompt, at low effort, was
rated 5 ("fantastic"); about $0.08 an hour of preaching for the tags and
1.4-2 s a call, against $3 and 2.1 s for Opus 5.5. Without an Anthropic key
the same prompts go to Luna (gpt-6-luna) on OpenAI's Chat Completions. A call
that is slow, fails or is refused means a plainer clip, never a held voice.

It is opt-in on the page (Settings › Voice › Expression tags, off by default).

    CT_VOICE_DIRECTOR        on (default: the setting is offered) | off
    CT_VOICE_DIRECTOR_MODEL  claude-haiku-5-5 (default; any Claude model)
"""
import collections
import concurrent.futures as cf
import json
import os
import re
import threading
import time

import costs
from translate_claude import VOICE_TAGS

ON = (os.environ.get("CT_VOICE_DIRECTOR") or "on").strip().lower() not in ("0", "off", "false", "no")
CLAUDE_MODEL = os.environ.get("CT_VOICE_DIRECTOR_MODEL") or "claude-haiku-5-5"
LUNA_MODEL = "gpt-6-luna"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
CONTEXT_SENTENCES = 14      # what has been said, as the lab asked it
CALL_TIMEOUT_S = 6.0        # past this the sentence has long been spoken
MAX_CAPS, MAX_PAUSES = 2, 2

GOAL = ("The goal is a voice that sounds natural and fits the moment, like a good interpreter: "
        "engaged and warm, never theatrical.")
WHEN = ("only when the moment clearly calls for a particular delivery: Scripture being quoted, words "
        "spoken to God, a direct appeal to the listeners, a searching question put to them, a warning, "
        "praise, a tender personal moment. Plain explanation, narration, lists and transitions get none")

SYSTEM = f"""You direct the delivery of a text-to-speech voice. It reads a live translation of a \
church sermon to the people in the room, a few words at a time, a moment behind the preacher. \
Before each new sentence you may give the voice ONE delivery tag, or none.

{GOAL} Most sentences need no tag. Give a tag {WHEN}. A run of sentences in the same mood (a \
series of appeals or questions) may keep the same tag.

Allowed tags: {", ".join(VOICE_TAGS)}. Answer "none" for no tag.

You see what has been said so far and only the first words of the new sentence: the rest has \
not been spoken yet."""

SCHEMA = {
    "type": "object",
    "properties": {
        "tag": {"type": "string", "enum": list(VOICE_TAGS) + ["none"]},
        "why": {"type": "string"},
    },
    "required": ["tag", "why"],
    "additionalProperties": False,
}

# How each speech model reads styled text (ElevenLabs: "Prompting Eleven v4",
# best practices; Soniox: TTS docs, emotion and tone). Both read the Expression
# tags as directions; none of them was spoken by either (2026-11-18).
ENGINE_NOTES = {
    "elevenlabs": ("The voice is ElevenLabs v4. It reads a [tag] in square brackets before the words as "
                   "how to deliver them, CAPITALS on a word as emphasis, and an ellipsis (…) as a pause "
                   "with weight; ordinary punctuation gives the rhythm. It takes no other markup."),
    "soniox": ("The voice is Soniox v2. It reads a [tag] in square brackets before the words as how to "
               "deliver them, a word in UPPERCASE as stress, and an ellipsis (…) as a pause; ordinary "
               "punctuation gives the rhythm. It takes no other markup."),
}


def style_system(engine):
    return f"""You prepare a live translation of a church sermon for a text-to-speech voice that \
reads it to the people in the room, one clip at a time, a moment behind the preacher. \
{ENGINE_NOTES.get(engine, ENGINE_NOTES["elevenlabs"])}

{GOAL} Keep every word of the clip exactly as it is, in the same order, with the same \
punctuation. You may only:
- begin the clip with ONE tag from this list, and only when the clip begins a sentence, {WHEN}: \
{", ".join(VOICE_TAGS)};
- write one word, at most two, in CAPITALS where the sentence clearly leans on it: the word a \
contrast turns on, the key word of an appeal;
- add an ellipsis (…), at most two, where a weighty pause fits.
Most clips need nothing at all. Less is more: when in doubt, return the clip unchanged.

Separately, under "say", list any word the voice would pronounce wrong, with a respelling it \
will read right: {{"word": the word exactly as it is in the clip, "say": the respelling}}. For \
example a Bible name it would read as an ordinary word (Job, said "Jobe"), a name or place it may \
not know, a word spelled alike but said differently where this sentence decides which (lead, \
read, live, wind, tear), or a Scripture reference in digits (John 3:16 as "John three sixteen"). \
Respell with letters only, as a reader of this language would sound it out. Keep the clip's own \
spelling in "text". Most clips need no respelling: leave "say" empty."""


STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "say": {"type": "array", "items": {
            "type": "object",
            "properties": {"word": {"type": "string"}, "say": {"type": "string"}},
            "required": ["word", "say"],
            "additionalProperties": False,
        }},
        "why": {"type": "string"},
    },
    "required": ["text", "say", "why"],
    "additionalProperties": False,
}
MAX_SAY = 4                 # respellings a clip may take


def prompt(said, first):
    return ("Said so far:\n" + "\n".join(said[-CONTEXT_SENTENCES:]) +
            f"\n\nThe new sentence begins: \"{first}\"\n\nWhich tag for this sentence?")


def style_prompt(said, clip, starts):
    where = ("it begins a sentence" if starts else
             "it continues the sentence before it, so it takes no tag")
    return ("Said so far:\n" + "\n".join(said[-CONTEXT_SENTENCES:]) +
            f"\n\nThe clip ({where}):\n\"{clip}\"\n\nReturn the clip as the voice should read it.")


def parse(text):
    """The tag in a model's answer, or None. Takes the JSON the models are asked
    for; anything else only if it names exactly one allowed tag."""
    try:
        tag = json.loads(text).get("tag")
    except (ValueError, AttributeError):
        low = (text or "").lower()
        hits = [t for t in VOICE_TAGS if t in low]
        tag = hits[0] if len(hits) == 1 else None
    return tag if tag in VOICE_TAGS else None


_LEAD_TAG = re.compile(r"^\s*\[([^\[\]]{1,40})\]\s*")
_WORD = re.compile(r"[^\W\d_]{2,}")


_QUOTES = re.compile("[\"“”„«»‘’']")


def _words_only(text):
    """The words and punctuation, without case, ellipses, quotation marks or
    extra spaces. Quotation marks are not spoken, and the director likes to
    mark Scripture as quoted (2026-11-18): harmless, so let through."""
    text = _QUOTES.sub("", text.replace("…", " ").replace("...", " "))
    return " ".join(text.lower().split())


def check_styled(original, styled, starts):
    """(tag or None, words) when `styled` is `original` with nothing but the
    allowed styling -- a listed tag in front (only where a sentence begins),
    up to MAX_CAPS words newly in capitals, up to MAX_PAUSES added ellipses --
    else None. A tag that is not allowed is dropped, keeping the rest."""
    if not isinstance(styled, str) or not styled.strip():
        return None
    tag = None
    m = _LEAD_TAG.match(styled)
    if m:
        tag = m.group(1).strip().lower()
        styled = styled[m.end():]
    if "[" in styled or "]" in styled:
        return None                                   # tags only in front
    if tag not in VOICE_TAGS or not starts:
        tag = None
    if _words_only(styled) != _words_only(original):
        return None                                   # a word, its order or punctuation changed
    was_caps = {w for w in _WORD.findall(original) if w.isupper()}
    if sum(1 for w in _WORD.findall(styled) if w.isupper() and w not in was_caps) > MAX_CAPS:
        return None
    pauses = lambda t: t.count("…") + t.count("...")   # noqa: E731
    if pauses(styled) - pauses(original) > MAX_PAUSES:
        return None
    return tag, styled.strip()


_RESPELL = re.compile(r"^[^\W\d_](?:[^\W\d_]|[' ,-])*$")      # letters, and ' , - and spaces


def apply_say(original, styled, say):
    """`styled` with each respelling in `say` ([{"word", "say"}]) put in for its
    word, for the voice only. A respelling counts only when its word is in the
    original clip (up to four words) and it is letters, spaces, hyphens,
    apostrophes and commas, not much longer than the word; others are dropped,
    as is everything past MAX_SAY."""
    if not isinstance(say, list):
        return styled
    for item in say[:MAX_SAY]:
        if not isinstance(item, dict):
            continue
        word, spoken = (item.get("word") or "").strip(), (item.get("say") or "").strip()
        if not word or not spoken or spoken == word or len(word.split()) > 4 \
                or not _RESPELL.match(spoken) or len(spoken) > 3 * len(word) + 20:
            continue
        exact = re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)")
        if not exact.search(original):
            continue
        if exact.search(styled):
            styled = exact.sub(lambda _m: spoken, styled)
        else:                                          # the director put it in capitals
            styled = re.sub(r"(?<!\w)" + re.escape(word.upper()) + r"(?!\w)",
                            lambda _m: spoken.upper(), styled)
    return styled


def backend():
    """Which model directs: Claude with an Anthropic key, else Luna with an
    OpenAI key, else none (the voice reads plainly)."""
    if not ON:
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    if os.environ.get("OPENAI_API_KEY"):
        return "luna"
    return None


_anthropic = None
_session = None


def _claude(system, user, schema):
    """Claude's JSON answer as text, or None if it declined."""
    global _anthropic
    import anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(timeout=CALL_TIMEOUT_S, max_retries=0)
    r = _anthropic.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    u = r.usage
    costs.llm(CLAUDE_MODEL, prompt=(u.input_tokens or 0) + (u.cache_read_input_tokens or 0),
              cached=u.cache_read_input_tokens or 0, output=u.output_tokens or 0,
              line="Voice director")
    if r.stop_reason == "refusal":
        return None
    return next((b.text for b in r.content if b.type == "text"), "")


def _luna(system, user, schema, shape):
    """Luna's JSON answer as text (Chat Completions, the way the translator
    calls it)."""
    global _session
    import requests
    if _session is None:
        _session = requests.Session()
    body = {
        "model": LUNA_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user + "\n\nAnswer with JSON only: " + shape}],
        "max_completion_tokens": 400,
        "reasoning_effort": "none",
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "voice_director", "strict": True, "schema": schema}},
    }
    headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
    r = _session.post(OPENAI_URL, json=body, headers=headers, timeout=(3, CALL_TIMEOUT_S))
    if r.status_code == 400 and "response_format" in r.text:
        body.pop("response_format")              # a model without structured output: plain JSON asked for
        r = _session.post(OPENAI_URL, json=body, headers=headers, timeout=(3, CALL_TIMEOUT_S))
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    costs.llm(LUNA_MODEL, prompt=u.get("prompt_tokens") or 0,
              cached=(u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0,
              output=u.get("completion_tokens") or 0, line="Voice director")
    return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _ask_claude(said, first):
    return parse(_claude(SYSTEM, prompt(said, first), SCHEMA) or "")


def _ask_luna(said, first):
    return parse(_luna(SYSTEM, prompt(said, first), SCHEMA,
                       '{"tag": "<one allowed tag or none>", "why": "<a few words>"}'))


def _styled(clip, answer, starts):
    """A style answer, checked: (tag or None, the words for the voice), or None."""
    try:
        got = json.loads(answer)
        text, say = got.get("text"), got.get("say")
    except (ValueError, AttributeError, TypeError):
        return None
    ok = check_styled(clip, text, starts)
    return None if ok is None else (ok[0], apply_say(clip, ok[1], say))


def _style_claude(said, clip, engine, starts):
    return _styled(clip, _claude(style_system(engine), style_prompt(said, clip, starts), STYLE_SCHEMA), starts)


def _style_luna(said, clip, engine, starts):
    return _styled(clip, _luna(style_system(engine), style_prompt(said, clip, starts), STYLE_SCHEMA,
                               '{"text": "<the clip as the voice should read it>", '
                               '"say": [{"word": "<as in the clip>", "say": "<respelled>"}], '
                               '"why": "<a few words>"}'), starts)


class VoiceDirector:
    """The director's answers for the clips the voice is about to speak.
    begin() when a sentence's first words arrive and tag() for its answer;
    begin_style() when a clip is cut and styled() for its answer."""

    def __init__(self):
        self._pool = cf.ThreadPoolExecutor(4, thread_name_prefix="director")
        self._lock = threading.Lock()
        self._said = collections.deque(maxlen=CONTEXT_SENTENCES)
        self._calls = {}
        self.last = None            # the latest answer, for the log

    def said(self, text):
        """Words the voice has been given: the context for later sentences."""
        text = (text or "").strip()
        if text:
            with self._lock:
                self._said.append(text)

    def _submit(self, key, fn, *args):
        which = backend()
        if not which:
            return
        with self._lock:
            said = list(self._said)
            if len(self._calls) > 64:              # never asked for: drop the oldest
                self._calls.pop(next(iter(self._calls)))
            self._calls[key] = self._pool.submit(fn, which, said, *args)

    def begin(self, key, first):
        self._submit(key, self._decide, first)

    def begin_style(self, key, clip, engine, starts):
        self._submit(key, self._style, clip, engine, starts)

    def _decide(self, which, said, first):
        t0 = time.monotonic()
        try:
            tag = _ask_claude(said, first) if which == "claude" else _ask_luna(said, first)
        except Exception as exc:                  # noqa: BLE001 -- no tag, never a held voice
            print(f"[director] {which}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            tag = None
        ms = round((time.monotonic() - t0) * 1000)
        self.last = {"tag": tag, "ms": ms, "model": CLAUDE_MODEL if which == "claude" else LUNA_MODEL}
        print(f"[director] {tag or 'none':16s} {ms:5d} ms  {first[:48]!r}", flush=True)
        return tag

    def _style(self, which, said, clip, engine, starts):
        t0 = time.monotonic()
        try:
            got = (_style_claude if which == "claude" else _style_luna)(said, clip, engine, starts)
        except Exception as exc:                  # noqa: BLE001 -- a plain clip, never a held voice
            print(f"[director] style {which}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            got = None
        ms = round((time.monotonic() - t0) * 1000)
        shown = "turned away (words changed or overdone)" if got is None else \
            (f"[{got[0]}] " if got[0] else "") + got[1]
        print(f"[director] styled {ms:5d} ms  {shown[:90]!r}", flush=True)
        return got

    def _result(self, key, wait):
        with self._lock:
            fut = self._calls.pop(key, None)
        if fut is None:
            return None, False
        try:
            return fut.result(timeout=max(0.0, wait)), True
        except cf.TimeoutError:
            return None, False

    def tag(self, key, wait):
        """The sentence's tag, waiting at most `wait` seconds for it; None if
        there is none, it was never asked for, or it is not back in time."""
        got, back = self._result(key, wait)
        if not back and key is not None and wait > 0.5:
            print(f"[director] no tag in {wait:.1f} s: speaking without one", flush=True)
        return got

    def styled(self, key, wait):
        """(tag or None, words) for a cut clip, waiting at most `wait` seconds;
        None if it was not asked for, is not back in time, or was turned away."""
        got, back = self._result(key, wait)
        if not back:
            print(f"[director] no styling in {wait:.1f} s: the clip goes plain", flush=True)
        return got

    def forget(self, key):
        """Drop an answer that will not be needed."""
        with self._lock:
            self._calls.pop(key, None)
