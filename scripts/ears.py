"""Extra ears: streaming recognizers that listen beside Soniox.

Soniox stays the app's ear of record -- it decides the cuts, the timing and the
text. An extra ear hears the same audio (fed from the server's audio tap) and
keeps a running transcript, out of which each Soniox packet's reading is lined
up (witness_align) -- shown under the packet once final, and offered to the
translator as a second reading.

Each provider is a small subclass of Ear: where to connect, what to send first,
how to wrap an audio frame and how to read a message. The base class does the
rest the same way for all of them: audio arrives through a bounded queue that
drops rather than waits (a slow ear can never hold up Soniox), a dropped
connection is reopened while the stream lives (Gemini Live sessions end after
~10 minutes), and the transcript is kept as committed text plus the current
partial. Measured in validation/results/2026-10-06-second-witness/ and
2026-10-07-three-witnesses/.

One session per ear for the whole stream (2026-10-08). The first live build
opened a fresh session for every Soniox packet, and its handshake put the ears
behind Soniox; now the session stays open and each packet seal only slices it
(slice(): the provider finalises what it has heard so far, and a long sermon
never piles into one ever-growing utterance). A packet's reading is lined up
out of the running transcript (witness_align), as in the lab. The server's
CT_EARS=0 switches them all off.
"""
import base64
import json
import os
import queue
import threading
import time
from collections import deque

import costs

# Languages as each provider spells them; an ear asked for one it cannot name
# is left to detect the language itself.
_BCP47 = {"ro": "ro-RO", "en": "en-US", "hu": "hu-HU", "es": "es-ES", "fr": "fr-FR",
          "de": "de-DE", "it": "it-IT", "pt": "pt-PT", "ru": "ru-RU", "uk": "uk-UA"}


