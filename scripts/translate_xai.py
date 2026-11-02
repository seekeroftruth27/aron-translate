#!/usr/bin/env python3
"""Any-language -> one chosen target translation via the xAI Grok API (cloud).

Third translator backend alongside translate_claude.py and translate_openai.py,
exposing the SAME interface (translate / translate_stream with identical
signatures) so the caption server can route a request to any provider by model
name alone. It reuses translate_claude's system prompt (editable prompt file +
injected glossary), target-language tag scheme, rolling-history replay, and
meta-suppression, so switching to Grok in the live picker changes ONLY the
model -- never the prompting or the caption-safety hardening.

Default model: grok-4.20-non-reasoning -- the low-latency, NON-reasoning
variant, the right fit for live captions. It's the same reasoning tradeoff
translate_openai makes by pinning the GPT-5 models to their lowest effort:
translation of a spoken sentence rarely benefits from reasoning, and every
reasoning token is caption latency. Grok's non-reasoning variant simply has
that decision baked into the model, so there is no effort knob to send.

It replaced grok-4-1-fast-non-reasoning, which xAI had stopped advertising and
was silently serving as grok-4.3 -- a REASONING model. The pin said
"non-reasoning"; the traffic was not. Measured on the repo's own
figurative_stress battery (61 RO->EN cases) plus serial streaming timings:

                          regional  idiom  reasoning tok  TTFT p50  rel. cost
  grok-4.20-non-reasoning    19/20  16/21              0     1.05s        1.0
  grok-4.3 (what we ran)     18/20  17/21         23,434     3.04s        3.1

Same aggregate score (55/61 each), but three times the speed at a third of the
cost, and genuinely zero reasoning tokens. It is also BETTER on the axis this
app cares most about -- regionalisms: grok-4.3 rendered "Țipă gunoiul afară"
(Transylvanian a țipa = to throw) as "Please YELL that garbage out of the
yard", the kind of caption that goes up on a wall in front of a congregation.

The one place it is genuinely weaker is literal calques of glossary-listed
idioms ("gura lumii" -> "the mouth of the world"). The anti-calque line in
config/translation_prompt.txt helps but does NOT eliminate it: over three runs
of the idiom set, 15.7/21 -> 16.7/21 mean, with that specific case going 0/3 ->
2/3. Probabilistic, not a fix -- an idiom the glossary does not list (the
harness's "ou și oțet") is unaffected either way. Worth having, worth not
trusting: at temperature 0.2 this battery moves +/-1 case run to run, so judge
any future prompt change on repeated runs, never a single pass.

Uses xAI's OpenAI-compatible Chat Completions endpoint over plain `requests`
(already a project dependency) -- no SDK needed. Being a non-reasoning model it
DOES accept sampling params, so `temperature` is passed through (unlike the
GPT-5 reasoning models, which reject it).

Requires:
    export XAI_API_KEY=xai-...

Usage:
    python translate_xai.py --text "Dumnezeu este bun."
    python translate_xai.py --target es --text "..."
"""
import argparse
import json
import os
import sys
import threading
import time
import uuid

# Shared prompting/hardening -- one source of truth in translate_claude.
from translate_claude import (SYSTEM_PROMPT, LANGUAGES, TARGET_LANGUAGES, anchored_turn,
                              DEFAULT_TARGET_LANG, _strip_meta, _tag_for,
                              history_turns, system_prompt_for)

# The ALIAS, not the dated id (grok-4.20-0309-non-reasoning), is pinned on
# purpose: a dated id is the thing that rots, and "non-reasoning" in the alias
# keeps the one property this backend actually depends on attached to the name.
# Neither form is self-protecting though -- xAI silently REMAPS a retired id
# rather than 404ing it, which is how the previous pin ended up being served by
# a reasoning model for who knows how long. check_default_model() below is the
# part that actually catches that.
XAI_MODELS = ("grok-4.20-non-reasoning", "grok-4.3")
DEFAULT_MODEL = "grok-4.20-non-reasoning"

