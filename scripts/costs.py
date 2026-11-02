"""The running cost estimate behind the cost box: what this server run has spent
on every API, at the list prices in config/prices.json.

An estimate, not a bill: token counts are the providers' own (each call's
usage block); audio and characters are what we sent. Not counted: Gemini's
explicit-cache storage (cents an hour) and subscription plans (ElevenLabs
credits are priced at the pay-as-you-go rate).
"""
import json
import os
import threading

_PRICES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config", "prices.json")
try:
    with open(_PRICES_FILE, encoding="utf-8") as f:
        P = json.load(f)
except (OSError, ValueError):
    P = {}

_lock = threading.Lock()
_usd = {}          # line -> dollars
_unpriced = set()  # models we saw but have no price for


def _add(line, usd):
    with _lock:
        _usd[line] = _usd.get(line, 0.0) + usd


def llm(model, prompt=0, cached=0, write=0, output=0, line=None):
    """One LLM call. `prompt` is the WHOLE prompt (cached part included), as
    the server's cache stats report it for every provider."""
    p = (P.get("llm") or {}).get(model)
    if not p:
        _unpriced.add(model)
        return
    fresh = max(0, (prompt or 0) - (cached or 0) - (write or 0))
    usd = (fresh * p["in"] + (cached or 0) * p.get("cached", p["in"])
           + (write or 0) * p.get("write", p["in"]) + (output or 0) * p["out"]) / 1e6
    _add(line or "Translator", usd)


def stt_seconds(secs):
    _add("Soniox", secs / 3600 * (P.get("soniox") or {}).get("stt_per_hour", 0))


def soniox_text_tokens(n):
    """Soniox's own translation, billed as output text tokens."""
    _add("Soniox", n * (P.get("soniox") or {}).get("text_out_per_m", 0) / 1e6)


def tts(provider, chars, el_model=None):
    if provider == "soniox":
        sx = P.get("soniox") or {}
        _add("Voice", chars / max(1, sx.get("tts_chars_per_hour", 54000)) * sx.get("tts_per_hour", 0))
    elif provider == "elevenlabs":
        per = (P.get("elevenlabs_tts_per_1k_chars") or {}).get(el_model or "", 0.08)
        _add("Voice", chars / 1000 * per)


def ear_seconds(name, secs):
    _add("Extra ears", secs / 3600 * (P.get("ears_per_hour") or {}).get(name, 0))


def snapshot():
    with _lock:
        lines = {k: round(v, 4) for k, v in _usd.items()}
    return {"total": round(sum(lines.values()), 4), "lines": lines,
            "unpriced": sorted(_unpriced), "as_of": P.get("as_of")}
