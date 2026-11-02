#!/usr/bin/env python3
"""Any-language -> one chosen target translation via the OpenAI API (GPT-5 family).

Alternative translator backend to translate_claude.py, exposing the SAME
interface (translate / translate_stream with identical signatures) so the
caption server can route a request to either provider by model name alone.
It reuses translate_claude's system prompt (editable prompt file + injected
glossary), target-language tag scheme, rolling-history replay, and
meta-suppression, so switching models in the live picker changes ONLY the
model -- never the prompting or the caption-safety hardening.

Model offered: gpt-6-luna (2026-10-01; it replaced gpt-5.2, which stays
accepted for saved settings: 0.68 s to the first word against 1.09 s, and a
little more unaided Romanian -- validation/results/2026-10-01-fast-models.
gpt-5-mini and gpt-5.4-nano were retired 2026-09-16; see REASONING_EFFORT) --
the low-latency end of OpenAI's line, the right fit for live captions. It is a reasoning model pinned to
its LOWEST effort ("none"; it rejects "minimal"), for the same reason
translate_claude pins Sonnet 5 to "low":
translation of a spoken sentence rarely benefits from reasoning, and every
reasoning token is caption latency.

Uses the Chat Completions endpoint over plain `requests` (already a project
dependency) -- no OpenAI SDK needed. GPT-5 reasoning models reject sampling
params (temperature/top_p), so none are sent; the `temperature` argument is
accepted for interface parity and ignored.

Requires:
    export OPENAI_API_KEY=sk-...

Usage:
    python translate_openai.py --text "Dumnezeu este bun."
    python translate_openai.py --model gpt-6-luna --target es --text "..."
"""
import argparse
import json
import os
import sys
import threading
import time

# Shared prompting/hardening -- one source of truth in translate_claude.
from translate_claude import (SYSTEM_PROMPT, LANGUAGES, TARGET_LANGUAGES, anchored_turn,
                              DEFAULT_TARGET_LANG, _strip_meta, _tag_for,
                              history_turns, system_prompt_for)

# gpt-6-sol on the default tier joined the picker 2026-10-19: the same model at
# half the fast tier's price (~$3.70 a service full-time against ~$7.40), and
# 0.96 / 1.28 s a caption (p50 / p90) against 0.74 / 0.91 on 60 sermon
# segments alternated (validation/results/2026-10-18-readability/tier.py).
# gpt-6.1-terra-fast joined 2026-10-26, its launch day: Sol's quality (idioms
# 113/136 against Sol's 107-110; 21/22 planted mis-hears with the hint against
# Sol fast's 20) at half Sol's price, 0.79 s to the first word against Sol
# fast's 0.70 (scratchpad probe, not committed). gpt-6.1-terra on the default
# tier joined the same day, as Sol did: the same model at the standard price.
OPENAI_MODELS = ("gpt-6-luna", "gpt-6-sol", "gpt-6-sol-fast", "gpt-6.1-terra", "gpt-6.1-terra-fast",
                 "gpt-5.2")

# Picker ids sent as a real model on OpenAI's fast service tier (billed at a
# premium). gpt-6-sol on the default tier is 0.69 s to the first word with a
# 1.17 s p90; fast trims that to 0.59 / 0.62 (2026-10-02), and on the mis-hear
# lab (validation/scripts/unsure_lab.py, 2026-10-05) it fixed 20/22 planted
# near-homophones unaided, 21/22 with Soniox's confidence hint.
FAST_TIER = {"gpt-6-sol-fast": "gpt-6-sol", "gpt-6.1-terra-fast": "gpt-6.1-terra"}
DEFAULT_MODEL = "gpt-6-luna"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Same live-caption timeout philosophy as translate_claude: fail fast on
# connect; the read timeout is per-chunk when streaming, so a healthy stream
# can run long but a wedged one cannot stall the caption feed.
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 25.0

# Reasoning tokens count toward the completion cap alongside output, so leave
# headroom even at minimal effort -- a truncated caption is worse than the
# few extra cents of ceiling.
MAX_COMPLETION_TOKENS = 2048

