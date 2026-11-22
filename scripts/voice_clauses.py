"""The ElevenLabs voice as clauses: captions are cut for reading, the voice
is cut for speaking.

A caption is a packet of about five words, cut wherever the preacher pauses;
read one request at a time, every packet sounded like its own little speech.
Here every caption's words go into one buffer, and the voice is given CLIPS
cut at punctuation: always at a full stop, and at a comma (; : —) once five
words are waiting only when waiting longer would make the clip late: the room's
voice is about to run dry AND the clip's moment (the page holds every clip
until his first word + CT_VOICE_DELAY_S) is near. While either is still ahead
it waits for the full stop, through commas and pauses alike, so it reads whole
sentences in the delay it has anyway; the longer the delay, the more of them.
When the moment comes with no full stop, the clip goes at once, up to its
last comma, or at a pause (2026-11-20: at 9 s behind, 7 of 22 clips were still
cut early by a rule that only looked at the queue; waiting for the next comma
instead kept more sentences whole but made clips late). Each clip
is one request with ElevenLabs' stitching (server.VoiceContext: the last three
clips and the words before it; Soniox takes none). With the page's Expression
tags setting on, the director (voice_director) styles each clip for the speech
model the moment it is cut -- a tag where a sentence begins, a word or two in
capitals, a pause -- and the sentence's quick tag, asked when its first words
arrived, stands in if the styling is not back in time.

Measured in the voice lab (validation/results/2026-11-17-voice-lab, six blind
rounds by Jo, 2026-11-17/18): five-word packets read as separate requests
sounded "choppy" (2/5); clauses cut at commas, 4; whole sentences with Haiku's
tags and stability 0.65 on Eleven v4, 5 ("flawless" was only the whole
passage read in one take, which no live voice can do). Fewer comma cuts cost
delay on long sentences: a clip cannot be spoken before he has finished it.

push() returns a Part: the caller's share of the voice. Part.wait() blocks
until its words are in a clip: then it either SPEAKS the clip (the first part
of every clip does, so the clip's audio comes back on the earliest caption's
request and the room's ordered timeline plays it in place) or rode along in an
earlier part's clip and has nothing to say.
"""
import re
import threading
import time

COMMA_WORDS = 5         # a comma cut needs at least this many words waiting...
COMMA_LAG_S = 1.5       # ...and less than this until the voice needs them: the room's voice
                        # queued, or the page's hold on the clip (its due), whichever is longer
LONG_WORDS = 40         # a sentence this long is cut at its next comma whatever the lag
IDLE_S = 3.5            # words waiting this long with no new caption go as they are, once
                        # the voice needs them (the same COMMA_LAG_S)
SPEECH_WPS = 2.5        # the voice's pace (Brian on v4 read the voice lab passage at ~2.5
                        # words a second): a clip just cut keeps it busy words/2.5 s more
STYLE_WAIT_S = 2.5      # the longest a clip waits for its styling, and only while it can: the
                        # room's voice still has that much speech queued, or the page would hold
                        # the clip that long anyway (its moment: his first word + the delay),
FIRST_AUDIO_S = 0.7     # less the voice's first audio; else it waits MIN_WAIT_S and takes the quick tag
MIN_WAIT_S = 0.4        # (Haiku styles in 1.8 s on median, 2.5-3 s one time in ten)
TAG_WAIT_S = 2.5        # without styling: the page holds every clip until CT_VOICE_DELAY_S after
                        # his first word, so a short sentence cut at once can wait for its tag

_END = re.compile(r"[.!?…。！？][\"'’”“»」)\]]*\s*$")
_COMMA = re.compile(r"[,;:—–][\"'’”»)\]]*\s*$")


class Part:
    """One caller's words in the buffer. wait() -> the clip text this part
    speaks (with .tag_key set when the clip starts a sentence), or None when
    the words were spoken in an earlier part's clip."""

    def __init__(self, text, tags=False):
        self.text = text
        self.tags = tags
        self.clip = None
        self.tag_key = None
        self.style_key = None
        self.queued = None          # (seconds of speech the voice had queued, when) at the cut
        self.due_at = None          # when the page will play the caption (clock), if it said
        self.speaks = False
        self._ev = threading.Event()

    def _resolve(self, clip=None, tag_key=None, style_key=None):
        self.clip, self.tag_key, self.style_key = clip, tag_key, style_key
        self.speaks = clip is not None
        self._ev.set()

    def wait(self, timeout=60.0):
        if not self._ev.wait(timeout):
            raise RuntimeError("voice clause never cut")
        return self.clip