# Models that REASON BY DEFAULT but accept a per-request pin that turns it off.
# Grok 4.20 ships as two ids (…-reasoning / …-non-reasoning); from 4.3 on there
# is one id and a `reasoning_effort` field instead -- the Gemini shape, and it
# is guarded the same way (see _note_usage). Measured 2026-09-16 on the app's
# prompt, one sentence, streaming:
#
#   grok-4.3   default             8.8s to first token,  376 reasoning tokens
#   grok-4.3   reasoning_effort=low 10.4s,               475
#   grok-4.3   reasoning_effort=none 1.0s,                 0   <- pinned here
#   grok-4.5   reasoning_effort=none -> 400 "does not support `none`"; low = 2.3s, 81 tokens
#   grok-4.6   reasoning_effort=none -> 400
#
# So 4.3 is offered, pinned; 4.5 and 4.6 stay out (their floor is `low`, which
# is still seconds of thinking before the first word). Keyed by the canonical
# id AND by alias, because the catalogue serves `grok-4.3` for `grok-4.3-latest`.
PINNED_EFFORT = {"grok-4.3": "none", "grok-4.3-latest": "none"}


def pinned_effort(name):
    """The reasoning_effort to send for `name`, or None for a model that has a
    non-reasoning id of its own (grok-4.20-non-reasoning) or is unknown."""
    return PINNED_EFFORT.get(name or "")
XAI_URL = "https://api.x.ai/v1/chat/completions"
MODELS_URL = "https://api.x.ai/v1/models"


def is_xai_model(name):
    """Route-by-name test used by the caption server and mt_router. Covers the
    static tuple above AND anything the live catalogue turned up, so a model
    that only exists in this month's catalogue still routes to this backend
    instead of falling through to Claude."""
    name = name or ""
    return name in XAI_MODELS or name.startswith("grok-")


# ---- live model catalogue --------------------------------------------------
# xAI's line-up turns over fast (the id this module shipped with is already
# unlisted -- see the CAVEAT above), so the picker is built from GET /v1/models
# rather than a hardcoded list that silently rots.
#
# The catalogue is NOT offered to the operator raw. Measured on the repo's own
# figurative_stress battery (61 RO->EN cases), the models it returns are wildly
# unequal for live captioning:
#
#   grok-4.20-non-reasoning   p50 TTFT 1.0s   0 reasoning tokens   19/20 regional
#   grok-4.3                  p50 TTFT 3.0s   23k reasoning tokens 18/20 regional
#   grok-4.6                  p50 10.6s, p95 24.6s -- a caption 25s behind the room
#
# So each entry is CLASSIFIED and the risky ones are flagged rather than hidden:
# an operator picking a model mid-service should be able to see that it reasons
# (which costs latency and is what makes a model narrate instead of translate)
# before they pick it. Non-text models (grok-imagine-*) and the coding model are
# dropped outright -- they cannot translate a sermon.
_CATALOGUE_TTL_S = 6 * 3600
_catalogue = {"at": 0.0, "models": []}
_CATALOGUE_LOCK = threading.Lock()

# Ids the catalogue showed as reasoning models. Kept rather than discarded so
# rejection can be based on POSITIVE knowledge: an id absent from here is simply
# unknown (an xAI outage leaves the catalogue empty), and an unknown id must not
# be refused -- that would turn a provider blip into "your model choice is
# silently ignored". Names are checked too, since the alias carries the property.
_reasoning_ids = set()


def is_reasoning_model(name):
    """True only when we can positively tell that `name` reasons -- never as a
    guess. Used to refuse a stale pick (a saved setting from before reasoning
    models were dropped) instead of quietly serving seconds of caption lag."""
    if not name:
        return False
    if "non-reasoning" in name or pinned_effort(name):
        return False
    return name in _reasoning_ids or "reasoning" in name

# Dropped from the picker entirely: no text pricing == not a text model
# (grok-imagine-*), and the coding models are the wrong tool regardless of price.
# Matched against the CANONICAL id only, never the aliases -- grok-4.5 is a
# perfectly good translator that happens to carry a "grok-build-latest" alias,
# and an alias-wide substring match silently swallowed it.
_NOT_TRANSLATORS = ("build", "code", "imagine")

# Reasoning models are dropped too, and for a live-captioning reason rather than
# a quality one: reasoning happens BEFORE the first token, so it lands entirely
# on the caption's time-to-first-word, where it is measured in seconds and the
# room is staring at an empty screen. They were previously offered with a
# "reasons -- slower" label; in practice the latency was not something an
# operator would ever knowingly choose mid-service, so the honest thing is to
# not offer them at all. Nothing about this is xAI-specific -- it is the same
# reason the Claude picker offers Sonnet 5 only at effort "low".
ALLOW_REASONING = False


