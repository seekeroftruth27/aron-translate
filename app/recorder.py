#!/usr/bin/env python3
"""Stage 3½: keep every spoken clip, so a service can be saved off afterwards.

The live path throws its audio away: `_tts_chunk_lines` relays the provider's
mp3 bytes to the browser and forgets them. This module is the tap on that path.
Each finished clip is appended to an in-memory buffer together with the SENTENCE
it speaks, the source line it was translated from, and two timestamps -- so the
saved file is not just an hour of audio but an hour of audio you can search.

Why here and not in the browser:
  * the bytes already exist on this side -- recording them costs one reference,
    while shipping them back would double the traffic;
  * a tab that is closed, crashed or reloaded takes a browser-side buffer with
    it, and this app is closed by closing its window;
  * "auto-save on exit" then means what it says: the process writes the session
    out on the way down (atexit + SIGTERM/SIGINT), with nothing for the operator
    to remember.

Two timelines per clip, because they answer different questions:
  `t`     seconds into the SAVED FILE. Exact -- summed from the mp3's own frame
          headers (see mp3_duration), not estimated from bitrate or measured by
          a decoder -- so a cue lands where the words are, in any player.
  `wall`  when it was spoken, wall-clock. This is the one that lines a clip up
          with the service's own recording, the stream, or the order of events.
The gap between them grows all service: the file holds only the spoken parts,
so silence between sentences exists in `wall` and not in `t`.

Concatenated mp3 frames are a valid mp3 -- each frame is independently
decodable, which is exactly why the browser can splice them into one
MediaSource -- so "save" is a join, with no re-encode and no decoder here.

Env:
  CT_RECORD           "0" to keep the buffer off entirely (default on)
  CT_RECORD_AUTOSAVE  "0" to disable writing the buffer out on exit (default on)
  CT_RECORD_DIR       where saved sessions land (default <project>/recordings)
  CT_RECORD_MAX_MB    stop buffering past this much audio (default 512 ≈ 9h)
"""
import io
import json
import os
import sys
import threading
import time
import zipfile
from datetime import datetime

# ---- mp3 frame arithmetic ---------------------------------------------------
# Layer III only: both backends emit Layer III (see scripts/synthesize.py), and
# a table that covers what we actually produce is easier to trust than one that
# covers everything we don't.
_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None)
_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None)
_RATES = {3: (44100, 48000, 32000),    # MPEG 1
          2: (22050, 24000, 16000),    # MPEG 2
          0: (11025, 12000, 8000)}     # MPEG 2.5


def _id3_len(data):
    """Length of a leading ID3v2 tag, so frame scanning starts at real audio."""
    if len(data) >= 10 and data[:3] == b"ID3":
        size = 0
        for b in data[6:10]:
            size = (size << 7) | (b & 0x7F)      # syncsafe: 7 bits per byte
        return 10 + size
    return 0


