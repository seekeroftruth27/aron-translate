#!/usr/bin/env python3
"""Any-language -> one chosen target translation via the Google Gemini API.

Fourth translator backend alongside translate_claude.py, translate_openai.py and
translate_xai.py, exposing the SAME interface (translate / translate_stream with
identical signatures) so the caption server can route a request to any provider
by model name alone. It reuses translate_claude's system prompt (editable prompt
file + injected glossary), target-language tag scheme, rolling-history replay,
and meta-suppression, so switching to Gemini in the live picker changes ONLY the
model -- never the prompting or the caption-safety hardening.

Two models are offered, measured here on the repo's own batteries (3 runs each)
and on time-to-first-token over the real system prompt (5 sermon sentences x 3
reps, streaming). The timings are GEMINI-INTERNAL -- taken from a cloud
container with only a Gemini key present, so they rank these two against each
other and are NOT comparable with the Claude/GPT/Grok latencies recorded in the
other backends, which were measured on the venue network:

                    TTFT p50  TTFT p95   figurative /61  homophone /66  ro-clitic /18
  gemini-3.5-flash-lite  0.54s   0.67s   57 / 57 / 56    63 / 65 / 64   16 / 17 / 16
  gemini-3.8-flash       0.96s   2.17s   56 / 56 / 58    65 / 64 / 66   18 / 17 / 18

On figurative language they are indistinguishable -- that battery moves +/-2
cases run to run, so judge any future change on repeated runs, never a single
pass -- and nearly two to one apart on latency, which is why the LITE model is
the default.

The one axis where the difference held in EVERY run is ro-clitic: recovering the
hyphen Romanian speech-to-text drops ("va dat" -> "v-a dat", "mai ascultat" ->
"m-ai ascultat"). 3.8-flash averaged 17.7/18 against the lite model's 16.3/18,
and the lite model's misses change the tense of the sentence -- "The Lord WILL
GIVE you a new heart" for what was said as "has given". That is the single most
STT-relevant failure mode this app has, so 3.8-flash earns its place as the
slower, steadier option: worth reaching for when the room's audio is rough, not
worth making every caption 0.4s later by default.

gemini-3.5-flash was measured too and is NOT offered: slower than the lite model
(0.77s p50) and no better on the figurative battery (56/54/55), so picking it
could only ever cost the operator something. The wider catalogue is 58 entries
of mostly image/video/embedding/robotics models; see GEMINI_MODELS.

NOTE -- the translation-specialised model is not one of these. Gemini does ship
gemini-3.5-live-translate-preview, but its supportedGenerationMethods is
["bidiGenerateContent"] only: it is a Live API speech-to-speech model spoken to
over a WebSocket, not a text translator, and there is no generateContent path to
reach it from here. It would be a fourth ENGINE alongside Soniox+LLM+TTS, not a
fourth entry in this picker.

CAVEAT -- every Gemini 3.x model THINKS BY DEFAULT.
Unlike Grok, Gemini has no separate "non-reasoning" model id to pin: the same id
is a reasoning or a non-reasoning model depending on one request field. Measured
here on a one-sentence RO->EN translation, no system prompt:

                        default              thinkingLevel pinned
  gemini-3.5-flash-lite  0 thought tok 1.5s   0 thought tok  1.0s   (minimal)
  gemini-3.5-flash     490 thought tok 3.1s   0 thought tok  1.3s   (minimal)
  gemini-3.6-flash     489 thought tok 4.1s   0 thought tok  1.9s   (minimal)
  gemini-3.7-flash     494 thought tok 2.4s   0 thought tok  1.4s   (low)
  gemini-3.8-flash     489 thought tok 2.3s   0 thought tok  1.3s   (low)

So the field is not a tuning nicety -- shipping without it would put a model
that spends ~490 hidden tokens before its first word in front of a congregation,
which is the grok-4.3 mistake (see translate_xai) with a different spelling. It
is pinned on every request, and thoughtsTokenCount is checked on every response
so the pin cannot go quietly wrong the way that one did.

The floor is PER MODEL and not guessable from the version number: 3.5/3.6 flash
accept "minimal", while 3.7/3.8 flash REJECT it with a 400 and have their floor
at "low". An unknown model tries "minimal" and falls back once, remembering the
answer -- see _thinking_for.

And a floor is NOT a guarantee -- for one of these two models. The difference is
MARGIN, not luck. Sweeping the levels on one input (thought tokens, same prompt):

                       minimal    low   medium   high
  gemini-3.5-flash-lite      0      0     2797    3964   (6 calls per level)
  gemini-3.8-flash      (400 err) 438      994    1643   (4 calls per level)

Flash-Lite is not incapable of reasoning -- asked at "medium" it reasons hard.
It is simply pinned TWO levels below its own threshold, so the pin has room to
be wrong in. 3.8-flash has none: "minimal" is refused outright, which leaves its
floor sitting ON its reasoning threshold, and every caption is a coin toss it
usually but not always wins.

That shows up exactly as you would expect under load. SOAK, gemini-3.5-flash-lite
at "minimal": 1600 calls, ZERO thought tokens -- 1200 with the prompt cache on
and 400 with it off, over nine stress conditions built from 293 real sentences
in the repo's own batteries (plain, three-sentence, comma run-on, mid-clause
fragment, diacritic- and hyphen-stripped STT damage, six-turn history, draft
anchor, bilingual pair, and the exact repeated input that trips the control).
95% upper bound on the leak rate, by the rule of three: 0.19%.

The same harness, same conditions, caught gemini-3.8-flash leaking 13 times in
180 calls (7.2%) -- 8 of 20 on the repeated input alone. So the zero above is a
measurement rather than a blind spot: the detector demonstrably detects.

What this does NOT prove is that Google will keep "minimal" meaning what it
means today -- 3.7 and 3.8 flash have already had that level taken away. That
is a catalogue risk, not a per-call one, and it is what config/model_pins.json
and the warn-once check below are for. LEAKY_FLOOR records which models have no
margin, so the log can tell "expected, occasional" apart from "the pin broke".

PROMPT CACHING is explicit here, not automatic. Gemini's implicit cache never
fired for this workload (0 hits over six byte-identical ~3.9K-token prefixes),
so the system prompt is uploaded once as a cachedContents object and referenced
by handle: 3872/3893 tokens (99.5%) served from cache, with latency unchanged --
it is a COST feature, not a caption-latency one. Set CT_GEMINI_CACHE=0 to turn
it off; see the "explicit prompt caching" section below for the whole design.

Uses the native REST endpoint over plain `requests` (already a project
dependency) -- no google-genai SDK needed. The native endpoint is chosen over
Gemini's OpenAI-compatibility shim because it reports thoughtsTokenCount, which
is the one number this backend has to be able to see.

Requires:
    export GEMINI_API_KEY=...

Usage:
    python translate_gemini.py --text "Dumnezeu este bun."
    python translate_gemini.py --model gemini-3.5-flash --target es --text "..."
"""
import argparse
import base64
import atexit
import hashlib
import json
import os
import sys
import threading
import time

