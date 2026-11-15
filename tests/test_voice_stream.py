"""The continuous ElevenLabs voice (scripts/voice_stream.py): every clip cut out
of the stream holds whole mp3 frames, and the stream's bytes stay the same, in
the same order.

    python -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import voice_stream as vs   # noqa: E402


def frame(pad, fill):
    """MPEG-1 Layer III, 128 kb/s, 44.1 kHz, stereo: 417 bytes, 418 padded."""
    hdr = bytes((0xFF, 0xFB, 0x90 | (pad << 1), 0x00))
    return hdr + bytes([fill]) * (413 + pad)


def stream(n, seed=1):
    rnd = random.Random(seed)
    return b"".join(frame(rnd.randint(0, 1), i % 200 + 1) for i in range(n))


def frames_in(b):
    """The frame lengths b splits into, or None if it doesn't split exactly."""
    out, i = [], 0
    while i < len(b):
        ln = vs._frame_len(b, i)
        if not ln or i + ln > len(b):
            return None
        out.append(ln)
        i += ln
    return out


def conn():
    """A _Conn with no socket, to drive _route by hand."""
    c = vs._Conn.__new__(vs._Conn)
    import threading
    c.lock, c.clips, c.sent, c.voiced, c.carry, c.dead = threading.Lock(), [], 0, 0, b"", None
    return c


def drain(clip):
    out = []
    while not clip.q.empty():
        item = clip.q.get()
        if item is None:
            break
        out.append(item)
    return out


class WholeFrames(unittest.TestCase):
    def test_a_cut_frame_is_held(self):
        s = stream(3)
        head, rest = vs.whole_frames(s[:1000])
        self.assertEqual(head + rest, s[:1000])
        self.assertEqual(frames_in(head) and len(frames_in(head)), 2)

    def test_exact_frames_pass_whole(self):
        s = stream(5)
        self.assertEqual(vs.whole_frames(s), (s, b""))

    def test_junk_and_tags_are_passed_not_held(self):
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"x" * 5
        s = b"\x00\x01" + tag + stream(2)
        self.assertEqual(vs.whole_frames(s), (s, b""))


class Routing(unittest.TestCase):
    def test_clips_get_whole_frames_and_every_byte_once_in_order(self):
        s = stream(120)
        c = conn()
        texts = ["One two three. ", "Four five six seven. ", "Eight nine. "]
        pos = 0
        for t in texts:
            clip = vs.Clip(t, pos, pos + len(t))
            c.clips.append(clip)
            pos += len(t)
        clips = list(c.clips)
        rnd = random.Random(7)
        chunks, i = [], 0
        while i < len(s):                       # network-sized chunks, cut anywhere
            n = rnd.randint(300, 3000)
            chunks.append(s[i:i + n])
            i += n
        per = pos / len(chunks)                 # characters voiced per chunk, roughly
        voiced = 0.0
        for k, ch in enumerate(chunks):
            nxt = voiced + per
            c._route(ch, int(nxt) - int(voiced))
            voiced = nxt
        c._route(b"", 0, final=True)
        c._turn_done()
        got = [b"".join(drain(cl)) for cl in clips]
        self.assertEqual(b"".join(got), s)              # same bytes, same order
        for g in got:
            self.assertTrue(g)
            self.assertIsNotNone(frames_in(g), "a clip holds a cut frame")


if __name__ == "__main__":
    unittest.main()


class BareFrames(unittest.TestCase):
    """What the website gets: a segment's sentence clips joined, as plain frames
    (app/recorder.py bare_frames), without each sentence's tag and length header."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "recorder_bf", os.path.join(os.path.dirname(__file__), "..", "app", "recorder.py"))
        self.rec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.rec)

    @staticmethod
    def sentence(n, fill):
        """A complete mp3 as an encoder writes it: an ID3 tag, an Info header frame
        (MPEG-1 stereo: the tag word after 4 + 32 bytes), then n audio frames."""
        tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"T" * 10
        info = bytearray(frame(0, 0)); info[36:40] = b"Info"
        return tag + bytes(info) + b"".join(frame(0, fill) for _ in range(n))

    def test_tags_and_headers_go_audio_stays(self):
        joined = self.sentence(5, 1) + self.sentence(7, 2)
        bare = self.rec.bare_frames(joined)
        self.assertEqual(bare, b"".join(frame(0, 1) for _ in range(5)) + b"".join(frame(0, 2) for _ in range(7)))
        self.assertNotIn(b"Info", bare)
        self.assertNotIn(b"ID3", bare)

    def test_plain_frames_pass_unchanged(self):
        s = stream(9)
        self.assertEqual(self.rec.bare_frames(s), s)

    def test_a_xing_header_with_crc_is_found(self):
        f = bytearray(frame(0, 0)); f[1] &= 0xFE        # protection on: a 2-byte CRC
        f[38:42] = b"Xing"
        self.assertEqual(self.rec.bare_frames(bytes(f) + frame(0, 3)), frame(0, 3))
