#!/usr/bin/env python3
"""Model pins: what each pinned model WAS when it was last validated, and a
check that says whether it still is.

Why this exists
---------------
A pinned model id fails in two different ways, and only one of them is loud:

  HARD RETIREMENT  the id 404s. Self-announcing; you find out immediately.
  SILENT REMAP     the id still works, still returns 200, still returns good
                   translations -- but is now served by a different model.

The second one is the dangerous one, and it has already happened here: a pin
named "...-non-reasoning" was withdrawn from xAI's catalogue and quietly served
by a reasoning model, which nothing noticed because every request succeeded. It
was slower and pricier, and the one property the name promised was gone.

So an existence check ("is the id in the catalogue?") is not enough. What is
recorded here is a FINGERPRINT -- the properties the app actually depends on --
and drift in any of them is reported. `translate_xai.check_default_model()` does
exactly this for one provider and one model; this generalises it.

Advisory only. Nothing here ever raises into the caption path, blocks startup,
or changes which model is used: a wrong-but-working model is something to tell
the operator about, not a reason to stop translating mid-service.

CLI
---
    python scripts/model_pins.py            # check pins, print a report
    python scripts/model_pins.py --adopt    # rewrite pins from what is live NOW
                                            # (i.e. "I have validated today's
                                            # catalogue; treat this as the
                                            # baseline")
"""
import json
import os
import re
import sys

_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
PINS_FILE = os.path.join(_CONFIG, "model_pins.json")

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 8.0

# How much slower than its pinned baseline a model may run before the runtime
# alarm fires. Generous on purpose: prompt length, batch size and network all
# move latency around, and a false alarm mid-service is worse than a late one.
# A model that silently started reasoning does not drift 20% -- it multiplies.
LATENCY_ALARM_RATIO = 2.0
LATENCY_MIN_SAMPLES = 8


