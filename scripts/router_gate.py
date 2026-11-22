#!/usr/bin/env python3
"""Router: should Soniox's own translation of a segment go on the wall, or
should a translator model redo it?

Two questions to OpenAI's Decisions API (gpt-6-luna), asked in parallel
because each wants a different amount of context:

  gate       -- is the caption a plain, faithful, decent translation of what was
                said? (three_prof; 4 earlier segments). Catches literal idioms,
                garbled wording and offensive words.
  soundalike -- is a word in what was said probably a mis-hearing of a
                similar-sounding word that fits the background better?
                (12 earlier segments). Catches Rusalii -> Ierusalim and kin.

Both name the languages of the line (questions()); they were written for, and
measured on, Romanian sermons captioned in English.

Route if P(good) < 1 - th on the gate, P(cuss word) > 0.5 ("sens", same call) or
P(misheard) > th, th being CT_ROUTER_THRESHOLD (server.py ROUTER_TH; 0.5 until
2026-10-19, 0.3 until 2026-10-28, 0.2 until 2026-10-30, 0.3 until 2026-11-17, now 0.2 again: validation/results/2026-10-28-router-threshold). Measured 2026-10-08 at 0.5
(validation/results/2026-10-08-luna-gate/, luna_gate_lab / luna_context_lab /
luna_combo_lab): on 200 Roman segments it caught 11 of 14 bad translations and
sent on 4 of 186 good ones; 11 of 18 real mis-hears; 8 of 8 offensive lines
with Bible words left alone; ~0.1 s a call (live 2026-10-08: 0.15-0.33 s,
~0.5 s cold). "sens" narrowed to cuss words 2026-10-09 (see there). Prompts
are the measured ones word for word -- change them and re-measure.
"""
import functools
import os
import re
import time
import concurrent.futures as cf

import requests

import costs
import languages

MODEL = os.environ.get("CT_ROUTER_MODEL", "gpt-6-luna")
URL = "https://api.openai.com/v1/decisions"
GATE_CTX, SL_CTX = 4, 12

# The questions name the languages of the line they judge: the spoken one (src)
# and the caption's (tgt), from the page; a code it doesn't know reads as "the
# source/target language". Until 2026-11-17 they said "A Romanian sermon ...
# into English" whatever was spoken. Measured again with the languages named
# (2026-11-17; the lab is on branch claude/quotes-pronouns-validation): on the 2026-10-08 sets 14
# of 14 bad Roman lines caught (13 before) with the same 22 of 186 good ones
# sent, idioms 29 of 33 with 6 false alarms (8), mis-hears 10 of 18 with 4 (6),
# cuss lines 8 of 8 with no Bible line; 11 of 12 bad lines in other language
# pairs with no false alarm (12, 2 before); on 600 lines of 2026-11-15 the same
# decision on 533, 21.8% sent on (24.3%), the English sermon's lines no longer
# read as Romanian.
def _name(code, other):
    return languages.CAPTION.get(str(code or "").lower(), other)


