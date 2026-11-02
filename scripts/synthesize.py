#!/usr/bin/env python3
"""Stage 3: translated text -> natural speech via ElevenLabs or Soniox (cloud TTS).

Two interchangeable backends behind one call. The live pipeline uses
`synthesize_stream()`, which yields mp3 as the provider produces it; the CLI and
anything wanting samples use `synthesize()`, which asks for PCM instead and
decodes to float32 at SAMPLE_RATE.

Why mp3 on the live path: Soniox's tts-rt endpoint STREAMS -- first audio byte
lands ~380ms in and generation then runs ~1.2x realtime, so playback started at
the first byte never catches up with the generator (measured lead grows from
+0.2s to +2.9s over a long sentence). Reading the whole response before speaking
threw all of that away and made a long sentence wait ~11s. Delivering it
progressively means the browser must accept partial audio, which it does through
MediaSource -- and MediaSource takes a container, not raw PCM (audio/wav is not
a supported MSE type; audio/mpeg is). mp3 also costs ~3x less bandwidth than the
PCM it replaces.

ElevenLabs (default): one voice slot, any target language -- its multilingual
models speak whatever language `text` is in from a single voice id (CT_EL_VOICE),
so no per-language voice mapping is needed. Bills per character; needs
ELEVENLABS_API_KEY. Live expressivity is stability / style / speed (Eleven v4:
stability only).

Soniox: reuses the SONIOX_API_KEY already needed for STT, so there is no second
credential to configure. Bills per generated hour (~4-8x cheaper than
ElevenLabs at typical speech rates) rather than per character, which makes cost
insensitive to how verbose a translation runs. Unlike ElevenLabs it does NOT
sniff the language out of the text -- `language` is a required field and a wrong
or unsupported code is a hard 400 -- so the caller must pass the target language
it already knows. Its expressivity surface is speed + reduce_silence; there is
no stability/style equivalent, which is why the UI shows a different set of
knobs per provider instead of pretending one set fits both.

Usage (quick check):
    python synthesize.py --text "Hello" --out out.wav --voice <EL_VOICE_ID>
    python synthesize.py --text "Hello" --out out.wav --provider soniox --lang en
"""
import argparse
import os
import sys
import time

import numpy as np
# soundfile is imported lazily in main(): the live pipeline ships the provider's
# own encoded bytes through, so the server never needs it.

SAMPLE_RATE = 24000             # PCM rate for the sample-returning path
# The live path's container. mp3 because MediaSource in the browser needs a real
# container (it rejects audio/wav) and both backends emit mp3 natively, so the
# bytes travel provider -> browser without us re-encoding anything.
AUDIO_FMT = "mp3"
AUDIO_MIME = "audio/mpeg"
EL_TTS_MODEL = os.environ.get("CT_EL_TTS_MODEL", "eleven_v4_turbo")
EL_VOICE = os.environ.get("CT_EL_VOICE", "")   # single voice id, any target language
# Eleven v4 (and v4 Turbo) takes two voice settings, stability and similarity.
# The API accepts style and speed but ignores them -- speed 0.7 and 1.2 gave
# clips of identical length on 2026-09-28 -- so a v4 request carries only these.
EL_V4_SETTINGS = ("stability", "similarity_boost")
# Models that read an [audio tag] as a delivery direction. The older ones (v2,
# flash, turbo v2.5) do not, and may say the word, so they never get one.
EL_TAG_MODELS = ("eleven_v3", "eleven_v4")


def el_takes_tags(model=None):
    """Whether this ElevenLabs model follows audio tags (the Expression box)."""
    return (model or EL_TTS_MODEL).startswith(EL_TAG_MODELS)


def el_takes_context(model=None):
    """Whether this ElevenLabs model accepts previous_text / previous_request_ids
    (request stitching). v3 400s on them ("not yet supported", 2026-10-01); v4,
    v4 Turbo and the v2.x models take them."""
    return not (model or EL_TTS_MODEL).startswith("eleven_v3")

# Soniox TTS. tts-rt-v1 is deprecated and removed 2026-08-31, so v2 is the only
# model worth pinning as the default; v1 also rejects reduce_silence outright.
SX_TTS_MODEL = os.environ.get("CT_SX_TTS_MODEL", "tts-rt-v2")
SX_VOICE = os.environ.get("CT_SX_VOICE", "Adrian")   # built-in voice name, not an opaque id
SX_TTS_URL = "https://tts-rt.soniox.com/tts"
# Which provider the pipeline uses when a request doesn't name one.
TTS_PROVIDER = (os.environ.get("CT_TTS_PROVIDER", "elevenlabs") or "elevenlabs").strip().lower()