class Ear:
    name = "?"            # the id the page and /translate use
    label = "?"           # what the panel shows
    MODEL = "?"           # the provider's model id (named in the session record)
    key_env = None        # the API key this ear needs
    rate = 16000          # the PCM rate the provider takes
    KEEP_S = 40           # committed text older than this is forgotten
    SLICE_MIN_S = 0.5     # a slice waits until at least this much audio follows the last one
    BACKLOG_S = 2.0       # audio still queued past this at a slice is the old packet's tail: dropped
    HANDOVER_GRACE_S = 3.0      # a handed-over session's time to deliver what it already heard
    HANDOVER_MAX_WAIT_S = 15.0  # no packet seal this long after the next session is ready: move anyway

    def __init__(self, sample_rate, lang, relay):
        self.in_rate, self.lang, self.relay = sample_rate, lang, relay
        self.q = queue.Queue(maxsize=100)       # ~10 s of 100 ms frames
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.committed = deque()                # (monotonic time, text)
        self.partial = ""
        self.dropped = 0
        self.queued = 0                         # bytes waiting in q (under qlock: three threads touch it)
        self.qlock = threading.Lock()
        self.since_cut = 0                      # bytes sent since the last slice reached the provider
        self.want_slice = threading.Event()
        self.relang = threading.Event()         # set_lang: reopen the session in the new language
        self.handover = threading.Event()       # the provider will end this session soon: open the next
        self.handovers = 0
        self.connected = False                  # audio queued while (re)connecting is kept, not skipped
        self.slices = self.skipped = 0          # slices sent; backlog bytes dropped at a slice
        threading.Thread(target=self._run, daemon=True, name=f"ear-{self.name}").start()

    @classmethod
    def available(cls):
        return bool(os.environ.get(cls.key_env)) if cls.key_env else True

    # ---- the stream's side ------------------------------------------------
    def feed(self, pcm):
        with self.qlock:
            try:
                self.q.put_nowait(pcm)
                self.queued += len(pcm)
            except queue.Full:
                self.dropped += 1

    def slice(self):
        """A Soniox packet just sealed: have the provider finalise what it has
        heard so far (sent by the sender thread, after the audio already queued).
        An ear more than BACKLOG_S behind drops the queued rest of the old packet
        instead, so it starts level with the next one -- unless it is between
        sessions, when the queue is only waiting for the new one."""
        with self.qlock:
            behind = self.connected and self.queued > self.BACKLOG_S * self.in_rate * 2
            n = 0
            while behind:
                try:
                    n += len(self.q.get_nowait())
                except queue.Empty:
                    break
            self.queued = max(0, self.queued - n)
        if behind:
            self.skipped += n
            print(f"[ear ] {self.label}: {n / (self.in_rate * 2):.1f}s behind at a packet end -> skipped", flush=True)
        self.want_slice.set()

    def close(self):
        self.stop.set()

    def set_lang(self, lang):
        """Listen in `lang` from now on (the language Soniox hears, latched by the
        server). Left to guess, ElevenLabs hears Romanian as Russian/Ukrainian and
        writes Cyrillic. The language is part of the session, so it reconnects --
        once per change; audio arriving meanwhile waits in the queue."""
        if lang and lang != self.lang:
            print(f"[ear ] {self.label}: listening in {lang} (was {self.lang or 'auto'})", flush=True)
            self.lang = lang
            self.relang.set()

    def snapshot(self):
        """The recent transcript: committed text, then the current partial."""
        with self.lock:
            now = time.monotonic()
            while self.committed and now - self.committed[0][0] > self.KEEP_S:
                self.committed.popleft()
            return " ".join([t for _, t in self.committed] + ([self.partial] if self.partial else []))

    # ---- what a subclass reports -------------------------------------------
    def _on_partial(self, text):
        with self.lock:
            self.partial = (text or "")[-600:]
        self._relay()

    def _on_final(self, text):
        text = (text or "").strip()
        with self.lock:
            # a provider may repeat a segment it already committed
            if text and not (self.committed and self.committed[-1][1] == text):
                self.committed.append((time.monotonic(), text))
            self.partial = ""
        self._relay()

    def _keep_unfinished(self):
        """A session is closing: what it was still finishing becomes committed
        text, so a reconnect never loses words already heard."""
        with self.lock:
            if self.partial.strip():
                self.committed.append((time.monotonic(), self.partial.strip()))
            self.partial = ""

    def _relay(self):
        if self.relay is None:
            return
        try:
            self.relay(self.name, self.snapshot()[-400:])
        except Exception:
            pass

    # ---- what a subclass defines -------------------------------------------
    def url(self):
        raise NotImplementedError

    def headers(self):
        return {}

    def setup(self):
        """Messages to send once connected, before any audio."""
        return []

    def wait_ready(self, ws):
        """Block until the provider will take audio (most take it at once)."""

    def audio(self, pcm):
        raise NotImplementedError

    def end_audio(self):
        """Messages telling a session no more audio follows (a handover), so it
        finishes transcribing what it has. [] = none needed."""
        return []

    def cut(self):
        """Messages that slice the live stream at a packet end: the provider
        finalises the audio so far and goes on listening. [] = its own voice
        activity detection already finalises at pauses."""
        return []

    def handle(self, msg):
        raise NotImplementedError

    # ---- the engine ----------------------------------------------------------
    def _pcm(self, data):
        if self.in_rate == self.rate:
            return data
        import numpy as np
        x = np.frombuffer(data, dtype="<i2").astype(np.float32)
        if self.in_rate % self.rate == 0:       # 48 kHz -> 16/24 kHz: average each run
            k = self.in_rate // self.rate
            x = x[: len(x) // k * k].reshape(-1, k).mean(axis=1)
        else:
            n = int(len(x) * self.rate / self.in_rate)
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        return x.astype(np.int16).tobytes()

    def _open(self):
        """A new session: connected, set up, ready for audio."""
        from websockets.sync.client import connect
        ws = connect(self.url(), open_timeout=10, max_size=None, additional_headers=self.headers())
        try:
            for m in self.setup():
                ws.send(json.dumps(m))
            self.wait_ready(ws)
        except Exception:
            ws.close()
            raise
        return ws

    def _rx(self, ws, done):
        """One session's reader: every message to handle() until it closes."""
        try:
            while not done.is_set():
                try:
                    raw = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                try:
                    self.handle(json.loads(raw))
                except ValueError:
                    pass
        except Exception:
            pass
        finally:
            done.set()

    def _open_next(self, box):
        """The handover's new session, opened beside the live one (box: the
        result; closed at once if the stream moved on meanwhile)."""
        try:
            ws = self._open()
        except Exception as exc:
            print(f"[ear ] {self.label}: next session failed: {str(exc)[:120]}", flush=True)
            ws = None
        if ws is not None and (box["abandon"].is_set() or self.stop.is_set()):
            try:
                ws.close()
            except Exception:
                pass
            ws = None
        box["ws"], box["at"] = ws, time.monotonic()
        box["done"].set()

    def _retire(self, ws, done):
        """A handed-over session: no more audio follows, so it finishes what it
        heard; its last words arrive within HANDOVER_GRACE_S, then it is closed
        (cleanly -- the provider never has to cut it off)."""
        def run():
            try:
                for m in self.end_audio():
                    ws.send(json.dumps(m))
            except Exception:
                pass
            done.wait(self.HANDOVER_GRACE_S)
            done.set()
            try:
                ws.close()
            except Exception:
                pass
        threading.Thread(target=run, daemon=True, name=f"ear-{self.name}-retire").start()

    def _run(self):
        backoff = 1.0
        while not self.stop.is_set():
            try:
                ws = self._open()
            except Exception as exc:
                print(f"[ear ] {self.label}: connect failed: {str(exc)[:120]}", flush=True)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            print(f"[ear ] {self.label} open", flush=True)
            backoff = 1.0
            self.connected = True
            self.handover.clear()
            done = threading.Event()
            threading.Thread(target=self._rx, args=(ws, done), daemon=True).start()
            nxt = None              # a handover in progress: {"ws", "at", "done", "abandon"}
            retry_at = 0.0
            try:
                while not self.stop.is_set() and not self.relang.is_set():
                    sliced = False
                    if not done.is_set():
                        try:
                            data = self.q.get(timeout=0.2)
                            with self.qlock:
                                self.queued = max(0, self.queued - len(data))
                            ws.send(json.dumps(self.audio(self._pcm(data))))
                            self.since_cut += len(data)
                            costs.ear_seconds(self.name, len(data) / (self.in_rate * 2))
                        except queue.Empty:
                            pass
                        # A slice goes out once the audio queued before it has
                        # (and never twice on the same half second).
                        if (self.want_slice.is_set() and self.q.empty()
                                and self.since_cut >= self.SLICE_MIN_S * self.in_rate * 2):
                            self.want_slice.clear()
                            sliced = True
                            msgs = self.cut()
                            for m in msgs:
                                ws.send(json.dumps(m))
                            if msgs:
                                self.slices += 1
                                self.since_cut = 0
                    # Make before break: the provider said this session ends soon
                    # (handover set by handle()), so the next one is opened beside
                    # it and the audio moves over at a packet seal -- no word is
                    # split between two sessions, and nothing waits for a reconnect.
                    if self.handover.is_set() and nxt is None and time.monotonic() >= retry_at:
                        nxt = {"ws": None, "at": None, "done": threading.Event(), "abandon": threading.Event()}
                        threading.Thread(target=self._open_next, args=(nxt,), daemon=True).start()
                    if nxt is not None and nxt["done"].is_set():
                        if nxt["ws"] is None:                    # it failed: try again shortly
                            nxt, retry_at = None, time.monotonic() + 2.0
                        elif sliced or done.is_set() or time.monotonic() - nxt["at"] > self.HANDOVER_MAX_WAIT_S:
                            old, old_done = ws, done
                            ws, nxt = nxt["ws"], None
                            done = threading.Event()
                            threading.Thread(target=self._rx, args=(ws, done), daemon=True).start()
                            self.handover.clear()
                            self.handovers += 1
                            self._retire(old, old_done)
                            print(f"[ear ] {self.label}: handed over to a new session", flush=True)
                    if done.is_set() and nxt is None:
                        break                                    # dropped, no session ready: reconnect
            except Exception as exc:
                print(f"[ear ] {self.label}: send failed: {str(exc)[:120]}", flush=True)
            finally:
                done.set()
                self.connected = False
                if nxt is not None:
                    nxt["abandon"].set()
                    if nxt["ws"] is not None:
                        try:
                            nxt["ws"].close()
                        except Exception:
                            pass
                try:
                    ws.close()
                except Exception:
                    pass
            self._keep_unfinished()
            if self.relang.is_set():
                self.relang.clear()                     # a language change: straight back
            elif not self.stop.is_set():
                print(f"[ear ] {self.label}: reconnecting", flush=True)
                self.stop.wait(backoff)
        print(f"[ear ] {self.label} closed ({self.slices} slices, {self.handovers} handovers, "
              f"{self.dropped} audio frames dropped, "
              f"{self.skipped / (self.in_rate * 2):.1f}s skipped behind)", flush=True)

    def _log_other(self, msg):
        print(f"[ear ] {self.label}: {json.dumps(msg)[:200]}", flush=True)


class ElevenLabsEar(Ear):
    """ElevenLabs Scribe v2 Realtime. As a reading beside the hearing translator:
    44, 42, 43 of 47 real mis-hears (the audio alone 39-41). Interims can flip a
    right word to a wrong one before committing."""
    name, label, key_env, rate = "elevenlabs", "ElevenLabs", "ELEVENLABS_API_KEY", 16000
    MODEL = "scribe_v2_realtime"

    def url(self):
        return (f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id={self.MODEL}"
                "&audio_format=pcm_16000&include_timestamps=false"
                "&commit_strategy=vad"
                + (f"&language_code={self.lang}" if self.lang else ""))

    def headers(self):
        return {"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}

    def audio(self, pcm):
        return {"message_type": "input_audio_chunk", "commit": False, "sample_rate": 16000,
                "audio_base_64": base64.b64encode(pcm).decode()}

    def handle(self, m):
        t = m.get("message_type")
        if t == "partial_transcript":
            self._on_partial(m.get("text"))
        elif t in ("committed_transcript", "committed_transcript_with_timestamps"):
            self._on_final(m.get("text"))
        elif t and t != "session_started":
            self._log_other(m)


class GeminiLiveEar(Ear):
    """Gemini 3.5 Transcribe Live: the best streaming witness on the corpus (32 of
    47 a second after the caption) and 2.4% WER on FLEURS-ro. Its pieces can come
    glued without spaces ("supusecuvântului"); witness_align compares letters, so
    a glued reading still lines up. Sessions end after ~10 minutes, announced by a
    goAway 50 s ahead: the next session is opened then and takes over at a packet
    seal (Ear._run), so the provider never has to cut one off."""
    name, label, key_env, rate = "gemini-live", "Gemini Live", "GEMINI_API_KEY", 16000
    MODEL = "models/gemini-3.5-transcribe-live"

    def url(self):
        return ("wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta."
                f"GenerativeService.BidiGenerateContent?key={os.environ['GEMINI_API_KEY']}")

    def setup(self):
        tr = {"languageCodes": [_BCP47[self.lang]]} if self.lang in _BCP47 else {}
        return [{"setup": {"model": self.MODEL,
                           "generationConfig": {"responseModalities": ["TEXT"]},
                           "inputAudioTranscription": tr,
                           "realtimeInputConfig": {"automaticActivityDetection": {"disabled": False}}}}]

    def wait_ready(self, ws):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            m = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if "setupComplete" in m:
                return
            if "error" in m:
                raise RuntimeError(json.dumps(m)[:200])
        raise RuntimeError("no setupComplete")

    def audio(self, pcm):
        return {"realtimeInput": {"audio": {"data": base64.b64encode(pcm).decode(),
                                            "mimeType": "audio/pcm;rate=16000"}}}

    def end_audio(self):
        return [{"realtimeInput": {"audioStreamEnd": True}}]

    def handle(self, m):
        if "goAway" in m:                     # ~50 s before the ~10-minute session limit
            print(f"[ear ] {self.label}: session ends in {(m['goAway'] or {}).get('timeLeft', '?')}"
                  " -> handing over", flush=True)
            self.handover.set()
            return
        sc = m.get("serverContent") or {}
        if "interimInputTranscription" in sc:
            self._on_partial(sc["interimInputTranscription"].get("text"))
        if "inputTranscription" in sc:
            self._on_final(sc["inputTranscription"].get("text"))
        if "error" in m:
            self._log_other(m)


class OpenAILiveEar(Ear):
    """OpenAI gpt-live-transcribe (31 of 47 a second after the caption). With turn
    detection off it streams each item's text as it hears it and finalises only
    on a commit -- so every packet seal commits (cut), or one item would grow for
    the whole sermon. The items still finishing make up the partial, in order, so
    a slice never blanks words that were already showing."""
    name, label, key_env, rate = "openai-live", "OpenAI live", "OPENAI_API_KEY", 24000
    MODEL = "gpt-live-transcribe"

    def __init__(self, *a, **k):
        self._items = {}                        # item id -> [text, done], in arrival order
        super().__init__(*a, **k)

    def url(self):
        return "wss://api.openai.com/v1/realtime?intent=transcription"

    def headers(self):
        return {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}

    def setup(self):
        tr = {"model": self.MODEL, **({"language": self.lang} if self.lang else {})}
        return [{"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24000}, "transcription": tr, "turn_detection": None}}}}]

    def audio(self, pcm):
        return {"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}

    def cut(self):
        return [{"type": "input_audio_buffer.commit"}]

    def _settle(self):
        """Finished items, oldest first, become committed text; the rest the partial."""
        with self.lock:
            while self._items:
                k = next(iter(self._items))
                text, done = self._items[k]
                if not done:
                    break
                del self._items[k]
                text = text.strip()
                if text and not (self.committed and self.committed[-1][1] == text):
                    self.committed.append((time.monotonic(), text))
            self.partial = " ".join(t.strip() for t, _ in self._items.values() if t.strip())[-600:]
        self._relay()

    def _keep_unfinished(self):
        """Items of a closing session will never complete: keep their text."""
        with self.lock:
            for it in self._items.values():
                it[1] = True
        self._settle()

    def handle(self, m):
        t = m.get("type", "")
        # under self.lock: the sender thread settles these too, at a reconnect
        if t.endswith("input_audio_transcription.delta"):
            with self.lock:
                it = self._items.setdefault(m.get("item_id"), ["", False])
                it[0] = (it[0] + m.get("delta", ""))[-2000:]
            self._settle()
        elif t.endswith("input_audio_transcription.completed"):
            with self.lock:
                self._items[m.get("item_id")] = [m.get("transcript") or "", True]
            self._settle()
        elif t == "error":
            self._log_other(m)


EARS = {cls.name: cls for cls in (ElevenLabsEar, GeminiLiveEar, OpenAILiveEar)}


def available():
    """[(name, label)] of the ears whose keys are configured."""
    return [(n, c.label) for n, c in EARS.items() if c.available()]


def models():
    """{name: (label, provider model id)} of every ear, configured or not."""
    return {n: (c.label, c.MODEL.split("/")[-1]) for n, c in EARS.items()}


def open_ears(names, sample_rate, lang, relay):
    """Start the named ears (unknown or unconfigured names are skipped)."""
    out = {}
    for n in names or []:
        cls = EARS.get(n)
        if cls and cls.available() and n not in out:
            out[n] = cls(sample_rate, lang, relay)
    return out