# Shared prompting/hardening -- one source of truth in translate_claude.
from translate_claude import (SYSTEM_PROMPT, LANGUAGES, TARGET_LANGUAGES, anchored_turn,
                              DEFAULT_TARGET_LANG, _strip_meta, _tag_for,
                              history_turns, system_prompt_for)

# The models offered in the picker. Deliberately a short, curated list rather
# than the live catalogue (58 entries, most of them image/video/embedding/robotics
# models): unlike xAI, Gemini's catalogue exposes no per-entry property that
# separates a translator from a TTS or a Veo model, so classifying it the way
# translate_xai.list_models does is not possible -- the honest version of that
# here is a hand-picked tuple with a measured note against each one.
GEMINI_MODELS = (
    "gemini-3.5-flash-lite",   # fastest, and the only reliably zero-reasoning one
    "gemini-3.8-flash",        # better clitics; slower, and thinks occasionally
    "gemini-3.6-flash",        # the one to let HEAR the caption (translate_stream audio=)
)

# Hearing: the translator gets the caption's stretch of audio beside Soniox's
# text (translate_stream's `audio`), so it can catch a word Soniox mis-heard
# (validation/results/2026-10-07-three-witnesses/: gemini-3.6-flash, one trip,
# 39-41 of 47 real mis-hears with the caption's own stretch, 42-44 with an extra
# ear's reading too, against 35-38 for text alone; ~1.1-1.3 s to the first word).
# The first build named it as a model, "<model>+hears"; hears_base() still reads
# such an id (a saved setting) so the server can turn it into hear=True.
HEARS_SUFFIX = "+hears"
HEARS_NOTE = ("The audio attached is this stretch as it was spoken. The stretch's text "
              "comes from speech recognition and can contain mis-heard words; where the "
              "audio clearly says something else, translate what was said. Translate only "
              "this stretch, and give one translation: never two readings side by side.")


def hears_base(name):
    """The Google model behind a "+hears" option, or None for any other id."""
    name = name or ""
    return name[:-len(HEARS_SUFFIX)] if name.endswith(HEARS_SUFFIX) else None
DEFAULT_MODEL = "gemini-3.5-flash-lite"

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
CACHE_ROOT = "https://generativelanguage.googleapis.com/v1beta/cachedContents"


def is_gemini_model(name):
    """Route-by-name test used by the caption server and mt_router. Covers the
    curated tuple AND the wider namespace, so a model pasted into a saved
    setting still routes here rather than falling through to Claude."""
    name = name or ""
    return name in GEMINI_MODELS or name.startswith("gemini-")