@functools.lru_cache(maxsize=64)
def questions(src=None, tgt=None):
    """{"gate", "sens", "sl", "div"} for a line spoken in `src` and captioned in
    `tgt` (codes); "div" is None where captions don't capitalise God's pronouns."""
    S, T = _name(src, "source-language"), _name(tgt, "target-language")
    note = (f"A {S} sermon in a church is translated live into {T} by a fast speech translator, "
            "one short segment at a time, and projected on a screen. ")
    good = (f"good: a plain, natural {T} rendering that a listener would understand and that fits "
            "what came before")
    bad = ("misleading, garbled, incomplete or oddly out of place -- e.g. an idiom or regional word "
           "translated word for word into something strange, a phrase that makes no sense in a church, "
           "or a meaning that clashes with the context")
    prof = (" Also bad: any swear word, crude or vulgar language, slur or other wording that would be "
            "offensive on a church screen (Bible words like devil, hell, harlot, naked or cursed, in "
            f"whatever language, are fine when the {S} means them).")
    mismatch = (f"context: the {T} renders the {S} words faithfully, but they don't fit what the "
                "preacher is talking about -- most likely the recogniser misheard a similar-sounding word "
                "(a feast heard as a city, a name heard as a common noun, 'He' where a person was meant), "
                "so the sentence is plausible on its own yet out of place here")
    gate = {"type": "choice", "name": "v", "instructions": note + f"Judge the {T} of the newest segment "
            f"against its {S} source and the earlier segments.",
            "choices": [{"value": "good", "description": good + f", faithful to the {S} meaning, "
                         "and decent language for a church screen"},
                        {"value": "translation", "description": "translation: " + bad + ", or not "
                         f"what the {S} means." + prof},
                        {"value": "context", "description": mismatch}]}
    # Cuss words go to the translator, in case Soniox's own translation ever puts
    # one on the wall: its own question, asked in the gate's call with the same
    # input. Narrowed 2026-10-09 at Jo's request (it used to take anything remotely
    # offensive or sensitive, Bible words included): on the 2026-10-08 profanity
    # set it still flags 8 of 8 cuss lines and none of the 10 Bible-language lines
    # (the broad one flagged all 18), and on the 82-minute 2026-10-09 sermon it
    # drops all 148 segments the broad one sent on (death, devil, sin, punishment;
    # none a cuss word) without flagging any of 200 others -- the Router's share of
    # segments sent on fell from 23.8% to 15.7%
    # (validation/scripts/sens_narrow_lab.py, results/2026-10-09-ear-sermon/sens_narrow.json).
    sens = {"type": "predicate", "name": "sens", "instructions": "Text shown on a screen in church. "
            f"Does the newest {T} segment contain a swear word, a curse used as an expletive "
            f"(\"damn\", \"what the hell\", or the like in {T}), a crude or vulgar word for the body or "
            "sex, or a slur? Words the Bible and preaching use plainly -- devil, hell as a place, harlot, "
            "naked, cursed, sin, killing, death, drunk, demon -- are NOT what is asked about, nor are "
            f"mild words like fool or stupid. Judge only the newest segment's {T}."}
    sl = {"type": "predicate", "name": "sl", "instructions":
          f"A {S} sermon, transcribed live by a speech recogniser, one clause at a time ({T} "
          "translation beside it). Recognisers swap words for similar-sounding ones (in Romanian: "
          "Betania -> bătaia, Iacov -> iacă, pescari -> păcate). Is a word in the NEWEST clause "
          "probably such a mis-hearing -- is there a similar-sounding word that would fit the "
          "background clearly better? Ignore grammar and clumsy wording."}
    div = None
    if str(tgt or "").lower() in languages.divine_caps():
        forms = languages.DIVINE_CAPS.get(str(tgt).lower(), ("its pronouns and possessives", ""))[0]
        div = {"type": "predicate", "name": "div", "instructions": "Text shown on a church screen, "
               f"where pronouns for God, Jesus Christ and the Holy Spirit take a capital letter in {T}: "
               f"{forms}. Look at the newest segment's {T}. Is a pronoun in it that refers to God, Christ "
               "or the Spirit written in lower case? Answer no when every such pronoun already has its "
               "capital, and when the lower-case pronouns refer to people: the preacher, the "
               "congregation, a man or woman in a Bible story."}
    return {"gate": gate, "sens": sens, "sl": sl, "div": div}


