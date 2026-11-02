#!/usr/bin/env python3
"""Router: should Soniox's own translation of a segment go on the wall, or
should a translator model redo it?

Two questions to OpenAI's Decisions API (gpt-6-luna), asked in parallel
because each wants a different amount of context:

  gate       -- is the English a plain, faithful, decent translation of the
                Romanian? (three_prof; 4 earlier segments). Catches literal
                idioms, garbled English and offensive words.
  soundalike -- is a word in the Romanian probably a mis-hearing of a
                similar-sounding word that fits the background better?
                (12 earlier segments). Catches Rusalii -> Ierusalim and kin.

Route if P(good) < 1 - th on the gate, P(cuss word) > 0.5 (SENS_Q, same call) or
P(misheard) > th, th being CT_ROUTER_THRESHOLD (server.py ROUTER_TH; 0.5 until
2026-10-19, 0.3 until 2026-10-28, 0.2 until 2026-10-30, now 0.3 again: validation/results/2026-10-28-router-threshold). Measured 2026-10-08 at 0.5
(validation/results/2026-10-08-luna-gate/, luna_gate_lab / luna_context_lab /
luna_combo_lab): on 200 Roman segments it caught 11 of 14 bad translations and
sent on 4 of 186 good ones; 11 of 18 real mis-hears; 8 of 8 offensive lines
with Bible words left alone; ~0.1 s a call (live 2026-10-08: 0.15-0.33 s,
~0.5 s cold). SENS_Q narrowed to cuss words 2026-10-09 (see there). Prompts
are the measured ones word for word -- change them and re-measure.
"""
import os
import time
import concurrent.futures as cf

import requests

import costs

MODEL = os.environ.get("CT_ROUTER_MODEL", "gpt-6-luna")
URL = "https://api.openai.com/v1/decisions"
GATE_CTX, SL_CTX = 4, 12

_NOTE = ("A Romanian sermon in a church is translated live into English by a fast speech translator, "
         "one short segment at a time, and projected on a screen. ")
_GOOD = ("good: a plain, natural English rendering that a listener would understand and that fits "
         "what came before")
_BAD = ("bad: misleading, garbled, incomplete or oddly out of place -- e.g. an idiom or regional word "
        "translated word for word into something strange, a phrase that makes no sense in a church, "
        "or a meaning that clashes with the context")
_PROF = (" Also bad: any swear word, crude or vulgar language, slur or other wording that would be "
         "offensive on a church screen (Bible words like devil, hell, harlot, naked or cursed are fine "
         "when the Romanian means them).")
_MISMATCH = ("context: the English renders the Romanian words faithfully, but they don't fit what the "
             "preacher is talking about -- most likely the recogniser misheard a similar-sounding word "
             "(a feast heard as a city, a name heard as a common noun, 'He' where a person was meant), "
             "so the sentence is plausible on its own yet out of place here")
GATE_Q = {"type": "choice", "name": "v", "instructions": _NOTE + "Judge the English of the newest segment "
          "against its Romanian source and the earlier segments.",
          "choices": [{"value": "good", "description": _GOOD + ", faithful to the Romanian meaning, "
                       "and decent language for a church screen"},
                      {"value": "translation", "description": "translation: " + _BAD[5:] + ", or not "
                       "what the Romanian means." + _PROF},
                      {"value": "context", "description": _MISMATCH}]}
# Cuss words go to the translator, in case Soniox (or its English) ever puts one
# on the wall: its own question, asked in the gate's call with the same input.
# Narrowed 2026-10-09 at Jo's request (it used to take anything remotely
# offensive or sensitive, Bible words included): on the 2026-10-08 profanity
# set it still flags 8 of 8 cuss lines and none of the 10 Bible-language lines
# (the broad one flagged all 18), and on the 82-minute 2026-10-09 sermon it
# drops all 148 segments the broad one sent on (death, devil, sin, punishment;
# none a cuss word) without flagging any of 200 others -- the Router's share of
# segments sent on fell from 23.8% to 15.7%
# (validation/scripts/sens_narrow_lab.py, results/2026-10-09-ear-sermon/sens_narrow.json).
SENS_Q = {"type": "predicate", "name": "sens", "instructions": "Text shown on a screen in church. "
          "Does the newest English segment contain a swear word, a curse used as an expletive "
          "(\"damn\", \"what the hell\"), a crude or vulgar word for the body or sex, or a slur? "
          "Words the Bible and preaching use plainly -- devil, hell as a place, harlot, naked, "
          "cursed, sin, killing, death, drunk, demon -- are NOT what is asked about, nor are "
          "mild words like fool or stupid. Judge only the newest segment's English."}