# ---- reasoning control -----------------------------------------------------
# See the CAVEAT in the module docstring: this is the field that decides whether
# the picker's "Gemini" option is a sub-second translator or a three-second one.
# Values are each model's measured LOWEST setting that emits zero thought
# tokens. "off"/"none" do not exist in the enum (400), and thinkingBudget is
# accepted by some models and rejected by others, so thinkingLevel is the only
# portable control.
THINKING_LEVEL = {
    "gemini-3.5-flash-lite": "minimal",
    "gemini-3.5-flash": "minimal",
    "gemini-3.6-flash": "minimal",
    "gemini-3.7-flash": "low",     # rejects "minimal" with a 400
    "gemini-3.8-flash": "low",     # rejects "minimal" with a 400
}

# Models whose pinned level is the lowest one they accept but still does not
# guarantee zero reasoning (see the docstring). Thought tokens from these are
# expected-occasional rather than evidence the pin broke, so they get a quieter
# NOTE instead of the drift WARNING -- an alarm that cries wolf on known
# behaviour is an alarm the operator learns to ignore.
LEAKY_FLOOR = frozenset({"gemini-3.7-flash", "gemini-3.8-flash"})
_DEFAULT_LEVEL = "minimal"
# Tried in order when a model rejects the level above. Ends at "low" on purpose:
# anything higher is a reasoning model by any other name, and this backend would
# rather fail loudly than quietly serve one.
_LEVEL_FALLBACK = {"minimal": "low"}
_level_override = {}          # model -> level, learned from a 400 at runtime
_LEVEL_LOCK = threading.Lock()


def _thinking_for(model):
    with _LEVEL_LOCK:
        if model in _level_override:
            return _level_override[model]
    return THINKING_LEVEL.get(model, _DEFAULT_LEVEL)


def _unsupported_level(detail):
    """True when a 400 is specifically 'this model has no such thinking level'
    (as opposed to a bad key, a bad model id, or a quota problem), which is the
    only 400 worth retrying at a higher level."""
    d = (detail or "").lower()
    return "thinking level" in d and ("not supported" in d or "invalid" in d)


# ---- prompt cache: what the numbers mean ------------------------------------
# IMPLICIT caching -- the automatic kind that needs no code -- never fired for
# this workload. Six back-to-back calls with a byte-identical ~3.9K-token prefix
# all reported cachedContentTokenCount 0:
#
#   [gemini] tokens: prompt 3889 (cached 0, 0%) + completion 10 + thoughts 0  x6
#
# Why is not something the API explains, and it is recorded as an observation
# rather than guessed at. The EXPLICIT cache above is the answer to it: a
# cachedContents handle is a guarantee rather than a best-effort optimisation,
# and it measured 3872/3893 tokens (99.5%) served from cache on the same prompt.
#
# The percentage below is therefore the live check on all of it. Steady state on
# a service is ~99%; a sustained 0% means the cache is not being used and every
# caption is re-billing the whole system prompt -- worth noticing, though never
# worth failing a caption over.
#
# Last call's token accounting (best-effort diagnostic; harness threads may
# interleave).
# `thoughts` is the one that matters: it must be 0, and a non-zero value means
# the pin above has stopped holding for this model. One stderr line per call
# keeps both visible in the server console, mirroring the [xai] log style.
LAST_USAGE = {}
_warned_thinking = set()
# Per-THREAD copy of the two numbers the Translation panel's cache light needs.
# LAST_USAGE above is process-wide and documented as best-effort, which is fine
# for a console diagnostic but not for a per-request answer the UI will show:
# the caption server is threaded, so two concurrent translations would otherwise
# report each other's counters. Same pattern as translate_claude._cache_stats.
_cache_stats = threading.local()


def last_cache_stats():
    """{"read", "write", "input", "thoughts"} for THIS thread's last Gemini
    call, or None if it hasn't made one.

    `thoughts` rides along because it has the same problem the cache counters
    do: LAST_USAGE is process-wide and best-effort, which is fine for a console
    line but useless for attributing a thought-token leak to the call that
    caused it. The server reads only the first three keys.

    Shaped to match translate_claude.last_cache_stats() so the server's
    _cache_state classifier works on it unchanged. `write` is always 0: unlike
    Claude there is no per-call cache-creation premium to report, because the
    cache is built by a separate createCachedContent request (see above), so a
    call either benefits or it does not. read == 0 therefore means exactly what
    the panel's "off" state is for -- this caption paid full price for the whole
    system prompt."""
    return getattr(_cache_stats, "last", None)


