#!/usr/bin/env python3
"""Live any-source -> one-chosen-target caption server (cloud pipeline).

The BROWSER captures audio and runs voice-activity detection, sending one short
segment per detected phrase, or streams continuously to Soniox. THIS server
brokers the cloud calls: Soniox for STT, Claude for translation, ElevenLabs for
TTS. Nothing runs locally, so it needs only Flask + requests + numpy +
soundfile and a couple of API keys.

Soniox is multilingual and detects the spoken language per segment (or can be
pinned to a specific one for accuracy); the operator picks ONE target language
for the whole session, and every segment -- whatever language it was spoken in
-- is translated into that same target. Source language never affects routing,
only (optionally) the STT accuracy hint.

Two-stage so the UI can show progress and you can see WHERE a stall is:
    POST /stt?source=...     audio -> {text, lang}  cloud STT + punctuation
    POST /translate          {text, target_lang} -> NDJSON stream of
                             delta/audio_chunk/final lines  Claude API (+ TTS)
    GET  /caps               -> {languages, target_languages, tts, stt}  what's available
    GET  /record             -> what the session recording holds so far
    POST /record             {action: save|clear|on|off}   save it off / reset it
    GET  /record/download    the recording as a .zip (audio + cues + transcript)

The source text is shown the moment STT returns; the translation follows when
the cloud answers.

Run it:
    python app/server.py
then open http://localhost:5000.

Env: CT_PORT (5000). Requires SONIOX_API_KEY (STT, its own translation and
voice). Optional: ANTHROPIC / OPENAI / GEMINI / XAI keys for LLM translators
(OpenAI also the Router), ELEVENLABS_API_KEY for the ElevenLabs voice.
"""
import atexit
import base64
import io
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

# Native Windows consoles default to a legacy code page (cp1252) that can't encode
# Romanian diacritics (ă, ș, ț, ...). Without this, the first log line containing a
# transcript or translation raises UnicodeEncodeError and 500s the whole request.
# Force UTF-8 on our streams; errors='replace' degrades a stray glyph to '?'
# rather than crashing. No-op where the stream is already UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _load_dotenv():
    """Load KEY=VALUE lines from <project>/.env into the environment -- BEFORE the
    app-module imports below, several of which read their config (CT_* vars) from
    os.environ at IMPORT time. This makes the API keys and voice ids work the same
    way however the server is started. Real env vars still win (setdefault).
    """
    path = os.path.join(os.path.dirname(HERE), ".env")
    if not os.path.exists(path):
        return
    # utf-8-sig: Notepad may start the file with a byte-order mark, which would
    # otherwise glue itself to the first key's name and hide that key.
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            # Strip an INLINE comment. .env.example annotates keys with a trailing
            # "# ..." (e.g. CT_PROPRESENTER_URL=... # http://<ip>:<port>), so a value
            # filled in ahead of that comment would otherwise swallow the whole
            # comment -- which is exactly how a projector URL ended up as
            # "presenter...:1025          # http://..." and failed to connect. For an
            # unquoted value the comment starts at the first "#" preceded by
            # whitespace; a quoted value is taken verbatim (a literal # is kept).
            # A blank value with a comment (`KEY=   # note`) arrives here as
            # "# note", so a "#" at the very start opens a comment too -- else
            # the note became the value (a ProPresenter "URL" of "# http://...").
            # launcher.py, model_pins.py and deploy_captions.py read .env the
            # same way (_dotenv_value); keep the four in step.
            if v[:1] in ("'", '"'):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end != -1 else v[1:]
            else:
                v = re.split(r"(?:^|\s)#", v, maxsplit=1)[0].strip()
            os.environ.setdefault(k.strip(), v)


_load_dotenv()   # must precede the imports below (they read env at import time)

import json
import soundfile as sf
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_sock import Sock   # WebSocket routes on the same threaded dev server

# Cloud pipeline. STT is Soniox realtime only -- one stateless cloud call,
# multilingual (detects/handles any of its 60+ languages), so any source
# language needs no extra model. Translation is always the Claude cloud call,
# into whichever ONE target language the operator picked; TTS is ElevenLabs or
# Soniox, one voice for any target. The server runs on just Flask + requests +
# soundfile -- nothing loads locally, and the live TTS path decodes nothing: the
# provider's own mp3 bytes are relayed straight to the browser as they arrive.
SONIOX_RT_MODEL = os.environ.get("CT_SONIOX_MODEL", "stt-rt-v5")
SONIOX_WS_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
HAS_SONIOX_KEY = bool(os.environ.get("SONIOX_API_KEY"))
HAS_EL_KEY = bool(os.environ.get("ELEVENLABS_API_KEY"))   # TTS only

from translate_claude import (translate_stream as claude_translate_stream,
                              translate as claude_translate,
                              _strip_meta as claude_strip_meta,
                              ascii_key_problem,
                              last_cache_stats as claude_cache_stats,
                              looks_like_meta as claude_looks_like_meta,
                              ReplayGuard, VoiceTagFilter, clue_at,
                              unsure_turn, drop_unsure_marks,
                              DEFAULT_MODEL, ALLOWED_MODELS, DEFAULT_TARGET_LANG, NO_UNSURE_HINT,
                              LANGUAGES, TARGET_LANGUAGES, SOURCE_LANGUAGES)
# Alternative translator backend: OpenAI (gpt-6-luna), same interface and
# same prompt/hardening (translate_openai reuses translate_claude's). Routed
# purely by model name; needs OPENAI_API_KEY or the UI hides the options.
from translate_openai import (translate_stream as openai_translate_stream,
                              last_cache_stats as openai_cache_stats,
                              OPENAI_MODELS)
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))
# Third translator backend: xAI Grok (grok-4.20-non-reasoning by default;
# the picker is built from the live catalogue -- see translate_xai), same
# interface and same prompt/hardening (translate_xai reuses translate_claude's).
# Routed purely by model name; needs XAI_API_KEY or the UI hides the option.
from translate_xai import (translate_stream as xai_translate_stream,
                           last_cache_stats as xai_cache_stats,
                           XAI_MODELS, is_xai_model, is_reasoning_model,
                           list_models as xai_list_models)
HAS_XAI_KEY = bool(os.environ.get("XAI_API_KEY"))
# Fourth translator backend: Google Gemini (gemini-3.5-flash-lite by default),
# same interface and same prompt/hardening (translate_gemini reuses
# translate_claude's). Routed purely by model name; needs GEMINI_API_KEY or the
# UI hides the options. Unlike the others its models REASON BY DEFAULT and are
# pinned non-reasoning per request -- see translate_gemini.THINKING_LEVEL.
from translate_gemini import (translate_stream as gemini_translate_stream,
                              GEMINI_MODELS, is_gemini_model, hears_base, HEARS_NOTE,
                              last_cache_stats as gemini_cache_stats,
                              cache_stats as gemini_cache_summary)
HAS_GEMINI_KEY = bool(os.environ.get("GEMINI_API_KEY"))
# Extra ears: streaming recognizers beside Soniox (scripts/ears.py), whose
# readings of each caption can ride to the translator as text
# (scripts/witness_align.py picks the stretch).
#
# Switched off 2026-10-07 (they trailed Soniox: every packet opened a fresh
# session and paid its handshake); back ON 2026-10-08 with one session per ear
# for the whole stream, sliced at each packet seal. An ear only runs when it is
# ticked. CT_EARS=0 is the whole switch -- /caps reports no ears (so the page
# draws none of its ear controls and sends none of its ear fields), and the
# stream and /translate ignore any that a stale page still sends. "Translator
# hears the caption" (Gemini taking the caption's own audio) is unaffected.
EARS_ENABLED = os.environ.get("CT_EARS", "1").strip().lower() not in ("0", "false", "no", "off")
import ears as extra_ears
import router_gate
import languages
import costs
# How long a Router check may take, page and server alike (the server's OpenAI
# calls give up at the same point, so a slow call is never cut short below the
# page's wait). 1200: here, warm 0.15-0.33 s and cold ~0.5 s (2026-10-08); a
# cold HTTPS connection from Europe adds ~3-4 round trips of ~0.15 s.
ROUTER_TIMEOUT_MS = int(os.environ.get("CT_ROUTER_TIMEOUT_MS", "1200"))
# How doubtful (mis-heard or badly translated) a Soniox line must be before the
# Router sends it on. 0.5 until 2026-10-19, 0.3 (readability lab: the lines
# between 0.3 and 0.5 were mostly literal Scripture worth redoing), 0.2 from
# 2026-10-28 (validation/results/2026-10-28-router-threshold, Sol, 370 lines
# judged blind): rough-or-wrong lines on the wall 7.5 -> 5.0 per 100, a third
# of lines sent on, +0.1 s a word; below 0.2 the redos only polish. Back to
# 0.3 on 2026-10-30 (Joseph): live, 0.2 sent on too many lines for what the
# redos gained. The page's slider was removed 2026-10-28; this is the one place
# to change it.
try:
    ROUTER_TH = min(0.9, max(0.1, float(os.environ.get("CT_ROUTER_THRESHOLD") or 0.3)))
except ValueError:
    ROUTER_TH = 0.3
from witness_align import (packet_state as witness_state, reaches_end as witness_reaches_end,
                           stale as witness_stale, WIT_NOTE)
import voice_stream
from synthesize import (synthesize_stream, synthesize_bytes, AUDIO_MIME,
                        prewarm as tts_prewarm,
                        get_session as _get_http_session, TTS_PROVIDER,
                        SX_TTS_MODEL, SX_VOICE, el_takes_tags,
                        EL_TTS_MODEL, EL_VOICE)
# Optional external caption relay (e.g. a PHP endpoint on the church's own web
# host): when configured, the browser asks THIS server to forward each finalized
# caption there, so people can read the translation on their phones anywhere.
# Server-to-server so the token never reaches a browser and there is no CORS.
CT_PUBLISH_URL = (os.environ.get("CT_PUBLISH_URL") or "").strip()
CT_PUBLISH_TOKEN = (os.environ.get("CT_PUBLISH_TOKEN") or "").strip()
PUBLISH_CAPABLE = bool(CT_PUBLISH_URL and CT_PUBLISH_TOKEN)
# Spoken-audio companion to the caption relay: the browser sends each finalized
# sentence's mp3, and we forward it to clip.php beside publish.php on the same
# host, so phone viewers can hear the service live and past services archive with
# their audio. Derived from CT_PUBLISH_URL (one setting configures both); override
# with CT_PUBLISH_CLIP_URL only if that endpoint lives elsewhere.
CT_PUBLISH_CLIP_URL = (os.environ.get("CT_PUBLISH_CLIP_URL") or "").strip()
if not CT_PUBLISH_CLIP_URL and CT_PUBLISH_URL:
    CT_PUBLISH_CLIP_URL = re.sub(r"/[^/]*$", "/clip.php", CT_PUBLISH_URL)
# One sentence of mp3 is tens of KB; anything past a few hundred is not a clip.
try:
    CT_PUBLISH_CLIP_MAX = int(float(os.environ.get("CT_PUBLISH_CLIP_MAX_MB") or 4) * 1024 * 1024)
except ValueError:
    CT_PUBLISH_CLIP_MAX = 4 * 1024 * 1024
# Optional ProPresenter projection: push each finalized caption to a ProPresenter
# "Message" over its local network API (Preferences -> Network), so ProPresenter
# overlays the live translation on the auditorium screen over whatever it is
# already showing. Server-to-server, exactly like the web relay above -- the
# ProPresenter API sends no CORS headers, so a browser fetch to it would be
# blocked; the broker calls it instead. Configure the message once IN ProPresenter
# (a message with one text token), then point these at it:
#   CT_PROPRESENTER_URL      e.g. http://192.168.1.50:1025  (host:port from the API panel)
#   CT_PROPRESENTER_MESSAGE  the message's name, uuid, or index
#   CT_PROPRESENTER_TOKEN    the text token's name within that message (default "Caption")
CT_PROPRESENTER_URL = (os.environ.get("CT_PROPRESENTER_URL") or "").strip().rstrip("/")
# Tolerate a bare host:port (e.g. "192.168.1.50:1025"): requests needs a scheme or
# it raises "No connection adapters were found", so default the http:// the local
# ProPresenter API speaks when none was given.
if CT_PROPRESENTER_URL and "://" not in CT_PROPRESENTER_URL:
    CT_PROPRESENTER_URL = "http://" + CT_PROPRESENTER_URL
CT_PROPRESENTER_MESSAGE = (os.environ.get("CT_PROPRESENTER_MESSAGE") or "").strip()
CT_PROPRESENTER_TOKEN = (os.environ.get("CT_PROPRESENTER_TOKEN") or "Caption").strip()
PROJECT_CAPABLE = bool(CT_PROPRESENTER_URL and CT_PROPRESENTER_MESSAGE)


def _pp_int_env(name, default, lo, hi):
    """A bounded integer from the environment (silently clamped, default on junk)."""
    try:
        v = int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# Optional typewriter/rolling projection. Default "off" keeps the original
# behaviour: each finalized caption REPLACES the ProPresenter Message text in one
# shot (ProPresenter wraps it). Set CT_PROPRESENTER_TYPE=word to instead reveal
# the caption a few words at a time into a rolling, bottom-aligned lower third --
# ProPresenter has no typewriter of its own, so the broker re-sends the Message
# token as the text grows. In ProPresenter make the text box CT_PROPRESENTER_LINES
# lines tall, bottom-aligned, "shrink to fit" OFF, and the theme's transitions set
# to none (rapid re-triggers would otherwise re-run the build and flicker). The
# LINES/COLS knobs only apply in "word" mode; "off" never wraps or truncates.
CT_PROPRESENTER_TYPE = (os.environ.get("CT_PROPRESENTER_TYPE") or "off").strip().lower()
CT_PROPRESENTER_TYPE_MS = _pp_int_env("CT_PROPRESENTER_TYPE_MS", 120, 10, 5000)
CT_PROPRESENTER_TYPE_WORDS = _pp_int_env("CT_PROPRESENTER_TYPE_WORDS", 1, 1, 40)
CT_PROPRESENTER_LINES = _pp_int_env("CT_PROPRESENTER_LINES", 2, 1, 8)
CT_PROPRESENTER_COLS = _pp_int_env("CT_PROPRESENTER_COLS", 42, 10, 300)
# When a line rolls off the top, the caption is first re-sent one row TALLER (the
# departing line raised into an extra top row) and held this long, so it visibly
# scrolls up before disappearing, then settles back to CT_PROPRESENTER_LINES. For
# this the ProPresenter Message text box must be CT_PROPRESENTER_LINES + 1 rows
# tall (bottom-aligned). 0 disables the raise (instant drop, box may be just
# CT_PROPRESENTER_LINES tall).
CT_PROPRESENTER_SCROLL_MS = _pp_int_env("CT_PROPRESENTER_SCROLL_MS", 220, 0, 3000)
PROJECT_TYPING = PROJECT_CAPABLE and CT_PROPRESENTER_TYPE == "word"
# Signal-only mode: when the /projector web-object page (CT_PROJECTOR_*) is doing
# the actual on-screen captioning, this Message no longer needs to carry text at
# all -- repurposed as a pure ON/OFF flag for the translation box instead, so
# ProPresenter-side automation (a prop/layer bound to the Message's active state,
# a macro's own Trigger/Clear) can react to it. The browser sends a fixed "." to
# mark the box ON and clears the Message to mark it OFF, instead of the real
# caption text. Off by default so the Message-only fallback path (no web object;
# the Message itself IS the visible caption, per the README's "Or:" section)
# keeps carrying real text exactly as before -- opt in only when also using the
# web-object page.
CT_PROPRESENTER_SIGNAL = bool(_pp_int_env("CT_PROPRESENTER_SIGNAL", 0, 0, 1))

# Diagnostic: log the INPUT of each /translate call (the source text handed to the
# model), so a runaway can be correlated with its output. Counts are always logged;
# CT_LOG_MT_INPUT=1 adds the full source text. Off-by-default full text keeps the
# console readable in normal use.
CT_LOG_MT_INPUT = (os.environ.get("CT_LOG_MT_INPUT") or "").strip().lower() in ("1", "true", "yes", "on")

# The ProPresenter word-stream sends at most one Message trigger this often, so a
# word-by-word reveal can never flood ProPresenter (which re-renders on every
# trigger). Reveal cadence stays CT_PROPRESENTER_TYPE_MS; only the network sends
# are coalesced to this floor.
_PP_MIN_SEND_S = 0.15

# Networked projector strip: the in-browser /projector page can be driven over the
# LAN (not just the same-machine BroadcastChannel), so a SECOND computer -- e.g. the
# one running ProPresenter -- can open http://<operator-ip>:PORT/projector in a
# browser or a media/browser source and show the scrolling captions. The operator's
# app mirrors each caption to a tiny in-memory store
# here; the projector polls /caption-feed. CT_PROJECTOR_W/H fix the strip's size (px,
# 0 = fill) so it drops cleanly into a ProPresenter media box; CT_PROJECTOR_BG sets
# its background (a CSS colour, or "transparent").
CT_PROJECTOR_W = _pp_int_env("CT_PROJECTOR_W", 0, 0, 8000)
CT_PROJECTOR_H = _pp_int_env("CT_PROJECTOR_H", 0, 0, 8000)
CT_PROJECTOR_BG = (os.environ.get("CT_PROJECTOR_BG") or "").strip()
# Caption text size on the projector, in pixels. 0 keeps the default that scales
# with the window height -- fine full-screen, but too small in a fixed media box,
# so set an explicit size there.
CT_PROJECTOR_FONT_PX = _pp_int_env("CT_PROJECTOR_FONT_PX", 0, 0, 400)
# Place the caption strip within a full-height (full-slide) projector page: lift it
# up from the bottom edge, and cap its height so it stays inside a lower-third band.
# Both in pixels of the page; 0 = default (hard to the bottom, no height cap).
CT_PROJECTOR_BOTTOM_PX = _pp_int_env("CT_PROJECTOR_BOTTOM_PX", 0, 0, 4000)
CT_PROJECTOR_MAX_H_PX = _pp_int_env("CT_PROJECTOR_MAX_H_PX", 0, 0, 4000)
# Left/right margins (the gutter between the text and the edges of the strip), in
# pixels; 0 = the default 6% of the window width on each side. Set them to line the
# caption up with a lower-third graphic, or to 0-with-a-value for a tighter box.
CT_PROJECTOR_LEFT_PX = _pp_int_env("CT_PROJECTOR_LEFT_PX", 0, 0, 4000)
CT_PROJECTOR_RIGHT_PX = _pp_int_env("CT_PROJECTOR_RIGHT_PX", 0, 0, 4000)
# Projector reveal scope + pacing. LINES is the rolling window: how many lines
# stay on the wall, the previous ones kept above the reading line until they scroll
# off the top. DWELL_MS holds each line a beat before it scrolls up, so a large
# paragraph does not fly past too fast to read (0 = off). LATEST_ONLY=1 instead
# keeps ONLY the message being read, retiring earlier sentences -- off by default
# because retiring a short previous sentence mid-line makes the next one jump left.
CT_PROJECTOR_LATEST_ONLY = _pp_int_env("CT_PROJECTOR_LATEST_ONLY", 0, 0, 1)
CT_PROJECTOR_LINES = _pp_int_env("CT_PROJECTOR_LINES", 3, 1, 12)
CT_PROJECTOR_DWELL_MS = _pp_int_env("CT_PROJECTOR_DWELL_MS", 1000, 0, 10000)
# How long a caption stays servable, in seconds. The store used to keep its last
# 40 segments until Clear, so a projector switched on at the start of the service
# opened on the sound check from twenty minutes earlier, and one opened after a
# break on the last words before it. Nothing on that wall is worth reading a
# minute after it was said: a segment older than this is dropped
# by the feed before anyone sees it. 0 keeps everything until Clear, as before.
CT_PROJECTOR_STALE_S = _pp_int_env("CT_PROJECTOR_STALE_S", 60, 0, 3600)
# How many of the most recent sentences stay BRIGHT; older ones on the wall are
# dimmed. 12 (default) is effectively "all" -- with the wall only a few lines tall,
# nothing dims, so the strip reads as one calm colour. Lower it (1/2/3) to keep only
# the last N sentences bright and dim the rest, for emphasis on the newest line(s).
CT_PROJECTOR_HIGHLIGHT = _pp_int_env("CT_PROJECTOR_HIGHLIGHT", 12, 1, 12)
_CAP_LOCK = threading.Lock()
_CAP = {"rev": 0, "epoch": int(time.time()), "segs": [],
        "pace": None,   # {wps, dwell, highlight} from the Wall knobs; None = the .env defaults
        # Send-order stamps (the page's `n`) of the newest post applied: the last
        # Clear, the newest segment's creation, the pace. See caption_ep.
        "clear_n": None, "made_n": None, "pace_n": None}
_CAP_MAX_SEGS = 40


def _cap_prune():
    """Drop what has gone stale (see CT_PROJECTOR_STALE_S). Called under _CAP_LOCK
    before the store is read; bumps rev when it removed something, so a projector
    polling with the last rev is told the wall changed even though nothing was
    posted -- the segments simply aged out."""
    if not CT_PROJECTOR_STALE_S:
        return
    cutoff = time.monotonic() - CT_PROJECTOR_STALE_S
    keep = [s for s in _CAP["segs"] if s["at"] >= cutoff]
    if len(keep) != len(_CAP["segs"]):
        _CAP["segs"] = keep
        _CAP["rev"] += 1

# External-relay worker. The church web host can stall (mod_security, a slow shared
# box), and a BLOCKING relay would tie up a Flask worker thread for its whole timeout
# -- so /publish and /publish-audio hand the relay to ONE background thread and return
# at once. The realtime local flow (STT / translate / caption / projector) then never
# waits on the external site. Best-effort: a failure logs; the queue is bounded and
# drops the oldest job under a sustained outage rather than growing without limit.
_RELAY_Q = queue.Queue(maxsize=200)
_relay_thread = None
# tag -> the last relay's outcome {"ok", "at", "err"}, for the page's Web light
# (/pipe-status). The page only ever sees "queued"; this is what happened next.
_RELAY_HEALTH = {}


def _relay_worker():
    while True:
        url, payload, tag, timeout = _RELAY_Q.get()
        try:
            r = _get_http_session().post(
                url, json=payload,
                headers={"X-Publish-Token": CT_PUBLISH_TOKEN,
                         # Shared hosts (Bluehost mod_security) 406-reject the default
                         # python-requests UA; present as an ordinary client instead.
                         "User-Agent": "ChurchTranslate/1.0",
                         "Accept": "application/json"}, timeout=timeout)
            if r.status_code != 200:
                print(f"[{tag}] relay HTTP {r.status_code}", flush=True)
            _RELAY_HEALTH[tag] = {"ok": r.status_code == 200, "at": time.time(),
                                  "err": "" if r.status_code == 200 else f"HTTP {r.status_code}"}
        except Exception as exc:
            print(f"[{tag}] ERROR: {str(exc)[:120]}", flush=True)
            _RELAY_HEALTH[tag] = {"ok": False, "at": time.time(), "err": str(exc)[:120]}


