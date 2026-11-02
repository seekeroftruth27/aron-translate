"""One continuous ElevenLabs voice for a whole service: the Text to Dialogue
WebSocket (/v1/text-to-dialogue/stream-input), the only streaming-input
endpoint that takes Eleven v4 and v4 Turbo (the TTS stream-input socket 400s on
both, 2026-10-26).

Captions are packets of about five words. Spoken one HTTP request each, every
packet is its own little speech: a fresh start, a falling end, a breath. Here
every caption's text goes into ONE open connection the moment it exists, and
the voice reads the whole service as one continuous speech. The server holds
text until it has about 40 characters / 8 words, so short captions are joined
the way a reader would join them.

Each push returns a Clip: the audio for exactly that text, cut out of the
continuous stream by the per-character alignment the socket sends with its
audio (sync_alignment=true; the characters match the text sent exactly, tags
included). A chunk that straddles two clips goes whole to the earlier one --
mp3 bytes cannot be split -- so the page's ordered timeline still plays every
byte once, in order.

Flushing. Text waits in the server's buffer until the threshold or a flush. A
sentence end is flushed while the voice is close behind the captions (lag
under FLUSH_LAG_S) so it speaks without waiting for the next caption. While the
voice is behind, the room is still hearing older audio, so nothing is lost by
holding the sentence for the next one: the voice then reads it with what follows
in hand. A pause in the captions flushes the rest after IDLE_FLUSH_S, or later
while the voice is behind.

Measured 2026-10-26 (20 captions of the 2026-10-19 service replayed at their
real times; caption shown -> its words spoken, median / p90): v4 Turbo 3.5 /
5.0 s, v4 3.7 / 5.3 s, against 1.8 / 2.6 s for one clip per caption and
3.4 / 3.7 s for clips joined to ~10 words.
"""
import base64
import json
import os
import queue
import re
import threading
import time

URL = "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
MODELS = ("eleven_v4", "eleven_v4_turbo")   # what the socket accepts that the app offers
FLUSH_LAG_S = 2.5      # voice this close behind the captions: flush at sentence ends
IDLE_FLUSH_S = 1.2     # captions paused this long: speak whatever is waiting
KEEPALIVE_S = 10       # the socket closes after 20 s without a client message
IDLE_CLOSE_S = 300     # no captions for this long: the service is over, hang up
STALL_S = 12           # a clip waiting this long for any audio has been lost

_SENTENCE_END = re.compile(r"[.!?…][\"'’”)\]]?\s*$")


def can_stream(provider, model):
    return provider == "elevenlabs" and (model or "") in MODELS


class Clip:
    """The audio of one pushed text. Iterate it for mp3 chunks as they arrive;
    it ends when every character has been voiced (or raises RuntimeError if the
    connection dies first)."""

    def __init__(self, text, start, end):
        self.text, self.start, self.end = text, start, end
        self.sent_at = time.monotonic()
        self.first_at = None
        self.q = queue.Queue()
        self.closed = False
        self.abandoned = False   # the page gave up on it: audio still arrives, nobody reads it

    def _put(self, b):
        if self.first_at is None:
            self.first_at = time.monotonic()
        self.q.put(b)

    def _close(self, error=None):
        if not self.closed:
            self.closed = True
            self.q.put(RuntimeError(error) if error else None)

    @property
    def ttfb_ms(self):
        return round(((self.first_at or time.monotonic()) - self.sent_at) * 1000)

    def __iter__(self):
        while True:
            try:
                item = self.q.get(timeout=STALL_S)
            except queue.Empty:
                self.closed = True
                raise RuntimeError("voice stream stalled: no audio for this caption")
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