def _note_usage(usage, model=None):
    if not isinstance(usage, dict):
        return
    LAST_USAGE.clear()
    LAST_USAGE.update(prompt=usage.get("promptTokenCount"),
                      completion=usage.get("candidatesTokenCount"),
                      cached=usage.get("cachedContentTokenCount") or 0,
                      thoughts=usage.get("thoughtsTokenCount") or 0)
    prompt = LAST_USAGE["prompt"] or 0
    pct = (100.0 * LAST_USAGE["cached"] / prompt) if prompt else 0.0
    _cache_stats.last = {"read": LAST_USAGE["cached"], "write": 0,
                         "input": prompt, "thoughts": LAST_USAGE["thoughts"],
                         "output": (LAST_USAGE["completion"] or 0) + LAST_USAGE["thoughts"]}
    print(f"[gemini] tokens: prompt {LAST_USAGE['prompt']} "
          f"(cached {LAST_USAGE['cached']}, {pct:.0f}%) + completion "
          f"{LAST_USAGE['completion']} + thoughts {LAST_USAGE['thoughts']}",
          file=sys.stderr)
    # The drift alarm. Warn ONCE per model rather than per call: a caption
    # operator does not need the same line every sentence, but they do need to
    # know that the option they picked has started reasoning at them.
    if LAST_USAGE["thoughts"] and model and model not in _warned_thinking:
        _warned_thinking.add(model)
        if model in LEAKY_FLOOR:
            print(f"[gemini] NOTE: {model} spent {LAST_USAGE['thoughts']} thought "
                  f"tokens at thinkingLevel {_thinking_for(model)!r} (~+0.5s on "
                  f"this caption). Known behaviour for this model -- its floor is "
                  f"the lowest level it accepts, not a guarantee. Pick "
                  f"gemini-3.5-flash-lite if the service needs reasoning fully "
                  f"off.", file=sys.stderr)
        else:
            print(f"[gemini] WARNING: {model} returned "
                  f"{LAST_USAGE['thoughts']} thought tokens at thinkingLevel "
                  f"{_thinking_for(model)!r} -- expect slower captions; this "
                  f"model's reasoning floor has moved (see THINKING_LEVEL in "
                  f"translate_gemini.py)", file=sys.stderr)


_session = None