# Reasoning effort per model -- each model's LOWEST-latency setting (a spoken
# sentence rarely benefits from reasoning; every reasoning token is caption
# latency). gpt-5-mini's floor is "minimal"; gpt-5.4-nano REJECTS "minimal"
# (its allowed set is none/low/medium/high/xhigh) so it's pinned to "none",
# reasoning fully off. Unknown models fall back to "minimal".
#
# The whole current GPT-5 text line-up was probed 2026-09-16 on the app's prompt
# (3 sentences each, streaming, median time to first character, hidden
# reasoning tokens):
#
#   gpt-5.4-nano  none     0.90s   0      REMOVED 2026-09-16: 4 of 11 STT-shaped hard
#                                         cases wrong in the sweep ("at peace with God
#                                         in our sin", "The profit Isaiah"), 12/18 clitics
#   gpt-5.2       none     0.93s   0      offered until 2026-10-01 (gpt-6-luna): 148/158 auto-pass,
#                                         0 wrong, 0.78s median
#   gpt-5-nano    minimal  1.15s   0      older than 5.4-nano, no faster; not offered
#   gpt-5-mini    minimal  1.19s   0      REMOVED 2026-09-16: 5.2 beats it on every count
#   gpt-5         minimal  1.26s   0      not offered
#   gpt-5.5       none     2.50s   0      not added: 2.5s to the first word is the
#                                         latency a caption cannot spend, and it
#                                         rejects `minimal`
#
# 5.2 and 5.5 reject "minimal" and floor at "none"; 5-nano rejects "none".
REASONING_EFFORT = {
    "gpt-6-luna": "none",   # 2026-10-01: 0.68 s first word, 0.79 s caption
    "gpt-6-sol": "none",
    "gpt-6.1-terra": "none",   # rejects "minimal"
    "gpt-5.2": "none",
    # Retired ids, kept so a battery or a saved setting naming one still gets
    # the effort it was measured at rather than the "minimal" fallback 5.4-nano rejects.
    "gpt-5-mini": "minimal",
    "gpt-5.4-nano": "none",
}
_DEFAULT_EFFORT = "minimal"


def _effort_for(model):
    return REASONING_EFFORT.get(model, _DEFAULT_EFFORT)

# Last call's token accounting (best-effort diagnostic; harness threads may
# interleave). OpenAI caches prompt prefixes >= 1024 tokens automatically --
# our shared system prompt is ~1.4K tokens, so `cached` should be > 0 from the
# second call onward within the cache TTL. One stderr line per call makes the
# cache behavior visible in the server console, mirroring the [mt] log style.
LAST_USAGE = {}
# Per-THREAD copy of the counters the Translation panel's cache light needs.
# LAST_USAGE above is process-wide and best-effort, fine for a console line but
# not for a per-request answer the UI will show: the caption server is
# threaded, so two concurrent translations would report each other's counters.
# Same pattern as translate_claude._cache_stats and translate_gemini's.
_cache_stats = threading.local()


def last_cache_stats():
    """{"read", "write", "input"} for THIS thread's last OpenAI call, or None if
    it hasn't made one. Shaped to match translate_claude.last_cache_stats() so
    the server's _cache_state classifier works on it unchanged. `write` is
    always 0: OpenAI's cache is automatic and free to populate, so there is no
    creation premium to report -- a call either read from it or paid full price.
    `read` is prompt_tokens_details.cached_tokens, which lands in 128-token
    blocks and only for prompts of 1024+ tokens; 0 on the first call, after
    roughly 5-10 minutes idle, or whenever the prompt prefix changed."""
    return getattr(_cache_stats, "last", None)


def _note_usage(usage):
    if not isinstance(usage, dict):
        return
    details = usage.get("prompt_tokens_details") or {}
    LAST_USAGE.clear()
    LAST_USAGE.update(prompt=usage.get("prompt_tokens"),
                      completion=usage.get("completion_tokens"),
                      cached=details.get("cached_tokens") or 0)
    _cache_stats.last = {"read": LAST_USAGE["cached"], "write": 0,
                         "input": LAST_USAGE["prompt"] or 0,
                         "output": LAST_USAGE["completion"] or 0}
    print(f"[openai] tokens: prompt {LAST_USAGE['prompt']} "
          f"(cached {LAST_USAGE['cached']}) + completion {LAST_USAGE['completion']}",
          file=sys.stderr)


_session = None