SL_Q = {"type": "predicate", "name": "sl", "instructions":
        "A Romanian sermon, transcribed live by a speech recogniser, one clause at a time (English "
        "translation beside it). Recognisers swap words for similar-sounding ones (Betania -> "
        "bătaia, Iacov -> iacă, pescari -> păcate). Is a word in the NEWEST clause probably such a "
        "mis-hearing -- is there a similar-sounding word that would fit the background clearly "
        "better? Ignore grammar and clumsy wording."}

_SES = requests.Session()
_POOL = cf.ThreadPoolExecutor(8)


def _gate_input(ctx, ro, en):
    ctx = ctx[-GATE_CTX:]
    head = ""
    if ctx:
        head = ("Earlier (Romanian): " + " ".join(c["ro"] for c in ctx) + "\n"
                "Earlier (English): " + " ".join(c["en"] for c in ctx) + "\n\n")
    return head + "Newest segment, Romanian: " + ro + "\nNewest segment, English: " + en


def _sl_input(ctx, ro, en):
    lines = []
    for c in ctx[-SL_CTX:]:
        lines += [f"[earlier, Romanian] {c['ro']}", f"[earlier, English] {c['en']}"]
    return "\n".join(lines + [f"[NEWEST, Romanian] {ro}", f"[NEWEST, English] {en}"])


def _ask(text, qs, timeout):
    """-> {question name: P(flag) or None (refusal: no opinion)}"""
    r = _SES.post(URL, headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                  json={"model": MODEL, "input": text, "questions": qs}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    costs.llm(MODEL, prompt=u.get("input_tokens") or 0,
              cached=(u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
              output=u.get("output_tokens") or 0, line="Router")
    out = {}
    for a in j["answers"]:
        if a["type"] == "choice":
            out[a["name"]] = 1 - {p["value"]: p["probability"] for p in a["probabilities"]}["good"]
        elif a["type"] == "predicate":
            out[a["name"]] = a["probability"]
        else:
            out[a["name"]] = None
    return out


def decide(ro, en, ctx, timeout=1.0, th=0.5):
    """-> {"route": bool, "bad": P(not good), "sens": P(offensive), "sl": P(misheard)
    (each None when unanswered), "why": "translation"|"sensitive"|"misheard"|"",
    "ms": wall time}. A call that fails or times out counts as no opinion; the
    caller keeps Soniox's text. `th` is the bar for the two doubts (mis-heard,
    badly translated): CT_ROUTER_THRESHOLD (server.py ROUTER_TH), 0.3 by default (0.3 from
    2026-10-19, 0.2 for 2026-10-28..30: validation/results/2026-10-28-router-threshold,
    back to 0.3 live because 0.2 sent on too many lines). The cuss-word question keeps 0.5 -- it was narrowed and
    measured at that bar."""
    t0 = time.perf_counter()
    futs = {"gate": _POOL.submit(_ask, _gate_input(ctx, ro, en), [GATE_Q, SENS_Q], timeout),
            "sl": _POOL.submit(_ask, _sl_input(ctx, ro, en), [SL_Q], timeout)}
    got = {}
    out = {}
    # One deadline for both: waited for in turn, each with its own timeout,
    # a stuck pair could hold the request for twice that.
    deadline = t0 + timeout + 0.2
    for k, f in futs.items():
        try:
            got.update(f.result(timeout=max(0.0, deadline - time.perf_counter())))
        except Exception as e:   # noqa: BLE001 -- any failure is "no opinion"
            out.setdefault("err", f"{k}: {type(e).__name__}")
    out.update(bad=got.get("v"), sens=got.get("sens"), sl=got.get("sl"))
    # A suspected mis-hear wins: the garbled word usually garbles the English
    # too (halferurit: both fire), and the hearing translator is its repair.
    why = ("misheard" if (out["sl"] or 0) > th else
           "translation" if (out["bad"] or 0) > th else
           "sensitive" if (out["sens"] or 0) > 0.5 else "")
    out.update(route=bool(why), why=why, ms=round(1000 * (time.perf_counter() - t0)))
    return out