def _relay_submit(url, payload, tag, timeout):
    """Queue one external relay for the background worker and return immediately.
    Preserves submission order (one worker, FIFO), so a caption's text is relayed
    before its audio. Drops the oldest queued job if the host is down long enough to
    fill the queue -- a stale caption is worthless, and the local flow must not stall."""
    global _relay_thread
    if _relay_thread is None or not _relay_thread.is_alive():
        _relay_thread = threading.Thread(target=_relay_worker, daemon=True)
        _relay_thread.start()
    try:
        _RELAY_Q.put_nowait((url, payload, tag, timeout))
    except queue.Full:
        try:
            _RELAY_Q.get_nowait()          # drop the oldest queued relay under a backlog
        except queue.Empty:
            pass
        try:
            _RELAY_Q.put_nowait((url, payload, tag, timeout))
        except queue.Full:
            pass


def _pp_send_text(text):
    """Set the ProPresenter Message token to `text`. Returns (ok, error-or-None).
    Best-effort: a failure is logged and reported, never raised, so neither the
    typewriter thread nor the request handler dies on a ProPresenter hiccup."""
    msg = quote(CT_PROPRESENTER_MESSAGE, safe="")
    body = [{"name": CT_PROPRESENTER_TOKEN, "text": {"text": text}}]
    try:
        r = _get_http_session().post(
            f"{CT_PROPRESENTER_URL}/v1/message/{msg}/trigger",
            json=body, headers={"Accept": "application/json"}, timeout=5)
        if r.status_code not in (200, 204):
            return False, f"ProPresenter HTTP {r.status_code}"
        return True, None
    except Exception as exc:
        print(f"[project] ERROR: {exc}", flush=True)
        return False, str(exc)[:120]


def _pp_clear():
    """Hide the ProPresenter Message. Returns (ok, error-or-None)."""
    msg = quote(CT_PROPRESENTER_MESSAGE, safe="")
    try:
        r = _get_http_session().get(
            f"{CT_PROPRESENTER_URL}/v1/message/{msg}/clear",
            headers={"Accept": "application/json"}, timeout=5)
        if r.status_code not in (200, 204):
            return False, f"ProPresenter HTTP {r.status_code}"
        return True, None
    except Exception as exc:
        print(f"[project] ERROR: {exc}", flush=True)
        return False, str(exc)[:120]