def _get_session():
    """One shared requests.Session (connection pooling/keep-alive), matching
    the server's pattern for the ElevenLabs calls."""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def warm():
    """Pre-open the pooled TLS connection to the API host (unauthenticated
    GET; any response counts -- urllib3 pools it with no idle-time expiry).
    Called at server startup so the first GPT translation doesn't pay the
    handshake. Best-effort."""
    try:
        from urllib.parse import urlsplit
        u = urlsplit(OPENAI_URL)
        _get_session().get(f"{u.scheme}://{u.netloc}/",
                           timeout=(CONNECT_TIMEOUT_S, 5.0))
    except Exception:
        pass


def _build_payload(text, history, model, target_lang, stream, draft=None, pair=None,
                   expressive=False):
    """Chat Completions payload with the shared system prompt and the same
    tagged rolling history translate_claude replays."""
    tag = _tag_for(target_lang)
    messages = [{"role": "system", "content": system_prompt_for(pair, expressive)}]
    messages += history_turns(history, tag)
    messages.append({"role": "user", "content": anchored_turn(tag, text, draft)})
    real = FAST_TIER.get(model, model)
    payload = {
        "model": real,
        "messages": messages,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "reasoning_effort": _effort_for(real),
        "stream": stream,
    }
    if model in FAST_TIER:
        payload["service_tier"] = "fast"
    if stream:
        # Final SSE chunk then carries usage (prompt/cached/completion tokens)
        # so cache behavior is observable on the live streaming path too.
        payload["stream_options"] = {"include_usage": True}
    return payload


def _post(payload, stream):
    """POST to the Chat Completions endpoint; normalize every failure mode to
    RuntimeError (the caption server maps that to a retryable NDJSON error
    line, same as a Claude failure)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY environment variable is not set")
    import requests
    try:
        r = _get_session().post(
            OPENAI_URL, json=payload,
            headers={"Authorization": f"Bearer {key}"},
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S), stream=stream)
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenAI API: {exc}") from exc
    if r.status_code != 200:
        # Surface the API's own message (model typo, quota, revoked key) --
        # it lands in the Debug log where it's actually diagnosable.
        try:
            detail = (r.json().get("error") or {}).get("message") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        r.close()
        raise RuntimeError(f"OpenAI API: HTTP {r.status_code}: {detail}")
    return r


def translate(text: str, history=None, model: str = DEFAULT_MODEL,
              temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
              pair=None) -> str:
    """Translate one segment INTO `target_lang`. Same contract as
    translate_claude.translate(); `temperature` is ignored (GPT-5 reasoning
    models reject sampling params)."""
    r = _post(_build_payload(text, history, model, target_lang, False, pair=pair),
              stream=False)
    try:
        j = r.json()
    except ValueError as exc:
        raise RuntimeError(f"OpenAI API: non-JSON response") from exc
    finally:
        r.close()
    _note_usage(j.get("usage"))
    choice = (j.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        print("[openai] WARNING: translation truncated at max_completion_tokens",
              file=sys.stderr)
    result = ((choice.get("message") or {}).get("content") or "").strip()
    return _strip_meta(result)


def translate_stream(text: str, history=None, model: str = DEFAULT_MODEL,
                     temperature: float = 0.2, target_lang: str = DEFAULT_TARGET_LANG,
                     draft: str = None, pair=None, expressive=False):
    """Like translate(), but yields the response text incrementally (SSE
    stream) -- same contract as translate_claude.translate_stream(): raw model
    output only, no meta-suppression (the caller does that on the full text)."""
    import requests
    r = _post(_build_payload(text, history, model, target_lang, True, draft, pair,
                             expressive),
              stream=True)
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
            _note_usage(j.get("usage"))   # include_usage: final data chunk before [DONE]
            choice = (j.get("choices") or [{}])[0]
            finish = choice.get("finish_reason") or finish
            delta = (choice.get("delta") or {}).get("content")
            if delta:
                yield delta
    except requests.RequestException as exc:
        raise RuntimeError(f"OpenAI API: {exc}") from exc
    finally:
        r.close()
    if finish == "length":
        print("[openai] WARNING: translation truncated at max_completion_tokens",
              file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=None)
    ap.add_argument("--infile", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL, choices=sorted(OPENAI_MODELS))
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

    print(f"[openai] {args.model} (-> {LANGUAGES[args.target]}, OpenAI API)", file=sys.stderr)
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
    print(f"\n=== {LANGUAGES[args.target]} (GPT translation) ===")
    print(out)
    print(f"\n[timing] {dt:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