def _get_session():
    """One shared requests.Session (connection pooling/keep-alive), matching
    the translate_openai/translate_xai pattern."""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def warm():
    """Pre-open the pooled TLS connection to the API host (unauthenticated
    GET; any response counts -- urllib3 pools it with no idle-time expiry).
    Called at server startup so the first Gemini translation doesn't pay the
    handshake. Best-effort."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(API_ROOT)
        _get_session().get(f"{u.scheme}://{u.netloc}/",
                           timeout=(CONNECT_TIMEOUT_S, 5.0))
    except Exception:
        pass
    # Start building the prompt cache for the default model now, so the first
    # caption of the service already gets the discount instead of being the call
    # that pays to create it. Fire-and-forget: _cache_name builds on its own
    # thread and this returns None immediately.
    if CACHE_ENABLED and os.environ.get("GEMINI_API_KEY"):
        try:
            _cache_name(DEFAULT_MODEL,
                        {"parts": [{"text": system_prompt_for(None)}]})
        except Exception:
            pass


# Same live-caption timeout philosophy as the other three backends: fail fast on
# connect; the read timeout is per-chunk when streaming, so a healthy stream can
# run long but a wedged one cannot stall the caption feed.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 25.0

# A truncated caption is worse than a few cents of ceiling. Unlike the OpenAI
# backend this budget is NOT shared with hidden reasoning (thinking is pinned
# off above), so it is purely output headroom.
MAX_OUTPUT_TOKENS = 2048



# ---- explicit prompt caching ------------------------------------------------
# Gemini's IMPLICIT cache never fired for this workload (see the measurement
# note further down), so the discount is taken the explicit way: the system
# prompt is uploaded once as a cachedContents object and referenced by handle on
# every call, instead of being re-sent and re-billed each caption. Measured on
# gemini-3.5-flash-lite with the real prompt stack:
#
#   inline system_instruction : prompt 3893, cached    0   p50 0.83s
#   cachedContent handle      : prompt 3893, cached 3872   p50 0.83s
#
# 99.5% of the prompt is served from cache. Note the latency is UNCHANGED -- this
# buys cost, not speed, and is not a caption-latency feature.
#
# Three properties of the API shape everything below:
#   - a request may send cachedContent OR system_instruction, never both (400);
#   - the cache is bound to ONE model id (a cross-model reference is a 400);
#   - the content must be >= 1024 tokens (measured: 1023 fails, 1024 passes).
#     The shared system prompt is ~3.9K, so there is real headroom, but the
#     glossary is operator-editable and could in principle shrink under it.
#
# Everything here is best-effort and never on the critical path: creation runs on
# a background thread, so no caption ever waits for it, and any failure at all
# falls back to sending the prompt inline -- the pre-caching behaviour, which
# always works. A cache that cannot be built is a slightly larger bill, never a
# missing caption.
CACHE_ENABLED = os.environ.get("CT_GEMINI_CACHE", "1").strip().lower() not in (
    "0", "false", "no", "off")
# One hour by default. Storage is billed per token-hour for as long as the object
# lives, so this is a real (if small) cost knob, not just a correctness one: a
# longer TTL means fewer rebuilds and more idle storage. Recreation is LAZY --
# an expired cache is only rebuilt when a translation actually needs it -- so an
# app left open but idle stops paying once the last one expires.
def _ttl_from_env():
    """Parse CT_GEMINI_CACHE_TTL defensively. This runs at IMPORT, inside the
    caption server's own import chain, so a typo in .env must not be the reason
    the app fails to start -- a bad value falls back to the default and says so."""
    raw = (os.environ.get("CT_GEMINI_CACHE_TTL") or "").strip()
    if not raw:
        return 3600
    try:
        return max(120, int(raw))
    except ValueError:
        print(f"[gemini] NOTE: CT_GEMINI_CACHE_TTL={raw!r} is not a number; "
              f"using 3600s", file=sys.stderr)
        return 3600


CACHE_TTL_S = _ttl_from_env()
# Stop using a cache slightly before it expires rather than racing the clock and
# eating a 403 on a live caption.
_CACHE_MARGIN_S = 60
_CACHE_MIN_TOKENS = 1024        # API minimum, measured

_caches = {}            # (model, sha1(prompt)) -> {"name": str, "expires": float}
_cache_off = set()      # keys proven un-cacheable (too small) -- never retried
_cache_creating = set()  # keys with a build in flight, so N threads build once
_CACHE_LOCK = threading.Lock()


def _prompt_text(sys_instr):
    """The system prompt as one string, or '' -- the thing that gets hashed."""
    if not sys_instr:
        return ""
    return "".join(part.get("text") or "" for part in (sys_instr.get("parts") or []))


def _cache_key(model, text):
    return (model, hashlib.sha1(text.encode("utf-8")).hexdigest())


def _cache_label(key):
    """The displayName stamped on a cache we create: this app, this model, this
    exact prompt. The API does not hand back a cache's CONTENT, so without a tag
    there is no way for a later run to tell whether an existing object holds the
    same prompt -- and no way to reuse it. With one, a restart adopts what the
    previous run left behind instead of building (and paying to store) a
    duplicate."""
    model, digest = key
    return f"church-translate:{model}:{digest[:16]}"


def _adopt_cache(key, model):
    """Look for a cache this app already created for this exact prompt+model and
    still has useful life in it. Returns (name, expires_epoch) or None.

    This is what makes an app restart free. atexit deletes our caches on a clean
    exit, but a SIGTERM or a pulled plug does not run it (measured), so without
    this every restart would strand the previous cache AND build another."""
    try:
        import requests
        from datetime import datetime, timezone
        r = _get_session().get(
            CACHE_ROOT, params={"pageSize": 100},
            headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")},
            timeout=(CONNECT_TIMEOUT_S, 10.0))
        if r.status_code != 200:
            return None
        want = _cache_label(key)
        best = None
        for c in r.json().get("cachedContents") or []:
            if c.get("displayName") != want:
                continue
            if (c.get("model") or "").replace("models/", "") != model:
                continue
            raw = (c.get("expireTime") or "").replace("Z", "+00:00")
            try:
                left = (datetime.fromisoformat(raw)
                        - datetime.now(timezone.utc)).total_seconds()
            except ValueError:
                continue
            # Only worth adopting if it will outlive the margin by a sensible
            # margin of its own -- adopting something about to expire just moves
            # the rebuild a few seconds later.
            if left > _CACHE_MARGIN_S * 3 and (best is None or left > best[1]):
                best = (c["name"], left)
        if best:
            return best[0], time.time() + best[1]
    except Exception:
        pass
    return None


def _create_cache(key, model, text):
    """Build one cachedContents object. Runs on a background thread; returns
    nothing and raises nothing -- it records the result and lets the next call
    pick it up."""
    try:
        adopted = _adopt_cache(key, model)
        if adopted:
            name, expires = adopted
            with _CACHE_LOCK:
                _caches[key] = {"name": name, "expires": expires,
                                "adopted": True}
            print(f"[gemini] prompt cache adopted for {model} "
                  f"({round(expires - time.time())}s left on it)", file=sys.stderr)
            return
        import requests
        r = _get_session().post(
            CACHE_ROOT,
            headers={"x-goog-api-key": os.environ.get("GEMINI_API_KEY", ""),
                     "Content-Type": "application/json"},
            json={"model": f"models/{model}",
                  "systemInstruction": {"parts": [{"text": text}]},
                  "displayName": _cache_label(key),
                  "ttl": f"{CACHE_TTL_S}s"},
            timeout=(CONNECT_TIMEOUT_S, 20.0))
        if r.status_code == 200:
            j = r.json()
            tokens = (j.get("usageMetadata") or {}).get("totalTokenCount")
            with _CACHE_LOCK:
                _caches[key] = {"name": j["name"],
                                "expires": time.time() + CACHE_TTL_S}
            print(f"[gemini] prompt cache ready for {model}: {tokens} tokens, "
                  f"ttl {CACHE_TTL_S}s", file=sys.stderr)
            return
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message") or ""
        except ValueError:
            detail = r.text[:200]
        # "Too small" is permanent for this exact prompt (a shrunken glossary),
        # so stop trying; anything else may be transient (quota, a blip) and is
        # left to be retried on a later call.
        if "too small" in detail.lower():
            with _CACHE_LOCK:
                _cache_off.add(key)
            print(f"[gemini] NOTE: prompt too small to cache "
                  f"(min {_CACHE_MIN_TOKENS} tokens) -- sending it inline, which "
                  f"costs more per call but works identically", file=sys.stderr)
        else:
            print(f"[gemini] NOTE: prompt cache unavailable ({detail[:120]}); "
                  f"sending the prompt inline", file=sys.stderr)
    except Exception as exc:                       # network, JSON, schema drift
        print(f"[gemini] NOTE: prompt cache unavailable ({exc}); sending the "
              f"prompt inline", file=sys.stderr)
    finally:
        with _CACHE_LOCK:
            _cache_creating.discard(key)


def _cache_name(model, sys_instr, build=True):
    """The cache handle to use for this call, or None to send the prompt inline.

    NEVER blocks: if no cache exists yet one is built on a background thread and
    THIS call goes inline. A caption is never made to wait on a cost
    optimisation. `build=False` asks the question without starting one."""
    if not CACHE_ENABLED:
        return None
    text = _prompt_text(sys_instr)
    if not text:
        return None
    key = _cache_key(model, text)
    now = time.time()
    with _CACHE_LOCK:
        if key in _cache_off:
            return None
        entry = _caches.get(key)
        if entry and entry["expires"] > now + _CACHE_MARGIN_S:
            return entry["name"]
        if entry:
            del _caches[key]           # expired; rebuild below
        if not build or key in _cache_creating:
            return None
        _cache_creating.add(key)
    threading.Thread(target=_create_cache, args=(key, model, text),
                     daemon=True).start()
    return None


def _forget_cache(model, sys_instr):
    """Drop a handle the API has just rejected, so the next call rebuilds."""
    text = _prompt_text(sys_instr)
    if not text:
        return
    with _CACHE_LOCK:
        _caches.pop(_cache_key(model, text), None)


def _cache_gone(status, detail):
    """True for the specific failure 'the handle you sent no longer exists'
    (expiry, deletion, a different project's key) -- the one worth retrying
    inline. A cross-model 400 is NOT this: that is a bug in the caller."""
    return status in (403, 404) and "cachedcontent" in (detail or "").lower()


def delete_caches():
    """Best-effort teardown of the caches this process created.

    Storage is billed for as long as a cachedContents object lives, so an
    operator who starts and stops the app a few times while setting up would
    otherwise leave an orphan behind on each run, each billing until its TTL
    runs out. Registered with atexit, which covers a clean shutdown and Ctrl-C.
    It does NOT cover SIGTERM or a pulled plug (measured: the object survives),
    which is what the TTL and _adopt_cache are for -- the next run reuses the
    orphan rather than adding to it.

    Never raises and never waits long -- nothing here is worth delaying an
    exit, let alone failing one."""
    with _CACHE_LOCK:
        # Only tear down what THIS process built. An adopted cache may still be
        # in use by the process that created it (two operators, one key), and
        # deleting it out from under them would cost them a rebuild.
        names = [e["name"] for e in _caches.values() if not e.get("adopted")]
        _caches.clear()
    key = os.environ.get("GEMINI_API_KEY")
    if not names or not key:
        return
    for name in names:
        try:
            _get_session().delete(
                f"{CACHE_ROOT.rsplit('/', 1)[0]}/{name}",
                headers={"x-goog-api-key": key}, timeout=(2.0, 3.0))
        except Exception:
            pass


atexit.register(delete_caches)


def cache_stats():
    """What the process currently holds, for diagnostics."""
    now = time.time()
    with _CACHE_LOCK:
        return {"enabled": CACHE_ENABLED, "live": len(_caches),
                "building": len(_cache_creating), "uncacheable": len(_cache_off),
                "ttl_s": CACHE_TTL_S,
                "expires_in_s": sorted(round(e["expires"] - now)
                                       for e in _caches.values())}


def _build_payload(text, history, model, target_lang, temperature,
                   draft=None, pair=None, expressive=False, audio=None):
    """Gemini generateContent payload with the shared system prompt and the same
    tagged rolling history translate_claude replays.

    Two shape differences from the OpenAI-style backends, both required by this
    API rather than chosen: the system prompt is a top-level system_instruction
    (not a message with role "system"), and the assistant role is spelled
    "model". Everything inside the turns is byte-identical to what the other
    backends send, which is what keeps a cross-provider comparison honest."""
    tag = _tag_for(target_lang)
    contents = []
    for turn in history_turns(history, tag):
        contents.append({"role": "user" if turn["role"] == "user" else "model",
                         "parts": [{"text": turn["content"]}]})
    parts = [{"text": anchored_turn(tag, text, draft)}]
    if audio:   # WAV bytes: the "+hears" options. Audio first, as in the lab runs.
        parts.insert(0, {"inline_data": {"mime_type": "audio/wav",
                                         "data": base64.b64encode(audio).decode("ascii")}})
    contents.append({"role": "user", "parts": parts})
    return {
        "system_instruction": {"parts": [{"text": system_prompt_for(pair, expressive)}]},
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingLevel": _thinking_for(model)},
        },
    }


def _post(payload, model, stream):
    """POST to generateContent/streamGenerateContent; normalize every failure
    mode to RuntimeError (the caption server maps that to a retryable NDJSON
    error line, same as a Claude/OpenAI/xAI failure).

    Two one-shot repairs are retried in-place, each at most once per call:
      - the model rejects its pinned thinking level (remembered per model, so
        the retry is paid once per process rather than once per sentence);
      - the prompt cache handle has gone (expired, deleted), in which case the
        system prompt goes back inline and the handle is dropped so the next
        call rebuilds one. That path is a slightly pricier caption, never a
        failed one."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")
    import requests
    method = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = f"{API_ROOT}/{model}:{method}"
    # Key travels as a header, not the ?key= query param the docs lead with:
    # a URL lands in logs and exception messages, and this one would carry the
    # credential with it.
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    # Swap the inline system prompt for a cached handle when one is ready. The
    # API refuses both together, so this is an exchange, not an addition -- and
    # `sys_instr` is kept so the exchange can be undone if the handle is stale.
    sys_instr = payload.get("system_instruction")
    cached = _cache_name(model, sys_instr)
    if cached:
        payload = dict(payload)          # never mutate the caller's dict
        payload.pop("system_instruction", None)
        payload["cachedContent"] = cached
    level_retried = cache_retried = False
    for _ in range(3):
        try:
            r = _get_session().post(
                url, json=payload, headers=headers,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), stream=stream)
        except requests.RequestException as exc:
            raise RuntimeError(f"Gemini API: {exc}") from exc
        if r.status_code == 200:
            if stream:
                # SSE is UTF-8 by spec; pin it rather than trust the header, for
                # the same reason translate_xai does (a missing charset makes
                # requests decode the body as Latin-1 and mangle every accent).
                r.encoding = "utf-8"
            return r
        try:
            detail = (r.json().get("error") or {}).get("message") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        r.close()
        # The handle went stale between being chosen and being used -- put the
        # prompt back inline and try again, rather than dropping the caption.
        if (not cache_retried and payload.get("cachedContent")
                and _cache_gone(r.status_code, detail)):
            cache_retried = True
            _forget_cache(model, sys_instr)
            payload.pop("cachedContent", None)
            if sys_instr:
                payload["system_instruction"] = sys_instr
            print("[gemini] NOTE: prompt cache handle expired; sending the "
                  "prompt inline and rebuilding", file=sys.stderr)
            continue
        level = _thinking_for(model)
        if (not level_retried and r.status_code == 400 and _unsupported_level(detail)
                and level in _LEVEL_FALLBACK):
            level_retried = True
            nxt = _LEVEL_FALLBACK[level]
            with _LEVEL_LOCK:
                _level_override[model] = nxt
            print(f"[gemini] NOTE: {model} rejected thinkingLevel {level!r}; "
                  f"retrying at {nxt!r}", file=sys.stderr)
            payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] = nxt
            continue
        # Surface the API's own message (model typo, quota, revoked key) --
        # it lands in the Debug log where it's actually diagnosable.
        raise RuntimeError(f"Gemini API: HTTP {r.status_code}: {detail}")
    raise RuntimeError("Gemini API: retry logic fell through")   # unreachable