def _classify(m):
    """One catalogue entry -> the shape the UI needs, or None if it can't
    translate (or isn't a well-formed entry -- one malformed row must not cost
    the operator the whole picker)."""
    if not isinstance(m, dict) or not m.get("id"):
        return None
    mid = m.get("id", "")
    ids = [mid] + list(m.get("aliases") or [])
    if not m.get("prompt_text_token_price"):
        return None                                   # image/video model
    if any(bad in mid for bad in _NOT_TRANSLATORS):
        return None                                   # coding model
    # Non-reasoning by its own id, or reasoning that this module pins off per
    # request (PINNED_EFFORT) -- either way it is offered, and the pin is what
    # the UI's cost/latency expectation is based on.
    pin = next((pinned_effort(i) for i in ids if pinned_effort(i)), None)
    non_reasoning = any("non-reasoning" in i for i in ids) or bool(pin)
    return {
        "id": m["id"],
        "non_reasoning": non_reasoning,
        "pinned_effort": pin,
        "context_length": m.get("context_length"),
        # Relative cost only -- the API prices in opaque "ticks" per token and
        # publishes no tick->USD scale, so a ratio is the honest thing to show.
        "in_price": m.get("prompt_text_token_price"),
        "cached_price": m.get("cached_prompt_text_token_price"),
        "out_price": m.get("completion_text_token_price"),
        "recommended": non_reasoning,
    }


def list_models(force=False):
    """Live text-model catalogue, classified and TTL-cached.

    NEVER raises and never blocks longer than a short timeout: the caption
    server calls this from /caps, which gates the whole UI, so an xAI outage
    must degrade to the static fallback rather than hang the operator's page.
    """
    now = time.time()
    with _CATALOGUE_LOCK:
        fresh = _catalogue["models"] and now - _catalogue["at"] < _CATALOGUE_TTL_S
        if fresh and not force:
            return list(_catalogue["models"])
    key = os.environ.get("XAI_API_KEY")
    models = []
    if key:
        try:
            import requests
            r = _get_session().get(MODELS_URL,
                                   headers={"Authorization": f"Bearer {key}"},
                                   timeout=(CONNECT_TIMEOUT_S, 8.0))
            if r.status_code == 200:
                models = [e for e in (_classify(m) for m in r.json().get("data") or [])
                          if e]
                # Remember what reasons, then drop it: the picker offers only
                # models whose latency an operator would accept mid-service.
                if not ALLOW_REASONING:
                    _reasoning_ids.update(e["id"] for e in models
                                          if not e["non_reasoning"])
                    models = [e for e in models if e["non_reasoning"]]
                models.sort(key=lambda e: (not e["recommended"], e["id"]))
        except Exception as exc:                      # network, JSON, schema drift
            print(f"[xai] model catalogue fetch failed ({exc}); using fallback",
                  file=sys.stderr)
    if not models:
        # Static fallback: whatever this module shipped with, so the picker is
        # never empty and the operator is never blocked from translating.
        models = [{"id": m, "non_reasoning": "non-reasoning" in m,
                   "context_length": None, "in_price": None, "cached_price": None,
                   "out_price": None, "recommended": True, "fallback": True}
                  for m in XAI_MODELS]
    with _CATALOGUE_LOCK:
        _catalogue.update(at=now, models=models)
    return list(models)