def _wrap_words(words, cols):
    """Greedy word-wrap a list of words into lines of at most `cols` characters.
    A single word longer than `cols` gets its own (over-long) line."""
    lines, cur = [], ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= cols:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class _ProjectorTyper:
    """Streams the caption to ProPresenter a word at a time into a bottom-aligned
    rolling window that stays FULL, so the Message reads like a live lower third
    rather than one sentence flashing up and vanishing.

    Fed by commit(text): a finalized caption, whose words join the permanent
    stream. The visible text is the last CT_PROPRESENTER_LINES lines of it,
    revealed CT_PROPRESENTER_TYPE_WORDS word every CT_PROPRESENTER_TYPE_MS ms. Once
    enough words have flowed the lines stay full; a short sentence no longer clears
    the box, it just adds to the stream.

    The box is CT_PROPRESENTER_LINES rows tall, BOTTOM-ALIGNED: as a new line fills
    at the bottom the previous line moves up a row on its own, and the oldest drops
    off the top -- a plain rolling window, no extra frames. Message triggers are
    coalesced to at most one every _PP_MIN_SEND_S so the word-by-word reveal cannot
    flood ProPresenter (which re-renders on each trigger).

    A single daemon thread does the timing; it reveals more per tick (or snaps) when
    a backlog builds, so the caption never trails the room."""

    def __init__(self):
        self._lock = threading.Lock()
        self._committed = []      # finalized words -- the permanent rolling stream
        self._shown = 0           # words of it already typed out
        self._last_sent = None
        self._last_send_at = 0.0  # monotonic time of the last ProPresenter trigger
        self._wake = threading.Event()
        self._thread = None

    def _screenful(self):
        # Rough word capacity of the visible window (~6 chars/word average).
        return max(1, CT_PROPRESENTER_LINES * max(1, CT_PROPRESENTER_COLS // 6))

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def commit(self, text):
        """A finalized caption: lock its words into the permanent stream. Memory is
        bounded to the last few windows -- the display
        never needs more, and _shown is shifted to stay pointing at the same word."""
        with self._lock:
            self._committed.extend(text.split())
            cap = self._screenful() * 4
            if len(self._committed) > cap:
                drop = len(self._committed) - cap
                self._committed = self._committed[drop:]
                self._shown = max(0, self._shown - drop)
            self._ensure_thread()
        self._wake.set()

    def clear(self):
        with self._lock:
            self._committed = []
            self._shown = 0
            self._last_sent = None
            self._last_send_at = 0.0
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            while True:
                time.sleep(CT_PROPRESENTER_TYPE_MS / 1000.0)
                caught_up = False
                with self._lock:
                    target = self._committed
                    behind = len(target) - self._shown
                    if behind <= 0:                       # caught up -- wait for more words
                        self._wake.clear()
                        caught_up = True
                    else:
                        screen = self._screenful()
                        # One word per tick normally; reveal more (or snap) when a
                        # backlog builds, so the caption never trails the speaker.
                        step = CT_PROPRESENTER_TYPE_WORDS
                        if behind > 2 * screen:
                            step = behind - screen        # snap: leave ~one screen queued
                        elif behind > screen:
                            step = max(step, CT_PROPRESENTER_TYPE_WORDS * 3)
                        self._shown = min(len(target), self._shown + step)
                    wrapped = _wrap_words(target[:self._shown], CT_PROPRESENTER_COLS)
                    text = "\n".join(wrapped[-CT_PROPRESENTER_LINES:])
                # One trigger per change, coalesced to at most one every _PP_MIN_SEND_S,
                # so a word-by-word stream never floods ProPresenter. No separate
                # "scroll" frame: a bottom-aligned window moves the previous line up on
                # its own as a new line fills below it. `caught_up` flushes the final
                # state at once, so the last words are never left unsent. Sent outside
                # the lock -- the network call must not block feeds.
                if text != self._last_sent and (
                        caught_up or time.monotonic() - self._last_send_at >= _PP_MIN_SEND_S):
                    self._last_sent = text
                    self._last_send_at = time.monotonic()
                    _pp_send_text(text)
                if caught_up:
                    break


_projector_typer = _ProjectorTyper() if PROJECT_TYPING else None
# Model pins: what each pinned model was when last validated, plus the latency
# baseline the runtime alarm compares against. Advisory throughout -- see
# scripts/model_pins.py for why an existence check alone is not enough.
import model_pins
# The session's spoken audio, kept clip by clip so a service can be saved off
# afterwards with its own timestamps and text (see app/recorder.py). Buffered
# HERE rather than in the browser: the mp3 bytes already exist on this side, and
# a tab that is closed takes a browser-side buffer with it.
sys.path.insert(0, HERE)
from recorder import Recording

RECORDING = Recording(mime=AUDIO_MIME)

PORT = int(os.environ.get("CT_PORT", "5000"))


def _read_version():
    """App version from version.txt, wherever it lives -- the repo root when run
    from source, or the PyInstaller bundle root when frozen. "unknown" if missing
    (e.g. an older build where version.txt was not bundled)."""
    candidates = []
    _mei = getattr(sys, "_MEIPASS", None)
    if _mei:
        candidates.append(os.path.join(_mei, "version.txt"))
    candidates.append(os.path.join(os.path.dirname(HERE), "version.txt"))  # repo root
    candidates.append(os.path.join(HERE, "version.txt"))
    for _p in candidates:
        try:
            with open(_p, encoding="utf-8") as _f:
                _v = _f.read().strip()
            if _v:
                return _v
        except OSError:
            continue
    return "unknown"


APP_VERSION = _read_version()

print(f"[app] Church Translate v{APP_VERSION}", flush=True)
print("[app] starting...", flush=True)
_t0 = time.time()

# A key pasted with a smart-quote or en/em-dash (the usual copy-through-Word
# mistake) is accepted here but later rejected by the HTTP client as an opaque
# UnicodeEncodeError 500 the first time it's used. Check all three at startup so
# the operator sees a plain-English warning in the console right away instead.
for _key_name in ("ANTHROPIC_API_KEY", "SONIOX_API_KEY", "ELEVENLABS_API_KEY"):
    _key_problem = ascii_key_problem(_key_name, os.environ.get(_key_name))
    if _key_problem:
        print(f"[app] WARNING: {_key_problem}", flush=True)

print("[app] STT: " + ("Soniox ready" if HAS_SONIOX_KEY
      else "NONE -- set SONIOX_API_KEY"), flush=True)

print("[app] MT: Claude"
      + (" + gpt-6-luna (OpenAI)" if HAS_OPENAI_KEY else "")
      + (" + Grok (xAI)" if HAS_XAI_KEY else "")   # exact models come from the live catalogue
      + (" + Gemini (Google)" if HAS_GEMINI_KEY else "")
      + ("" if (HAS_OPENAI_KEY and HAS_XAI_KEY and HAS_GEMINI_KEY)
         else " (set OPENAI_API_KEY / XAI_API_KEY / GEMINI_API_KEY for more options)"),
      flush=True)

# TTS: either backend can speak any target language. ElevenLabs needs its own
# key plus a voice id; Soniox rides on the SONIOX_API_KEY already required for
# STT and ships built-in voices, so having STT configured is enough for it.
# Whichever are available are offered to the operator; a missing voice leaves
# only ElevenLabs off. Captions always work regardless.
EL_TTS_CAPABLE = bool(HAS_EL_KEY and os.environ.get("CT_EL_VOICE"))
SX_TTS_CAPABLE = HAS_SONIOX_KEY
TTS_PROVIDERS = ([("elevenlabs", "ElevenLabs")] if EL_TTS_CAPABLE else []) \
              + ([("soniox", "Soniox")] if SX_TTS_CAPABLE else [])
TTS_CAPABLE = bool(TTS_PROVIDERS)
# Fall back to whatever is actually configured rather than honouring a
# CT_TTS_PROVIDER pointing at a backend with no key behind it.
DEFAULT_TTS_PROVIDER = (TTS_PROVIDER
                        if any(p == TTS_PROVIDER for p, _ in TTS_PROVIDERS)
                        else (TTS_PROVIDERS[0][0] if TTS_PROVIDERS else "elevenlabs"))
print("[app] TTS " + (f"ready via {', '.join(n for _, n in TTS_PROVIDERS)}"
                      f" (default {DEFAULT_TTS_PROVIDER}; any target language can be spoken)"
                      if TTS_CAPABLE else
                      "OFF (set ELEVENLABS_API_KEY + CT_EL_VOICE, or SONIOX_API_KEY)"), flush=True)
# Voice settings the page no longer shows (2026-10-28): set once here.
# Stream: ElevenLabs v4 / v4 Turbo read the service as one continuous speech
# (voice_stream) instead of a clip per caption. Expression: the translator may
# lead a spoken line with a delivery tag such as [gently]. Tighten pauses:
# Soniox shortens the pauses between words (~10% shorter audio). Stability:
# ElevenLabs voice stability (not used by Stream).
def _env_on(name, default):
    return (os.environ.get(name) or default).strip().lower() not in ("0", "false", "no", "off")
EL_STREAM = _env_on("CT_EL_STREAM", "1")
EL_EXPRESSION = _env_on("CT_EL_EXPRESSION", "0")
SX_TIGHTEN = _env_on("CT_SX_TIGHTEN", "1")
try:
    EL_STABILITY = min(1.0, max(0.0, float(os.environ.get("CT_EL_STABILITY") or 0.5)))
except ValueError:
    EL_STABILITY = 0.5
# The translator that also hears each caption's audio (gemini-3.6-flash + audio)
# left the Model menu 2026-10-29; CT_GEMINI_AUDIO=1 offers it again.
GEMINI_AUDIO = _env_on("CT_GEMINI_AUDIO", "0")

print(f"[app] Web captions {'ON -> ' + CT_PUBLISH_URL if PUBLISH_CAPABLE else 'OFF (set CT_PUBLISH_URL + CT_PUBLISH_TOKEN to stream captions to your website)'}", flush=True)

_pp_mode = (f" [typing {CT_PROPRESENTER_TYPE_WORDS}w/{CT_PROPRESENTER_TYPE_MS}ms, "
            f"{CT_PROPRESENTER_LINES}x{CT_PROPRESENTER_COLS}]" if PROJECT_TYPING else "")
print(f"[app] ProPresenter {'ON -> ' + CT_PROPRESENTER_URL + ' (message ' + CT_PROPRESENTER_MESSAGE + ')' + _pp_mode if PROJECT_CAPABLE else 'OFF (set CT_PROPRESENTER_URL + CT_PROPRESENTER_MESSAGE to project captions to ProPresenter)'}", flush=True)

print(f"[app] ready in {time.time() - _t0:.0f}s -> http://localhost:{PORT}", flush=True)


def _warm_mt():
    """Pre-open the translator TLS connections in the background so the first
    caption of the service doesn't pay client construction + TCP/TLS handshake
    on top of the model call."""
    import translate_claude, translate_openai, translate_xai, translate_gemini
    translate_claude.warm()
    if HAS_OPENAI_KEY:
        translate_openai.warm()
    if HAS_XAI_KEY:
        translate_xai.warm()
    if HAS_GEMINI_KEY:
        translate_gemini.warm()
        # Catch a moved reasoning floor at startup rather than mid-service --
        # the Gemini-shaped version of translate_xai's check_default_model.
        translate_gemini.check_default_model()


threading.Thread(target=_warm_mt, daemon=True).start()


def _check_pins():
    """Report any pinned model that is no longer what it was validated as.

    Runs in the background AFTER "ready" is printed, on purpose: it makes up to
    three provider calls, and a slow catalogue must never stand between the
    operator and a service that is about to start. Advisory only -- the result
    is printed and offered on /caps, and never changes which model is used."""
    try:
        results = model_pins.check_pins()
        if results:
            model_pins.report(results)
        with PIN_LOCK:
            PIN_STATUS.extend(results)
    except Exception as exc:                 # a broken pin file is not an outage
        print(f"[pins] check skipped ({exc})", flush=True)


PIN_STATUS = []
PIN_LOCK = threading.Lock()
threading.Thread(target=_check_pins, daemon=True).start()

# Convenience for the one-click Windows launcher: pop the browser open once the
# server is up (via cmd.exe; a no-op off Windows). CT_OPEN=0 skips it.
if os.environ.get("CT_OPEN", "1") not in ("0", "false", "no"):
    try:
        subprocess.Popen(["cmd.exe", "/c", "start", "", f"http://localhost:{PORT}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False   # preserve LANGUAGES' order (e.g. "auto" first) over the wire
sock = Sock(app)   # registers the /stt-stream WebSocket on the same server


@app.errorhandler(Exception)
def _json_errors(e):
    """Live captions consume JSON; an unhandled exception must never reach the
    browser as an HTML error page (the client's r.json() would choke on '<').
    Return the error as JSON and log the full traceback server-side."""
    import traceback
    from werkzeug.exceptions import HTTPException
    code = e.code if isinstance(e, HTTPException) else 500
    print(f"[err] {request.path}: {type(e).__name__}: {e}", flush=True)
    if not isinstance(e, HTTPException):
        traceback.print_exc()
    return jsonify(error=f"{type(e).__name__}: {e}"), code

# Flask runs threaded so a slow cloud call never blocks another request. The
# cloud waits in /stt and /translate hold nothing at all.
#
# TTS used to share ONE mutex, which bought no ordering -- clips are already
# kept in order by the per-request FIFO in /translate -- and cost real latency:
# the first sentence of segment N+1 queued behind the last sentence of segment
# N, and a Replay tap could stall the live voice entirely. What's actually
# worth bounding is how many clips we have in flight at a cloud provider at
# once, so this is a semaphore, not a lock.
# .env.example ships the key blank, so "" and other junk must fall back rather
# than crash the server on boot.
try:
    TTS_MAX_INFLIGHT = max(1, int(os.environ.get("CT_TTS_MAX_INFLIGHT") or 3))
except ValueError:
    TTS_MAX_INFLIGHT = 3
TTS_SEM = threading.BoundedSemaphore(TTS_MAX_INFLIGHT)


class VoiceContext:
    """The running context of the ElevenLabs voice: what it said last, so each
    clip continues the one before it instead of starting cold.

    Captions are packets cut mid-sentence and every sentence of one is its own
    request, so without this each clip is read as a fresh opening: its own pitch
    reset, its own falling end. ElevenLabs takes the context two ways, and every
    clip sends both: `previous_text` (the words said just before) and
    `previous_request_ids` (up to three earlier generations, so it can match the
    actual audio, not only the words). An id is known only once its clip has
    finished, and clips of consecutive packets overlap (TTS_SEM), so ids are
    cited only while every clip since them is finished: never one that skips
    the line said in between.

    One thread per (voice, model, language): a voice or language switch starts
    a new one, as does a pause of CONTEXT_IDLE_S (a new service, or the voice
    was off). Replay (/speak) uses none -- it repeats a line out of order."""
    KEEP = 3                  # clips remembered (the API takes up to 3 ids)
    TEXT_MAX = 600            # chars of previous_text
    CONTEXT_IDLE_S = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._said = []       # [{"text", "id" (None until done), "at"}], oldest first

    def begin(self, key, text):
        """Context for the clip about to speak `text`, plus its handle for done()."""
        now = time.monotonic()
        with self._lock:
            if key != self._key or (self._said and now - self._said[-1]["at"] > self.CONTEXT_IDLE_S):
                self._key, self._said = key, []
            prev = " ".join(e["text"] for e in self._said)[-self.TEXT_MAX:].strip()
            ids = []
            for e in reversed(self._said):
                if not e["id"]:
                    break
                ids.insert(0, e["id"])
            entry = {"text": text, "id": None, "at": now}
            self._said = (self._said + [entry])[-self.KEEP:]
        ctx = {}
        if prev:
            ctx["previous_text"] = prev
        if ids:
            ctx["previous_request_ids"] = ids[-self.KEEP:]
        return ctx, entry

    def done(self, entry, request_id):
        with self._lock:
            entry["id"] = request_id
            entry["at"] = time.monotonic()

    def failed(self, entry):
        """The clip was never spoken: it is not context for the next one."""
        with self._lock:
            if entry in self._said:
                self._said.remove(entry)


VOICE_CONTEXT = VoiceContext()

# Rolling translation context: recent (source, target) pairs replayed to Claude
# so each segment is translated with the sermon's running context. Keyed by
# SESSION, not by target: under auto-detect the source already varies per
# segment, and a replayed turn's context value never depended on what language
# its source happened to be in. A one-way session keys on its single target; a
# bilingual pair keys on the PAIR, so one pooled history spans both directions
# and a speaker handover doesn't halve the context (each entry carries its own
# target, so the replay still tags every turn the way it was actually
# translated). A defaultdict means any key lazily gets its own deque, no fixed
# keys. Entries older than HISTORY_MAX_AGE_S are dropped (after a long silence
# the old context is more likely to mislead than help).
from collections import defaultdict
HISTORY = defaultdict(lambda: deque(maxlen=6))   # key -> deque of (ts, src, tgt, target_lang)
HISTORY_LOCK = threading.Lock()
HISTORY_MAX_AGE_S = 300
# Runaway guard (translate_ep): a reply longer than RATIO x its source + SLACK
# characters is the model over-generating. A faithful translation runs about
# 0.8-1.5x; the slack keeps a two-word fragment's reply ("So I." -> "Așa că
# eu.") and an expanded abbreviation well clear of it.
RUNAWAY_RATIO, RUNAWAY_SLACK = 3, 60

_seq = 0  # server-side counter for log correlation
_seq_lock = threading.Lock()

# Rolling per-model translate latency, and the alarm built on it. This is the
# RUNTIME half of the model-pin idea: a silent remap onto a reasoning model is
# invisible to a catalogue check (the id still resolves, requests still return
# 200) but it cannot hide from a median -- reasoning happens before the first
# token, so it lands squarely on the number measured here. Compared against the
# model's pinned baseline rather than a fixed threshold, because Sonnet is
# legitimately slower than Haiku and one global number would either cry wolf or
# say nothing.
MT_LATENCY = defaultdict(lambda: deque(maxlen=25))   # model -> recent seconds
MT_LATENCY_LOCK = threading.Lock()
MT_SLOW = {}                                          # model -> (observed_p50, baseline)
MT_UNPINNED_SAID = set()


def _cache_state(stats):
    """Classify one call's prompt-cache counters for the Translation panel light.

    States, and what each one means for the bill:
      hit   -- the system prefix was served from cache, at ~0.1x input price.
               The steady state during a service.
      write -- this call CREATED the entry, at ~1.25x input price. Expected on
               the first call, on the first call after a >5min gap (the TTL),
               and on the first call after any edit to the prompt or glossary.
               Claude only -- Gemini builds its cache in a separate request, so
               no single caption is ever the one that pays to create it.
      off   -- nothing was served from cache, so this caption paid full input
               price for the whole system prompt, silently. For Claude that
               means the prefix is under the minimum cacheable size; for Gemini
               it means the explicit cache is disabled, still being built, or
               could not be built; for OpenAI and xAI (automatic caching, no
               write premium, so never `write`) it is the first call, a call
               after a few minutes idle, or a prompt under their 1024-token
               floor. Either way it is the state worth catching.
      None  -- no usage came back from the call. The light stays dark rather
               than guessing.
    """
    if not stats:
        return None
    read, write = stats["read"], stats["write"]
    if read:
        state = "hit"
    elif write:
        state = "write"
    else:
        state = "off"
    return {"state": state, "read": read, "write": write,
            "input": stats["input"],
            # Whole prompt, every provider. Claude reports it separately because
            # its input_tokens excludes the cached part; the others count it in.
            "prompt": stats.get("prompt", stats["input"])}


def _mt_p50(model):
    d = MT_LATENCY.get(model)
    if not d:
        return None
    ordered = sorted(d)
    return round(ordered[len(ordered) // 2], 2)


def _note_latency(model, seconds):
    """Record one call and, if the rolling median has drifted well past the
    pinned baseline, say so ONCE per model rather than every call -- a warning
    that repeats every line is a warning the operator learns to scroll past."""
    with MT_LATENCY_LOCK:
        d = MT_LATENCY[model]
        d.append(seconds)
        if len(d) < model_pins.LATENCY_MIN_SAMPLES:
            return
        p50 = _mt_p50(model)
        status, base = model_pins.latency_verdict(model, p50)
        if status == "unpinned":
            # No baseline means no alarm is possible, so say what one WOULD be:
            # otherwise the only way to learn the number is to already have it.
            # Once per model per run -- it is an invitation, not a problem.
            if model not in MT_UNPINNED_SAID:
                MT_UNPINNED_SAID.add(model)
                print(f"[mt ] {model} median {p50:.1f}s over {len(d)} calls; no "
                      f"baseline pinned -- set latency_p50_s in "
                      f"config/model_pins.json to arm the slow-model alarm",
                      flush=True)
        elif status == "slow" and model not in MT_SLOW:
            MT_SLOW[model] = (p50, base)
            print(f"[mt ] WARNING: {model} median {p50:.1f}s vs pinned {base:.1f}s "
                  f"-- more than {model_pins.LATENCY_ALARM_RATIO:g}x slower than "
                  f"when it was validated; check it has not been remapped",
                  flush=True)
        elif status == "ok" and model in MT_SLOW:
            MT_SLOW.pop(model, None)      # recovered (a transient network spell)

# Usage tracking for the credit meter (this server run). Cloud STT bills per
# minute, cloud TTS per character; we tally what we actually sent to ElevenLabs.
_usage = {"stt_secs": 0.0, "tts_chars": 0, "claude_calls": 0}
_usage_lock = threading.Lock()

# Cache the ElevenLabs subscription (remaining credits) so /usage polling does
# not hammer the API.
_el_sub = {"at": 0.0, "data": None}
_EL_SUB_TTL = 30.0

# Cache the ElevenLabs voice list (for the in-app voice dropdown).
_el_voices = {"at": 0.0, "data": None}
_EL_VOICES_TTL = 300.0


def _bump_usage(**deltas):
    with _usage_lock:
        for k, v in deltas.items():
            _usage[k] += v


# _get_http_session is synthesize.get_session, imported above: ONE pool for the
# whole process rather than a second one alongside the TTS session. Both talk to
# api.elevenlabs.io, so sharing means the subscription/voices polling below
# doubles as a keep-alive heartbeat for the very connection the next clip
# synthesizes over, instead of warming a pool TTS never touches.


def _el_voice_settings(data):
    """ElevenLabs voice_settings from the request's `vs` block (style / speed)
    for live expressivity tuning; stability is CT_EL_STABILITY. None if
    absent/invalid (EL defaults)."""
    vs = data.get("vs")
    if not isinstance(vs, dict):
        return None
    def _f(k, default, lo, hi):
        try:
            return min(max(float(vs.get(k, default)), lo), hi)
        except (TypeError, ValueError):
            return default
    return {
        "stability": EL_STABILITY,
        "similarity_boost": 0.75,
        "style": _f("style", 0.0, 0.0, 1.0),
        "use_speaker_boost": True,
        "speed": _f("speed", 1.0, 0.7, 1.2),
    }


def _sx_voice_settings(data):
    """Soniox knobs from the request's `sx` block. Deliberately NOT the same
    shape as _el_voice_settings: Soniox has no stability/style equivalent, and
    its speed is a top-level field rather than part of a voice_settings dict."""
    sx = data.get("sx")
    if not isinstance(sx, dict):
        return None
    try:
        speed = min(max(float(sx.get("speed", 1.0)), 0.7), 1.3)
    except (TypeError, ValueError):
        speed = 1.0
    return {"speed": speed, "reduce_silence": SX_TIGHTEN}


_prewarm_at = 0.0
_PREWARM_TTL = 60.0


def _prewarm_tts():
    """Open a pooled connection to each configured TTS host (see
    synthesize.prewarm). Throttled: a flaky Soniox socket reconnects up to three
    times with backoff, and each of those re-enters /stream -- one handshake per
    minute is all this is worth."""
    global _prewarm_at
    now = time.time()
    if now - _prewarm_at < _PREWARM_TTL:
        return
    _prewarm_at = now
    for provider, _ in TTS_PROVIDERS:
        warmed = tts_prewarm(provider)
        if warmed:
            print(f"[tts ] prewarmed {', '.join(warmed)}", flush=True)


def _tts_provider(data):
    """Which backend this request wants, ignoring anything not configured."""
    p = (data.get("tts_provider") or "").strip().lower()
    return p if any(p == name for name, _ in TTS_PROVIDERS) else DEFAULT_TTS_PROVIDER


def _el_subscription():
    """ElevenLabs character usage/limit for the billing period (cached). Reports
    the reason if it can't read (e.g. a key scoped to STT+TTS only lacks the
    'user read' permission the subscription endpoint needs)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None
    now = time.time()
    if _el_sub["data"] is not None and now - _el_sub["at"] < _EL_SUB_TTL:
        return _el_sub["data"]
    try:
        r = _get_http_session().get("https://api.elevenlabs.io/v1/user/subscription",
                         headers={"xi-api-key": key}, timeout=10)
        if r.status_code == 200:
            j = r.json()
            _el_sub["data"] = {"used": j.get("character_count"),
                               "limit": j.get("character_limit")}
        elif r.status_code in (401, 403):
            _el_sub["data"] = {"error": "key lacks 'user read' scope"}
        else:
            _el_sub["data"] = {"error": f"HTTP {r.status_code}"}
        _el_sub["at"] = now
    except Exception as exc:
        _el_sub["data"] = {"error": str(exc)[:60]}
        _el_sub["at"] = now
    return _el_sub["data"]


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/help")
def help_pdf():
    """The quick start guide (QUICKSTART.pdf at the repo root), for the ? button."""
    return send_from_directory(os.path.dirname(HERE), "QUICKSTART.pdf")


@app.route("/projector")
def projector():
    """Standalone caption strip for the big screen -- point a ProPresenter web
    object here, or open it on any machine on the LAN. It carries only the
    projector's own CSS/JS (no operator UI) and feeds from /caption-feed plus the
    aron-captions BroadcastChannel. The operator page's Project button opens it."""
    return send_from_directory(HERE, "projector.html")


@app.route("/cut-models")
def cut_models():
    """Weights for the experimental Cut rule menu (index.html, sxCutClf):
    logistic regressions exported by validation/scripts/unit_export.py."""
    resp = send_from_directory(HERE, "cut_models.json")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# SaT (wtpsplit's Segment any Text, a 3-layer ONNX model) for the "+ SaT" cut
# rules. Optional and loaded on first use: the rest of the app never needs it,
# and the Windows build does not bundle it. Scores the words it is sent, once on
# the text with the punctuation stripped and once as sent, and returns the
# boundary probability at the end of each word -- the two SaT inputs the
# classifier was trained with (validation/scripts/sat_lab.py, TEXT strip / raw).
_SAT = {"model": None, "lock": threading.Lock(), "error": None}


def _sat_available():
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("wtpsplit", "onnxruntime"))


def _sat_model():
    with _SAT["lock"]:
        if _SAT["model"] is None and _SAT["error"] is None:
            try:
                from wtpsplit import SaT
                _SAT["model"] = SaT("sat-3l-sm", ort_providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001
                _SAT["error"] = str(exc)[:200]
        return _SAT["model"]


def _sat_word_probs(sat, words):
    text, ends = "", []
    for w in words:
        text += (" " if text else "") + w
        ends.append(len(text) - 1)
    pr = sat.predict_proba(text)
    return [round(float(pr[e]), 5) for e in ends]


@app.route("/sat", methods=["POST"])
def sat_ep():
    words = [str(w) for w in ((request.get_json(silent=True) or {}).get("words") or [])][-64:]
    if not _sat_available():
        return jsonify(error="wtpsplit / onnxruntime not installed"), 501
    sat = _sat_model()
    if sat is None:
        return jsonify(error=_SAT["error"] or "SaT failed to load"), 503
    if not words:
        return jsonify(strip=[], raw=[])
    strip = [re.sub(r"[^\w'-]+", "", w) or w for w in words]
    return jsonify(strip=_sat_word_probs(sat, strip), raw=_sat_word_probs(sat, words))


@app.route("/models/<path:fn>")
def model_asset(fn):
    """Self-hosted ONNX models (YAMNet, for the music detector), same offline
    rule as /vad: no CDN, no cloud call, works in a building with no internet."""
    resp = send_from_directory(os.path.join(HERE, "models"), fn)
    if fn.endswith(".onnx"):
        resp.headers["Content-Type"] = "application/octet-stream"
    # Immutable vendored weights -- let the browser keep them across restarts
    # rather than re-reading 16 MB from disk on every page load.
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/vad/<path:fn>")
def vad_asset(fn):
    """Self-hosted Silero VAD assets (model + onnxruntime wasm + worklet) so the
    browser VAD runs fully offline -- no CDN. Force the MIME types onnxruntime-web
    and the AudioWorklet require."""
    resp = send_from_directory(os.path.join(HERE, "vad"), fn)
    if fn.endswith(".wasm"):
        resp.headers["Content-Type"] = "application/wasm"
    elif fn.endswith((".mjs", ".js")):
        resp.headers["Content-Type"] = "text/javascript"
    return resp


# Cloud STT can tag non-speech events inline, e.g. "(laughter)", "(dog barking)",
# "(no audio)". They are not spoken words -- strip them so they are neither shown
# nor translated. In transcribed speech a parenthetical/bracketed span is
# essentially always such an annotation, so removing them is safe.
_STT_TAG_RE = re.compile(r"[(\[][^)\]]*[)\]]")


def _strip_stt_tags(text):
    return " ".join(_STT_TAG_RE.sub(" ", text).split())


def _soniox_majority_lang(tokens):
    """Whichever language the majority of a segment's finalized tokens carry, for
    DISPLAY purposes only (a per-row badge in the UI) -- never a routing decision,
    since translation always targets the session's fixed target language. None if
    no token carried a language (rare, but the caller must tolerate it)."""
    if not isinstance(tokens, list):
        return None
    from collections import Counter
    langs = [t.get("language") for t in tokens
             if isinstance(t, dict) and t.get("language")]
    return Counter(langs).most_common(1)[0][0] if langs else None


def parse_hints(raw):
    """Normalize a language-hint request into a list of at most two valid codes.

    Accepts a list, a comma-separated string, or a single code; "auto" and
    anything not in SOURCE_LANGUAGES is dropped, as are duplicates. The result's
    LENGTH is what the rest of the pipeline switches on: 0 = auto-detect,
    1 = a pinned one-way hint, 2 = a bilingual pair with automatic direction."""
    if isinstance(raw, str):
        raw = raw.split(",")
    elif not isinstance(raw, (list, tuple)):
        raw = [raw] if raw else []
    out = []
    for h in raw:
        code = str(h or "").strip().lower()
        if code and code != "auto" and code in SOURCE_LANGUAGES and code not in out:
            out.append(code)
    return out[:2]


def _soniox_lang_config(hints):
    """The language half of a Soniox config, shared by the per-segment and
    streaming paths so they cannot drift.

    Language identification is switched on for auto (as before) and for a PAIR,
    where it is load-bearing rather than cosmetic: the browser's direction engine
    decides which way to translate each unit from the per-token `language` field,
    and without LID those tokens carry none. A single pinned hint deliberately
    still leaves it off, exactly as before -- that path has no direction to
    decide, so this is not the place to change what it sends."""
    config = {}
    if hints:
        config["language_hints"] = list(hints)
    if len(hints) != 1:
        config["enable_language_identification"] = True
    return config


def _soniox_stt(audio_bytes, hints, n):
    """Soniox realtime STT over WebSocket (wss://stt-rt.soniox.com): open -> send a
    JSON config -> stream the segment's audio -> send "" to close the stream -> read
    token messages until 'finished'. One streamed connection, NO upload/poll/fetch
    round-trips, so it adds far less latency than the async file API -- the right fit
    for the live per-segment path. (The async API still suits whole-file offline runs;
    that lives in scripts/compare_stt.py.)

    `hints` is a parse_hints() list: empty (auto-detect), one pinned code, or a
    bilingual pair. The returned language is the detected one whenever Soniox
    identified it, and only falls back to the hint when a single code was pinned
    -- with a PAIR, which of the two was actually spoken is the whole question,
    so a hint is no answer. Returns (text, lang_or_None)."""
    import json
    from websockets.sync.client import connect
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        raise RuntimeError("SONIOX_API_KEY not set")
    config = {"api_key": key, "model": SONIOX_RT_MODEL, "audio_format": "auto"}
    config.update(_soniox_lang_config(hints))
    final_tokens = []
    with connect(SONIOX_WS_URL, open_timeout=15, close_timeout=5) as ws:
        ws.send(json.dumps(config))
        for i in range(0, len(audio_bytes), 16384):       # stream the WAV in chunks
            ws.send(audio_bytes[i:i + 16384])
        ws.send("")                                       # end-of-audio sentinel
        while True:                                       # collect finalized tokens
            res = json.loads(ws.recv(timeout=30))
            if res.get("error_code") or res.get("error_message"):
                raise RuntimeError(
                    f"Soniox RT {res.get('error_code')}: {res.get('error_message')}")
            final_tokens += [t for t in res.get("tokens", []) if t.get("is_final")]
            if res.get("finished"):
                break
    # Soniox tokens carry their own leading spaces/punctuation, so concatenate as-is.
    text = _strip_stt_tags("".join(t.get("text", "") for t in final_tokens).strip())
    lang = _soniox_majority_lang(final_tokens)
    if lang is None and len(hints) == 1:
        lang = hints[0]
    return text, lang


@app.route("/stt", methods=["POST"])
def stt_ep():
    """Audio segment -> punctuated source text.

    ?hints=<code>[,<code>] (or the older ?source=<code>|auto) is an STT accuracy
    HINT, never routing -- the direction is decided client-side and arrives
    separately as /translate's target_lang. Two hints put Soniox in bilingual
    mode; the returned `lang` is then what the browser's direction engine reads,
    so it matters there in a way it never did on the one-way path. Returns
    {text, lang}; lang is null if Soniox never identified one."""
    global _seq
    with _seq_lock:
        _seq += 1
        n = _seq
    hints = parse_hints(request.args.get("hints") or request.args.get("source"))
    raw = request.get_data()
    print(f"[stt #{n}] recv {len(raw)} bytes [hints={hints or 'auto'}]", flush=True)
    if not raw:
        return jsonify(error="no audio received"), 400
    if not HAS_SONIOX_KEY:
        return jsonify(error="Soniox key not configured"), 503
    t = time.time()
    text_punct, lang = _soniox_stt(raw, hints, n)
    dt = time.time() - t
    print(f"[stt #{n}] [{lang}] -> {dt:.1f}s -> {text_punct!r}", flush=True)
    return jsonify(text=text_punct, lang=lang, seconds=round(dt, 1))


# Soniox exposes three endpoint controls and the app historically sent only one.
# Measured here on synthesised Romanian (6 sentences x 3 runs, end-of-speech to
# the '<end>' token, over a 2.5s trailing silence):
#
#   max_endpoint_delay_ms  700 / 1300 / 2200  ->  0.34s / 0.32s / 0.31s
#   endpoint_latency_adjustment_level 0/1/2/3 ->  0.31s / 0.21s / 0.15s / 1.70s
#   endpoint_sensitivity  -0.5 / 0 / +0.5 / +1 -> 0.33s / 0.32s / 0.32s / 0.31s
#
# Two things follow. max_endpoint_delay_ms is a CEILING (default 2000) and the
# detector fires far below it, so the Pace bar's pause value -- which is wired to
# it -- changes nothing on clean audio; it earns its keep on the browser-side
# idle timers instead. And level 1 is a real ~100ms win whose spread also
# TIGHTENS (0.19-0.45s -> 0.11-0.30s), with no extra endpoints and an unchanged
# transcript. Levels 2 and 3 contradict their own documentation ("higher values
# reduce endpoint latency more aggressively"): 2 goes bimodal and 3 is five times
# slower than 1. Level 3 is not broken though, it is PATIENT -- on a clip with a
# 0.9s pause inside each sentence it is the only level that does not split there
# (0 wrong splits, against 3 of 3 at levels 0, 1 and 2), which is a trade a
# hesitant preacher might want. All of it was measured on synthetic speech with
# digital silence; a real sanctuary has noise, breath and trailing filler, which
# is exactly the regime endpoint_sensitivity exists for and the regime this clip
# cannot reproduce. The knobs are on the panel to be settled on a Sunday rather
# than guessed at from a lab clip.
ENDPOINT_ADJUST_MAX = 3          # stt-rt-v5: endpoint_latency_adjustment_max_level


def _endpoint_tuning(hello):
    """(level, sensitivity) from the browser's hello frame, clamped to the API's
    documented ranges. Junk degrades to 'unset' rather than raising: a bad knob
    must cost the service a tuning option, never the transcription itself."""
    try:
        level = int(hello.get("ep_adjust") or 0)
    except (TypeError, ValueError):
        level = 0
    try:
        sens = float(hello.get("ep_sens") or 0.0)
    except (TypeError, ValueError):
        sens = 0.0
    return (max(0, min(ENDPOINT_ADJUST_MAX, level)),
            round(max(-1.0, min(1.0, sens)), 2))


# ---- the audio tap: the caption's own audio, for a translator that hears ----
# The browser's PCM passes through stt_stream on its way to Soniox. Each stream
# keeps its last TAP_KEEP_S of it, counted from the stream's first byte -- the
# clock Soniox's own token times (start_ms / end_ms) run on, since Soniox times
# exactly the bytes it is sent. So a caption's word times, read in the browser,
# name the same stretch here. Every stream gets a fresh id, sent to the browser
# before any token; a /translate that names another stream's id gets no audio,
# which is what keeps a reconnect from pairing a caption with the wrong clip.
TAP_KEEP_S = 90
HEARS_PAD_BEFORE_MS = 200     # onset only: a longer lead-in hands the model the previous caption's last words
HEARS_PAD_AFTER_MS = 100      # the last word's tail; more lets the next caption's first word in
HEARS_MAX_MS = 30000          # a runaway span (a merged batch) is trimmed to its end
HEARS_FIRST_S = float(os.environ.get("CT_HEARS_FIRST_S") or "2.5")   # blank in .env.example = default


class _AudioTap:
    def __init__(self, tap_id, sample_rate):
        self.id, self.rate = tap_id, sample_rate
        self.lock = threading.Lock()
        self.buf = bytearray()
        self.base = 0                     # bytes dropped off the front so far
        self.ears = {}                    # the stream's extra ears (scripts/ears.py), by name
        self.packets = {}                 # packet id -> {ear: {"ev": Event, "text": str}}
        self.packet_meta = {}             # packet id -> (sealed at, monotonic; text; prev; earlier)

    def feed(self, data):
        with self.lock:
            self.buf += data
            extra = len(self.buf) - TAP_KEEP_S * self.rate * 2
            if extra > 0:
                extra -= extra % 2
                del self.buf[:extra]
                self.base += extra

    def clip(self, start_ms, end_ms):
        """WAV bytes for [start_ms, end_ms) of the stream, 16 kHz mono, or None
        when the stretch has scrolled out of the buffer or never arrived."""
        bpm = self.rate * 2 / 1000.0
        with self.lock:
            have = (self.base + len(self.buf)) / bpm
            end_ms = min(end_ms, have)
            a = int(start_ms * bpm) // 2 * 2 - self.base
            b = int(end_ms * bpm) // 2 * 2 - self.base
            if a < 0 or b - a < int(0.3 * self.rate) * 2:
                return None
            pcm = bytes(self.buf[a:b])
        return _wav16k(pcm, self.rate)


def _wav16k(pcm, rate):
    """s16le mono PCM -> 16 kHz WAV bytes (Gemini bills audio by the second, not
    the byte, so this only shrinks the upload: 48 kHz -> 16 kHz by averaging
    each run of three samples, a crude but sufficient low-pass for speech)."""
    import numpy as np
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if rate % 16000 == 0 and rate > 16000:
        k = rate // 16000
        x = x[: len(x) // k * k].reshape(-1, k).mean(axis=1)
        rate = 16000
    out = io.BytesIO()
    sf.write(out, x.astype(np.int16), rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


# Per-packet ears. When Soniox seals a packet the page sends its text; every ear
# is sliced there (ears.Ear.slice: the provider finalises what it has heard, on
# the session that stays open) and its reading of the packet is lined up out of
# its running transcript as soon as it has heard the packet out: its last word
# and a word past it (witness_align.packet_state). The text goes back to the page
# (shown under the packet) and waits here for /translate (_ear_clues). An ear that has not
# heard the packet's end within PACKET_READ_S has nothing more to say about it.
PACKETS_KEEP = 80
PACKET_READ_S = 6.0
# An ear's partial changes every ~0.1-0.2 s and each lining-up costs 3-100 ms of
# CPU (Pi 5, 8-40 words) under the GIL the Soniox relay shares. Each ear is
# lined up again at most every PACKET_ALIGN_MIN_S, or four times as long as its
# last lining-up took: a Router packet (~5 words, ~3 ms) every tick, a long
# assembler row no more than a quarter of the time.
PACKET_ALIGN_MIN_S = 0.2
# A stretch that reaches the packet's last word but has no word past it yet (the
# speaker paused, or the ear is still on it) counts as whole -- sent without "…" --
# only once it has stood unchanged this long. An ear still on the word rewrites it
# every 0.1-0.2 s ("Domnului Iis" -> "Domnului Iisus Hristos").
PACKET_STABLE_S = 0.4
# How long /translate may wait, from a packet's seal, for each ear to reach its
# last word when the page asks (ear_wait_ms; the Router's sent-on segments).
# Whole sermon, 27 meaning-changing mis-hears, gpt-6-sol-fast with all three
# ears: their readings as of seal +0.15 s (when the check returns) 21 right,
# +0.4 s 25, +0.7 s 26; without ears 20 (validation/results/2026-10-09-ear-sermon).
try:
    EAR_WAIT_MS = max(0, min(1500, int(os.environ.get("CT_EAR_WAIT_MS") or 700)))
except ValueError:
    EAR_WAIT_MS = 700


_EAR_LANG = [None]            # the language the ears last latched to (survives a stream restart)
EAR_LANG_WORDS, EAR_LANG_SHARE = 30, 0.75


def _ear_lang_latch(tap, lang):
    """-> feed(tokens): the language of Soniox's last EAR_LANG_WORDS final
    source words; when one holds EAR_LANG_SHARE of them and is not the ears'
    language, every ear switches to it (a pair's other side needs as much
    sustained speech before it takes over)."""
    recent = deque(maxlen=EAR_LANG_WORDS)
    cur = [lang]

    def feed(tokens):
        for t in tokens:
            if not t.get("is_final") or not t.get("language") or t.get("translation_status") == "translation":
                continue
            if t.get("text", "").startswith(" ") or not recent:     # a word's first piece
                recent.append(t["language"])
        if len(recent) < EAR_LANG_WORDS:
            return
        top = max(set(recent), key=recent.count)
        if top != cur[0] and recent.count(top) >= EAR_LANG_SHARE * len(recent):
            cur[0] = _EAR_LANG[0] = top
            for ear in tap.ears.values():
                ear.set_lang(top)
    return feed


def _start_packet_ears(tap, pid, text, prev, send):
    if not tap.ears:
        return
    for ear in tap.ears.values():
        ear.slice()
    if not text:
        return
    # text: the ear's reading once it has heard the packet out (packet_state);
    # partial: the best-lined-up stretch so far (what the Router sends when that is
    # all there is); reach: it gets to the packet's last word; since: when it last changed
    slots = {n: {"ev": threading.Event(), "text": "", "partial": "", "reach": False, "since": 0.0}
             for n in tap.ears}
    tap.packets[pid] = slots
    # The texts of the packets sealed before this one, newest last: a reading that
    # lines up with one of them better than with this packet is an ear running
    # behind, not its reading of this packet (witness_align.stale), and is dropped.
    earlier = [m[1] for m in list(tap.packet_meta.values())[-12:]]
    tap.packet_meta[pid] = (time.monotonic(), text, prev, earlier)
    for old in list(tap.packets)[:-PACKETS_KEEP]:
        tap.packets.pop(old, None)
        tap.packet_meta.pop(old, None)

    def run():
        t0 = time.monotonic()
        todo, seen, nxt = dict(tap.ears), {}, {}
        while todo and time.monotonic() - t0 < PACKET_READ_S:
            for n, ear in list(todo.items()):
                if time.monotonic() < nxt.get(n, 0.0):
                    continue
                snap = ear.snapshot()
                if seen.get(n) == snap:                  # nothing new heard: nothing to line up again
                    continue
                seen[n] = snap
                ta = time.monotonic()
                alt, settled = witness_state(text, snap, prev)
                if alt and witness_stale(alt, text, earlier):
                    alt, settled = None, False
                nxt[n] = time.monotonic() + max(PACKET_ALIGN_MIN_S, 4 * (time.monotonic() - ta))
                if alt and alt != slots[n]["partial"]:
                    slots[n].update(partial=alt, reach=witness_reaches_end(text, alt), since=time.monotonic())
                r = alt if settled else None
                if r:
                    slots[n]["text"] = r
                    slots[n]["ev"].set()
                    del todo[n]
                    try:
                        send(json.dumps({"packet_ear": n, "id": pid, "text": r}))
                    except Exception:
                        pass
            if todo:
                time.sleep(0.2)
        for n in todo:
            # Never moved on past the packet (the speaker paused): a stretch that
            # reaches the last word is still its reading; one that does not, nothing.
            if _slot_whole(slots[n]):
                slots[n]["text"] = slots[n]["partial"]
            slots[n]["ev"].set()
            try:
                send(json.dumps({"packet_ear": n, "id": pid, "text": slots[n]["text"]}))
            except Exception:
                pass
    threading.Thread(target=run, daemon=True, name=f"pkt-{pid}").start()


def _slot_whole(slot, alt=None):
    """Does the packet thread's stretch (or `alt`, if it is the same) reach the
    packet's last word and has it stood still for PACKET_STABLE_S?"""
    return (slot["reach"] and (alt is None or alt == slot["partial"])
            and time.monotonic() - slot["since"] >= PACKET_STABLE_S)


def _ear_clues(req, packets, names, wait_s=0.0):
    """{ear: its text of a caption's packets} -- the one rule for every path that
    translates (the Router's sent-on segments and the normal pipeline alike;
    Joseph 2026-10-08): every named ear's text goes -- the same as Soniox's or
    not, and for a packet whose end the ear has not reached, the stretch it has
    so far, marked "…". Only an ear with nothing lined up for any of the packets
    is left out. Reads what the packet threads (_start_packet_ears) have lined up.

    wait_s > 0 (the page asks, for the Router's sent-on segments): first wait,
    until each packet's seal + wait_s at most, for every ear to hear the packet
    out (its last word, and a word past it: witness_align.packet_state); an ear
    still short then is lined up once more here, on
    its newest text, so the reading is as fresh as the deadline allows."""
    tap = _tap_for(req)
    out = {}
    if tap is None:
        return out
    fresh = {}
    if wait_s > 0:
        for pid in packets:
            meta, slots = tap.packet_meta.get(pid), tap.packets.get(pid) or {}
            if not meta:
                continue
            for n in names:
                if n in slots:
                    slots[n]["ev"].wait(max(0.0, meta[0] + wait_s - time.monotonic()))
            for n in names:
                ear = tap.ears.get(n)
                if n in slots and not slots[n]["text"] and ear is not None:
                    alt, settled = witness_state(meta[1], ear.snapshot(), meta[2])
                    if alt and not witness_stale(alt, meta[1], meta[3]):
                        whole = settled or _slot_whole(slots[n], alt)
                        fresh[(pid, n)] = alt if whole else alt + " …"
    for n in names:
        parts = []
        for pid in packets:
            slot = (tap.packets.get(pid) or {}).get(n)
            if slot and slot["ev"].is_set() and slot["text"]:
                parts.append(slot["text"])
            elif (pid, n) in fresh:
                parts.append(fresh[(pid, n)])
            elif slot and slot["partial"]:
                parts.append(slot["partial"] + ("" if _slot_whole(slot) else " …"))
        alt = " ".join(parts).strip()
        if alt:
            out[n] = alt
    return out


_TAPS = {}                    # id -> _AudioTap, the newest few streams
_TAPS_LOCK = threading.Lock()
_TAP_SEQ = [0]


def _open_tap(sample_rate):
    with _TAPS_LOCK:
        _TAP_SEQ[0] += 1
        tap = _AudioTap(_TAP_SEQ[0], sample_rate)
        _TAPS[tap.id] = tap
        for old in sorted(_TAPS)[:-2]:
            del _TAPS[old]
    return tap


def _tap_for(req):
    try:
        return _TAPS.get(int(req["tap"]))
    except (KeyError, TypeError, ValueError):
        return None


def _hears_clip(req):
    """The caption's audio for a /translate body's {"audio": {"tap", "s", "e"}}."""
    try:
        tap = _TAPS.get(int(req["tap"]))
        s, e = float(req["s"]), float(req["e"])
    except (KeyError, TypeError, ValueError):
        return None
    if tap is None or e <= s:
        return None
    # The caption's own stretch only. A longer lead-in (20 s heard more in the
    # lab) is NOT offered: in the app, with little history, gemini-3.6-flash
    # translated the whole 20 s onto one caption (2026-10-07, test server).
    start = max(s, e - HEARS_MAX_MS) - HEARS_PAD_BEFORE_MS
    return tap.clip(max(0.0, start), e + HEARS_PAD_AFTER_MS)


def _first_within(gen, secs):
    """Run `gen` on a worker thread; return an iterator over its items if the
    first one arrives within `secs`, else None (the worker is told to stop and
    whatever it still yields is dropped). An exception before the first item is
    re-raised here; after it, it surfaces where the iterator is read."""
    q = queue.Queue()
    stop = threading.Event()
    END = object()

    def run():
        try:
            for item in gen:
                if stop.is_set():
                    break
                q.put(item)
        except BaseException as exc:            # noqa: BLE001 -- handed to the reader
            q.put(exc)
        finally:
            q.put(END)
            try:
                gen.close()
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    try:
        first = q.get(timeout=secs)
    except queue.Empty:
        stop.set()
        return None
    if isinstance(first, BaseException):
        raise first

    def rest():
        item = first
        while item is not END:
            if isinstance(item, BaseException):
                raise item
            yield item
            item = q.get()
    return rest()


def _provider_for(model):
    """The translate_stream for a model id (routing by name, as /translate does)."""
    if model in OPENAI_MODELS:
        return openai_translate_stream
    if is_xai_model(model):
        return xai_translate_stream
    if is_gemini_model(model):
        return gemini_translate_stream
    return claude_translate_stream


# Where a hearing caption goes when Gemini is slow or failing: a text-only
# translator on another provider, so one outage does not take both paths.
HEARS_FALLBACK = (os.environ.get("CT_HEARS_FALLBACK")
                  or ("gpt-6-sol-fast" if HAS_OPENAI_KEY else DEFAULT_MODEL))


@sock.route("/stt-stream")
def stt_stream(ws):
    """Live Soniox streaming: a persistent browser<->server<->Soniox bridge for one
    listening session. The browser streams raw PCM (s16le) CONTINUOUSLY; we proxy it
    to Soniox's realtime WebSocket and forward Soniox's token JSON straight back. No
    server-side VAD -- Soniox's own endpoint detection emits '<end>' tokens that mark
    phrase boundaries (the browser acts on those). This is the low-latency path: the
    text is transcribed DURING speech, so the caption is ready at the pause.

    Protocol (browser side):
      1. first text frame: {"hints": [<code>, ...], "sample_rate": N, "endpoint_ms": M,
                            "translate_to": <code>|"auto"|null,
                            "ep_adjust": 0-3, "ep_sens": -1.0..1.0}
         ("source": <code>|"auto" is still accepted as the older single-hint form.)
      2. then binary frames = PCM audio; text frames = control {"type":"finalize"|"stop"}
    We forward token messages as-is and tally processed audio into the credit meter."""
    import json
    if not HAS_SONIOX_KEY:
        try:
            ws.send(json.dumps({"error": "Soniox key not configured"}))
        except Exception:
            pass
        return
    gen = _stream_opened()     # a page is (back) on the audio stream: a pending page-gone save is off
    stopped = False            # True when the page said "stop" (the operator's Stop), not just went away
    # 1) handshake
    try:
        hello = json.loads(ws.receive(timeout=10) or "{}")
    except Exception:
        return
    hints = parse_hints(hello.get("hints") or hello.get("source"))
    sample_rate = int(hello.get("sample_rate") or 16000)
    endpoint_ms = int(hello.get("endpoint_ms") or 0)
    ep_adjust, ep_sens = _endpoint_tuning(hello)
    # Soniox's own translation, asked for only under the model "none, fast", where
    # it IS the caption. Its tokens come back on the SAME stream tagged
    # translation_status="translation" (originals stay "original").
    translate_to = (hello.get("translate_to") or "").lower()
    config = {
        "api_key": os.environ["SONIOX_API_KEY"],
        "model": SONIOX_RT_MODEL,
        "audio_format": "pcm_s16le",
        "sample_rate": sample_rate,
        "num_channels": 1,
        "enable_endpoint_detection": True,   # '<end>' tokens mark phrase boundaries
    }
    config.update(_soniox_lang_config(hints))
    if endpoint_ms:                          # the page's SX_ENDPOINT_MS (3 s) -> endpoint delay
        config["max_endpoint_delay_ms"] = max(500, min(3000, endpoint_ms))
    # The two detector knobs, off by default so the stream is byte-identical to
    # what it was before they existed -- which is what makes an A/B honest.
    if ep_adjust:
        config["endpoint_latency_adjustment_level"] = ep_adjust
    if ep_sens:
        config["endpoint_sensitivity"] = ep_sens
    # With a PAIR, Soniox's own two_way mode translates in whichever direction it
    # hears, which is why a direction flip never needs the socket re-handshaking
    # the way the one-way target does -- the config is already symmetric.
    if translate_to and len(hints) == 2:
        config["translation"] = {"type": "two_way",
                                 "language_a": hints[0], "language_b": hints[1]}
    elif (translate_to and translate_to != "auto" and [translate_to] != hints
          and translate_to in languages.HEARD):   # a caption-only language: Soniox can't write it
        config["translation"] = {"type": "one_way", "target_language": translate_to}

    from websockets.sync.client import connect
    try:
        sx = connect(SONIOX_WS_URL, open_timeout=15)
    except Exception as exc:
        try:
            ws.send(json.dumps({"error": f"Soniox connect failed: {str(exc)[:120]}"}))
        except Exception:
            pass
        return
    sx.send(json.dumps(config))
    tap = _open_tap(sample_rate)
    send_lock = threading.Lock()     # the Soniox reader and the second ear both write

    def to_browser(msg):
        with send_lock:
            ws.send(msg)

    try:
        to_browser(json.dumps({"tap": tap.id}))
    except Exception:
        pass
    wanted = (hello.get("ears") or ([hello["witness"]] if hello.get("witness") else [])) if EARS_ENABLED else []
    # The ears' language. One hint names it. Otherwise (any -> target, or a
    # pair) it is latched from Soniox's own per-word language: an ear left to
    # guess (ElevenLabs) heard Romanian as Cyrillic. Starts from the last
    # stream's, so a restart does not begin by guessing.
    ear_lang = hints[0] if len(hints) == 1 else _EAR_LANG[0]
    # No relay: the page shows each ear's reading of a packet once it is final
    # (packet_ear, from _start_packet_ears), never the ear's running text.
    tap.ears = extra_ears.open_ears(wanted, sample_rate, ear_lang, None)
    ear_latch = _ear_lang_latch(tap, ear_lang) if tap.ears and len(hints) != 1 else None
    xl = config.get("translation") or {}
    xl_note = ""
    if xl.get("type") == "two_way":
        xl_note = f", translating {xl['language_a']}<->{xl['language_b']}"
    elif xl:
        xl_note = f", translating ->{xl.get('target_language')}"
    # Name the endpoint tuning in the log only when it is NOT the default, so a
    # tuned Sunday is distinguishable from an untuned one in the console history
    # -- the whole point of the knobs is comparing one service against another.
    ep_note = ""
    if ep_adjust:
        ep_note += f", ep_adjust={ep_adjust}"
    if ep_sens:
        ep_note += f", ep_sens={ep_sens:+.2f}"
    print(f"[stream] Soniox RT open [hints={hints or 'auto'}, {sample_rate}Hz"
          + xl_note + ep_note + "]", flush=True)

    # The operator just hit Start, so the first spoken sentence is seconds away.
    # Open the TTS connection now, off the critical path, rather than making
    # that sentence wait through a DNS+TCP+TLS handshake it could have skipped.
    if TTS_CAPABLE:
        threading.Thread(target=_prewarm_tts, daemon=True).start()

    stop = threading.Event()

    def soniox_to_browser():
        """Reader thread: Soniox token JSON -> browser, plus the credit meter."""
        last_proc = 0.0
        try:
            while not stop.is_set():
                try:
                    msg = sx.recv(timeout=1)
                except TimeoutError:
                    continue
                try:
                    j = json.loads(msg)
                    if ear_latch is not None:
                        ear_latch(j.get("tokens") or ())
                    proc = float(j.get("total_audio_proc_ms") or 0)
                    if proc > last_proc:
                        _bump_usage(stt_secs=(proc - last_proc) / 1000.0)
                        costs.stt_seconds((proc - last_proc) / 1000.0)
                        last_proc = proc
                    xl = sum(1 for t in j.get("tokens") or ()
                             if t.get("is_final") and t.get("translation_status") == "translation")
                    if xl:
                        costs.soniox_text_tokens(xl)
                    done = bool(j.get("finished")) or bool(j.get("error_code"))
                except Exception:
                    done = False
                to_browser(msg)
                if done:
                    break
        except Exception:
            pass
        finally:
            stop.set()
            try:
                ws.close()
            except Exception:
                pass

    reader = threading.Thread(target=soniox_to_browser, daemon=True)
    reader.start()

    # main loop: browser -> Soniox (audio + control)
    try:
        while not stop.is_set():
            data = ws.receive(timeout=1)
            if data is None:
                continue                                  # idle tick
            if isinstance(data, (bytes, bytearray)):
                sx.send(data)                             # PCM audio
                tap.feed(data)                            # for a translator that hears
                for ear in tap.ears.values():
                    ear.feed(data)                        # the extra ears (scripts/ears.py)
            else:
                try:
                    typ = (json.loads(data) or {}).get("type")
                except Exception:
                    continue
                if typ == "packet":
                    try:
                        m = json.loads(data)
                        _start_packet_ears(tap, int(m["id"]), str(m.get("text") or "")[:600],
                                           str(m.get("prev") or "")[:600] or None, to_browser)
                    except Exception as exc:              # noqa: BLE001
                        print(f"[ear ] packet request: {str(exc)[:120]}", flush=True)
                elif typ == "finalize":
                    sx.send(json.dumps({"type": "finalize"}))
                elif typ == "stop":
                    stopped = True
                    break
    except Exception:
        pass
    finally:
        stop.set()
        if not stopped:            # the page closed, crashed or slept: save if it doesn't come back
            _page_gone_later(gen)
        try:
            sx.send("")        # empty frame = end-of-stream (Soniox flushes + finishes)
        except Exception:
            pass
        try:
            sx.close()
        except Exception:
            pass
        for ear in tap.ears.values():
            ear.close()
        print("[stream] Soniox RT closed", flush=True)


# Mirrors the client's splitAtLastTerminal (app/index.html) -- used to pull
# COMPLETE sentences off Claude's growing streamed output so each one can be
# handed to TTS as soon as it's ready, instead of waiting for the whole batch.
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'’”)\]]?(?=\s|$)")


def _split_complete_sentences(text):
    last = -1
    for m in _SENTENCE_END_RE.finditer(text):
        last = m.end()
    if last == -1:
        return "", text
    return text[:last], text[last:]


def _tts_opts(data, target_lang):
    """Everything the TTS stage needs for this request, in one bundle: which
    backend, that backend's voice/model, and its own knobs. `language` is the
    target language the operator picked, as the voices name it -- ElevenLabs
    infers it from the text and ignores this, but Soniox requires it and errors
    on a bad code."""
    return {
        "provider": _tts_provider(data),
        "language": languages.voice_code(target_lang),
        "el_settings": _el_voice_settings(data),
        "el_voice": (data.get("el_voice") or "").strip() or None,
        "el_model": (data.get("el_model") or "").strip() or None,
        "sx_settings": _sx_voice_settings(data),
        "sx_voice": (data.get("sx_voice") or "").strip() or None,
        "sx_model": (data.get("sx_model") or "").strip() or None,
    }


def _voice_can(opts, target_lang):
    """Can the voice in `opts` say this caption language? A caption language
    the chosen voice model does not know is shown and left unspoken -- the page
    checks first, this is the server holding the same line."""
    p = opts.get("provider") or TTS_PROVIDER
    model = (opts.get("el_model") or EL_TTS_MODEL) if p == "elevenlabs" else None
    return languages.can_speak(p, model, target_lang)


def _tts_chunk_lines(text, opts, meta=None):
    """One sentence -> a run of {"type":"audio_chunk"} NDJSON lines, yielded as
    the provider produces the audio rather than after the clip is complete.

    Chunks of one sentence are contiguous and in order; `last` marks the final
    one so a client that needs whole clips (Replay's cache, and the non-MSE
    playback fallback) can reassemble them. The semaphore is held for the whole
    stream -- it bounds concurrent generations, and a Soniox generation lasts as
    long as the audio does.

    The seq-0 line also carries this clip's latency numbers, so the browser can
    average them alongside the stages it times for itself:
      ttfb_ms   request sent -> first audio byte (the provider's own answer time)
      queue_ms  how long this sentence waited for a TTS_SEM slot
    Split apart because they are different problems with different fixes: a slow
    ttfb_ms is a provider question, while a queue_ms that is never 0 means
    CT_TTS_MAX_INFLIGHT is too low for how fast the speaker is going. From the
    browser the two are one undivided wait, which is why they are measured here.

    The seq-0 line also carries `text`: the exact sentence these bytes speak. It
    costs nothing (the client already has the caption) and it is what lets a
    saved recording line each clip up with its words -- the caption is a whole
    batch, while a clip is one sentence of it.

    `meta` is the recorder's half: the source line and target language this
    sentence came from. The clip is buffered as it streams, so saving a service
    afterwards needs no second synthesis and no second copy of the traffic."""
    t0 = time.monotonic()
    kept = [] if RECORDING.enabled else None
    spoke_at = time.time()
    entry = None
    if (opts.get("provider") or TTS_PROVIDER) == "elevenlabs" and not opts.get("no_context"):
        ctx, entry = VOICE_CONTEXT.begin(
            (opts.get("el_voice") or EL_VOICE, opts.get("el_model") or EL_TTS_MODEL,
             opts.get("language")), text)
        opts = {**opts, "el_context": ctx}
    opts = {k: v for k, v in opts.items() if k != "no_context"}
    finished = False
    try:
        with TTS_SEM:
            queue_ms = round((time.monotonic() - t0) * 1000)
            seq = 0
            pending = None
            stats = {}
            metrics = None      # attached to the seq-0 line, whichever one that turns out to be
            for chunk in synthesize_stream(text, stats=stats, **opts):
                if metrics is None:
                    metrics = {"ttfb_ms": stats.get("ttfb_ms", 0), "queue_ms": queue_ms}
                    # first_bytes is logged and not sent: it answers a question about
                    # OUR buffering (a first chunk of exactly chunk_bytes means the
                    # wait was requests filling a buffer, not the provider being
                    # slow), which is a developer's question, not an operator's.
                    print(f"[tts] first byte {metrics['ttfb_ms']}ms"
                          f" (open {stats.get('open_ms', 0)}ms,"
                          f" {stats.get('first_bytes', 0)}B"
                          + (f", queued {queue_ms}ms" if queue_ms >= 5 else "")
                          + (f", context {len(opts['el_context'].get('previous_request_ids', []))} clip(s)"
                             f"/{len(opts['el_context'].get('previous_text', ''))}c"
                             if opts.get("el_context") else "") + ")"
                          f" [{opts.get('provider') or TTS_PROVIDER}] {text[:48]!r}", flush=True)
                # Hold one chunk back so the final line can carry last=true without
                # needing a separate terminator.
                if pending is not None:
                    line = {"type": "audio_chunk", "seq": seq,
                            "data": base64.b64encode(pending).decode("ascii")}
                    if seq == 0:
                        line.update(metrics)
                        line["text"] = text
                    if kept is not None:
                        kept.append(pending)
                    yield json.dumps(line) + "\n"
                    seq += 1
                pending = chunk
            if pending is not None:
                line = {"type": "audio_chunk", "seq": seq, "last": True,
                        "data": base64.b64encode(pending).decode("ascii")}
                if seq == 0:
                    line["text"] = text
                    if metrics:
                        line.update(metrics)
                if kept is not None:
                    kept.append(pending)
                yield json.dumps(line) + "\n"
            finished = True
            if entry is not None:
                VOICE_CONTEXT.done(entry, stats.get("request_id"))
    finally:
        if entry is not None and not finished:
            VOICE_CONTEXT.failed(entry)
        # In a `finally` so a stream that dies part way through still banks the
        # seconds the room actually heard: those bytes were played, and a
        # recording that silently omits them would put every later cue out by
        # their length.
        if kept:
            meta = meta or {}
            RECORDING.add(b"".join(kept), text=text, src=meta.get("src", ""),
                          lang=meta.get("lang", ""),
                          provider=opts.get("provider") or TTS_PROVIDER,
                          spoke_at=spoke_at)
    # A voice tag goes to the provider as "[tag] " in front of the text and is
    # billed like the rest of it.
    tag = opts.get("el_tag")
    _bump_usage(tts_chars=len(text) + (len(tag) + 3 if tag else 0))
    costs.tts(opts.get("provider"), len(text) + (len(tag) + 3 if tag else 0),
              opts.get("el_model") or EL_TTS_MODEL)


VOICE_STREAM = voice_stream.VoiceStream()


def _stream_wanted(data, opts):
    """How far behind the captions the page's voice is (seconds) when this
    request should speak through the one continuous voice connection
    (voice_stream), else None. CT_EL_STREAM turns it on; the page sends
    `voice_lag` from its audio queue."""
    if not EL_STREAM:
        return None
    model = opts.get("el_model") or EL_TTS_MODEL
    if not voice_stream.can_stream(opts.get("provider") or TTS_PROVIDER, model):
        return None
    try:
        return max(0.0, float(data.get("voice_lag") or 0))
    except (TypeError, ValueError):
        return 0.0


def _stream_push(text, opts, lag):
    """Hand `text` to the continuous voice now; its audio is read later with
    _stream_tts_lines. Pushing is separate from reading so the next sentence
    reaches the voice while this one is still being spoken."""
    model = opts.get("el_model") or EL_TTS_MODEL
    tag = opts.get("el_tag") if el_takes_tags(model) else None
    said = (f"[{tag}] " if tag else "") + text.strip() + " "
    return VOICE_STREAM.push(said, opts.get("el_voice") or EL_VOICE, model,
                             opts.get("language"), lag)


def _stream_tts_lines(text, opts, meta=None, lag=0.0, clip=None):
    """_tts_chunk_lines for the continuous voice: the text goes into the
    service's open connection and the audio cut out for it comes back as the
    same audio_chunk lines, so the page plays both the same way. No TTS_SEM
    and no VoiceContext: one connection carries its own context. `clip` is the
    text already pushed by _stream_push."""
    model = opts.get("el_model") or EL_TTS_MODEL
    kept = [] if RECORDING.enabled else None
    spoke_at = time.time()
    if clip is None:
        clip = _stream_push(text, opts, lag)
    said = clip.text
    seq, pending = 0, None
    try:
        for chunk in clip:
            if pending is not None:
                line = {"type": "audio_chunk", "seq": seq,
                        "data": base64.b64encode(pending).decode("ascii")}
                if seq == 0:
                    line.update({"ttfb_ms": clip.ttfb_ms, "queue_ms": 0, "text": text})
                    print(f"[tts] stream first byte {clip.ttfb_ms}ms (lag {lag:.1f}s)"
                          f" [elevenlabs {model}] {text[:48]!r}", flush=True)
                if kept is not None:
                    kept.append(pending)
                yield json.dumps(line) + "\n"
                seq += 1
            pending = chunk
        line = {"type": "audio_chunk", "seq": seq, "last": True,
                "data": base64.b64encode(pending or b"").decode("ascii")}
        if seq == 0:
            line.update({"ttfb_ms": clip.ttfb_ms, "queue_ms": 0, "text": text})
        if kept is not None and pending:
            kept.append(pending)
        yield json.dumps(line) + "\n"
    finally:
        clip.abandoned = True   # a page that stopped reading: drop the rest unheard
        if kept:
            meta = meta or {}
            RECORDING.add(b"".join(kept), text=text, src=meta.get("src", ""),
                          lang=meta.get("lang", ""), provider="elevenlabs",
                          spoke_at=spoke_at)
        _bump_usage(tts_chars=len(said))
        costs.tts("elevenlabs", len(said), model)


def _safe_tts_chunk(text, opts, meta=None, stream_lag=None, clip=None):
    """Like _tts_chunk_lines, but never raises -- the caption may have already
    streamed successfully by the time a sentence's TTS call happens, and a
    synthesis failure (quota, bad voice id, network blip) shouldn't cost that.
    Yields audio_chunk lines on success, or a stage-tagged error line on failure
    so the client can tell a TTS problem apart from a translate one.

    A failure PART WAY through a stream still ends the sentence with a `last`
    marker before the error, so a client reassembling chunks is never left
    holding an unterminated clip."""
    started = False
    try:
        lines = (_tts_chunk_lines(text, opts, meta) if stream_lag is None
                 else _stream_tts_lines(text, opts, meta, stream_lag, clip))
        for line in lines:
            started = True
            yield line
    except (RuntimeError, OSError) as exc:
        print(f"[tts] ERROR: {exc}", flush=True)
        if started:
            yield json.dumps({"type": "audio_chunk", "last": True, "data": ""}) + "\n"
        yield json.dumps({"type": "error", "stage": "tts", "error": str(exc)}) + "\n"


@app.route("/translate", methods=["POST"])
def translate_ep():
    """Punctuated source text -> translation (Claude) into target_lang, optional TTS.

    Streams a newline-delimited JSON response (one object per line) so the
    caption can fill in as Claude generates it instead of appearing all at
    once, and (when TTS is on) each completed sentence can be spoken as soon
    as it's ready instead of waiting for the whole batch to finish translating:
      {"type":"delta","text":...}        -- caption text to APPEND, in order
      {"type":"audio_chunk","data":...,"seq":N[,"last":true]}
                                         -- a slice of encoded audio (/caps
                                            audio_mime) to append to playback.
                                            Slices of one sentence are
                                            contiguous and ordered; "last"
                                            closes that sentence. Soniox emits
                                            these WHILE generating, so speech
                                            starts a third of a second in
                                            rather than after the whole clip.
                                            The seq-0 line also carries
                                            ttfb_ms/queue_ms for that clip (see
                                            _tts_chunk_lines)
      {"type":"final","text":...,"seconds":...}  -- authoritative full caption,
                                         sent as soon as the model is done and
                                         BEFORE this batch's audio. Everything
                                         after it is audio_chunk (or a tts
                                         error); the stream ends when the last
                                         clip has gone. Clients may commit the
                                         caption here and stop waiting. Carries
                                         "replayed":N when the reply opened by
                                         restating N words of earlier captions,
                                         which were dropped (ReplayGuard), and
                                         "clue_cut":N when it copied the
                                         Router's clue, whose N words were
                                         dropped (translate_claude.clue_at)
      {"type":"error","stage":"translate","error":...}  -- Claude call failed;
                                             nothing more follows (no caption
                                             text exists to send)
      {"type":"error","stage":"tts","error":...}  -- one clip failed to
                                             synthesize; the stream continues
                                             (that sentence just goes unspoken)
                                             and the stream continues; note this
                                             arrives AFTER "final", so the
                                             caption is already committed and a
                                             tts error must never be treated as
                                             a failed translation

    `target_lang` (a code from TARGET_LANGUAGES, default DEFAULT_TARGET_LANG)
    picks the language to translate INTO -- the source language is whatever
    Claude infers from the text itself. `pair` (optional, two codes) says the
    session is bilingual with automatic direction: it pools the rolling history
    across both directions and selects the pair-aware system prompt, but the
    direction of THIS call is target_lang's alone -- the browser decided it. TTS
    uses one ElevenLabs voice, any target language.

    The request carries nothing about the state of playback. Translation is
    one-way: the model's text goes to the caption, the history and TTS, and
    what the browser then does with the audio (speed it up, skip ahead) is its
    own business. A "condense" flag used to let audio lag reach back into this
    prompt and ask for a shortened spoken variant; that coupling is gone.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    target_lang = (data.get("target_lang") or DEFAULT_TARGET_LANG).lower()
    if target_lang not in TARGET_LANGUAGES:
        target_lang = DEFAULT_TARGET_LANG
    # `pair` marks a bilingual session and scopes the history + system prompt.
    # It never decides direction -- target_lang alone does that, already chosen by
    # the browser's direction engine -- so a pair whose target isn't a member is
    # incoherent and is dropped rather than second-guessed.
    pair = parse_hints(data.get("pair"))
    pair = tuple(pair) if len(pair) == 2 and target_lang in pair else None
    model = data.get("model") or DEFAULT_MODEL
    # What the translator is given beside Soniox's text, each its own switch:
    #   hear     -- the caption's own audio (models that take audio: Gemini)
    #   readings -- the extra ears' readings of the caption, as text
    # A saved "<model>+hears" (the first build of this) means hear=True.
    hear = bool(data.get("hear"))
    if hears_base(model):
        model, hear = hears_base(model), True
    readings_on = bool(data.get("readings")) and EARS_ENABLED
    # Grok ids come from the LIVE catalogue (/caps), so they cannot be checked
    # against a hardcoded tuple -- is_xai_model covers the whole namespace.
    # Gemini is the opposite case and checked the strict way: its options are a
    # curated, static tuple (the catalogue is 58 entries of mostly image/video
    # models -- see translate_gemini.GEMINI_MODELS), so an id outside it was
    # never offered and falls back rather than being passed upstream.
    if not (model in ALLOWED_MODELS or model in OPENAI_MODELS
            or is_xai_model(model) or model in GEMINI_MODELS):
        model = DEFAULT_MODEL
    # A reasoning model spends its thinking BEFORE the first token, so the whole
    # cost lands on time-to-first-word with the room watching a blank screen.
    # They are no longer offered; refuse one that still arrives (a setting saved
    # before they were dropped) rather than serve seconds of caption lag.
    if is_reasoning_model(model):
        print(f"[mt ] refusing reasoning model {model!r} -> {DEFAULT_MODEL}", flush=True)
        model = DEFAULT_MODEL
    if model in OPENAI_MODELS and not HAS_OPENAI_KEY:
        model = DEFAULT_MODEL   # GPT picked but no key configured -> Claude default
    if is_xai_model(model) and not HAS_XAI_KEY:
        model = DEFAULT_MODEL   # Grok picked but no key configured -> Claude default
    if is_gemini_model(model) and not HAS_GEMINI_KEY:
        model = DEFAULT_MODEL   # Gemini picked but no key configured -> Claude default
    speak = bool(data.get("tts")) and TTS_CAPABLE
    # A wording anchor for the model (translate_claude.anchored_turn). The app no
    # longer sends one -- the Live draft it came from was removed 2026-10-01 --
    # but research scripts still may. Absent (or empty) it changes nothing.
    # Bounded: a runaway anchor must not inflate the prompt.
    draft = (data.get("draft") or "").strip()[:600]
    # The Router's clue: Soniox's translation of a segment its checker doubted,
    # and why (translate_claude.FLAGGED_RULE). Rides in the draft slot.
    fl = data.get("flagged")
    if isinstance(fl, dict) and (fl.get("en") or "").strip():
        draft = {"en": str(fl["en"]).strip()[:600], "why": str(fl.get("why") or "")}
    # Soniox's per-word confidence, one entry per word of `text`. Only the model
    # sees the brackets it produces; history, replay guard and recorder keep `text`.
    conf = data.get("conf")
    # Not for models the hint misleads: Haiku 5.5 left flagged words untranslated
    # in the English ("obtain mânuirea", "come with mulțime") and fell from 12 to
    # 9 of 22 planted mis-hears with it (validation/results/2026-10-07-haiku-5-5/).
    model_text = (unsure_turn(text, conf) if isinstance(conf, list) and model not in NO_UNSURE_HINT
                  else text)
    tts_opts = _tts_opts(data, target_lang)
    speak = speak and _voice_can(tts_opts, target_lang)
    stream_lag = _stream_wanted(data, tts_opts) if speak else None
    # Expression (CT_EL_EXPRESSION): the translator may lead its reply with a delivery tag
    # for the voice (translate_claude.VOICE_TAG_NOTE). Asked for only when the
    # reply will be spoken by a model that reads tags -- otherwise the prompt is
    # exactly what it was and there is nothing to take off.
    expressive = (speak and EL_EXPRESSION
                  and tts_opts["provider"] == "elevenlabs"
                  and el_takes_tags(tts_opts["el_model"]))
    # What the recorder files each clip under: the line that was spoken came
    # from THIS source text, into THIS language. One sentence of the batch is
    # one clip, so the source is the batch's -- the honest attribution, and the
    # one that lets a saved transcript sit source beside translation.
    rec_meta = {"src": text, "lang": target_lang}

    if not text:
        def _empty():
            yield json.dumps({"type": "final", "text": "", "seconds": 0}) + "\n"
        return Response(_empty(), mimetype="application/x-ndjson")

    hist = HISTORY["-".join(sorted(pair)) if pair else target_lang]
    now = time.monotonic()
    with HISTORY_LOCK:
        while hist and now - hist[0][0] > HISTORY_MAX_AGE_S:
            hist.popleft()
        # (src, tgt, that turn's own target) -- in a pooled bilingual history the
        # turns run both ways, and each must be replayed under the tag it was
        # actually translated with. See translate_claude.history_turns.
        history = [(src, tgt, lang) for _, src, tgt, lang in hist]
    # What this reply may not simply restate: the captions already given in this
    # direction. Its head is judged against them before it is shown, spoken or
    # stored -- see translate_claude.ReplayGuard.
    same_way = [(src, tgt) for src, tgt, lang in history if lang == target_lang]
    replay = ReplayGuard([tgt for _, tgt in same_way], text, [src for src, _ in same_way])
    # A reply that copies the Router's clue (FLAGGED_RULE, in this call's user
    # turn) is cut where the clue begins -- see translate_claude.clue_at.
    clued = isinstance(draft, dict)
    # The TTS worker's in-queue and its stop flag, reachable from guarded() below.
    tts_ctl = {"in": None, "cancel": threading.Event()}

    def generate():
        t0 = time.time()
        full = ""
        body = ""          # `full` short of the Router's clue, if the model copied it
                           # in: all of the reply that is ever judged, shown or kept
        skip = None if replay.armed else 0   # leading chars of `body` that replay earlier
                                             # captions; None = not judged yet
        shown = 0          # chars of body[skip:] already emitted as a caption delta
        fed = 0            # chars of body[skip:] already handed to TTS
        tts_pending = ""   # accumulated NOT-yet-sent-to-TTS tail
        audio_started = False
        suppressed = False # the reply turned into a refusal/meta note -- stop
                           # revealing and speaking it mid-stream (see below)
        # Takes the voice tag off before the text reaches `full`, so the caption,
        # the history and everything downstream of them never see one.
        tags = VoiceTagFilter() if expressive else None

        # Per-sentence TTS runs on a background worker so an ElevenLabs call
        # (0.5-1s each) never stalls the caption deltas. Sentences go in FIFO
        # and ready NDJSON lines (audio_chunk, or a stage-tagged tts error --
        # _safe_tts_chunk never raises) come out FIFO, so clips keep sentence
        # order. TTS_SEM inside _safe_tts_chunk still bounds how many clips are
        # in flight at the provider server-wide. The client already accepts delta /
        # audio_chunk / error lines in any interleaving; only "final" must be
        # (and stays) last.
        tts_in = tts_out = None
        if speak:
            tts_in, tts_out = queue.Queue(), queue.Queue()
            tts_ctl["in"] = tts_in
            cancel = tts_ctl["cancel"]

            if stream_lag is not None:
                # The continuous voice: every sentence goes into the open
                # connection the moment it is complete; a reader takes the audio
                # out in order. The other path's single worker would wait out each
                # sentence's audio before handing over the next.
                pushed = queue.Queue()

                def _tts_feeder():
                    while True:
                        sentence = tts_in.get()
                        if sentence is None or cancel.is_set():
                            pushed.put(None)
                            return
                        try:
                            pushed.put((sentence, _stream_push(sentence, tts_opts, stream_lag)))
                        except (RuntimeError, OSError) as exc:
                            pushed.put((sentence, exc))

                def _tts_worker():
                    while True:
                        item = pushed.get()
                        if item is None:
                            tts_out.put(None)
                            return
                        sentence, clip = item
                        if isinstance(clip, Exception):
                            print(f"[tts] ERROR: {clip}", flush=True)
                            tts_out.put(json.dumps({"type": "error", "stage": "tts",
                                                    "error": str(clip)}) + "\n")
                            continue
                        lines = _safe_tts_chunk(sentence, tts_opts, rec_meta, stream_lag, clip)
                        for line in lines:
                            if cancel.is_set():
                                lines.close()
                                break
                            tts_out.put(line)

                threading.Thread(target=_tts_feeder, daemon=True).start()

            def _tts_worker_clips():
                while True:
                    sentence = tts_in.get()
                    if sentence is None:                # shutdown sentinel
                        tts_out.put(None)
                        return
                    if cancel.is_set():                 # the page gave up: speak nothing more
                        continue
                    # One sentence now produces MANY lines, pushed as the audio
                    # arrives. One worker means sentences never overlap, so a
                    # sentence's chunks stay contiguous and in order.
                    lines = _safe_tts_chunk(sentence, tts_opts, rec_meta, stream_lag)
                    for line in lines:
                        if cancel.is_set():
                            lines.close()
                            break
                        tts_out.put(line)

            threading.Thread(target=_tts_worker if stream_lag is not None
                             else _tts_worker_clips, daemon=True).start()

        # Hands on whatever of the reply is new since the last call -- to the
        # caption as a delta, to TTS as whole sentences -- from `skip` on. The part
        # before `skip` restates captions already given and goes to neither.
        def _reveal():
            nonlocal shown, fed, tts_pending, audio_started
            visible = body[skip:]
            lines = []
            if len(visible) > shown:
                lines.append(json.dumps({"type": "delta", "text": visible[shown:]}) + "\n")
                shown = len(visible)
            if tts_in is not None and len(visible) > fed:
                tts_pending += visible[fed:]
                fed = len(visible)
                complete, tts_pending = _split_complete_sentences(tts_pending)
                if complete.strip():
                    audio_started = True
                    tts_in.put(complete)
            return lines

        # Same streaming contract for every provider (yields text chunks, raises
        # RuntimeError on API failure) -- provider is decided by model name.
        translate_fn = _provider_for(model)
        used_model = model          # what actually answered, for the latency books
        heard_cache = []            # the hearing call's cache counters (another thread's)
        audio_req = data.get("audio") or {}
        # Hearing: the caption's own audio rides beside Soniox's text, for a model
        # that takes audio. No clip (another STT path, a reconnect, a stretch
        # already scrolled out) means the same model on the text alone.
        hearing = hear and is_gemini_model(model)
        clip = _hears_clip(audio_req) if hearing else None
        if hearing and clip is None:
            print("[mt ] hears: no audio for this caption -> text only", flush=True)
        ear_names = set(data.get("ears") or extra_ears.EARS)
        packet_ids = [p for p in (data.get("packets") or []) if isinstance(p, int)]
        # the ears' texts of exactly this caption's packets (_ear_clues: one rule)
        try:
            ear_wait = max(0, min(1500, int(data.get("ear_wait_ms") or 0))) / 1000
        except (TypeError, ValueError):
            ear_wait = 0.0
        t_ears = time.monotonic()
        readings = _ear_clues(audio_req, packet_ids, ear_names, ear_wait) if readings_on and packet_ids else {}
        if ear_wait and readings_on and packet_ids:
            print(f"[mt ] ears: waited {time.monotonic() - t_ears:.2f}s (cap seal +{ear_wait:g}s)", flush=True)
        # The INPUT half of the [mt] pair: what the model is actually being asked to
        # translate this call. Printed BEFORE the call so it lands even if the call
        # is slow or fails, and right above the "[mt] Ns … -> out" line, so a giant
        # output can be read against the size of the input that produced it. A tiny
        # input beside a giant output would point at the model; a giant input beside
        # it points upstream (STT/segmentation). Full source only when asked.
        _dr = (f", flagged {draft['why']}" if isinstance(draft, dict)
               else f", draft {len(draft)}c" if draft else "")
        _un = model_text.split("\n\n")[0].count("⟦")
        print(f"[mt ] IN [->{target_lang}] {len(text)} chars, {len(text.split())} words, "
              f"ctx {len(history)}{_dr}" + (f", unsure {_un}" if _un else "")
              + (f": {model_text.split(chr(10))[0]!r}" if CT_LOG_MT_INPUT else ""), flush=True)
        # The extra ears' readings, as the lab sent them: a numbered list after the
        # text (and before the audio note). Measured with a translator that hears
        # (43 of 47 mis-hears, no right word lost); a text-only translator gains
        # 2-3 and loses 1-3 right words. Sent whenever an ear is ticked, to any
        # translator (Joseph, 2026-10-08: the clue always goes when it is there).
        ears_note = ("\n\n" + WIT_NOTE.format(alts="\n".join(
            f"{k + 1}. «{r}»" for k, r in enumerate(readings.values())))) if readings else ""
        for n, r in readings.items():
            print(f"[mt ] ear: {extra_ears.EARS[n].label} heard {r!r}", flush=True)
        if readings_on and not readings:
            print("[mt ] ears: no reading in time", flush=True)

        def _call(fn, txt, m, **extra):
            return fn(txt, history=history, target_lang=target_lang, model=m,
                      draft=draft, pair=pair, expressive=expressive, **extra)
        try:
            stream = None
            if clip is not None:
                # One trip: the model hears the stretch and reads Soniox's text.
                # Its first word must come within HEARS_FIRST_S, or the caption
                # goes to HEARS_FALLBACK on the text alone -- an overloaded Gemini
                # (2026-10-07: 15-28 s calls for an hour) must not hold the room.
                def _heard():
                    # Runs on _first_within's worker: Gemini's cache counters are
                    # thread-local, so they are read here and carried back.
                    yield from _call(gemini_translate_stream, model_text + ears_note + "\n\n" + HEARS_NOTE,
                                     model, audio=clip)
                    heard_cache.append(gemini_cache_stats())
                try:
                    stream = _first_within(_heard(), HEARS_FIRST_S)
                    why = f"no first word in {HEARS_FIRST_S:g}s"
                except Exception as exc:                     # noqa: BLE001 -- any failure falls back
                    why = str(exc)[:120]
                if stream is None:
                    print(f"[mt ] hears: {why} -> {HEARS_FALLBACK} on the text", flush=True)
                    translate_fn = _provider_for(HEARS_FALLBACK)
                    used_model = HEARS_FALLBACK
                    stream = _call(translate_fn, model_text, HEARS_FALLBACK)
                else:
                    print(f"[mt ] hears: {(len(clip) - 44) / 32000:.1f}s of audio", flush=True)
            else:
                stream = _call(translate_fn, model_text + ears_note, model)
            for chunk in stream:
                chunk = drop_unsure_marks(chunk)
                full += tags.feed(chunk) if tags else chunk
                # The Router's clue, copied in, is never caption text: `body` stops
                # where it begins, and a tail that may be its first words waits
                # for the next chunk. The rest of the stream is still read to its
                # end, for the cache and cost books.
                body = full[:clue_at(full, final=False)] if clued else full
                if tags and tags.tag:
                    # Known before any of the reply's text is, so before its
                    # first sentence can reach the TTS worker that reads it.
                    tts_opts["el_tag"] = tags.tag
                # A refusal / meta-reply must never be shown or spoken. Once the
                # partial text trips the meta detector, stop here: emit no more
                # deltas and feed no more sentences to TTS. claude_strip_meta
                # blanks the final caption below, and that empty "final" replaces
                # whatever few words already streamed. Detection is early
                # (refusals lead with the marker) so TTS usually never starts.
                if not suppressed and claude_looks_like_meta(body):
                    suppressed = True
                    tts_pending = ""            # never speak the buffered tail
                if suppressed:
                    continue
                # Nothing is shown or spoken until the head is judged: a reply that
                # opens by restating earlier captions must reach neither the screen
                # nor the voice. An ordinary reply is judged at its third word.
                if skip is None:
                    skip = replay.cut(body, final=False)
                    if skip is None:
                        continue
                yield from _reveal()
                if tts_out is not None:
                    while True:                         # ship finished clips, never wait
                        try:
                            line = tts_out.get_nowait()
                        except queue.Empty:
                            break
                        yield line
        except RuntimeError as exc:
            print(f"[mt ] ERROR: {exc}", flush=True)
            if tts_in is not None:
                tts_in.put(None)   # let the worker exit; pending clips are moot
            yield json.dumps({"type": "error", "stage": "translate", "error": str(exc)}) + "\n"
            return
        if tags:
            full += tags.flush()
        body = full[:clue_at(full)] if clued else full
        if skip is None:                  # the reply ended while still being judged
            skip = replay.cut(body, final=True)
        if not suppressed:
            yield from _reveal()

        dt = time.time() - t0
        _bump_usage(claude_calls=1)
        # Prompt-cache state of the call just made, for the Translation panel's
        # cache light and the latency panel's cache row. Every backend reports
        # exact per-call counters: Claude via cache_control, Gemini via an
        # explicit cachedContents handle, OpenAI and xAI via
        # prompt_tokens_details.cached_tokens on the usage chunk that
        # stream_options.include_usage asks for. Each backend keeps its counters
        # thread-local, so this is the answer for THIS request.
        if translate_fn is claude_translate_stream:
            stats = claude_cache_stats()
        elif translate_fn is gemini_translate_stream:
            stats = heard_cache[0] if heard_cache else gemini_cache_stats()
        elif translate_fn is openai_translate_stream:
            stats = openai_cache_stats()
        elif translate_fn is xai_translate_stream:
            stats = xai_cache_stats()
        else:
            stats = None
        cache = _cache_state(stats)
        if stats:   # the cost box
            costs.llm(used_model, prompt=stats.get("prompt", stats["input"]), cached=stats["read"],
                      write=stats["write"], output=stats.get("output", 0))
        # From here on the reply IS what follows `skip`: the caption, the history
        # entry and the voice all agree, so the next call is shown exactly what the
        # room was shown -- never a restatement it could learn to repeat.
        out = claude_strip_meta(body[skip:].strip())
        replayed = body[:skip].split()
        clue_cut = full[len(body):].split()
        _note_latency(used_model, dt)
        if replayed:
            print(f"[mt ] replay: dropped {len(replayed)} leading word(s) restating earlier "
                  f"captions: {' '.join(replayed)!r}", flush=True)
        if clue_cut:
            print(f"[mt ] WARNING: {used_model} copied the Router's clue into its reply -- "
                  f"cut {len(clue_cut)} word(s) before display: {' '.join(clue_cut)[:100]!r}",
                  flush=True)
            src = text if len(text) <= 60 else text[:59].rstrip() + "…"
            _server_warn(f"Router: {used_model} copied the clue into its reply to «{src}»"
                         f" — {len(clue_cut)} word(s) cut before display", kind="clue")
        print(f"[mt ] {dt:.1f}s [->{target_lang}] {used_model} (ctx {len(history)})"
              f" -> {out!r}", flush=True)
        if tags and (tags.tag or tags.dropped):
            print(f"[mt ] voice tag: {tags.tag or '-'}"
                  + (f" (removed, not spoken: {', '.join(tags.dropped)})"
                     if tags.dropped else ""), flush=True)
        # Runaway guard: an output many times its source's length is the model
        # over-generating (a recognised Scripture completed past what was said, a
        # hallucinated run). Keeping just THAT reply off the history is not enough:
        # the turns that led the model there stay, so every later call is asked
        # with the same bad examples, and nothing new is stored to push them out.
        # Observed 2026-09-25: six restating turns froze in place and every
        # caption for minutes restated the same prefix. So the context goes too --
        # a few captions without it cost far less than one poisoned. The over-long
        # caption itself still ships. Rare in practice: ReplayGuard strips the
        # commonest cause above, and the batch cap
        # (BATCH_MAX_CHARS) keeps one call from carrying minutes of sermon.
        if out and len(out) > RUNAWAY_RATIO * len(text) + RUNAWAY_SLACK:
            with HISTORY_LOCK:
                hist.clear()
            print(f"[mt ] WARNING: output {len(out)}c vs source {len(text)}c is "
                  f"disproportionate -- not stored, history cleared (runaway guard)", flush=True)
        elif out:
            with HISTORY_LOCK:
                hist.append((time.monotonic(), text, out, target_lang))

        # The caption goes out HERE, before a single byte of audio, because it is
        # already authoritative: the model has finished and `out` will not change.
        # It used to be emitted last, after the whole batch had been synthesized,
        # which made the request -- and therefore the client's one-batch-at-a-time
        # gate -- last as long as the SPEECH did. Soniox generates at about
        # realtime, so a batch took roughly as long to process as it took to say,
        # and the pipeline could never catch up: measured at 7.0s held per call
        # against 0.9s of actual model time, with the room 13s behind. Now the
        # next translation starts while this batch is still being spoken.
        #
        # Audio lines continue AFTER this one and the stream ends when the last
        # of them has gone. A client that only wants text is finished at `final`
        # and may simply stop reading.
        yield json.dumps({"type": "final", "text": out, "seconds": round(dt, 1),
                          "cache": cache,
                          **({"replayed": len(replayed)} if replayed else {}),
                          **({"clue_cut": len(clue_cut)} if clue_cut else {}),
                          **({"tag": tags.tag} if tags and tags.tag else {}),
                          # the ears' readings the translator was given (shown under the source)
                          **({"ears": readings} if readings and used_model == model else {})}) + "\n"

        if speak and out:
            if tts_pending.strip():
                # Flush whatever tail never hit a terminal (e.g. no trailing
                # punctuation) -- the pipeline above only ships COMPLETE
                # sentences as they're detected.
                tts_in.put(tts_pending)
            elif not audio_started:
                # Nothing was ever split off as a complete sentence (e.g. a
                # very short reply) -- synthesize the whole thing now.
                tts_in.put(out)

        if tts_in is not None:
            tts_in.put(None)       # end of sentences: drain the worker fully
            while True:
                line = tts_out.get()
                if line is None:
                    break
                yield line

    def guarded():
        # The page aborts a call it has given up on (a stall before "final", the
        # audio deadline after it): the generator is closed at its next yield and
        # never reaches the sentinel. Without this the TTS worker went on
        # synthesizing -- and recording -- sentences nobody would hear (and a
        # resend then spoke them again), and then waited on its queue forever.
        try:
            yield from generate()
        finally:
            if tts_ctl["in"] is not None:
                tts_ctl["cancel"].set()
                tts_ctl["in"].put(None)

    return Response(guarded(), mimetype="application/x-ndjson")


@app.route("/route", methods=["POST"])
def route_ep():
    """Router (STT panel): keep Soniox's own translation of a segment, or have
    the translator model redo it? body {ro, en, ctx:[{ro,en},...]} (earlier
    segments, oldest first; the doubt bar is ROUTER_TH). -> router_gate.decide(). The page waits only
    CT_ROUTER_TIMEOUT_MS for the answer and keeps Soniox's text past that, so
    a slow call costs a check, never a caption."""
    if not HAS_OPENAI_KEY:
        return jsonify(route=False, err="no OPENAI_API_KEY"), 200
    data = request.get_json(silent=True) or {}
    ro, en = (data.get("ro") or "").strip(), (data.get("en") or "").strip()
    if not ro or not en:
        return jsonify(route=False), 200
    ctx = [c for c in (data.get("ctx") or []) if isinstance(c, dict)][-router_gate.SL_CTX:]
    out = router_gate.decide(ro, en, ctx, timeout=ROUTER_TIMEOUT_MS / 1000, th=ROUTER_TH)
    print(f"[rt ] {out['ms']}ms bad {out.get('bad')} sens {out.get('sens')} sl {out.get('sl')}"
          f" -> {out['why'] or 'keep'}: {en[:60]!r}", flush=True)
    return jsonify(out), 200


@app.route("/history", methods=["POST"])
def history_ep():
    """Add a caption the translator did not write (Soniox's, kept by the
    Router) to the rolling context, so a segment the Router does send on is
    translated with the whole recent sermon behind it, not only the few the
    model wrote itself. body {src, tgt, target_lang[, pair]}."""
    data = request.get_json(silent=True) or {}
    src, tgt = (data.get("src") or "").strip(), (data.get("tgt") or "").strip()
    target_lang = (data.get("target_lang") or DEFAULT_TARGET_LANG).lower()
    if not src or not tgt or target_lang not in TARGET_LANGUAGES:
        return jsonify(ok=False), 200
    pair = parse_hints(data.get("pair"))
    pair = tuple(pair) if len(pair) == 2 and target_lang in pair else None
    with HISTORY_LOCK:
        HISTORY["-".join(sorted(pair)) if pair else target_lang].append(
            (time.monotonic(), src, tgt, target_lang))
    return jsonify(ok=True), 200


@app.route("/speak", methods=["POST"])
def speak_ep():
    """Synthesize one already-translated line on demand (the caption Replay
    button). Plays back in the browser; does not touch the live
    translation/history path."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(audio=""), 200
    if not TTS_CAPABLE:
        return jsonify(error="TTS not configured"), 503
    # Replay re-speaks a line that was already translated, so the language is
    # whatever that line was translated INTO -- the browser sends it back with
    # the request rather than us guessing from the current picker, which may
    # have moved on since.
    target_lang = (data.get("target_lang") or DEFAULT_TARGET_LANG).lower()
    if target_lang not in TARGET_LANGUAGES:
        target_lang = DEFAULT_TARGET_LANG
    opts = _tts_opts(data, target_lang)
    if not _voice_can(opts, target_lang):
        return jsonify(audio=""), 200
    with TTS_SEM:
        clip = synthesize_bytes(text, **opts)
    _bump_usage(tts_chars=len(text))
    costs.tts(opts.get("provider"), len(text), opts.get("el_model") or EL_TTS_MODEL)
    return jsonify(audio=base64.b64encode(clip).decode("ascii"), mime=AUDIO_MIME)


@app.route("/speak-stream", methods=["POST"])
def speak_stream_ep():
    """Speak a caption that did not come through /translate -- Soniox's own
    translation under "none, fast", where there is no LLM call to carry the
    voice. Same NDJSON audio lines as /translate (audio_chunk / tts error), same
    per-sentence clips, and the same running voice context, so the page plays
    it through the same ordered timeline. Body: {text, target_lang, ...tts
    params}."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not TTS_CAPABLE:
        return jsonify(error="TTS not configured"), 503
    target_lang = (data.get("target_lang") or DEFAULT_TARGET_LANG).lower()
    if target_lang not in TARGET_LANGUAGES:
        target_lang = DEFAULT_TARGET_LANG
    opts = _tts_opts(data, target_lang)
    meta = {"src": (data.get("src") or "").strip(), "lang": target_lang}
    lag = _stream_wanted(data, opts)

    def generate():
        if not text or not _voice_can(opts, target_lang):
            return
        # what /translate's worker would have been handed: the complete
        # sentences, then the open tail
        parts = [p.strip() for p in _split_complete_sentences(text) if p.strip()]
        clips = [None] * len(parts)
        if lag is not None:
            try:
                clips = [_stream_push(p, opts, lag) for p in parts]
            except (RuntimeError, OSError) as exc:
                yield json.dumps({"type": "error", "stage": "tts", "error": str(exc)}) + "\n"
                return
        for part, clip in zip(parts, clips):
            yield from _safe_tts_chunk(part, opts, meta, lag, clip)

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/publish", methods=["POST"])
def publish_ep():
    """Forward one finalized caption to the external relay (CT_PUBLISH_URL).

    The browser calls this at commit/edit/clear time; we relay it server-to-server
    with the shared token in a header, so the secret never reaches a browser and
    there is no CORS. Best-effort and non-fatal: a relay hiccup must never disturb
    the live in-room captions, so any failure returns a JSON error the browser
    just logs. Body: {id, text, done, lang?, src?, t?} to add/update, or
    {action:"clear"}; lang is the language the caption was translated into (e.g.
    "ro", "en"), src the words it translates and t when the first of them was
    spoken (epoch ms) -- for the website's archive."""
    if not PUBLISH_CAPABLE:
        return jsonify(error="web captions not configured"), 503
    data = request.get_json(silent=True) or {}
    action = data.get("action") or "seg"
    payload = {"action": "clear"} if action == "clear" else {
        "id": str(data.get("id") or ""),
        "text": (data.get("text") or "")[:2000],
        "done": bool(data.get("done")),
    }
    lang = str(data.get("lang") or "").strip().lower()[:12]
    if action != "clear" and lang:
        payload["lang"] = lang
    src = str(data.get("src") or "").strip()[:2000]
    if action != "clear" and src:
        payload["src"] = src
    try:
        t = int(data.get("t") or 0)
    except (TypeError, ValueError):
        t = 0
    if action != "clear" and t > 0:
        payload["t"] = t
    if action != "clear" and not payload["id"]:
        return jsonify(error="missing id"), 400
    _relay_submit(CT_PUBLISH_URL, payload, "publish", 8)   # off to the background worker
    return jsonify(ok=True)


@app.route("/publish-audio", methods=["POST"])
def publish_audio_ep():
    """Forward one finalized sentence's spoken mp3 to the external relay (clip.php).

    Companion to /publish: the browser POSTs the raw mp3 bytes here with ?id=<the
    caption segment id it already published>, and we relay them server-to-server
    (token in a header, so the secret never reaches a browser and there is no CORS)
    as base64 JSON, matching publish.php's shape. clip.php files the audio under the
    live session's epoch and flags that caption audio-ready. Best-effort and
    non-fatal by design: a relay hiccup only costs one unspoken sentence on the
    website and never disturbs the in-room captions, so failures return a JSON
    error the browser just logs."""
    if not PUBLISH_CAPABLE or not CT_PUBLISH_CLIP_URL:
        return jsonify(error="web audio not configured"), 503
    seg_id = (request.args.get("id") or "").strip()
    if not seg_id:
        return jsonify(error="missing id"), 400
    audio = request.get_data(cache=False) or b""
    if not audio:
        return jsonify(error="no audio"), 400
    if len(audio) > CT_PUBLISH_CLIP_MAX:
        return jsonify(error="clip too large"), 413
    payload = {"id": seg_id, "audio_b64": base64.b64encode(audio).decode("ascii")}
    _relay_submit(CT_PUBLISH_CLIP_URL, payload, "publish-audio", 15)   # background worker
    return jsonify(ok=True)


@app.route("/caption", methods=["POST"])
def caption_ep():
    """Mirror one caption into the in-memory projector store, so a networked
    #projector page (on another machine) can poll it. The operator app posts here
    alongside the same-machine BroadcastChannel. Body: {id,text,done} for a segment,
    {type:"pace",wps,dwell,highlight,lines,font} for the wall's reveal pacing,
    line count and text size (% of the default) (kept across
    Clear), or {action:"clear"}. Local, best-effort.

    Posts can overtake each other on the way here: the page fires them without
    waiting, and each runs on its own server thread. Measured under load, a
    sentence's last partial landed after its own final 1 time in 300 -- and the
    store kept the partial, so every projector reading this feed trimmed the
    sentence back and nothing ever put it right. So the page stamps each post
    with `n`, its place in send order, and the store only moves forward: a post
    older than what it already holds is dropped. A late partial never replaces
    its final; a sentence landing after its successor is dropped rather than
    shown out of order; a stale pace never replaces a newer one; nothing
    sent before a Clear comes back after it, and a Clear that lands late wipes
    only what was sent before it. A post without `n` (a tool, a test) applies as
    it arrives, as before."""
    data = request.get_json(silent=True) or {}
    n = data.get("n")
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        n = None

    def newer(*marks):                 # is this post newer than every one of `marks`?
        return n is None or all(m is None or n > m for m in marks)

    with _CAP_LOCK:
        if data.get("action") == "clear":
            if newer(_CAP["clear_n"]):
                late = [s for s in _CAP["segs"]              # posted after the Clear, arrived first
                        if n is not None and s["n"] is not None and s["n"] > n]
                _CAP.update(segs=late, epoch=int(time.time()), rev=_CAP["rev"] + 1, clear_n=n)
        elif data.get("type") == "pace":
            if newer(_CAP["pace_n"]):
                try:
                    _CAP["pace"] = {"wps": max(0, min(1000, int(data.get("wps") or 0))),
                                    "dwell": max(0, min(10000, int(data.get("dwell") or 0))),
                                    "highlight": max(1, min(12, int(data.get("highlight") or 12))),
                                    "lines": max(1, min(12, int(data.get("lines") or CT_PROJECTOR_LINES))),
                                    "font": max(30, min(300, int(data.get("font") or 100)))}
                except (TypeError, ValueError):
                    pass                               # a bad post keeps the last good pace
                _CAP["pace_n"] = n
                _CAP["rev"] += 1
        else:
            sid = str(data.get("id") or "")
            if sid:
                text = (data.get("text") or "")[:2000]
                done = bool(data.get("done"))
                now = time.monotonic()
                seg = next((s for s in _CAP["segs"] if s["id"] == sid), None)
                if seg is not None:
                    if newer(seg["n"]):
                        seg.update(text=text, done=done, at=now, n=n)
                        _CAP["rev"] += 1
                elif newer(_CAP["made_n"], _CAP["clear_n"]):
                    _CAP["segs"].append({"id": sid, "text": text, "done": done, "at": now, "n": n})
                    if n is not None:
                        _CAP["made_n"] = n
                    if len(_CAP["segs"]) > _CAP_MAX_SEGS:
                        _CAP["segs"] = _CAP["segs"][-_CAP_MAX_SEGS:]
                    _CAP["rev"] += 1
        rev = _CAP["rev"]
    return jsonify(ok=True, rev=rev)


@app.route("/caption-feed")
def caption_feed_ep():
    """What the networked #projector polls (~4x/second). Returns {changed:false}
    with just the rev/epoch when nothing moved since ?since=<rev>, else the current
    segments, less anything older than CT_PROJECTOR_STALE_S. Epoch
    bumps on Clear so a late-joining projector resets."""
    try:
        since = int(request.args.get("since"))
    except (TypeError, ValueError):
        since = -1
    _PROJ_SEEN[request.args.get("pid") or request.remote_addr or "?"] = time.monotonic()
    with _CAP_LOCK:
        _cap_prune()
        if since == _CAP["rev"]:
            return jsonify(changed=False, rev=_CAP["rev"], epoch=_CAP["epoch"])
        return jsonify(changed=True, rev=_CAP["rev"], epoch=_CAP["epoch"],
                       segs=[{"id": s["id"], "text": s["text"], "done": s["done"]}
                             for s in _CAP["segs"]],
                       pace=_CAP["pace"])


# Projector pages seen polling /caption-feed: page id (or address) -> when.
_PROJ_SEEN = {}


@app.route("/pipe-status")
def pipe_status_ep():
    """For the operator page's lights: how many projector pages polled the
    caption store in the last 15 s (on any machine, a ProPresenter web object
    included; a window in the background may poll only once a second or so),
    and how the last web-caption relay went (None before the first)."""
    now = time.monotonic()
    for k, t in list(_PROJ_SEEN.items()):
        if now - t > 30:
            _PROJ_SEEN.pop(k, None)
    return jsonify(projectors=sum(1 for t in list(_PROJ_SEEN.values()) if now - t < 15),
                   web=_RELAY_HEALTH.get("publish"))


@app.route("/project", methods=["POST"])
def project_ep():
    """Push one finalized caption to ProPresenter's on-screen Message.

    Companion to /publish, aimed at the auditorium screen instead of phones: the
    browser calls this at commit/edit/clear time, and we drive ProPresenter's local
    network API (Preferences -> Network) server-to-server -- it sends no CORS
    headers, so a browser fetch to it would be blocked. Triggering a message both
    shows it and updates its text, so each finalized caption simply re-triggers the
    same message with the new line; clear hides it. Best-effort and non-fatal: a
    ProPresenter hiccup must never disturb the in-room captions or the projector
    window, so any failure returns a JSON error the browser just logs.

    Body: {text} to show/update the line, or {action:"clear"} to hide it.

    Matches the current official ProPresenter API (verified against the published
    OpenAPI): POST /v1/message/{id}/trigger with an array of token values -- a text
    token is {"name": <token>, "text": {"text": <value>}} -- and GET
    /v1/message/{id}/clear (204). The {id} may be the message's name, uuid or index,
    and CT_PROPRESENTER_TOKEN must match the text token's name in ProPresenter.

    With CT_PROPRESENTER_TYPE=word the text is instead handed to the typewriter
    (see _ProjectorTyper), which reveals it a few words at a time on its own thread;
    this endpoint then returns immediately and those sends happen asynchronously."""
    if not PROJECT_CAPABLE:
        return jsonify(error="ProPresenter not configured"), 503
    data = request.get_json(silent=True) or {}
    action = data.get("action") or "seg"
    if action == "clear":
        if _projector_typer is not None:
            _projector_typer.clear()
        ok, err = _pp_clear()
        return jsonify(ok=True) if ok else (jsonify(error=err), 502)
    text = (data.get("text") or "")[:1000]
    if not text:
        return jsonify(error="missing text"), 400
    if _projector_typer is not None:
        # Word-stream mode: hand the text to the reveal thread and return at once.
        _projector_typer.commit(text)
        return jsonify(ok=True)
    ok, err = _pp_send_text(text)
    return jsonify(ok=True) if ok else (jsonify(error=err), 502)


# ---- the session recording -------------------------------------------------
# Everything spoken this session, kept clip by clip with the sentence it speaks
# and when it was said (app/recorder.py). Four endpoints, no state machine: ask
# what is in the buffer, download it, write it to disk, throw it away.


@app.route("/record")
def record_status_ep():
    """What the buffer holds right now -- clip count, size, playing time. The
    browser polls this to keep the Rec readout honest without holding any audio
    of its own."""
    return jsonify(**RECORDING.status())


@app.route("/record", methods=["POST"])
def record_ctl_ep():
    """save | clear | on | off.

    `save` writes the buffer to disk as a folder (audio.mp3 + captions.srt +
    captions.vtt + segments.json + transcript.html) and is what auto-save-on-exit
    calls -- from a `pagehide` beacon it is a few dozen bytes, because the audio
    never left this process. `clear` starts a fresh buffer; `on`/`off` set
    whether clips are kept at all, for the run (CT_RECORD sets the default)."""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or request.args.get("action") or "").strip().lower()
    if action == "save":
        path = RECORDING.save(reason=(data.get("reason") or "manual"))
        if not path:
            return jsonify(error="nothing recorded yet", **RECORDING.status()), 409
        if data.get("clear"):
            RECORDING.clear()
        return jsonify(**RECORDING.status())      # status carries `saved`, the path
    if action == "clear":
        RECORDING.clear()
    elif action in ("on", "off"):
        RECORDING.enabled = (action == "on")
    elif action in ("autosave_on", "autosave_off"):
        RECORDING.autosave = action.endswith("_on")
    elif action:
        return jsonify(error=f"unknown action {action!r}"), 400
    return jsonify(**RECORDING.status())


@app.route("/record/download")
def record_download_ep():
    """The buffer as a download. `fmt=zip` (default) bundles the audio with its
    cues, the timestamp map and a transcript; `fmt=mp3` is the bare audio for
    someone who only wants the sound.

    Built in memory: a service is tens of megabytes of already-compressed mp3,
    and this runs on the operator's own machine."""
    fmt = (request.args.get("fmt") or "zip").lower()
    if RECORDING.empty():
        return jsonify(error="nothing recorded yet"), 409
    name = RECORDING.name()
    if fmt == "mp3":
        body, mime, fn = RECORDING.mp3(), AUDIO_MIME, name + ".mp3"
    elif fmt == "srt":
        body, mime, fn = RECORDING.srt().encode("utf-8"), "text/plain; charset=utf-8", name + ".srt"
    elif fmt == "json":
        body, mime, fn = (RECORDING.segments_json().encode("utf-8"),
                          "application/json; charset=utf-8", name + ".json")
    else:
        body, mime, fn = RECORDING.zip_bytes(), "application/zip", name + ".zip"
    return Response(body, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{fn}"',
        "Content-Length": str(len(body)),
    })


# ---- the session record -----------------------------------------------------
# The page's own account of a service (index.html, "the session record"): every
# row with when it was heard, what each extra ear heard, the Router's scores, who
# translated it and why, timings and hand edits; the settings at the first Start
# and every change after; and its event log. The page pushes the whole record
# every few seconds while it changes; each push is written straight to
# recordings/<session id>/ as session.json (the data) and session.html (the same
# data inside app/session.html, which renders it), so a crash or a closed window
# costs at most the last few seconds. A speech recording's save and .zip carry
# the same two files. What only the server knows (model ids, pins, latency,
# spend) is added here at each write. CT_SESSION_SAVE=0 keeps them off disk
# (the ⭳ HTML download still works).
SESSION_SAVE = os.environ.get("CT_SESSION_SAVE", "1").strip().lower() not in ("0", "false", "no", "off")
SESSION_TEMPLATE = os.path.join(HERE, "session.html")
_SESSION_ID = re.compile(r"^session-\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d$")
SESSIONS = {}                 # id -> the page's latest record
_session_lock = threading.Lock()
_session_last = [None]        # id of the record pushed last (what a recording save carries)

# ---- the operator page went away ---------------------------------------------
# A page closing cleanly saves the recording itself (index.html's pagehide
# beacon) and sends its record's last rows (jFinal). A browser that crashes, is
# force-quit, or a laptop that sleeps sends nothing -- but its audio stream
# (/stt-stream) drops without the "stop" the Stop button sends. If no stream has
# reconnected PAGE_GONE_S later (the page's own reconnect, or a reload and
# Start, cancels it), save the recording and mark the session closed.
PAGE_GONE_S = 30
_stream_gen = [0]             # bumped by every /stt-stream connection
_stream_gen_lock = threading.Lock()


def _stream_opened():
    with _stream_gen_lock:
        _stream_gen[0] += 1
        return _stream_gen[0]


def _page_gone_later(gen):
    t = threading.Timer(PAGE_GONE_S, _page_gone, args=(gen,))
    t.daemon = True
    t.start()


def _page_gone(gen):
    with _stream_gen_lock:
        if _stream_gen[0] != gen:
            return                         # a stream reconnected: the page is back
    print(f"[stream] the operator page's audio stream dropped and did not come back"
          f" in {PAGE_GONE_S}s -- saving the recording and the session record", flush=True)
    _autosave("page gone")
    with _session_lock:
        sid = _session_last[0]
        rec = SESSIONS.get(sid) if sid else None
        if rec is None or rec.get("closed"):
            return
        rec = dict(rec, closed=int(time.time() * 1000))
        SESSIONS[sid] = rec
    if SESSION_SAVE:
        try:
            _write_files(os.path.join(RECORDING.save_dir(), sid), _session_files(rec))
        except Exception as exc:           # noqa: BLE001 -- the record is still in memory
            print(f"[session] ERROR writing {sid}: {exc}", flush=True)

# What the server cut out of a caption before the page ever saw it (the Router's
# clue, copied into a reply), which the page's own log therefore cannot hold.
# Each record carries the ones since its page opened, as server.warnings, in the
# page log's shape ({at, level, msg}, plus a `kind`); session.html shows them in
# its event log.
SERVER_WARNINGS = deque(maxlen=200)
_warnings_lock = threading.Lock()


def _server_warn(msg, kind):
    with _warnings_lock:
        SERVER_WARNINGS.append({"at": int(time.time() * 1000), "level": "warn",
                                "kind": kind, "msg": msg})


def _session_server_info(since=0):
    with _usage_lock:
        u = dict(_usage)
    with _warnings_lock:
        warnings = [w for w in SERVER_WARNINGS if w["at"] >= since]
    avail = {n for n, _ in extra_ears.available()} if EARS_ENABLED else set()
    return {
        "version": APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "stt": {"provider": "Soniox", "model": SONIOX_RT_MODEL},
        "ears": [{"name": n, "label": label, "model": model, "configured": n in avail}
                 for n, (label, model) in extra_ears.models().items()],
        "ears_enabled": EARS_ENABLED,
        "ear_wait_ms_default": EAR_WAIT_MS,
        "router": {"model": router_gate.MODEL, "available": HAS_OPENAI_KEY,
                   "timeout_ms": ROUTER_TIMEOUT_MS,
                   "context": {"gate": router_gate.GATE_CTX, "soundalike": router_gate.SL_CTX}},
        "translator_default": DEFAULT_MODEL,
        "hearing_fallback": HEARS_FALLBACK,
        "tts_default": DEFAULT_TTS_PROVIDER,
        "model_p50_s": {m: _mt_p50(m) for m in sorted(MT_LATENCY)},
        "model_pins": _pin_status(),
        "usage": {"stt_min": round(u["stt_secs"] / 60.0, 1), "tts_chars": u["tts_chars"],
                  "llm_calls": u["claude_calls"]},
        "cost": costs.snapshot(),
        "warnings": warnings,
    }


def _session_files(rec):
    """{filename: bytes} for one record: session.json and session.html."""
    opened = rec.get("opened")
    doc = dict(rec, server=_session_server_info(opened if isinstance(opened, (int, float)) else 0))
    data = json.dumps(doc, ensure_ascii=False, indent=1)
    with open(SESSION_TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    # Inside <script type="application/json">: only "</" could end it early.
    html = tpl.replace("__SESSION_JSON__", data.replace("</", "<\\/"))
    return {"session.json": data.encode("utf-8"), "session.html": html.encode("utf-8")}


def _write_files(folder, files):
    os.makedirs(folder, exist_ok=True)
    for fn, data in files.items():
        tmp = os.path.join(folder, fn + ".part")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(folder, fn))     # never a half-written file


def _latest_session_files():
    """The last-pushed record's files, for a speech recording's save and .zip."""
    with _session_lock:
        rec = SESSIONS.get(_session_last[0])
    return _session_files(rec) if rec else {}


RECORDING.extras = _latest_session_files


# ---- the operator's settings, kept here too ----------------------------------
# The page saves its settings in the browser (localStorage), so a different
# browser, a private window or a cleared cache opened on defaults -- and sent
# those defaults to the projector at once. The page now also posts every save
# here and, opening with nothing newer of its own, takes this copy before it
# starts. Kept in memory and in operator-settings.json beside the recordings so
# it survives a restart. `at` is the page's save time (epoch ms): only a newer
# save replaces the stored one, so a stale tab cannot roll the settings back.
_OP_SETTINGS_KEYS = ("ct_router", "ct_webstream", "ct_ppstream")   # flags kept outside ct_settings
_OP_SETTINGS_MAX = 64 * 1024
_op_settings_lock = threading.Lock()


def _op_settings_path():
    return os.path.join(RECORDING.save_dir(), "operator-settings.json")


def _op_settings_clean(data):
    """The stored shape, or None if `data` is not a usable settings post."""
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        return None
    try:
        at = int(data.get("at") or 0)
    except (TypeError, ValueError):
        return None
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    return {"at": at, "settings": data["settings"],
            "keys": {k: str(keys[k])[:8] for k in _OP_SETTINGS_KEYS if keys.get(k) is not None}}


def _op_settings_load():
    try:
        with open(_op_settings_path(), encoding="utf-8") as f:
            return _op_settings_clean(json.load(f)) or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f"[settings] could not read {_op_settings_path()}: {exc}", flush=True)
        return {}


OP_SETTINGS = _op_settings_load()


@app.route("/settings", methods=["GET", "POST"])
def op_settings_ep():
    """GET: the last settings the operator page saved ({} if none). POST {at,
    settings, keys}: store them if newer than what is held."""
    global OP_SETTINGS
    if request.method == "GET":
        with _op_settings_lock:
            return jsonify(OP_SETTINGS)
    if (request.content_length or 0) > _OP_SETTINGS_MAX:
        return jsonify(ok=False, error="too large"), 413
    new = _op_settings_clean(request.get_json(silent=True))
    if new is None:
        return jsonify(ok=False, error="bad settings"), 400
    with _op_settings_lock:
        if new["at"] < OP_SETTINGS.get("at", 0):
            return jsonify(ok=True, kept="newer")
        OP_SETTINGS = new
        path = _op_settings_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:              # still served from memory this run
            print(f"[settings] could not write {path}: {exc}", flush=True)
    return jsonify(ok=True)


def _session_patch(base, p):
    """Lay a closing page's tail over the full record it pushed last. The page
    pushes its whole record every 5 s, but a closing page can only send a beacon
    (size-limited), so it sends the rows from `rows_from` on and the events from
    `events_from` on -- what changed since that push, with a few rows of margin
    for captions still settling -- plus `config_now`, `saved` and `closed`."""
    out = dict(base)
    for key, frm in (("rows", "rows_from"), ("events", "events_from")):
        tail = p.get(key)
        if not isinstance(tail, list):
            continue
        have = list(out.get(key) or [])
        try:
            i = max(0, min(len(have), int(p.get(frm) or 0)))
        except (TypeError, ValueError):
            continue
        out[key] = have[:i] + tail
    for k in ("config_now", "saved", "closed"):
        if p.get(k) is not None:
            out[k] = p[k]
    return out


@app.route("/session", methods=["POST"])
def session_push_ep():
    """The page's session record, whole (it replaces the last push of the same id)."""
    rec = request.get_json(silent=True) or {}
    sid = str(rec.get("id") or "")
    if not _SESSION_ID.match(sid):
        return jsonify(error="bad session id"), 400
    with _session_lock:
        if rec.get("patch"):               # a closing page's last word (index.html jFinal)
            base = SESSIONS.get(sid)
            if base is None:
                return jsonify(ok=False, error="unknown session"), 404
            rec = _session_patch(base, rec)
        SESSIONS[sid] = rec
        _session_last[0] = sid
    path = None
    if SESSION_SAVE:
        try:
            path = os.path.join(RECORDING.save_dir(), sid)
            _write_files(path, _session_files(rec))
        except Exception as exc:               # the record is still served from memory
            print(f"[session] ERROR writing {sid}: {exc}", flush=True)
            return jsonify(ok=True, saved=None, error=str(exc))
    return jsonify(ok=True, saved=path)


# ---- resuming the service on a reopened page ---------------------------------
# A page reopened mid-service picks up the last session: its transcript rows
# come back and it goes on writing the same record rather than a second one.
# Only a session a page has LEFT is offered -- marked closed by the closing
# page's beacon or by _page_gone -- so a second tab opened beside a live one
# never takes over its record. And only within RESUME_S of leaving: after a
# longer gap it is another service. "Start fresh" (POST action=forget) stops
# that session being offered again.
RESUME_S = 20 * 60


@app.route("/session/resume", methods=["GET", "POST"])
def session_resume_ep():
    """GET: the last session record if a reopened page may resume it, else {}.
    POST {action:"forget", id}: the page chose Start fresh; don't offer it again."""
    with _session_lock:
        sid = _session_last[0]
        rec = SESSIONS.get(sid) if sid else None
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if data.get("action") == "forget" and rec is not None and data.get("id") == sid:
                SESSIONS[sid] = dict(rec, resume_declined=int(time.time() * 1000))
            return jsonify(ok=True)
        if (not rec or not rec.get("closed") or rec.get("resume_declined")
                or not rec.get("rows")):
            return jsonify({})
        try:
            age_ms = time.time() * 1000 - float(rec["closed"])
        except (TypeError, ValueError):
            return jsonify({})
        return jsonify(rec if age_ms <= RESUME_S * 1000 else {})


@app.route("/session/download")
def session_download_ep():
    """One session record as session.html (default) or `fmt=json`."""
    sid = request.args.get("id") or _session_last[0] or ""
    with _session_lock:
        rec = SESSIONS.get(sid)
    if not rec:
        return jsonify(error="no such session"), 404
    fmt = (request.args.get("fmt") or "html").lower()
    files = _session_files(rec)
    if fmt == "json":
        body, mime, fn = files["session.json"], "application/json; charset=utf-8", sid + ".json"
    else:
        body, mime, fn = files["session.html"], "text/html; charset=utf-8", sid + ".html"
    return Response(body, mimetype=mime, headers={
        "Content-Disposition": f'attachment; filename="{fn}"',
        "Content-Length": str(len(body)),
    })


def _autosave(reason):
    """Write the buffer out on the way down, so closing the window is a save.
    Idempotent and quiet: nothing recorded means nothing written, and a failure
    here must not stop the process exiting."""
    try:
        if RECORDING.autosave and not RECORDING.empty():
            RECORDING.save(reason=reason)
    except Exception as exc:
        print(f"[rec] ERROR autosaving: {exc}", flush=True)


atexit.register(_autosave, "exit")


def _autosave_signal(signum, _frame):
    _autosave(f"signal {signum}")
    # Restore the default and re-raise, so Ctrl-C still reads as Ctrl-C to
    # whatever started us and we do not change how the process dies.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
    if _sig is not None:
        try:
            signal.signal(_sig, _autosave_signal)
        except (ValueError, OSError):
            pass      # not the main thread (frozen launcher): atexit still covers us


# Cache the Soniox voice list. Soniox returns its voices nested inside the
# model catalogue, so one call covers every model's roster.
_sx_voices = {"at": 0.0, "data": None}


# Soniox voice cloning. A cloned voice is created from one reference clip and
# then used exactly like a built-in one -- the voice id simply goes in the same
# `voice` field -- so nothing in the synthesis path needs to know the
# difference. Two hard limits shape the code below: the reference clip may be
# at most 2 minutes, and an ORGANISATION may hold only 20 cloned voices at once
# (not 20 per project), which is why deletion is offered in the UI rather than
# left to the console.
SX_VOICES_URL = "https://api.soniox.com/v1/voices"
SX_CLONE_MAX_SECONDS = 120        # Soniox rejects longer clips during processing
SX_CLONE_READY_TIMEOUT = 45.0     # measured ~2s; this is the give-up bound, not the norm


def _wav_seconds(raw):
    """Duration of a WAV payload, or None if it can't be read. Only used to
    reject an over-long reference clip before upload, so an unreadable header
    is not fatal here -- Soniox will reject a genuinely broken file anyway."""
    try:
        return sf.info(io.BytesIO(raw)).duration
    except Exception:
        return None


def _sx_clone_state(voice):
    """(status, error_message) of a cloned voice for the model we synthesize
    with. A voice reports per-model status because a model released after the
    voice was created starts out 'not_computed'; only the model we actually
    call matters here."""
    for m in voice.get("models") or []:
        if m.get("model") == SX_TTS_MODEL:
            return m.get("status"), m.get("error_message")
    return "not_computed", None


def _soniox_cloned_voices():
    """Voices cloned on this account. Deliberately uncached: the list changes
    when the operator captures or deletes one, and it is read only when the
    voice picker is refreshed."""
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        return [], "no Soniox key"
    try:
        r = _get_http_session().get(SX_VOICES_URL,
                                    headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        out = []
        for v in (r.json().get("voices") or []):
            if not v.get("id"):
                continue
            status, err = _sx_clone_state(v)
            out.append({"name": v.get("name") or v["id"], "voice_id": v["id"],
                        "cloned": True, "status": status,
                        "description": err or "Captured voice"})
        out.sort(key=lambda x: (x["name"] or "").lower())
        return out, None
    except Exception as exc:
        return [], str(exc)[:60]


def _soniox_clone(name, audio, filename="reference.wav"):
    """Create a cloned voice from one reference clip and wait for it to become
    usable. Returns (voice_dict, error). Soniox prepares the voice
    asynchronously, so this polls rather than trusting the 201 -- synthesis with
    a voice that is still 'processing' would fail."""
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        return None, "no Soniox key"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = _get_http_session().post(
            SX_VOICES_URL, headers=headers,
            files={"file": (filename, audio, "audio/wav")},
            data={"name": name}, timeout=60)
        if r.status_code >= 400:
            detail = r.text[:200]
            try:
                j = r.json()
                detail = j.get("error_message") or j.get("message") or detail
                # The org-wide cap is the one failure an operator can actually
                # act on, so say what to do about it instead of relaying jargon.
                if j.get("error_type") == "limit_exceeded" or r.status_code == 429:
                    detail = ("Soniox allows 20 captured voices per organisation. "
                              "Delete one you no longer need and try again.")
                elif r.status_code == 409:
                    detail = f"a captured voice named {name!r} already exists"
            except ValueError:
                pass
            return None, detail
        v = r.json()
        vid = v.get("id")
        if not vid:
            return None, "Soniox returned no voice id"
        # Poll until the voice is usable with the model we synthesize through.
        deadline = time.time() + SX_CLONE_READY_TIMEOUT
        status, err = _sx_clone_state(v)
        while status in ("processing", "not_computed") and time.time() < deadline:
            time.sleep(1.0)
            g = _get_http_session().get(f"{SX_VOICES_URL}/{vid}", headers=headers, timeout=15)
            if g.status_code != 200:
                break
            status, err = _sx_clone_state(g.json())
        if status != "ready":
            # A voice that never came good is dead weight against the 20-voice
            # cap, so don't leave it behind for the operator to find later.
            try:
                _get_http_session().delete(f"{SX_VOICES_URL}/{vid}", headers=headers, timeout=15)
            except Exception:
                pass
            return None, err or f"voice did not become ready ({status})"
        return {"voice_id": vid, "name": v.get("name") or name}, None
    except Exception as exc:
        return None, str(exc)[:120]


# ElevenLabs voice cloning, which exists alongside Soniox's for one reason:
# accent. A clone reproduces the speaker's accent along with their timbre, so a
# Romanian speaker cloned and then asked to speak English sounds like a
# Romanian speaking English -- faithful to the person, but not what a service
# translating INTO English wants. Soniox offers no lever for that; its clone is
# whatever it captured. ElevenLabs does: remixing takes a voice you own plus a
# description of what to change and returns previews of a new voice, and accent
# is explicitly one of the attributes it will change. So the flow is
# capture -> clone -> remix towards native pronunciation in the target
# language -> keep the remix and delete the raw clone, which would otherwise
# hold a voice slot for nothing.
EL_API = "https://api.elevenlabs.io/v1"
EL_CLONE_CAPABLE = HAS_EL_KEY
# How far the remix may travel from the reference audio. Both ends are failure
# modes -- too low and the accent survives, too high and it stops sounding like
# the speaker at all, which defeats the point of cloning them -- so this is
# offered as three named steps rather than a raw 0-1 slider.
EL_REMIX_STRENGTH = {"light": 0.35, "medium": 0.6, "strong": 0.85}
EL_REMIX_TIMEOUT = 180            # the remix generates and speaks a test script; it is not quick


def _el_key_headers():
    return {"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}


def _el_error(r):
    """The useful part of an ElevenLabs failure. It nests the real message in
    detail.message, and its two actionable failures -- the account's voice slots
    being full, and instant cloning not being on the plan -- are worth saying in
    the operator's terms rather than relaying the API's."""
    detail = r.text[:200]
    try:
        d = (r.json() or {}).get("detail")
    except ValueError:
        return detail
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        detail = d.get("message") or detail
        status = d.get("status") or ""
        if "voice_limit" in status:
            detail = ("this ElevenLabs account has no free voice slots. Delete a "
                      "captured voice you no longer need and try again.")
        elif "instant_voice_cloning" in status:
            detail = "this ElevenLabs plan does not include instant voice cloning"
    return detail


# The demo script the remix is conditioned on, and why we write it ourselves.
# The endpoint will generate one for you (auto_generate_text), but MEASURED
# against the live API it writes that script in ENGLISH even when the voice
# description asks for a native Spanish speaker -- it sprinkles in a "Amigos"
# and carries on in English. Demonstrating a "native Spanish voice" on English
# text is exactly the wrong conditioning: it teaches the remix to speak ENGLISH
# with a Spanish accent, which is the problem we are trying to remove. So the
# script is rendered into the target language first, through the translator the
# app already runs, and auto_generate_text is kept only as the fallback.
EL_REMIX_SEED = (
    "Good morning, and thank you all for coming. It is good to be together "
    "again, and to have a quiet hour in the middle of a noisy week. Before we "
    "begin, take a breath, settle in, and let the room go still. There is no "
    "hurry here. We have time enough for everything that matters.")
EL_REMIX_TEXT_MIN, EL_REMIX_TEXT_MAX = 100, 1000    # the endpoint's own bounds


def _el_remix_text(lang):
    """The seed paragraph in `lang`, or None to let ElevenLabs write its own.

    Never fatal: this runs at soundcheck, and a translator hiccup should cost
    the accent pass some quality, not the capture."""
    if lang == "en":
        return EL_REMIX_SEED
    try:
        out = (claude_translate(EL_REMIX_SEED, target_lang=lang) or "").strip()
    except Exception as exc:
        print(f"[clone] remix script stays English: {str(exc)[:90]}", flush=True)
        return None
    # A translation that came back empty, truncated or somehow enormous is worse
    # than no script at all -- the endpoint would reject it and lose the take.
    if EL_REMIX_TEXT_MIN <= len(out) <= EL_REMIX_TEXT_MAX:
        return out
    print(f"[clone] remix script stays English: translation was {len(out)} chars", flush=True)
    return None


def _el_remix_prompt(lang):
    """What to ask the remix model for. Names the target language twice on
    purpose -- once for the accent, once for the pronunciation -- and spells out
    what must NOT change, because a remix told only what to fix will happily
    drift the age, gender and pace of the voice along with the accent."""
    name = LANGUAGES.get(lang, lang)
    return (f"A native {name} speaker. Neutral, standard {name} accent with "
            f"native {name} pronunciation and no trace of a foreign accent. "
            f"Otherwise the same person: same timbre, same age, same gender, "
            f"same warmth, same unhurried pace.")


def _el_delete_voice(voice_id):
    """Best-effort delete. Used both for the operator's own delete and to clear
    up the intermediate clone after a remix, where a failure is not worth
    surfacing -- it costs a voice slot, not the take."""
    try:
        r = _get_http_session().delete(f"{EL_API}/voices/{voice_id}",
                                       headers=_el_key_headers(), timeout=15)
        # A voice that is already gone is the state the caller wanted, so it is
        # a success rather than an error. ElevenLabs reports that as a 400 with
        # status "voice_does_not_exist" -- NOT as a 404 -- so the status code
        # alone can't be trusted here (measured against the live API).
        if r.status_code in (200, 202, 204, 404):
            return True, None
        try:
            d = (r.json() or {}).get("detail") or {}
            if isinstance(d, dict) and (d.get("code") == "voice_not_found"
                                        or d.get("status") == "voice_does_not_exist"):
                return True, None
        except ValueError:
            pass
        return False, _el_error(r)
    except Exception as exc:
        return False, str(exc)[:120]


def _elevenlabs_clone(name, audio, accent_lang=None, strength="medium",
                      filename="reference.wav"):
    """Clone the captured voice and, when a target language is given, remix the
    speaker's own accent out of it. Returns (voice_dict, error).

    A failed accent pass is deliberately NOT an error: the plain clone is still
    a usable voice, and throwing away a take the speaker already stood there and
    recorded is the worse outcome. The dict carries a `warning` instead, so the
    UI can say the voice is the speaker's accent and all."""
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return None, "no ElevenLabs key"
    headers = _el_key_headers()
    try:
        r = _get_http_session().post(
            f"{EL_API}/voices/add", headers=headers,
            files={"files": (filename, audio, "audio/wav")},
            # remove_background_noise: the reference is recorded in the room the
            # service happens in, so it carries that room. Cleaning it up gives
            # the clone the speaker rather than the building.
            data={"name": name, "remove_background_noise": "true"}, timeout=120)
        if r.status_code >= 400:
            return None, _el_error(r)
        base_id = (r.json() or {}).get("voice_id")
        if not base_id:
            return None, "ElevenLabs returned no voice id"
        if not accent_lang:
            return {"voice_id": base_id, "name": name}, None

        desc = _el_remix_prompt(accent_lang)
        script = _el_remix_text(accent_lang)
        body = {"voice_description": desc}
        if script:
            body["text"] = script
        else:
            body["auto_generate_text"] = True
        if strength in EL_REMIX_STRENGTH:
            body["prompt_strength"] = EL_REMIX_STRENGTH[strength]
        warning = None
        remixed = None
        try:
            g = _get_http_session().post(f"{EL_API}/text-to-voice/{base_id}/remix",
                                         headers=headers, json=body,
                                         timeout=EL_REMIX_TIMEOUT)
            if g.status_code >= 400:
                warning = _el_error(g)
            else:
                previews = (g.json() or {}).get("previews") or []
                gid = previews[0].get("generated_voice_id") if previews else None
                if not gid:
                    warning = "the accent pass returned no preview"
                else:
                    c = _get_http_session().post(
                        f"{EL_API}/text-to-voice", headers=headers,
                        json={"voice_name": name, "voice_description": desc,
                              "generated_voice_id": gid}, timeout=60)
                    if c.status_code >= 400:
                        warning = _el_error(c)
                    else:
                        remixed = (c.json() or {}).get("voice_id")
                        if not remixed:
                            warning = "the accent pass returned no voice id"
        except Exception as exc:
            warning = str(exc)[:120]
        if remixed:
            # The raw clone has served its purpose as the remix's reference and
            # would otherwise sit on a voice slot sounding wrong.
            _el_delete_voice(base_id)
            return {"voice_id": remixed, "name": name}, None
        return ({"voice_id": base_id, "name": name,
                 "warning": f"kept the speaker's own accent — {warning}"}, None)
    except Exception as exc:
        return None, str(exc)[:120]


def _soniox_voices():
    """Built-in Soniox voices for the configured model (cached 5 min). Unlike
    ElevenLabs these are shared names ("Adrian", "Maya"), not per-account ids,
    and they're scoped per model -- so filter to the model we actually call."""
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        return [], "no Soniox key"
    now = time.time()
    if _sx_voices["data"] is not None and now - _sx_voices["at"] < _EL_VOICES_TTL:
        return _sx_voices["data"], None
    try:
        r = _get_http_session().get("https://api.soniox.com/v1/tts-models",
                                    headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        models = r.json().get("models") or []
        m = next((x for x in models if x.get("id") == SX_TTS_MODEL), None) \
            or (models[0] if models else None)
        if not m:
            return [], "no models"
        vs = [{"name": v.get("id"), "voice_id": v.get("id"),
               "gender": v.get("gender"), "description": v.get("description")}
              for v in (m.get("voices") or []) if v.get("id")]
        vs.sort(key=lambda x: (x["name"] or "").lower())
        _sx_voices["data"] = vs
        _sx_voices["at"] = now
        return vs, None
    except Exception as exc:
        return [], str(exc)[:60]


@app.route("/voices")
def voices_ep():
    """Voices for the in-app voice dropdown (cached 5 min), for whichever
    backend the UI asks about. Degrades gracefully: returns {voices: [],
    error: ...} rather than failing, so a listing problem on one provider never
    takes the picker down."""
    if request.args.get("provider") == "soniox":
        vs, err = _soniox_voices()
        # Captured (cloned) voices first: if the operator went to the trouble of
        # capturing one for this service, it's the one they mean to use.
        cloned, cerr = _soniox_cloned_voices()
        ready = [v for v in cloned if v.get("status") == "ready"]
        return jsonify(voices=ready + vs, **({"error": err or cerr} if (err or cerr) else {}))
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return jsonify(voices=[], error="no ElevenLabs key")
    now = time.time()
    if _el_voices["data"] is not None and now - _el_voices["at"] < _EL_VOICES_TTL:
        return jsonify(voices=_el_voices["data"])
    try:
        r = _get_http_session().get("https://api.elevenlabs.io/v2/voices?page_size=100",
                         headers={"xi-api-key": key}, timeout=10)
        if r.status_code != 200:
            return jsonify(voices=[], error=f"HTTP {r.status_code}")
        # "cloned" is an instant clone of a captured voice; "generated" is what a
        # remix of one becomes. Both are this account's own captures rather than
        # library voices, so both get the captured treatment in the picker.
        vs = [{"name": v.get("name"), "voice_id": v.get("voice_id"),
               **({"cloned": True, "description": "Captured voice"}
                  if v.get("category") in ("cloned", "generated") else {})}
              for v in (r.json().get("voices") or []) if v.get("voice_id")]
        # Captured voices first (if the operator captured one for this service,
        # it is the one they mean to use), then the go-to EN/RO voices pinned to
        # the top regardless of their ElevenLabs library name (the RO one is
        # labeled "Bogdan, voce radio/tv" but is actually Jora Slobod) so they
        # don't get lost among 30+ voices.
        _PINNED = ("auq43ws1oslv0tO4BDa7", "OlBp4oyr3FBAGEAtJOnU")  # Adam Stone, Jora/Bogdan
        vs.sort(key=lambda x: (not x.get("cloned"),
                                x["voice_id"] not in _PINNED,
                                _PINNED.index(x["voice_id"]) if x["voice_id"] in _PINNED else 0,
                                (x["name"] or "").lower()))
        _el_voices["data"] = vs
        _el_voices["at"] = now
        return jsonify(voices=vs)
    except Exception as exc:
        return jsonify(voices=[], error=str(exc)[:60])


@app.route("/voice-clone", methods=["POST"])
def voice_clone_ep():
    """Capture the speaker's own voice from a soundcheck clip, so the spoken
    translation can carry it into any target language.

    Deliberately an explicit, operator-driven step rather than something that
    happens automatically to whoever is talking: a clone reproduces pace,
    accent and breathing, not just timbre, so it wants one clean take of one
    consenting speaker -- not a snippet grabbed mid-service off a room mic with
    the congregation in the background. Body is raw WAV (same convention as
    /stt); ?name= labels it in the picker.

    ?provider= says which backend clones it, because the two differ in what a
    clone can be: Soniox keeps the speaker exactly as captured, accent and all,
    while ElevenLabs can remix the accent towards the target language. With
    ElevenLabs, ?accent= is the target language code to sound native in (omit it
    to keep the speaker's own accent) and ?strength= is light/medium/strong."""
    provider = (request.args.get("provider") or "").strip() or DEFAULT_TTS_PROVIDER
    raw = request.get_data()
    if not raw:
        return jsonify(error="no audio received"), 400
    secs = _wav_seconds(raw)
    name = (request.args.get("name") or "").strip()[:60] or "Captured voice"
    # Names must be unique within the project, and an operator recapturing after
    # a bad take would otherwise collide with their own previous attempt.
    name = f"{name} · {time.strftime('%d %b %H:%M')}"
    print(f"[clone] {provider}: {len(raw)} bytes ({secs:.1f}s) -> {name!r}" if secs is not None
          else f"[clone] {provider}: {len(raw)} bytes -> {name!r}", flush=True)

    if provider == "elevenlabs":
        if not EL_CLONE_CAPABLE:
            return jsonify(error="ElevenLabs not configured"), 503
        accent = (request.args.get("accent") or "").strip()
        if accent and accent not in TARGET_LANGUAGES:
            return jsonify(error=f"unknown target language {accent!r}"), 400
        strength = (request.args.get("strength") or "medium").strip()
        v, err = _elevenlabs_clone(name, raw, accent or None, strength)
        # A capture changes the account's voice list, which is cached for five
        # minutes -- without this the operator's own new voice wouldn't appear.
        _el_voices["data"] = None
    else:
        if not SX_TTS_CAPABLE:
            return jsonify(error="Soniox not configured"), 503
        if secs is not None and secs > SX_CLONE_MAX_SECONDS:
            # Soniox accepts an over-long clip at upload and only fails later
            # during processing, which would read as a mysterious failure.
            return jsonify(error=f"reference clip is {secs:.0f}s; "
                                 f"Soniox allows at most {SX_CLONE_MAX_SECONDS}s"), 400
        v, err = _soniox_clone(name, raw)
    if err:
        print(f"[clone] ERROR: {err}", flush=True)
        return jsonify(error=err), 502
    if v.get("warning"):
        print(f"[clone] {v['voice_id']} with a caveat: {v['warning']}", flush=True)
    else:
        print(f"[clone] ready: {v['voice_id']}", flush=True)
    return jsonify(**v)


@app.route("/voice-clone/<voice_id>", methods=["DELETE"])
def voice_clone_delete_ep(voice_id):
    """Delete a captured voice. Offered in-app because both backends cap how
    many you may hold -- Soniox at 20 per ORGANISATION, ElevenLabs by plan -- so
    a season of Sundays would otherwise quietly fill the ceiling. ?provider=
    says whose voice this is."""
    if (request.args.get("provider") or "").strip() == "elevenlabs":
        if not EL_CLONE_CAPABLE:
            return jsonify(error="ElevenLabs not configured"), 503
        ok, err = _el_delete_voice(voice_id)
        _el_voices["data"] = None
        return jsonify(ok=True) if ok else (jsonify(error=err or "delete failed"), 502)
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        return jsonify(error="Soniox not configured"), 503
    try:
        r = _get_http_session().delete(f"{SX_VOICES_URL}/{voice_id}",
                                       headers={"Authorization": f"Bearer {key}"}, timeout=15)
        # 404 means it's already gone, which is the state the caller wanted.
        if r.status_code in (200, 202, 204, 404):
            return jsonify(ok=True)
        return jsonify(error=f"HTTP {r.status_code}"), 502
    except Exception as exc:
        return jsonify(error=str(exc)[:120]), 502


@app.route("/usage")
def usage_ep():
    """Credit meter: ElevenLabs remaining-credit snapshot + this-run tally of
    what we sent (cloud STT minutes, cloud TTS characters, Claude calls)."""
    with _usage_lock:
        u = dict(_usage)
    return jsonify(
        session={
            "stt_min": round(u["stt_secs"] / 60.0, 1),
            "tts_chars": u["tts_chars"],
            "claude_calls": u["claude_calls"],
        },
        elevenlabs=_el_subscription(),
        cost=costs.snapshot(),
    )


@app.route("/caps")
def caps_ep():
    """Which features are available, so the UI can build its source/target
    language menus and enable spoken output. Soniox STT is multilingual and
    handles any source language natively -- there's no per-language gating on
    the source side. TTS reports which backends have a key behind them, so the
    UI only offers a provider it can actually reach."""
    return jsonify(
        languages=LANGUAGES,                           # {code: display name}, includes "auto"
        target_languages=list(TARGET_LANGUAGES.keys()), # valid targets (excludes "auto")
        heard_languages=list(languages.HEARD),         # Soniox hears + translates these; pairs use only these
        model_languages=languages.model_languages(),   # {model: [codes]} each Model may caption (measured)
        voice_languages=languages.voice_languages(),   # {soniox: [...], elevenlabs: {model: [...]}}
        tts=TTS_CAPABLE,
        router=HAS_OPENAI_KEY,                         # /route needs OpenAI's Decisions API
        router_timeout_ms=ROUTER_TIMEOUT_MS,
        router_th=ROUTER_TH,                           # named in the session record
        router_model=router_gate.MODEL,                # named in the session record
        soniox_model=SONIOX_RT_MODEL,
        ear_wait_ms=EAR_WAIT_MS,
        tts_providers=[{"id": p, "name": n} for p, n in TTS_PROVIDERS],
        tts_provider=DEFAULT_TTS_PROVIDER,
        # Container the audio_chunk / Replay bytes arrive in, so the browser
        # picks its MediaSource type and data: URLs from the server rather than
        # hardcoding a format the server might change under it.
        audio_mime=AUDIO_MIME,
        sx_tts_model=SX_TTS_MODEL,
        # the voice settings set in .env, named in the session record
        el_stream=EL_STREAM, el_expression=EL_EXPRESSION,
        el_stability=EL_STABILITY, sx_tighten=SX_TIGHTEN,
        # Soundcheck voice capture: which backends can clone, and which of them
        # can additionally remix the speaker's accent towards the target
        # language (ElevenLabs can, Soniox can't -- see _elevenlabs_clone).
        voice_clone=bool(SX_TTS_CAPABLE or EL_CLONE_CAPABLE),
        voice_clone_providers=(["soniox"] if SX_TTS_CAPABLE else [])
                            + (["elevenlabs"] if EL_CLONE_CAPABLE else []),
        voice_clone_accent=(["elevenlabs"] if EL_CLONE_CAPABLE else []),
        voice_clone_max_s=SX_CLONE_MAX_SECONDS,
        stt=HAS_SONIOX_KEY,
        stt_stream=HAS_SONIOX_KEY,   # Soniox realtime streaming (/stt-stream) available
        sat=_sat_available(),        # wtpsplit installed: the "+ SaT" cut rules can run (/sat)
        anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),   # Claude translator options available
        openai=HAS_OPENAI_KEY,       # OpenAI translator option (gpt-6-luna) available
        xai=HAS_XAI_KEY,             # xAI Grok translator option available
        gemini=HAS_GEMINI_KEY,       # Google Gemini translator options available
        gemini_audio=GEMINI_AUDIO,   # offer gemini-3.6-flash + audio in the Model menu (CT_GEMINI_AUDIO)
        ears=([{"name": n, "label": l} for n, l in extra_ears.available()]   # extra ears beside Soniox
              if EARS_ENABLED else []),                                      # (off unless CT_EARS=1)
        publish=PUBLISH_CAPABLE,     # external caption relay (/publish) configured
        propresenter=PROJECT_CAPABLE, # ProPresenter message projection (/project) configured
        propresenter_signal=CT_PROPRESENTER_SIGNAL, # the Message is an on/off box signal, not caption text
        projector_w=CT_PROJECTOR_W,  # networked #projector fixed size (px, 0 = fill the window)
        projector_h=CT_PROJECTOR_H,
        projector_bg=CT_PROJECTOR_BG,        # its background (CSS colour, or "transparent")
        projector_font=CT_PROJECTOR_FONT_PX, # caption text size in px (0 = scale with window)
        projector_bottom=CT_PROJECTOR_BOTTOM_PX,  # lift the strip up from the bottom edge (px)
        projector_max_h=CT_PROJECTOR_MAX_H_PX,    # cap the strip height so it stays in a band (px)
        projector_left=CT_PROJECTOR_LEFT_PX,      # left margin (px, 0 = default 6vw)
        projector_right=CT_PROJECTOR_RIGHT_PX,    # right margin (px, 0 = default 6vw)
        projector_latest_only=CT_PROJECTOR_LATEST_ONLY,  # show only the current message, not a history
        projector_lines=CT_PROJECTOR_LINES,       # lines a single long message may fill
        projector_dwell=CT_PROJECTOR_DWELL_MS,    # hold each line this long before it scrolls up (ms)
        projector_highlight=CT_PROJECTOR_HIGHLIGHT,  # how many recent sentences stay bright

        # Session recording: the spoken clips are piling up server-side and can
        # be saved off (/record, /record/download). `record_autosave` is the
        # startup default -- the browser may flip it live for the run.
        record=True,
        record_on=RECORDING.enabled,
        record_autosave=RECORDING.autosave,
        record_dir=RECORDING.save_dir(),
        # Live xAI catalogue so the picker follows xAI's month-to-month
        # turnover instead of rotting. Served from the background-refreshed
        # cache (see translate_xai.list_models) -- this endpoint gates the whole
        # UI, so it must never block on an upstream call.
        xai_models=xai_list_models() if HAS_XAI_KEY else [],
        # Model health, both halves of it: identity drift found at startup, and
        # any model whose live median has run away from its pinned baseline.
        # Advisory -- the UI shows it, nothing acts on it.
        model_pins=[r for r in _pin_status() if r["status"] in ("missing", "drifted")],
        model_slow=[{"model": m, "p50": p50, "baseline": base}
                    for m, (p50, base) in sorted(MT_SLOW.items())],
        # Observed medians for every model used this run, pinned or not -- what
        # you read after a service to decide what latency_p50_s should be.
        model_p50={m: _mt_p50(m) for m in sorted(MT_LATENCY)},
    )


def _pin_status():
    with PIN_LOCK:
        return list(PIN_STATUS)


def _key_health(name):
    """Per-key status for the diagnostics panel: is it set, and if so is it
    usable (plain ASCII -- see ascii_key_problem). Lets the UI show a clear
    good / missing / invalid indicator instead of the operator guessing why a
    stage isn't working."""
    val = os.environ.get(name)
    problem = ascii_key_problem(name, val)
    return {
        "present": bool(val),
        "ok": bool(val) and not problem,
        "problem": problem,   # human-readable reason when set-but-invalid, else null
    }


@app.route("/netdiag")
def netdiag_ep():
    """On-demand transport probe -- the in-app version of scripts/net_diag.ps1.
    For each realtime host it times DNS, TCP connect (one round trip, so this IS
    the RTT) and the TLS handshake, so a slow service can be split into transport
    vs provider time. It opens its OWN throwaway sockets, only when the operator
    asks, and nothing here runs on the live caption path -- so it cannot slow it.
    Best run between services or during a lull, since it does touch the network."""
    import socket, ssl, concurrent.futures as _cf
    SAMPLES = 3
    hosts = []
    if HAS_SONIOX_KEY:
        hosts.append(("Soniox STT", "stt-rt.soniox.com"))
        hosts.append(("Soniox TTS", "tts-rt.soniox.com"))
    hosts.append(("Anthropic", "api.anthropic.com"))
    if HAS_OPENAI_KEY:
        hosts.append(("OpenAI", "api.openai.com"))
    if HAS_XAI_KEY:
        hosts.append(("xAI", "api.x.ai"))
    if HAS_GEMINI_KEY:
        hosts.append(("Google Gemini", "generativelanguage.googleapis.com"))
    if HAS_EL_KEY:
        hosts.append(("ElevenLabs", "api.elevenlabs.io"))
    ctx = ssl.create_default_context()

    def probe(host, port=443):
        t = time.perf_counter()
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as e:
            return {"error": "DNS", "detail": str(e)[:80]}
        dns_ms = (time.perf_counter() - t) * 1000.0
        fam = infos[0][0]; addr = infos[0][4]
        tcps, tlss, loss = [], [], 0
        for _ in range(SAMPLES):
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(5.0)
            try:
                t = time.perf_counter(); s.connect(addr); tcps.append((time.perf_counter() - t) * 1000.0)
                t = time.perf_counter()
                ss = ctx.wrap_socket(s, server_hostname=host); ss.close()
                tlss.append((time.perf_counter() - t) * 1000.0)
            except Exception:
                loss += 1
                try: s.close()
                except Exception: pass
        def stat(xs):
            return None if not xs else {"min": round(min(xs), 1),
                                        "avg": round(sum(xs) / len(xs), 1),
                                        "jit": round(max(xs) - min(xs), 1)}
        return {"dns": round(dns_ms, 1), "tcp": stat(tcps), "tls": stat(tlss),
                "loss": loss, "samples": SAMPLES}

    results = []
    with _cf.ThreadPoolExecutor(max_workers=max(1, len(hosts))) as ex:
        futs = [(label, h, ex.submit(probe, h)) for (label, h) in hosts]
        for label, h, f in futs:
            try:
                r = f.result(timeout=25)
            except Exception as e:
                r = {"error": "probe", "detail": str(e)[:80]}
            results.append({"label": label, "host": h, **r})
    return jsonify(hosts=results, at=int(time.time()))


@app.route("/status")
def status_ep():
    """Full under-the-hood snapshot for the in-app diagnostics panel: which
    cloud engines are active, basic config, and per-key health."""
    return jsonify(
        stt=HAS_SONIOX_KEY,
        tts=TTS_CAPABLE,
        uptime_s=round(time.time() - _t0),
        keys={
            "anthropic": _key_health("ANTHROPIC_API_KEY"),    # translation (required)
            "soniox": _key_health("SONIOX_API_KEY"),          # speech-to-text (required)
            "elevenlabs": _key_health("ELEVENLABS_API_KEY"),  # spoken output (optional)
        },
    )


if __name__ == "__main__":
    # threaded=True: /stt and /translate overlap, so Romanian keeps appearing
    # while a translation is in flight.
    app.run(host="0.0.0.0", port=PORT, threaded=True)