EL_TTS_HOST = "https://api.elevenlabs.io"
# Split connect/read deadlines. One combined number lets a stalled TCP connect
# occupy a synthesis slot for the whole read budget; a connect that hasn't
# landed in 3s never will, while generation itself legitimately takes seconds.
TIMEOUT = (3, 30)

_session = None


def get_session():
    """One shared requests.Session (connection pooling/keep-alive) instead of a
    fresh TCP+TLS handshake per call -- TTS fires on every spoken segment, so
    that overhead would otherwise be paid over and over for no reason.

    Deliberately THE session for the whole process, not just for synthesis: the
    server's ElevenLabs subscription/voices polling shares it, so those calls
    keep the same api.elevenlabs.io connection warm that the next clip will use
    instead of maintaining a second, redundant pool to the same host."""
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
    return _session


def prewarm(provider=None):
    """Open (DNS + TCP + TLS) a pooled connection to a TTS host before anything
    needs it, so the first spoken sentence of a service doesn't pay the
    handshake on top of synthesis. Called when the operator starts listening.

    Deliberately not a real request: it borrows a connection from the pool,
    connects it, and puts it back ready for the first live call -- no API call,
    nothing billed, and no key required. Entirely best-effort; a failure here
    just means the first clip pays what it pays today."""
    hosts = []
    if provider in (None, "soniox"):
        hosts.append(SX_TTS_URL)
    if provider in (None, "elevenlabs"):
        hosts.append(EL_TTS_HOST)
    warmed = []
    for url in hosts:
        host = url.split("/")[2]
        conn = pool = None
        try:
            pool = get_session().get_adapter(url).poolmanager.connection_from_url(url)
            conn = pool._get_conn(timeout=TIMEOUT[0])
            # _get_conn hands back a LIVE connection when the pool already holds
            # one. Reconnecting it would drop that socket on the floor and open
            # a second, so only handshake when this one really is cold.
            if _is_connected(conn):
                warmed.append(host + " (already warm)")
            else:
                conn.connect()
                warmed.append(host)
        except Exception as exc:
            print(f"[tts ] prewarm {host} skipped: {str(exc)[:80]}",
                  file=sys.stderr, flush=True)
        finally:
            if pool is not None and conn is not None:
                pool._put_conn(conn)     # back in the pool for the first real call
    return warmed


def _is_connected(conn):
    """Whether a pooled urllib3 connection already has a live socket. `is_connected`
    is urllib3 2.x; the `.sock` fallback covers 1.x."""
    flag = getattr(conn, "is_connected", None)
    return bool(flag) if flag is not None else getattr(conn, "sock", None) is not None