def check_default_model():
    """Warn if DEFAULT_MODEL has quietly stopped being what we think it is.

    This is the guard the previous pin lacked. grok-4-1-fast-non-reasoning was
    withdrawn from the catalogue and silently served as grok-4.3 -- a reasoning
    model -- and nothing in the app noticed, because the requests kept
    returning 200 with perfectly good translations, just slower and pricier and
    with reasoning tokens the pin's own name said would not be there.

    Two distinct failures are checked, because they have different fixes:
      - the pin is no longer in the catalogue at all (repin needed);
      - the pin resolves to a model that REASONS (the property the name
        promises is gone -- a caption model that reasons is slower and is what
        makes a model narrate instead of translate).

    Advisory only: it prints and returns, never raises and never blocks the
    caption path. A wrong-but-working model is a thing to tell the operator
    about, not a reason to refuse to translate.
    """
    try:
        models = list_models()
    except Exception:
        return None
    if any(m.get("fallback") for m in models):
        return None                       # catalogue unavailable; nothing to check against
    ids = {m["id"] for m in models}
    aliases_ok = DEFAULT_MODEL in ids
    entry = next((m for m in models if m["id"] == DEFAULT_MODEL), None)
    if not aliases_ok:
        # An alias is legitimately absent from the id list (the catalogue keys
        # on canonical ids), so resolve it the only way that is authoritative:
        # ask the API what it actually serves for this name.
        served = _resolve_served_model(DEFAULT_MODEL)
        if served is None:
            print(f"[xai] WARNING: default model {DEFAULT_MODEL!r} is not in the "
                  f"live catalogue and could not be resolved -- repin needed",
                  file=sys.stderr)
            return False
        entry = next((m for m in models if m["id"] == served), None)
        if served != DEFAULT_MODEL:
            print(f"[xai] NOTE: {DEFAULT_MODEL!r} is served as {served!r}",
                  file=sys.stderr)
    if entry and not entry["non_reasoning"]:
        print(f"[xai] WARNING: default model {DEFAULT_MODEL!r} now resolves to a "
              f"REASONING model ({entry['id']}) -- expect slower captions and "
              f"higher cost; repin to a non-reasoning model", file=sys.stderr)
        return False
    return True