def _texts(chunk):
    """Visible output text from one response chunk.

    Parts carrying `thought: true` are the model's reasoning and are DROPPED --
    with thinking pinned off there should be none, but a caption is the wrong
    place to find out that the pin slipped. Parts carrying only a
    thoughtSignature (no text) appear on the final chunk of every stream and
    would otherwise yield empty strings into the caption feed."""
    for cand in chunk.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts") or []):
            if part.get("thought"):
                continue
            t = part.get("text")
            if t:
                yield t


def _finish(chunk):
    for cand in chunk.get("candidates") or []:
        if cand.get("finishReason"):
            return cand["finishReason"]
    return None


def translate(text: str, history=None, model: str = DEFAULT_MODEL,
              temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
              pair=None) -> str:
    """Translate one segment INTO `target_lang`. Same contract as
    translate_claude.translate()."""
    r = _post(_build_payload(text, history, model, target_lang, temperature,
                             pair=pair), model, stream=False)
    try:
        j = r.json()
    except ValueError as exc:
        raise RuntimeError("Gemini API: non-JSON response") from exc
    finally:
        r.close()
    _note_usage(j.get("usageMetadata"), model)
    if _finish(j) == "MAX_TOKENS":
        print("[gemini] WARNING: translation truncated at maxOutputTokens",
              file=sys.stderr)
    return _strip_meta("".join(_texts(j)).strip())