class _Conn:
    """One socket for one (voice, model). Characters pushed are counted from 0;
    audio alignment advances a second cursor through the same count, and each
    chunk belongs to the oldest clip that cursor has not finished."""

    def __init__(self, key, voice, model, api_key):
        from websockets.sync.client import connect
        self.key = key
        url = f"{URL}?model_id={model}&output_format=mp3_44100_128&sync_alignment=true"
        self.ws = connect(url, additional_headers={"xi-api-key": api_key},
                          open_timeout=5, max_size=None)
        self.voice = voice
        self.ws.send(json.dumps({"voices": [voice]}))
        self.lock = threading.Lock()
        self.clips = []          # not yet closed, oldest first
        self.sent = 0            # characters pushed
        self.voiced = 0          # characters the audio has covered
        self.lang = None
        self.last_send = time.monotonic()
        self.last_text = time.monotonic()
        self.unflushed = False
        self.idle_after = IDLE_FLUSH_S
        self.dead = None
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, msg):
        self.ws.send(json.dumps(msg))
        self.last_send = time.monotonic()

    def push(self, text, lang, lag, flush_ok=True):
        with self.lock:
            if self.dead:
                raise RuntimeError(self.dead)
            clip = Clip(text, self.sent, self.sent + len(text))
            item = {"text": text, "voice_id": self.voice}
            if self.lang is not None and lang != self.lang:
                item["new_turn"] = True     # another language: let the delivery reset
            self.lang = lang
            msg = {"inputs": [item]}
            flush = flush_ok and lag < FLUSH_LAG_S and _SENTENCE_END.search(text)
            if flush:
                msg["flush"] = True
            self.clips.append(clip)
            self.sent = clip.end
            self._send(msg)
            self.last_text = time.monotonic()
            self.unflushed = not flush
            # Behind, hold a paused tail longer: it is not due on the speaker yet.
            # Capped well under STALL_S, which would call the clip lost.
            self.idle_after = min(6.0, max(IDLE_FLUSH_S, lag - FLUSH_LAG_S))
            return clip

    def tick(self):
        """Housekeeping, about twice a second: flush a paused tail, keep alive."""
        now = time.monotonic()
        with self.lock:
            if self.dead:
                return
            try:
                if self.unflushed and now - self.last_text > self.idle_after:
                    self._send({"inputs": [], "flush": True})
                    self.unflushed = False
                elif now - self.last_send > KEEPALIVE_S:
                    self._send({"keep_alive": True})
            except Exception as exc:
                self._die(f"voice stream lost: {exc}")

    def idle_for(self):
        return time.monotonic() - self.last_text

    def close(self):
        """Hang up once everything pushed has been spoken (close_socket flushes)."""
        with self.lock:
            if self.dead:
                return
            try:
                self._send({"close_socket": True})
            except Exception:
                pass

    def _die(self, why):
        self.dead = why
        for c in self.clips:
            c._close(why)
        self.clips = []

    def _route(self, audio, nchars):
        """Give one chunk to its clip; close clips the audio has moved past."""
        with self.lock:
            if nchars:
                while self.clips and self.voiced >= self.clips[0].end:
                    self.clips.pop(0)._close()
            if self.clips:
                c = self.clips[0]
                if not c.abandoned:
                    c._put(audio)
            self.voiced += nchars

    def _turn_done(self):
        """A flush has been spoken to its end. The alignment leaves out the
        whitespace after the last word of a turn (each pushed text ends in a
        space), hence the slack."""
        with self.lock:
            while self.clips and self.voiced + 2 >= self.clips[0].end:
                self.clips.pop(0)._close()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if msg.get("error"):
                    raise RuntimeError(f"ElevenLabs: {msg['error']}")
                if msg.get("audio"):
                    al = msg.get("alignment") or {}
                    self._route(base64.b64decode(msg["audio"]), len(al.get("chars") or []))
                if msg.get("is_final_audio_for_turn"):
                    self._turn_done()
                if msg.get("is_final"):
                    break
            with self.lock:
                self._die("voice stream closed")
        except Exception as exc:
            with self.lock:
                if not self.dead:
                    self._die(f"voice stream lost: {exc}")


class VoiceStream:
    """The service's voice connection. One at a time: another voice or model
    hangs up the old one (after it finishes what it was given) and dials anew."""

    def __init__(self):
        self.lock = threading.Lock()
        self.conn = None
        threading.Thread(target=self._house, daemon=True).start()

    def push(self, text, voice, model, lang, lag=0.0):
        key = (voice, model)
        with self.lock:
            c = self.conn
            if c is None or c.dead or c.key != key:
                if c is not None and not c.dead:
                    c.close()
                c = self.conn = _Conn(key, voice, model, os.environ["ELEVENLABS_API_KEY"])
        return c.push(text, lang, lag)

    def _house(self):
        while True:
            time.sleep(0.4)
            with self.lock:
                c = self.conn
                if c is not None and not c.dead and c.idle_for() > IDLE_CLOSE_S and not c.clips:
                    c.close()
                    self.conn = c = None
            if c is not None:
                c.tick()
