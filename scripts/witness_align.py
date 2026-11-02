"""Lining a second recognizer's text up with a caption, for the "+11" translator
option: which stretch of the witness's running transcript is THIS caption, and
has the witness heard all of it yet?

Ported unchanged from the lab that measured it (validation/results/
2026-10-06-mishears/mishear_lab.py and 2026-10-07-three-witnesses/): with
ElevenLabs's reading picked this way and Soniox's text, gemini-3.6-flash hearing
the caption fixed 44 of 47 real mis-hears and broke none of 19 right words.
"""
import re
from collections import Counter
import unicodedata

# Spoken Romanian vs the 1924 spelling: Soniox writes Iisus, Cornilescu Isus.
_SPELL = {"iisus": "isus", "isuse": "isus", "iisuse": "isus", "sint": "sunt", "sintem": "suntem"}

# Numbers as Romanian says them, folded: Soniox writes "versetul 8", "100%", the ears
# "versetul opt", "sută la sută". Without this a packet ending in a digit never lined
# up to its end (2026-10-10, whole sermon: about 1 packet in 60 for each ear).
_UNITS = "zero unu doi trei patru cinci sase sapte opt noua".split()
_TEENS = ("zece unsprezece doisprezece treisprezece paisprezece cincisprezece saisprezece "
          "saptesprezece optsprezece nouasprezece").split()
_TENS = {2: "douazeci", 3: "treizeci", 4: "patruzeci", 5: "cincizeci", 6: "saizeci",
         7: "saptezeci", 8: "optzeci", 9: "nouazeci"}


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _spell(n):
    """0-9999 in words ("21" -> "douazeci si unu"); None past that."""
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t] + (" si " + _UNITS[u] if u else "")
    for size, one, many in ((1000, "o mie", "mii"), (100, "o suta", "sute")):
        if size <= n < 10 * size:
            h, r = divmod(n, size)
            head = one if h == 1 else ("doua" if h == 2 else _UNITS[h]) + " " + many
            return head + (" " + _spell(r) if r else "")
    return None


def _numbers(t):
    t = re.sub(r"(\d)\s*%", r"\1 la suta", t)
    return re.sub(r"\d+", lambda m: _spell(int(m.group())) or m.group(), t)


def vkeys(text):
    out = []
    for w in re.findall(r"[\w'-]+", _numbers(fold(text))):
        for part in w.replace("'", "-").split("-"):
            if part:
                out.append(_SPELL.get(part, part))
    return out


WIT_NOTE = ("Other speech recognizers, listening to the same audio, heard this stretch as:\n{alts}\n"
            "Where the transcripts differ, translate the reading that fits the sentence and the lines before it; "
            "a word two recognizers agree on is more likely what was said. Translate only this stretch.")


def _align_window(packet, transcript):
    span = _align_span(packet, transcript)
    return " ".join(span[0]) if span else None


def _align_span(packet, transcript):
    """-> (the window's words, how many of the transcript's words follow it), or None.
    The window of about the caption's length that reads most like it. Later windows win
    ties (the caption is near the end), and a window shorter than the caption is extended
    when that barely costs similarity: a mis-heard last word ("ochi" for "obște") lowers the
    score just enough to leave it out."""
    import difflib
    pw, tw = packet.split(), transcript.split()
    if not pw or not tw:
        return None
    # Only the recent tail can be the caption (it is the newest thing said);
    # searching the whole 40 s cost ~0.7 s an ear on a 2-core laptop.
    tw = tw[-(2 * len(pw) + 12):]
    pk = " ".join(vkeys(packet))
    keys = [" ".join(vkeys(w)) for w in tw]          # each word folded once, not once per window
    sm = difflib.SequenceMatcher(None, "", pk)       # pk is the fixed side: indexed once
    def score(st, L):
        sm.set_seq1(" ".join(k for k in keys[st:st + L] if k))
        return sm.ratio()
    best, best_r = None, 0.0
    for L in range(max(1, len(pw) - 3), len(pw) + 4):
        for st in range(0, max(1, len(tw) - L + 1)):
            # real_quick_ratio / quick_ratio bound ratio() from above, so a window
            # whose bound is under the best so far cannot win (or tie): skipped
            # without the full match. Same answer, a fraction of the time.
            sm.set_seq1(" ".join(k for k in keys[st:st + L] if k))
            if sm.real_quick_ratio() < best_r or sm.quick_ratio() < best_r:
                continue
            r = sm.ratio()
            if r >= best_r:
                best, best_r = (st, L), r
    if best is None or best_r < 0.5:
        return None
    st, L = best
    while L < len(pw):
        grown = [(st, L + 1), (st - 1, L + 1)] if st > 0 else [(st, L + 1)]
        grown = [(a, l) for a, l in grown if a >= 0 and a + l <= len(tw)]
        if not grown:
            break
        a, l = max(grown, key=lambda g: score(*g))
        if score(a, l) < best_r - 0.03:
            break
        st, L, best_r = a, l, max(best_r, score(a, l))
    return tw[st:st + L], len(tw) - (st + L)


def _aligned(packet, transcript, prev=None):
    """The best-fitting window at the transcript's end, minus a leading word that is
    really the end of the previous caption (`prev`): Soniox sometimes opens a caption
    with a filler ("și.") that the second recognizer did not hear, and the window then
    reaches back one word. None when it cannot be lined up."""
    span = _aligned_span(packet, transcript, prev)
    return span[0] if span else None