class ClauseVoice:
    """The service's clause buffer, one at a time (like VoiceStream): a new
    voice, model or language finishes what is waiting and starts again."""

    def __init__(self, director=None, clock=time.monotonic, ticker=True):
        self.director = director
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = []          # parts not yet in a clip, oldest first
        self._key = None
        self._open = False          # a sentence has begun and not ended
        self._sentence = 0          # the current sentence's number (director key)
        self._sentence_tagged = 0   # the last sentence whose tag a clip has taken
        self._clips = 0             # clips cut (the styling key)
        self._last = 0.0
        self._lag = (0.0, 0.0)      # the latest voice_lag, and when it came
        self._busy_until = 0.0      # when the voice will have spoken the clips cut so far
        self.cuts = {"full stop": 0, "comma": 0, "pause": 0, "long": 0, "switch": 0}
        if ticker:
            threading.Thread(target=self._tick, daemon=True).start()

    def push(self, text, key, lag, tags=False, due=None):
        """Add one caption's words (a complete sentence, or the open tail of
        one). `key` is (voice, model, language); `lag` is how many seconds of
        speech the room's voice still has queued (the page's voice_lag);
        `tags` asks the director for the tag of a sentence these words begin
        (the page's Expression tags setting, off by default); `due` is how
        many seconds until the page would play this caption (voice_due_s)."""
        text = (text or "").strip()
        part = Part(text, tags)
        if due is not None:
            part.due_at = self.clock() + due
        if not text:
            part._resolve(None)
            return part
        with self._lock:
            if self._pending and key != self._key:
                self._cut("switch")
                self._open = False
            self._key = key
            if not self._open:
                self._sentence += 1
                part.sentence = self._sentence
                if self.director is not None and tags:
                    self.director.begin((key, self._sentence), text)
            else:
                part.sentence = self._sentence
            self._pending.append(part)
            self._last = self.clock()
            self._lag = (lag, self._last)
            ends = bool(_END.search(text))
            self._open = not ends
            words = sum(len(p.text.split()) for p in self._pending)
            if ends:
                self._cut("full stop")
            elif _COMMA.search(text):
                if words >= LONG_WORDS:
                    self._cut("long")
                elif words >= COMMA_WORDS and self._slack() < COMMA_LAG_S:
                    self._cut("comma")
        return part

    def _slack(self):
        """Seconds until the voice needs the waiting words: what the room's voice
        still has queued (the page's voice_lag, or the clips just cut, at
        SPEECH_WPS), or the page's hold on their clip, whichever is longer.
        Without a due from the page, the queue alone (as before due existed)."""
        lag, at = self._lag
        now = self.clock()
        left = max(0.0, lag - (now - at), self._busy_until - now)
        head = self._pending[0] if self._pending else None
        if head is not None and head.due_at is not None:
            left = max(left, head.due_at - now)
        return left



    def speech(self, part):
        """(words, tag) to give the voice for a speaking part's clip: the
        director's styling if it is back in time; else the plain clip, with the
        sentence's quick tag if the clip begins it and that is back."""
        if self.director is None:
            return part.clip, None
        wait = TAG_WAIT_S
        if part.style_key is not None:
            lag, at = part.queued or (0.0, self.clock())
            ahead = lag - (self.clock() - at)                     # speech still ahead of this clip
            if part.due_at is not None:                           # ...or the page's hold until its moment
                ahead = max(ahead, part.due_at - self.clock())
            left = ahead - FIRST_AUDIO_S
            got = self.director.styled(part.style_key, min(STYLE_WAIT_S, max(MIN_WAIT_S, left)))
            if got is not None:
                if part.tag_key is not None:
                    self.director.forget(part.tag_key)
                tag, words = got
                return words, tag
            wait = 0.3                    # the quick tag began with the sentence: it has had its time
        tag = self.director.tag(part.tag_key, wait) if part.tag_key is not None else None
        return part.clip, tag

    def _last_comma(self):
        """How many waiting parts end at the last comma with COMMA_WORDS words
        up to it, or 0."""
        words, best = 0, 0
        for i, p in enumerate(self._pending):
            words += len(p.text.split())
            if words >= COMMA_WORDS and _COMMA.search(p.text):
                best = i + 1
        return best

    def _cut(self, why, upto=None):
        upto = len(self._pending) if upto is None else upto
        parts, self._pending = self._pending[:upto], self._pending[upto:]
        if not parts:
            return
        self.cuts[why] += 1
        head = parts[0]
        clip = " ".join(p.text for p in parts if p.text)
        # every sentence's first clip takes its tag; a later clause of it goes plain
        starts = head.sentence > self._sentence_tagged
        tag_key = None
        if starts:
            self._sentence_tagged = head.sentence
            tag_key = (self._key, head.sentence)
        style_key = None
        if self.director is not None:
            if head.tags:
                self._clips += 1
                style_key = (self._key, "clip", self._clips)
                engine = self._key[0] if self._key else None
                self.director.begin_style(style_key, clip, engine, starts)
            self.director.said(clip)
        head.queued = self._lag
        # The voice will be speaking this clip before it needs the next one, and
        # the page's voice_lag cannot count it until its audio has come back.
        now = self.clock()
        self._busy_until = max(self._busy_until, now) + len(clip.split()) / SPEECH_WPS
        head._resolve(clip, tag_key, style_key)
        for p in parts[1:]:
            p._resolve(None)

    def flush_idle(self):
        """Waiting words whose clip the voice now needs go up to their last
        comma if they have one, else, after IDLE_S with no new caption (he
        stopped mid-thought, or the service paused), as they are. Until the voice
        needs them they keep waiting for the full stop. Without a due from the
        page, a pause sends them whatever the queue, as before."""
        with self._lock:
            if not self._pending:
                return
            idle = self.clock() - self._last >= IDLE_S
            if self._pending[0].due_at is None:
                if idle:
                    self._cut("pause")
                return
            if self._slack() >= COMMA_LAG_S:
                return
            k = self._last_comma()
            if k:
                self._cut("comma", upto=k)
            elif idle:
                self._cut("pause")

    def _tick(self):
        while True:
            time.sleep(0.25)
            self.flush_idle()