def _resolve_served_model(name):
    """Ask the API which model it actually serves for `name` (the response's
    `model` field), with a minimal request. Returns None if it can't be
    determined. This is the only reliable way to detect a silent remap."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        return None
    try:
        r = _get_session().post(
            XAI_URL, headers={"Authorization": f"Bearer {key}"},
            json={"model": name, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=(CONNECT_TIMEOUT_S, 15.0))
        if r.status_code == 200:
            return r.json().get("model")
    except Exception:
        pass
    return None

# Same live-caption timeout philosophy as translate_claude/translate_openai:
# fail fast on connect; the read timeout is per-chunk when streaming, so a
# healthy stream can run long but a wedged one cannot stall the caption feed.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 25.0

# A truncated caption is worse than a few cents of ceiling; leave generous
# headroom (a non-reasoning model spends none of this on hidden reasoning).
MAX_COMPLETION_TOKENS = 2048

# ---- prompt-cache routing --------------------------------------------------
# xAI caches automatically -- there is no cache_control marker to place and no
# minimum prefix length (measured: a 186-token prompt already cached 128
# tokens). What it DOES need is server affinity: the cache lives on whichever
# backend served the earlier call, so without the x-grok-conv-id header a
# request can land on a machine that holds nothing. xAI's docs say to "always
# set" it, and a measured 14-segment service bears that out:
#
#   no header, sliding history : 49.8% of prompt tokens cached (reproduced twice)
#   x-grok-conv-id, same history: 66-85% cached, ~23-50% lower cost
#
# Without the header the hit rate does not merely wobble -- it COLLAPSES partway
# through and stays collapsed (cached fell to 128 tokens from segment ~9 onward
# in both runs), which is the signature of having drifted onto a cold backend
# mid-service. The id only has to be stable, not meaningful; it is scoped per
# target language because that is also how the rolling history is keyed
# (app/server.py HISTORY), so one id == one stable message prefix.
_RUN_ID = uuid.uuid4().hex[:12]


def conv_id_for(target_lang, pair=None):
    """Stable x-grok-conv-id for this process + whatever scopes the history.

    Must track app/server.py's HISTORY key, not merely the target: a bilingual
    session pools ONE history across both directions, so scoping the id by
    target would split that single message prefix over two cache lines and hand
    each one a prefix that keeps changing underneath it -- the exact cold-backend
    drift the header exists to prevent."""
    if pair:
        return f"ct-{_RUN_ID}-{'-'.join(sorted(pair))}"
    return f"ct-{_RUN_ID}-{target_lang or DEFAULT_TARGET_LANG}"


# Last call's token accounting (best-effort diagnostic; harness threads may
# interleave). xAI's endpoint reports cached prompt tokens the same way OpenAI
# does (prompt_tokens_details.cached_tokens). With the conv-id header the
# steady state is the whole ~1.9K-token system prompt served from cache (in
# 128-token blocks, so it lands on 1792) from the second call onward; the
# rolling history above it re-bills every call because the sliding window
# evicts from the front. One stderr line per call keeps that visible in the
# server console, mirroring the [openai] log style.
LAST_USAGE = {}
# Per-THREAD copy of the counters the Translation panel's cache light needs --
# LAST_USAGE is process-wide and the caption server is threaded, so it cannot
# be trusted to describe THIS request. Same pattern as the other backends.
_cache_stats = threading.local()


def last_cache_stats():
    """{"read", "write", "input"} for THIS thread's last xAI call, or None if it
    hasn't made one. Shaped to match translate_claude.last_cache_stats() so the
    server's _cache_state classifier works on it unchanged. `write` is always
    0: xAI's cache is automatic and free to populate. `read` is
    prompt_tokens_details.cached_tokens, 128-token blocks; with the conv-id
    header the steady state is the whole system prompt served from cache."""
    return getattr(_cache_stats, "last", None)


_warned_reasoning = set()


def _note_usage(usage, model=None):
    if not isinstance(usage, dict):
        return
    details = usage.get("prompt_tokens_details") or {}
    LAST_USAGE.clear()
    LAST_USAGE.update(prompt=usage.get("prompt_tokens"),
                      completion=usage.get("completion_tokens"),
                      cached=details.get("cached_tokens") or 0)
    prompt = LAST_USAGE["prompt"] or 0
    pct = (100.0 * LAST_USAGE["cached"] / prompt) if prompt else 0.0
    _cache_stats.last = {"read": LAST_USAGE["cached"], "write": 0, "input": prompt,
                         "output": LAST_USAGE["completion"] or 0}
    # The pin's guard: a pinned model that reports reasoning tokens has stopped
    # honouring the pin (or was silently remapped), and every one of those tokens
    # lands on time-to-first-word. Once per model, like translate_gemini.
    details_c = usage.get("completion_tokens_details") or {}
    thought = details_c.get("reasoning_tokens") or 0
    if thought and model and pinned_effort(model) and model not in _warned_reasoning:
        _warned_reasoning.add(model)
        print(f"[xai] WARNING: {model} spent {thought} reasoning tokens despite "
              f"reasoning_effort={pinned_effort(model)!r} -- the pin is not holding; "
              f"expect slower captions", file=sys.stderr)
    print(f"[xai] tokens: prompt {LAST_USAGE['prompt']} "
          f"(cached {LAST_USAGE['cached']}, {pct:.0f}%) + completion "
          f"{LAST_USAGE['completion']}", file=sys.stderr)


_session = None


def _get_session():
    """One shared requests.Session (connection pooling/keep-alive), matching
    the translate_openai pattern."""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def warm():
    """Pre-open the pooled TLS connection to the API host (unauthenticated
    GET; any response counts -- urllib3 pools it with no idle-time expiry).
    Called at server startup so the first Grok translation doesn't pay the
    handshake. Best-effort."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(XAI_URL)
        _get_session().get(f"{u.scheme}://{u.netloc}/",
                           timeout=(CONNECT_TIMEOUT_S, 5.0))
    except Exception:
        pass
    # Populate the model catalogue on the same background thread, so /caps is
    # served from cache on the operator's first page load instead of blocking
    # on an upstream round trip.
    try:
        list_models(force=True)
        check_default_model()   # catch a silently-remapped pin at startup, not mid-service
    except Exception:
        pass


def _build_payload(text, history, model, target_lang, stream, temperature,
                   draft=None, pair=None, expressive=False):
    """Chat Completions payload with the shared system prompt and the same
    tagged rolling history translate_claude replays."""
    tag = _tag_for(target_lang)
    messages = [{"role": "system", "content": system_prompt_for(pair, expressive)}]
    messages += history_turns(history, tag)
    messages.append({"role": "user", "content": anchored_turn(tag, text, draft)})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": MAX_COMPLETION_TOKENS,
        "temperature": temperature,   # non-reasoning model -> sampling accepted
        "stream": stream,
    }
    if pinned_effort(model):
        payload["reasoning_effort"] = pinned_effort(model)   # see PINNED_EFFORT
    if stream:
        # Final SSE chunk then carries usage (prompt/cached/completion tokens)
        # so cache behavior is observable on the live streaming path too.
        payload["stream_options"] = {"include_usage": True}
    return payload