def mp3_duration(data):
    """Exact playing time of `data` in seconds, summed frame by frame.

    Walks the frame headers rather than dividing bytes by a nominal bitrate:
    the providers emit CBR today, but a VBR clip (or a stray tag between
    sentences) would silently skew every timestamp after it, and a cue that
    drifts is worse than no cue. Unparseable bytes are skipped, never guessed
    at -- a clip that yields nothing readable is reported as 0 rather than as
    a plausible-looking wrong number.
    """
    n = len(data)
    i = _id3_len(data)
    total = 0.0
    while i + 4 <= n:
        if data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
            i += 1                                # not a sync word -- resync
            continue
        h1, h2, h3 = data[i + 1], data[i + 2], data[i + 3]
        ver = (h1 >> 3) & 0x03                    # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (h1 >> 1) & 0x03                  # 1 = Layer III
        br_i = (h2 >> 4) & 0x0F
        sr_i = (h2 >> 2) & 0x03
        pad = (h2 >> 1) & 0x01
        if ver == 1 or layer != 1 or sr_i == 3 or br_i in (0, 15):
            i += 1                                # reserved/free-form: false sync
            continue
        rate = _RATES[ver][sr_i]
        kbps = (_V1_L3 if ver == 3 else _V2_L3)[br_i]
        if not kbps:
            i += 1
            continue
        # Layer III: 1152 samples per frame on MPEG 1, 576 on MPEG 2/2.5.
        samples = 1152 if ver == 3 else 576
        length = (samples // 8) * kbps * 1000 // rate + pad
        if length <= 4:
            i += 1
            continue
        total += samples / rate
        i += length
    return round(total, 3)


# ---- the buffer -------------------------------------------------------------

def _env_flag(name, default=True):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


def _ts(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d_%H-%M-%S")


def _clock(seconds, comma=True):
    """SRT/VTT timestamp: HH:MM:SS,mmm (SRT) or HH:MM:SS.mmm (WebVTT)."""
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{',' if comma else '.'}{ms:03d}"


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Recording:
    """The service's spoken audio, piling up clip by clip.

    Thread-safe: clips arrive from the per-request TTS workers while the
    operator's status polls and a save both read. One lock, held only around
    list mutation and snapshotting -- never around a file write.
    """

    def __init__(self, mime="audio/mpeg"):
        self.mime = mime
        self._lock = threading.Lock()
        self._clips = []          # [{i, text, src, lang, provider, wall, t, dur, off, n}]
        self._blobs = []          # the mp3 bytes, parallel to _clips
        self._bytes = 0
        self._seconds = 0.0
        self._started = None      # epoch of the first clip in this buffer
        self._full = False        # hit the size cap: recording, but not buffering
        self.enabled = _env_flag("CT_RECORD", True)
        self.autosave = _env_flag("CT_RECORD_AUTOSAVE", True)
        try:
            self.max_bytes = int(float(os.environ.get("CT_RECORD_MAX_MB") or 512) * 1024 * 1024)
        except ValueError:
            self.max_bytes = 512 * 1024 * 1024
        self.dir = os.environ.get("CT_RECORD_DIR") or ""
        self._saved = None        # path of the last save, for the UI to show
        self._saved_clips = -1    # how many clips that save covered (see save)
        # () -> {filename: bytes}: files saved and zipped beside the audio (the
        # server sets it to the page's session record: session.json + .html).
        self.extras = None

    def _extra_files(self):
        try:
            return dict(self.extras()) if self.extras else {}
        except Exception as exc:                           # never cost the audio its save
            print(f"[rec] ERROR building extra files: {exc}", flush=True)
            return {}

    # -- capture --------------------------------------------------------------
    def add(self, audio, text="", src="", lang="", provider="", spoke_at=None):
        """Append one finished clip. Never raises: this runs inside the live
        speech path, where a bookkeeping slip must not cost the room its audio."""
        if not self.enabled or not audio:
            return
        try:
            dur = mp3_duration(audio)
            with self._lock:
                if self._bytes + len(audio) > self.max_bytes:
                    if not self._full:
                        self._full = True
                        print(f"[rec] buffer full at {self._bytes // (1024 * 1024)} MB"
                              " -- save and clear to keep recording", flush=True)
                    return
                self._full = False
                wall = spoke_at or time.time()
                if self._started is None:
                    self._started = wall
                self._clips.append({
                    "i": len(self._clips),
                    "text": text.strip(),
                    "src": (src or "").strip(),
                    "lang": lang or "",
                    "provider": provider or "",
                    "wall": round(wall, 3),
                    "at": round(wall - self._started, 3),   # s since the buffer opened
                    "t": round(self._seconds, 3),           # s into the saved mp3
                    "dur": dur,
                    "off": self._bytes,                     # byte offset in the mp3
                    "n": len(audio),
                })
                self._blobs.append(audio)
                self._bytes += len(audio)
                self._seconds += dur
        except Exception as exc:                            # never break the live path
            print(f"[rec] ERROR buffering clip: {exc}", flush=True)

    # -- state ----------------------------------------------------------------
    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "autosave": self.autosave,
                "clips": len(self._clips),
                "bytes": self._bytes,
                "seconds": round(self._seconds, 1),
                "started": self._started and datetime.fromtimestamp(self._started).isoformat(timespec="seconds"),
                "full": self._full,
                "max_bytes": self.max_bytes,
                "saved": self._saved,
                # …and whether that save still covers everything in the buffer.
                # Once a line has been spoken since, it does not, and the UI must
                # stop claiming the service is safely on disk.
                "saved_current": bool(self._saved) and self._saved_clips == len(self._clips),
                "dir": self.save_dir(),
            }

    def clear(self):
        with self._lock:
            self._clips, self._blobs = [], []
            self._bytes, self._seconds, self._started, self._full = 0, 0.0, None, False
            self._saved_clips = -1

    def empty(self):
        with self._lock:
            return not self._clips

    def _snapshot(self):
        with self._lock:
            return list(self._clips), list(self._blobs), self._started

    # -- rendering ------------------------------------------------------------
    def name(self, started=None):
        return "service-" + _ts(started or self._started or time.time())

    def mp3(self):
        """The whole buffer as one file: a plain join of the clips' frames."""
        return b"".join(self._snapshot()[1])

    def segments_json(self, clips=None, started=None):
        clips = self._clips if clips is None else clips
        started = started if started is not None else self._started
        return json.dumps({
            "app": "Aron / Church Translate",
            "mime": self.mime,
            "started": started and datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            "clips": len(clips),
            "seconds": round(sum(c["dur"] for c in clips), 3),
            # `t` is the position in audio.mp3, `wall`/`at` are when it was said.
            # `off`/`n` locate the clip's own bytes inside audio.mp3, so a single
            # sentence can be cut back out of the file without a decoder.
            "fields": {"t": "seconds into audio.mp3", "dur": "clip length, s",
                       "at": "seconds since the recording opened (wall clock)",
                       "wall": "unix time the clip was voiced", "off": "byte offset in audio.mp3",
                       "n": "byte length", "text": "spoken translation",
                       "src": "source line it came from", "lang": "target language"},
            "segments": clips,
        }, ensure_ascii=False, indent=1)

    def srt(self, clips=None):
        clips = self._clips if clips is None else clips
        out = []
        for n, c in enumerate(clips, 1):
            out.append(f"{n}\n{_clock(c['t'])} --> {_clock(c['t'] + c['dur'])}\n{c['text']}\n")
        return "\n".join(out)

    def vtt(self, clips=None):
        clips = self._clips if clips is None else clips
        out = ["WEBVTT\n"]
        for n, c in enumerate(clips, 1):
            out.append(f"{n}\n{_clock(c['t'], False)} --> {_clock(c['t'] + c['dur'], False)}\n{c['text']}\n")
        return "\n".join(out)

    def transcript_html(self, clips=None, started=None):
        """Side-by-side source/translation with both clocks -- the same table the
        app's HTML export gives, plus where each line sits in the audio."""
        clips = self._clips if clips is None else clips
        started = started if started is not None else self._started
        when = datetime.fromtimestamp(started).strftime("%d %b %Y, %H:%M") if started else ""
        rows = "\n".join(
            "<tr><td class=t>{}</td><td class=t>{}</td><td>{}</td><td>{}</td></tr>".format(
                _clock(c["t"], False)[:-4],
                datetime.fromtimestamp(c["wall"]).strftime("%H:%M:%S"),
                _esc(c["src"]), _esc(c["text"]))
            for c in clips)
        return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
                "<title>Áron · Service transcript — " + _esc(when) + "</title>"
                "<style>body{font:16px/1.55 system-ui,-apple-system,sans-serif;margin:2rem;color:#111}"
                "h1{font-size:1.3rem;margin:0 0 .2rem}p{color:#666;margin:0 0 1rem}"
                "table{border-collapse:collapse;width:100%}"
                "th,td{border:1px solid #ccc;padding:.45rem .6rem;vertical-align:top;text-align:left}"
                "th{background:#f0f0f0}td.t{white-space:nowrap;color:#777;font-variant-numeric:tabular-nums;width:1%}"
                "</style></head><body><h1>Áron · Service transcript</h1>"
                "<p>" + _esc(when) + " · " + str(len(clips)) + " spoken lines · "
                + _clock(sum(c["dur"] for c in clips), False)[:-4] + " of audio</p>"
                "<table><thead><tr><th>In audio</th><th>Spoken at</th>"
                "<th>Original</th><th>Translation</th></tr></thead><tbody>\n"
                + rows + "\n</tbody></table></body></html>")

    def zip_bytes(self):
        """Everything for one service in a single download: the audio, the same
        cues in the two formats players actually read, the machine-readable map,
        and a transcript to open in a browser. Stored, not deflated -- mp3 is
        already compressed, and this runs on the operator's machine mid-service."""
        clips, blobs, started = self._snapshot()
        base = self.name(started)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            z.writestr(base + "/audio.mp3", b"".join(blobs))
            z.writestr(base + "/captions.srt", self.srt(clips))
            z.writestr(base + "/captions.vtt", self.vtt(clips))
            z.writestr(base + "/segments.json", self.segments_json(clips, started))
            z.writestr(base + "/transcript.html", self.transcript_html(clips, started))
            for fn, data in self._extra_files().items():
                z.writestr(base + "/" + fn, data)
        return buf.getvalue()

    # -- disk -----------------------------------------------------------------
    def save_dir(self):
        """Where saved services land. CT_RECORD_DIR wins; otherwise
        <project>/recordings -- and in a frozen build that means next to the
        .exe, never __file__, which points inside PyInstaller's temp extraction
        dir and is deleted the moment the app closes."""
        if self.dir:
            return os.path.abspath(self.dir)
        if getattr(sys, "frozen", False):
            root = os.path.dirname(os.path.abspath(sys.executable))
        else:
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "recordings")

    @staticmethod
    def _write(out, parts):
        os.makedirs(out, exist_ok=True)
        for fn, data in parts.items():
            tmp = os.path.join(out, fn + ".part")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(out, fn))     # never a half-written file

    def save(self, reason="manual"):
        """Write the buffer out as a folder of files. Returns the folder, or
        None when there was nothing to save."""
        clips, blobs, started = self._snapshot()
        if not clips:
            return None
        if len(clips) == self._saved_clips and self._saved:
            # Nothing spoken since the last save: closing the window after saving
            # by hand must not rewrite the audio -- only the session record, which
            # goes on changing without speech.
            self._write(self._saved, self._extra_files())
            return self._saved
        out = os.path.join(self.save_dir(), self.name(started))
        parts = {
            "audio.mp3": b"".join(blobs),
            "captions.srt": self.srt(clips).encode("utf-8"),
            "captions.vtt": self.vtt(clips).encode("utf-8"),
            "segments.json": self.segments_json(clips, started).encode("utf-8"),
            "transcript.html": self.transcript_html(clips, started).encode("utf-8"),
            **self._extra_files(),
        }
        self._write(out, parts)
        self._saved, self._saved_clips = out, len(clips)
        mins = sum(c["dur"] for c in clips) / 60
        print(f"[rec] saved {len(clips)} clips ({mins:.1f} min) -> {out} [{reason}]", flush=True)
        return out