def _aligned_span(packet, transcript, prev=None):
    """_aligned, and how many of the transcript's words follow the reading."""
    import difflib
    span = _align_span(packet, transcript)
    if not span:
        return None
    aw, after = span
    alt, pw = " ".join(aw), packet.split()
    if prev and len(aw) > 1:
        key = lambda w: "".join(vkeys(w))
        tail = [key(w) for w in prev.split()[-2:]]
        first = key(aw[0])
        if first in tail and difflib.SequenceMatcher(None, first, key(pw[0])).ratio() < 0.5:
            alt = " ".join(aw[1:])
    return alt, after


def align_tail(packet, transcript, prev=None):
    """The second recognizer's reading of `packet` (the caption just sent), or None when
    it says the same thing or cannot be lined up."""
    alt = _aligned(packet, transcript, prev)
    return alt if alt and differs(packet, alt) else None


def _share(a, b):
    """How much of word list `a` is also in `b` (counted with repeats)."""
    A, B = Counter(a), Counter(b)
    return sum((A & B).values()) / max(1, len(a))


def stale(reading, src, earlier):
    """Is `reading` really an ear's text of an EARLIER packet? An ear running
    behind has nothing of the new packet yet, and the window that lines up best
    is the end of what it heard before -- often the previous line, which reads
    alike ("cinci fecioare inteleapte" for "5 fecioare neintelepte"). Stale: the
    reading shares at least 0.6 of its words with one of `earlier` (the packets
    sealed before, newest last) and 0.2 more than with `src`.
    2026-10-18 service, Router off, every caption with its ears' readings: ~140
    stale readings, ~10 of them turned into wrong captions ("five wise virgins",
    a negation lost); dropping them fixed those 10 and harmed none of 142
    (validation/results/2026-10-18-session-review/)."""
    rw = vkeys(reading)
    if len(rw) < 2 or not earlier:
        return False
    before = max(_share(rw, vkeys(e)) for e in earlier[-12:])
    return before >= 0.6 and before > _share(rw, vkeys(src)) + 0.2


def differs(packet, alt):
    """Does the second reading say anything the caption does not? Identical words
    = no hint; case counts (Soniox's "El" vs "el" is the whole difference in some
    mis-hears), except on a caption's first word."""
    words = lambda t: re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", t)
    a, b = words(alt), words(packet)
    return not ([w.lower() for w in a] == [w.lower() for w in b] and a[1:] == b[1:])


def reaches_end(src, alt):
    """Has the witness heard the caption's last word? (A text stand-in for the
    watermark: the app would use the witness's own audio position.) True when the
    last run of >= 2 matching letters lands inside the caption's last word -- so
    "... peste" against "... peste sirta" is cut short, while "Rusalii" against
    "Ierusalim" (a different word in the same place) still counts."""
    import difflib
    key = lambda t: "".join(vkeys(t))
    words = src.split()
    if not words:
        return False
    a, b = key(src), key(alt)
    last_start = len(key(" ".join(words[:-1])))
    blocks = [m for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size >= 2]
    return bool(blocks) and blocks[-1].a + blocks[-1].size > last_start



def caption_reading(src, transcript, prev=None):
    """The witness's reading of the caption just sent, or None: align_tail, kept
    only if it reaches the caption's last word. The lab's rule, measured three
    times at 44, 42 and 43 of 47 with gemini-3.6-flash hearing the caption.
    Also requiring the reading to sit near the end of the witness's text (it is
    the newest thing said) scored 41, 41 and 42 -- it threw away good readings
    of captions sent seconds after their last word -- and protected nothing:
    the translator ignored the stale readings it would have caught."""
    alt = align_tail(src, transcript, prev)
    return alt if alt and reaches_end(src, alt) else None


def packet_reading(src, transcript, prev=None):
    """The witness's reading of one packet for display beside it, the same as
    Soniox's or not: lined up and reaching the packet's last word, else None
    (not heard yet)."""
    alt = _aligned(src, transcript, prev)
    return alt if alt and reaches_end(src, alt) else None


def packet_window(src, transcript, prev=None):
    """The stretch of the witness's text that lines up with `src` so far, whether
    or not it has reached the packet's last word yet (None: nothing lines up).
    packet_reading is this plus reaches_end."""
    return _aligned(src, transcript, prev)


def packet_state(src, transcript, prev=None):
    """-> (packet_window's stretch or None, settled): has the ear finished hearing the
    packet? reaches_end alone was not enough. Any two letters of the last word count,
    so a partial cut mid-word ("Domnului Iis" for "Domnului Iisus Hristos", "un
    princip") passed at the seal and went to the translator unfinished. Settled now
    also needs a word of the ear's own past the stretch: it has moved on to what
    follows. Whole sermon, 27 real mis-hears, any ear holding the right word with a
    0.7 s cap: 23 -> 25 (with the digits in vkeys); the wait then mostly runs to the
    cap (median 0.4 -> 0.7 s). See validation/results/2026-10-10-ear-done/."""
    span = _aligned_span(src, transcript, prev)
    if not span:
        return None, False
    alt, after = span
    return alt, after >= 1 and reaches_end(src, alt)