def load_pins():
    """The pin file as {model_id: pin dict}. Missing/malformed -> {} (the whole
    feature is advisory, so a broken pin file must not cost the operator the
    app)."""
    try:
        with open(PINS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("pins") or {}
    except Exception:
        return {}


def save_pins(pins):
    payload = {
        "_comment": ("What each pinned model looked like when it was last "
                     "validated. Drift against this is reported at startup; see "
                     "scripts/model_pins.py. Edit latency_p50_s to set the "
                     "runtime latency alarm's baseline, or run the script with "
                     "--adopt to re-record identity from the live catalogues."),
        "pins": pins,
    }
    os.makedirs(_CONFIG, exist_ok=True)
    with open(PINS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def _get_json(url, key_env, extra_headers=None):
    """GET a provider's model endpoint. Returns parsed JSON or None -- never
    raises, and never waits long: this runs off the startup path of a live
    captioning app, and a provider being slow must not delay the service."""
    key = os.environ.get(key_env)
    if not key:
        return None
    try:
        import requests
        headers = {"Authorization": f"Bearer {key}"}
        headers.update(extra_headers or {})
        # A provider can DROP a default header by mapping it to None, not just
        # override it: Google rejects the request outright (401) if a Bearer
        # Authorization header rides along with its own x-goog-api-key.
        headers = {k: v for k, v in headers.items() if v is not None}
        r = requests.get(url, headers=headers,
                         timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


# Both Anthropic and OpenAI are asked about ONE id rather than handed a list to
# search, because the pins are deliberately ALIASES ("claude-haiku-4-5", not
# "claude-haiku-4-5-20251001") -- a dated id is the thing that rots. An alias
# never appears in the catalogue listing, which keys on canonical ids, so a
# list-and-search check reports the app's own default model as missing on every
# startup. The per-id endpoint resolves the alias and reports what it actually
# resolves TO, which is both correct and the more interesting answer: a change
# in `served_as` is precisely the silent remap this file exists to catch.
def _anthropic_fingerprint(model_id):
    j = _get_json(f"https://api.anthropic.com/v1/models/{model_id}",
                  "ANTHROPIC_API_KEY",
                  {"anthropic-version": "2023-06-01",
                   "x-api-key": os.environ.get("ANTHROPIC_API_KEY", "")})
    if j is None:
        return None
    if not j.get("id"):
        return {"present": False}
    fp = {"present": True, "display_name": j.get("display_name")}
    if j.get("id") != model_id:
        fp["served_as"] = j["id"]
    return fp


def _openai_fingerprint(model_id):
    j = _get_json(f"https://api.openai.com/v1/models/{model_id}", "OPENAI_API_KEY")
    if j is None:
        return None
    if not j.get("id"):
        return {"present": False}
    fp = {"present": True, "owned_by": j.get("owned_by")}
    if j.get("id") != model_id:
        fp["served_as"] = j["id"]
    return fp


def _xai_fingerprint(model_id):
    """xAI is the richest case, and the one that has actually bitten: the
    catalogue carries the reasoning property the pin's NAME promises, and an
    alias has to be resolved against what the API really serves."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import translate_xai as tx
    except Exception:
        return None
    models = tx.list_models()
    if not models or any(m.get("fallback") for m in models):
        return None                       # catalogue unavailable -- unknown, not drifted
    entry = next((m for m in models if m["id"] == model_id), None)
    served = None
    if entry is None:
        # An alias is legitimately absent from the canonical id list, so ask the
        # API what it actually serves for this name -- the only authoritative
        # answer, and the check the silent remap defeated.
        served = tx._resolve_served_model(model_id)
        if served is None:
            # Not in the catalogue AND unresolvable -- but the picker drops
            # reasoning models, so absence may just mean "we hid it".
            return {"present": False,
                    "reasoning": tx.is_reasoning_model(model_id) or None}
        entry = next((m for m in models if m["id"] == served), None)
    fp = {"present": True, "non_reasoning": bool(entry and entry["non_reasoning"])}
    if served and served != model_id:
        fp["served_as"] = served
    if entry and entry.get("context_length"):
        fp["context_length"] = entry["context_length"]
    return fp


def _gemini_fingerprint(model_id):
    """Gemini's drift has a different shape from xAI's. The id does not get
    remapped -- it keeps working -- and what moves underneath it is the
    REASONING FLOOR: gemini-3.7/3.8-flash reject the "minimal" thinking level
    that 3.5/3.6 accept, and a model that quietly stops honouring its pinned
    level starts spending ~490 hidden tokens before its first word (see
    translate_gemini). So the pinned level is fingerprinted alongside the
    catalogue identity, and a change in either is reported."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    j = _get_json(f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}",
                  "GEMINI_API_KEY",
                  {"x-goog-api-key": key, "Authorization": None})
    if j is None:
        return None
    if not j.get("name"):
        return {"present": False}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import translate_gemini as tg
        level = tg._thinking_for(model_id)
    except Exception:
        level = None
    fp = {"present": True, "version": j.get("version"), "thinking_level": level}
    served = (j.get("name") or "").replace("models/", "")
    if served and served != model_id:
        fp["served_as"] = served
    return fp


_PROVIDERS = {
    "anthropic": _anthropic_fingerprint,
    "openai": _openai_fingerprint,
    "xai": _xai_fingerprint,
    "gemini": _gemini_fingerprint,
}

# Fields whose change is worth a warning. Deliberately NOT everything a provider
# returns: `created`/`created_at` move for harmless catalogue rewrites, and
# alarming on those would teach the operator to ignore the alarm.
_MATERIAL = ("present", "non_reasoning", "served_as", "context_length",
             "display_name", "owned_by", "version", "thinking_level")


def check_pins(pins=None):
    """Compare each pin against what its provider serves now.

    Returns a list of {model, provider, status, detail}, where status is:
      ok       -- matches the fingerprint it was validated against
      missing  -- the provider no longer lists it (repin needed)
      drifted  -- still there, but a property the app depends on has changed
      unknown  -- no key, or the provider's catalogue was unreachable
    """
    pins = load_pins() if pins is None else pins
    out = []
    for model_id, pin in sorted(pins.items()):
        provider = pin.get("provider", "")
        fn = _PROVIDERS.get(provider)
        if fn is None:
            out.append({"model": model_id, "provider": provider,
                        "status": "unknown", "detail": "no such provider"})
            continue
        live = fn(model_id)
        if live is None:
            out.append({"model": model_id, "provider": provider,
                        "status": "unknown", "detail": "catalogue unavailable"})
            continue
        if not live.get("present"):
            out.append({"model": model_id, "provider": provider,
                        "status": "missing",
                        "detail": "not listed by the provider any more"})
            continue
        seen = pin.get("seen") or {}
        changes = [f"{k}: {seen.get(k)!r} -> {live.get(k)!r}"
                   for k in _MATERIAL
                   if k in seen and k in live and seen.get(k) != live.get(k)]
        out.append({"model": model_id, "provider": provider,
                    "status": "drifted" if changes else "ok",
                    "detail": "; ".join(changes) or "unchanged"})
    return out


def report(results=None, out=sys.stderr):
    """Print the check, loudest first. Returns True if nothing needs attention."""
    results = check_pins() if results is None else results
    bad = [r for r in results if r["status"] in ("missing", "drifted")]
    for r in bad:
        print(f"[pins] WARNING: {r['model']} ({r['provider']}) {r['status'].upper()}"
              f" -- {r['detail']}", file=out, flush=True)
    unknown = [r for r in results if r["status"] == "unknown"]
    if unknown and not bad:
        print(f"[pins] {len(unknown)} model(s) unchecked ("
              + ", ".join(r["model"] for r in unknown) + ")", file=out, flush=True)
    return not bad


def latency_verdict(model_id, observed_p50, pins=None):
    """Is `observed_p50` (seconds) out of line with this model's pinned baseline?

    Returns (status, baseline) with status in ok|slow|unpinned. This is the
    runtime half of the same idea: a model that has silently become a reasoning
    model does not announce itself, but it cannot hide a doubled time-to-first-
    word from a rolling median.
    """
    pin = (load_pins() if pins is None else pins).get(model_id) or {}
    base = pin.get("latency_p50_s")
    if not base:
        return "unpinned", None
    return ("slow" if observed_p50 > base * LATENCY_ALARM_RATIO else "ok"), base


def adopt():
    """Re-record every pin's fingerprint from what is live now. Use after
    deliberately validating a model, never to silence a warning you don't
    understand -- adopting a drift is how the previous silent remap survived."""
    pins = load_pins()
    for model_id, pin in pins.items():
        fn = _PROVIDERS.get(pin.get("provider", ""))
        live = fn(model_id) if fn else None
        if live is None:
            print(f"[pins] skipped {model_id}: catalogue unavailable", file=sys.stderr)
            continue
        if not live.get("present"):
            # Recording "absent" as the validated baseline would make the check
            # pass forever on a model that isn't there -- the failure mode this
            # file exists to prevent, arrived at by way of the fix for it.
            print(f"[pins] REFUSED {model_id}: not present -- repin it, don't "
                  f"adopt its absence", file=sys.stderr)
            continue
        pin["seen"] = {k: v for k, v in live.items() if k in _MATERIAL}
        print(f"[pins] adopted {model_id}: {pin['seen']}", file=sys.stderr)
    save_pins(pins)
    return pins


def _dotenv_value(v):
    """A .env value as app/server.py's _load_dotenv reads it: a quoted value
    verbatim, else up to an inline comment (a "#" at the start or after
    whitespace) -- so `KEY=   # note` is blank, not the note."""
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        return v[1:end] if end != -1 else v[1:]
    return re.split(r"(?:^|\s)#", v, maxsplit=1)[0].strip()


def _load_dotenv():
    """CLI convenience only: the server loads .env itself before its imports (see
    app/server.py), but run standalone this script would otherwise find no keys
    and report every pin as 'unknown' -- which looks like a passing check."""
    path = os.path.join(os.path.dirname(_CONFIG), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), _dotenv_value(v))


def main():
    _load_dotenv()
    if "--adopt" in sys.argv:
        adopt()
        return 0
    results = check_pins()
    if not results:
        print(f"[pins] no pins recorded ({PINS_FILE})", file=sys.stderr)
        return 0
    for r in results:
        print(f"  {r['status']:8} {r['model']:34} {r['detail']}")
    return 0 if report(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