def _elevenlabs_tts(text, voice_id, settings=None, model=None, fmt=AUDIO_FMT,
                   tag=None, context=None):
    """ElevenLabs cloud TTS -> a streaming Response of `fmt` audio.

    `settings` is an optional voice_settings dict (stability / style / speed) and
    `model` overrides the model_id (e.g. eleven_v3 for more expression) -- both
    for live control; v4 gets only the settings it takes (EL_V4_SETTINGS).
    `tag` is a delivery tag ("gently") from the translator: it goes in front of
    the text sent here and nowhere else, and only to a model that reads tags.

    `context` is the running context of what this voice said just before:
    {"previous_text": str, "previous_request_ids": [up to 3 ids]}. A caption is
    one packet of a sentence the speaker is still saying, and without it every
    clip is read as if it opened a new paragraph -- its own pitch reset, its
    own falling end. With it the clip continues the voice it follows. Costs
    ~10 ms of first byte (v4 Turbo, 2026-10-01). A request the API refuses
    because of its context (an id that expired or came from another voice) is
    sent again without it: a clip that starts fresh beats a silent one.

    This is the /stream endpoint, so audio arrives as it is generated rather
    than as a finished clip. First byte for a 115-character caption, measured
    from the Pi 2026-09-28 (whole-clip endpoint -> /stream): v4 Turbo 1.3 ->
    0.25 s, v4 2.4 -> 0.7 s, v3 4.9 -> 0.65 s, flash 0.36 -> 0.19 s, v2 1.5 ->
    1.5 s. Every model generates faster than it speaks, so playback started at
    the first byte does not run dry."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    if not voice_id:
        raise RuntimeError("ElevenLabs voice id not set (CT_EL_VOICE)")
    model = model or EL_TTS_MODEL
    if tag and el_takes_tags(model):
        text = f"[{tag}] {text}"
    body = {"text": text, "model_id": model}
    if settings:
        if model.startswith("eleven_v4"):
            settings = {k: v for k, v in settings.items() if k in EL_V4_SETTINGS}
        body["voice_settings"] = settings
    ctx = {k: v for k, v in (context or {}).items() if v} if el_takes_context(model) else {}

    def post(b):
        return get_session().post(
            f"{EL_TTS_HOST}/v1/text-to-speech/{voice_id}/stream",
            params={"output_format": "mp3_44100_128" if fmt == "mp3" else f"pcm_{SAMPLE_RATE}"},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json=b,
            timeout=TIMEOUT,
            stream=True,
        )
    r = post({**body, **ctx})
    if ctx and r.status_code in (400, 422):
        print(f"[tts] ElevenLabs refused the running context ({r.status_code}: "
              f"{r.text[:120]}); sending this clip without it", flush=True)
        r.close()
        r = post(body)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs TTS {r.status_code}: {r.text[:200]}")
    return r


def _soniox_tts(text, voice_id, language, settings=None, model=None, fmt=AUDIO_FMT):
    """Soniox cloud TTS -> a streaming Response of `fmt` audio.

    This endpoint genuinely streams: bytes start arriving ~380ms in and keep
    coming at ~1.2x realtime, so the caller should consume it progressively
    rather than waiting for the body. `settings` carries the Soniox-side knobs
    (speed, reduce_silence); `model` overrides the model id."""
    key = os.environ.get("SONIOX_API_KEY")
    if not key:
        raise RuntimeError("SONIOX_API_KEY not set")
    if not voice_id:
        raise RuntimeError("Soniox voice not set (CT_SX_VOICE)")
    # Soniox will not infer this and 400s on a bad code, so fail with a message
    # that names the real cause instead of relaying an opaque API error.
    if not language:
        raise RuntimeError("Soniox TTS needs a target language code")
    body = {
        "model": model or SX_TTS_MODEL,
        "language": language,
        "voice": voice_id,
        "audio_format": "mp3" if fmt == "mp3" else "pcm_s16le",
        "text": text,
    }
    if fmt != "mp3":
        body["sample_rate"] = SAMPLE_RATE   # only meaningful for the raw formats
    if settings:
        # Only send the optional knobs when they differ from the API defaults:
        # reduce_silence is rejected outright by models that don't support it
        # (v1), so an unconditional `false` would break those for no gain.
        if settings.get("speed") is not None:
            body["speed"] = settings["speed"]
        if settings.get("reduce_silence"):
            body["reduce_silence"] = True
    r = get_session().post(
        SX_TTS_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=TIMEOUT,
        stream=True,
    )
    if r.status_code != 200:
        # Soniox reports failures as JSON with a human-readable error_message;
        # surface that rather than a bare status code.
        detail = r.text[:200]
        try:
            detail = r.json().get("error_message") or detail
        except ValueError:
            pass
        raise RuntimeError(f"Soniox TTS {r.status_code}: {detail}")
    return r


def _open_stream(text, el_settings=None, el_voice=None, el_model=None,
                 provider=None, sx_settings=None, sx_voice=None, sx_model=None,
                 language=None, fmt=AUDIO_FMT, el_tag=None, el_context=None):
    """Start a synthesis request and hand back the streaming Response.

    Defaults to ElevenLabs (CT_TTS_PROVIDER / CT_EL_VOICE). `el_voice`/`sx_voice`
    (from the UI's voice dropdown) override the env default per call. `language`
    is the target language code -- ignored by ElevenLabs, whose multilingual
    model reads it off the text, and required by Soniox. `el_tag` is the
    translator's delivery tag, which Soniox has no use for."""
    if (provider or TTS_PROVIDER) == "soniox":
        return _soniox_tts(text, sx_voice or SX_VOICE, language, sx_settings,
                           sx_model, fmt)
    return _elevenlabs_tts(text, el_voice or EL_VOICE, el_settings, el_model, fmt,
                           el_tag, el_context)


def synthesize_stream(text, chunk_bytes=16384, stats=None, **kw):
    """Text -> encoded audio chunks (AUDIO_FMT), yielded as the provider sends them.

    The live pipeline's entry point. Both providers begin yielding long before
    the clip has finished generating (Soniox roughly a third of a second in,
    ElevenLabs 0.2-0.7 s in on all but v2), which is the whole point.

    Pass a dict as `stats` to have this record where the time went, filled in by
    the time the first chunk is yielded:
      open_ms     request sent -> response headers back
      ttfb_ms     request sent -> first audio chunk in hand
      first_bytes size of that first chunk
    open_ms and ttfb_ms are separated because they fail differently: a slow
    open_ms is the provider thinking (or a cold connection), while a gap between
    the two is audio we are waiting on. first_bytes settles the third
    possibility -- if it comes back at exactly chunk_bytes, the wait was our own
    HTTP layer filling a buffer and not the provider being slow at all."""
    t0 = time.monotonic()
    r = _open_stream(text, **kw)
    if stats is not None:
        stats["open_ms"] = round((time.monotonic() - t0) * 1000)
        # ElevenLabs names every generation; the next clip can cite it as context
        stats["request_id"] = r.headers.get("request-id")
    try:
        for chunk in r.iter_content(chunk_bytes):
            if chunk:
                if stats is not None and "ttfb_ms" not in stats:
                    stats["ttfb_ms"] = round((time.monotonic() - t0) * 1000)
                    stats["first_bytes"] = len(chunk)
                yield chunk
    finally:
        r.close()


def synthesize_bytes(text, **kw):
    """Text -> one complete clip of AUDIO_FMT audio (Replay, and anything that
    wants the whole thing rather than a progressive feed)."""
    return b"".join(synthesize_stream(text, **kw))


def synthesize(text, **kw):
    """Text -> speech as float32 @ 24 kHz, for callers that want samples rather
    than encoded audio (the CLI below, and anything doing numpy work on a clip).

    Asks the provider for PCM regardless of AUDIO_FMT, so this stays a plain
    sample buffer and never needs an mp3 decoder."""
    kw["fmt"] = "pcm"
    pcm = b"".join(synthesize_stream(text, **kw))
    # A stray odd byte would be half a sample -- drop it rather than fail.
    pcm = pcm[:len(pcm) - (len(pcm) % 2)]
    return np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0


def main() -> int:
    import soundfile as sf
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", default=None)
    ap.add_argument("--infile", default=None)
    ap.add_argument("--out", required=True, help="output WAV path")
    ap.add_argument("--provider", default=None, choices=("elevenlabs", "soniox"),
                    help=f"TTS backend (else CT_TTS_PROVIDER, currently {TTS_PROVIDER})")
    ap.add_argument("--voice", default=None,
                    help="voice: ElevenLabs voice id (else CT_EL_VOICE), "
                         "or Soniox voice name (else CT_SX_VOICE)")
    ap.add_argument("--model", default=None,
                    help="model id (else CT_EL_TTS_MODEL / CT_SX_TTS_MODEL)")
    ap.add_argument("--lang", default=None,
                    help="target language code -- required by Soniox, ignored by ElevenLabs")
    ap.add_argument("--speed", type=float, default=None, help="Soniox speaking rate (0.7-1.3)")
    ap.add_argument("--reduce-silence", action="store_true",
                    help="Soniox: shorten pauses between words (tts-rt-v2 only)")
    args = ap.parse_args()

    if args.text is not None:
        text = args.text
    elif args.infile is not None:
        with open(args.infile, encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        print("no input text", file=sys.stderr)
        return 1

    prov = args.provider or TTS_PROVIDER
    if prov == "soniox":
        print(f"[tts ] Soniox voice={args.voice or SX_VOICE} "
              f"model={args.model or SX_TTS_MODEL} lang={args.lang}", file=sys.stderr)
    else:
        print(f"[tts ] ElevenLabs voice={args.voice or EL_VOICE} "
              f"model={args.model or EL_TTS_MODEL}", file=sys.stderr)
    t0 = time.time()
    audio = synthesize(text, provider=prov, language=args.lang,
                       el_voice=args.voice, el_model=args.model,
                       sx_voice=args.voice, sx_model=args.model,
                       sx_settings={"speed": args.speed,
                                    "reduce_silence": args.reduce_silence})
    dt = time.time() - t0
    sf.write(args.out, audio, SAMPLE_RATE)
    dur = len(audio) / SAMPLE_RATE
    rtf = dt / max(dur, 1e-3)
    print(f"[tts ] wrote {args.out}  ({dur:.1f}s audio, synth {dt:.1f}s, RTF {rtf:.2f})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