def translate_stream(text: str, history=None, model: str = DEFAULT_MODEL,
                     temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
                     draft: str = None, pair=None, expressive=False, audio=None):
    """Like translate(), but yields the response text incrementally (SSE
    stream) -- same contract as translate_claude.translate_stream(): raw model
    output only, no meta-suppression (the caller does that on the full text).
    `audio` (WAV bytes) rides in the last user turn; pass the "+hears" option's
    base model, not the option id."""
    import requests
    r = _post(_build_payload(text, history, model, target_lang, temperature,
                             draft, pair, expressive, audio), model, stream=True)
    finish = None
    usage = None
    try:
        it = r.iter_lines(decode_unicode=True)
        for line in it:
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                j = json.loads(data)
            except ValueError:
                continue
            # usageMetadata repeats on every chunk and is CUMULATIVE, so only
            # the last one is meaningful -- reporting each would print a running
            # total per token. Held and logged once the stream ends.
            usage = j.get("usageMetadata") or usage
            finish = _finish(j) or finish
            for t in _texts(j):
                yield t
    except requests.RequestException as exc:
        raise RuntimeError(f"Gemini API: {exc}") from exc
    finally:
        r.close()
    _note_usage(usage, model)
    if finish == "MAX_TOKENS":
        print("[gemini] WARNING: translation truncated at maxOutputTokens",
              file=sys.stderr)