def _post(payload, stream, conv_id=None):
    """POST to the Chat Completions endpoint; normalize every failure mode to
    RuntimeError (the caption server maps that to a retryable NDJSON error
    line, same as a Claude/OpenAI failure)."""
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY environment variable is not set")
    import requests
    headers = {"Authorization": f"Bearer {key}"}
    if conv_id:
        # Prompt-cache server affinity -- see conv_id_for() above.
        headers["x-grok-conv-id"] = conv_id
    try:
        r = _get_session().post(
            XAI_URL, json=payload, headers=headers,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), stream=stream)
    except requests.RequestException as exc:
        raise RuntimeError(f"xAI API: {exc}") from exc
    if r.status_code != 200:
        # Surface the API's own message (model typo, quota, revoked key) --
        # it lands in the Debug log where it's actually diagnosable.
        try:
            detail = (r.json().get("error") or {}).get("message") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        r.close()
        raise RuntimeError(f"xAI API: HTTP {r.status_code}: {detail}")
    if stream:
        # SSE is UTF-8 by spec, but xAI's `text/event-stream` response carries
        # NO charset, so requests defaults r.encoding to ISO-8859-1 (its RFC
        # fallback for text/*). iter_lines(decode_unicode=True) would then
        # decode the UTF-8 body as Latin-1 and mangle every multi-byte char --
        # e.g. a curly quote “ (E2 80 9C) becomes "â" + two control bytes.
        # Pin it to UTF-8. (The non-streaming path is unaffected: r.json()
        # already defaults to UTF-8 for the application/json response.)
        r.encoding = "utf-8"
    return r


def translate(text: str, history=None, model: str = DEFAULT_MODEL,
              temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
              conv_id: str = None, pair=None) -> str:
    """Translate one segment INTO `target_lang`. Same contract as
    translate_claude.translate(); `conv_id` overrides the prompt-cache routing
    id, which otherwise defaults to this process + target language (or + the
    language pair, in a bilingual session)."""
    r = _post(_build_payload(text, history, model, target_lang, False, temperature,
                             pair=pair),
              stream=False, conv_id=conv_id or conv_id_for(target_lang, pair))
    try:
        j = r.json()
    except ValueError as exc:
        raise RuntimeError(f"xAI API: non-JSON response") from exc
    finally:
        r.close()
    _note_usage(j.get("usage"), model)
    choice = (j.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        print("[xai] WARNING: translation truncated at max_tokens",
              file=sys.stderr)
    result = ((choice.get("message") or {}).get("content") or "").strip()
    return _strip_meta(result)


def translate_stream(text: str, history=None, model: str = DEFAULT_MODEL,
                     temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
                     draft: str = None, conv_id: str = None, pair=None,
                     expressive=False):
    """Like translate(), but yields the response text incrementally (SSE
    stream) -- same contract as translate_claude.translate_stream(): raw model
    output only, no meta-suppression (the caller does that on the full text).
    `conv_id` overrides the prompt-cache routing id (default: this process +
    target language, or + the language pair in a bilingual session), so the
    caption server needs no signature change."""
    import requests
    r = _post(_build_payload(text, history, model, target_lang, True, temperature,
                             draft, pair, expressive),
              stream=True, conv_id=conv_id or conv_id_for(target_lang, pair))
    finish = None
    try:
        it = r.iter_lines(decode_unicode=True)
        for line in it:
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                # Drain to EOF so the socket goes back to the session's pool
                # (keep-alive); a close() on an unread tail would instead drop
                # the connection and force a new TLS handshake next call.
                for _ in it:
                    pass
                break
            try:
                j = json.loads(data)
            except ValueError:
                continue
            _note_usage(j.get("usage"), model)   # include_usage: final data chunk before [DONE]
            choice = (j.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                yield delta
    except requests.RequestException as exc:
        raise RuntimeError(f"xAI API: {exc}") from exc
    finally:
        r.close()
    if finish == "length":
        print("[xai] WARNING: translation truncated at max_tokens",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=None)
    ap.add_argument("--infile", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(XAI_MODELS))
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

    print(f"[xai] {args.model} (-> {LANGUAGES[args.target]}, xAI API)", file=sys.stderr)
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
    print(f"\n=== {LANGUAGES[args.target]} (Grok translation) ===")
    print(out)
    print(f"\n[timing] {dt:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