# Pronouns for God written small ("that he may pour out his grace", "God raised
# him from the dead"): Soniox's own translation leaves them lower case, and on
# 2026-11-15 about 200 lines of the wall had a lower-case he/him/his, many of
# them God's. Asked in the gate's call (same input, no extra request), only for
# captions in a language that capitalises them (languages.divine_caps); the line
# goes to the translator, whose prompt capitalises them, only when its caption
# holds one of that language's pronouns in lower case at all (lower_pronoun).
# Measured on those services (2026-11-16, branch claude/quotes-pronouns-validation):
# over 0.5 it caught 17 of 18 God's pronouns picked out by hand, and would have
# sent 179 more lines on (about 4% of all) -- some 4 in 10 really God's; the rest
# cost a translator call, not a wrong capital, as the prompt keeps lower case
# for a person or a doubt. Over 0.7: 13 of 18, 100 lines, about 6 in 10.
@functools.lru_cache(maxsize=32)
def lower_pronoun(tgt):
    """A regex for `tgt`'s pronouns for God in lower case (languages.DIVINE_CAPS),
    or None where none are listed. "-nya" is a suffix, "l-" a clitic; Romanian
    "lui" before a name is the genitive article ("lui Dumnezeu"), not a pronoun."""
    forms = languages.DIVINE_CAPS.get(str(tgt or "").lower(), ("", ""))[1].split()
    if not forms:
        return None
    alts = []
    for f in forms:
        if f.startswith("-"):
            alts.append(re.escape(f[1:]) + r"\b")
        elif f.endswith("-"):
            alts.append(r"(?<![\w-])" + re.escape(f))
        elif f == "lui":
            alts.append(r"\blui\b(?!\s+[A-ZĂÂÎȘŞȚŢ])")
        else:
            alts.append(r"\b" + re.escape(f) + r"\b")
    return re.compile("|".join(alts))


# ---- quotation marks, one caption later ---------------------------------------
# Soniox's recogniser types the quotation marks into the source, and both
# translators copy them: it opens a quotation after every "X spune:", the
# preacher's own aside included, and often closes it one to four captions late,
# over his commentary. On 2026-11-15 that was 13 quotations on the wall, nine
# of them Bible passages. Once the next caption is up, this asks whether the
# quotation it continues is still running; when not, the page closes it on the
# caption before. Measured on those services (2026-11-16, branch claude/quotes-
# pronouns-validation): of the 13, 5 put right and 5 cut shorter; 1 of 126 right
# ones closed a caption early. A second question -- did a quotation begin at
# the opening mark at all? -- was dropped there: right ones scored as low as the
# misplaced ones, so it took back good quotations as often as bad.
QIN_Q = {"type": "predicate", "name": "qin", "instructions":
         "A sermon is transcribed live by a speech recogniser, one short segment at a time, "
         "translated beside it, and shown on a church screen. "
         "A quotation opened at the segment marked [QUOTE OPENS] -- a Bible passage being read or "
         "cited, a hymn, or words someone said or would say -- and has not been closed yet. Is the "
         "NEWEST segment still part of the quoted words? Answer no if the newest segment is the "
         "preacher speaking in his own voice again: his comment or explanation, an application to "
         "the congregation, a question to them, or the reference to the next verse. A quotation "
         "mark the recogniser typed is only its guess: judge the words."}
QUOTE_IN_BAR = 0.3

_SES = requests.Session()
_POOL = cf.ThreadPoolExecutor(8)


# `ro` and `en` are the spoken line and its caption, whatever their languages (the
# names are the page's). Earlier lines are "spoken" and "translated": in a
# bilingual service they may have gone the other way.
def _gate_input(ctx, ro, en, src=None, tgt=None):
    S, T = _name(src, "source language"), _name(tgt, "target language")
    ctx = ctx[-GATE_CTX:]
    head = ""
    if ctx:
        head = ("Earlier, spoken: " + " ".join(c["ro"] for c in ctx) + "\n"
                "Earlier, translated: " + " ".join(c["en"] for c in ctx) + "\n\n")
    return head + f"Newest segment, {S}: " + ro + f"\nNewest segment, {T}: " + en


def _sl_input(ctx, ro, en, src=None, tgt=None):
    S, T = _name(src, "source language"), _name(tgt, "target language")
    lines = []
    for c in ctx[-SL_CTX:]:
        lines += [f"[earlier, spoken] {c['ro']}", f"[earlier, translated] {c['en']}"]
    return "\n".join(lines + [f"[NEWEST, {S}] {ro}", f"[NEWEST, {T}] {en}"])


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


def _has_lower_pronoun(en, tgt):
    rx = lower_pronoun(tgt)
    return bool(rx.search(en)) if rx else True    # forms not listed (CT_DIVINE_CAPS): Luna alone