def check_default_model():
    """Warn if DEFAULT_MODEL isn't what this module thinks it is.

    The xAI equivalent exists because a withdrawn pin was silently served by a
    reasoning model. Gemini's version of that failure is different in shape --
    the id stays valid and it is the thinking floor that moves under it -- so
    this checks both: that the id still answers, and that it answers with zero
    thought tokens at the level pinned above. Advisory only: it prints and
    returns, never raises and never blocks the caption path."""
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    try:
        r = _post(_build_payload("Domnul fie lăudat.", None, DEFAULT_MODEL,
                                 "en", 0.2), DEFAULT_MODEL, stream=False)
        j = r.json()
        r.close()
    except Exception as exc:
        print(f"[gemini] NOTE: could not verify default model ({exc})",
              file=sys.stderr)
        return None
    served = j.get("modelVersion")
    if served and served != DEFAULT_MODEL:
        print(f"[gemini] NOTE: {DEFAULT_MODEL!r} is served as {served!r}",
              file=sys.stderr)
    thoughts = (j.get("usageMetadata") or {}).get("thoughtsTokenCount") or 0
    if thoughts:
        print(f"[gemini] WARNING: default model {DEFAULT_MODEL!r} spent "
              f"{thoughts} thought tokens at thinkingLevel "
              f"{_thinking_for(DEFAULT_MODEL)!r} -- expect slower captions; "
              f"repin THINKING_LEVEL in translate_gemini.py", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=None)
    ap.add_argument("--infile", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(m for m in GEMINI_MODELS if not hears_base(m)))
    ap.add_argument("--target", default=DEFAULT_TARGET_LANG,
                    choices=sorted(TARGET_LANGUAGES),
                    help="language code to translate INTO (default: en)")
    ap.add_argument("--stream", action="store_true",
                    help="exercise the streaming path (prints chunks as they arrive)")
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

    print(f"[gemini] {args.model} (-> {LANGUAGES[args.target]}, Gemini API)",
          file=sys.stderr)
    t0 = time.time()
    try:
        if args.stream:
            out = ""
            for chunk in translate_stream(src, model=args.model, target_lang=args.target):
                out += chunk
                print(chunk, end="", flush=True)
            print()
            out = _strip_meta(out.strip())
        else:
            out = translate(src, model=args.model, target_lang=args.target)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    dt = time.time() - t0

    print("\n=== source ===")
    print(src)
    print(f"\n=== {LANGUAGES[args.target]} (Gemini translation) ===")
    print(out)
    print(f"\n[timing] {dt:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