def decide(ro, en, ctx, timeout=1.0, th=0.5, conf=None, unsure=0.0, div=0.0, src=None, tgt=None):
    """`ro` spoken in `src`, captioned `en` in `tgt` (codes; the names are kept from
    when every service was Romanian -> English).
    -> {"route": bool, "bad": P(not good), "sens": P(offensive), "sl": P(misheard),
    "div": P(a pronoun for God in lower case) (each None when unanswered, "div" when
    not asked), "low": Soniox's least confident word in ro (None without `conf`),
    "why": "translation"|"sensitive"|"misheard"|"pronoun"|"",
    "ms": wall time}. When the mis-hear check gives no answer, a word scored under
    `unsure` (server.py ROUTER_UNSURE) stands in for it and makes the line
    "misheard". `div` is the bar for the pronoun question (server.py
    ROUTER_PRONOUN; 0 does not ask it; nor is it asked in a caption language that
    doesn't capitalise God's pronouns). A call that fails or times out counts as no opinion; the
    caller keeps Soniox's text. `th` is the bar for the two doubts (mis-heard,
    badly translated): CT_ROUTER_THRESHOLD (server.py ROUTER_TH), 0.2 by default (0.3 from
    2026-10-19, 0.2 for 2026-10-28..30: validation/results/2026-10-28-router-threshold,
    back to 0.3 live because 0.2 sent on too many lines, 0.2 again from 2026-11-17). The cuss-word question keeps 0.5 -- it was narrowed and
    measured at that bar."""
    t0 = time.perf_counter()
    Q = questions(src, tgt)
    div = div if Q["div"] else 0.0
    gate_qs = [Q["gate"], Q["sens"]] + ([Q["div"]] if div else [])
    futs = {"gate": _POOL.submit(_ask, _gate_input(ctx, ro, en, src, tgt), gate_qs, timeout),
            "sl": _POOL.submit(_ask, _sl_input(ctx, ro, en, src, tgt), [Q["sl"]], timeout)}
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
    if div:
        out["div"] = got.get("div")
    cs = [c for c in (conf or []) if isinstance(c, (int, float))]
    out["low"] = round(min(cs), 2) if cs else None
    # A suspected mis-hear wins: the garbled word usually garbles the English
    # too (halferurit: both fire), and the hearing translator is its repair.
    # Soniox's confidence only stands in for a mis-hear check that did not answer:
    # on 188 lines with a word under 0.5 (2026-11-09), the check already flagged
    # every mis-hear a low score pointed at.
    unsure_low = out["sl"] is None and out["low"] is not None and out["low"] < unsure
    why = ("misheard" if (out["sl"] or 0) > th or unsure_low else
           "translation" if (out["bad"] or 0) > th else
           "sensitive" if (out["sens"] or 0) > 0.5 else
           # last: the redo is for casing, and any doubt above already brings one
           "pronoun" if div and (out.get("div") or 0) > div and _has_lower_pronoun(en, tgt) else "")
    out.update(route=bool(why), why=why, ms=round(1000 * (time.perf_counter() - t0)))
    return out


def _quote_input(lines, at):
    out = []
    for i, c in enumerate(lines):
        tag = "NEWEST" if i == len(lines) - 1 else "QUOTE OPENS" if i == at else "earlier"
        out.append(f"[{tag}] {c.get('ro') or ''}  ||  {c.get('en') or ''}")
    return "\n".join(out)


def quote_check(lines, at, timeout=1.5):
    """A quotation opened on caption `at` of `lines` ({ro: source, en: caption},
    oldest first, a few captions before it included) and was still open when the
    last of them, the newest caption, went up. -> {"qin": P(the newest caption is
    still inside it), "act": "close" (it ended with the caption before the newest)
    or "" (leave it), "ms"}. No answer counts as no opinion: "act" is ""."""
    t0 = time.perf_counter()
    try:
        qin = _ask(_quote_input(lines, at), [QIN_Q], timeout).get("qin")
    except Exception:   # noqa: BLE001 -- any failure is "no opinion"
        qin = None
    return {"qin": qin, "act": "close" if qin is not None and qin < QUOTE_IN_BAR else "",
            "ms": round(1000 * (time.perf_counter() - t0))}
