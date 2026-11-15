"""YouTube captions: /youtube posts each finalized caption live to the ingestion URL
(CT_YOUTUBE_CAPTION_URL) and keeps it for /youtube/captions.srt, the replay.

    python -m unittest discover -s tests -v
"""
import sys
import time
import unittest

from support import FakeProPresenter, load_server, need_flask

T0 = 1794000000000   # an epoch-ms moment; tests that post live use "now" instead


def post(server, **body):
    return server.app.test_client().post("/youtube", json=body)


def srt(server, start=None):
    q = f"?start={start}" if start is not None else ""
    return server.app.test_client().get("/youtube/captions.srt" + q).get_data(as_text=True)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@need_flask
class LivePosting(unittest.TestCase):
    def setUp(self):
        self.yt = FakeProPresenter(status=200).__enter__()   # any HTTP endpoint that records requests
        self.addCleanup(self.yt.__exit__)
        self.s = load_server(CT_YOUTUBE_CAPTION_URL=self.yt.url + "/closedcaption?cid=abc")

    def test_a_caption_is_posted_as_youtube_text_with_a_rising_seq(self):
        now = int(time.time() * 1000)
        post(self.s, id="s1", text="Let there be light.", lang="en", t=now - 4000)
        post(self.s, id="s2", text="And there was light.", lang="en", t=now - 1000)
        self.assertTrue(wait_for(lambda: len(self.yt.requests) == 2))
        (m1, p1, b1), (m2, p2, b2) = self.yt.requests
        self.assertEqual(m1, "POST")
        self.assertTrue(p1.startswith("/closedcaption?cid=abc&seq="))
        self.assertGreater(int(p2.rsplit("=", 1)[1]), int(p1.rsplit("=", 1)[1]))
        stamp, text = b1.decode().rstrip("\n").split("\n")
        self.assertEqual(text, "Let there be light.")
        self.assertEqual(stamp, self.s._yt_stamp(now - 4000))   # when it was spoken, UTC
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}$")

    def test_a_segment_goes_live_once_even_when_fixed(self):
        post(self.s, id="s1", text="Let ther be light.", lang="en")
        post(self.s, id="s1", text="Let there be light.", lang="en")
        self.assertTrue(wait_for(lambda: len(self.yt.requests) == 1))
        time.sleep(0.1)
        self.assertEqual(len(self.yt.requests), 1)
        self.assertIn("Let there be light.", srt(self.s))      # ...but the replay has the fix

    def test_other_languages_are_not_sent(self):
        r = post(self.s, id="s1", text="Să fie lumină.", lang="ro")
        self.assertEqual(r.get_json(), {"ok": True, "skipped": "lang"})
        time.sleep(0.1)
        self.assertEqual(self.yt.requests, [])
        self.assertEqual(srt(self.s), "")

    def test_status_counts_what_was_sent(self):
        post(self.s, id="s1", text="Amen.", lang="en")
        self.assertTrue(wait_for(lambda: self.s.app.test_client().get("/youtube").get_json()["posted"] == 1))
        st = self.s.app.test_client().get("/youtube").get_json()
        self.assertEqual((st["live"], st["lang"], st["kept"], st["err"]), (True, "en", 1, ""))

    def test_caps_advertise_it(self):
        caps = self.s.app.test_client().get("/caps").get_json()
        self.assertTrue(caps["youtube"])
        self.assertEqual(caps["youtube_lang"], "en")


@need_flask
class YouTubeRefuses(unittest.TestCase):
    def test_an_error_is_reported_in_the_status(self):
        with FakeProPresenter(status=403) as yt:
            s = load_server(CT_YOUTUBE_CAPTION_URL=yt.url + "/cc?cid=x")
            post(s, id="s1", text="Amen.", lang="en")
            self.assertTrue(wait_for(lambda: s.app.test_client().get("/youtube").get_json()["failed"] == 1))
            self.assertIn("403", s.app.test_client().get("/youtube").get_json()["err"])


@need_flask
class Replay(unittest.TestCase):
    """Without a URL nothing is posted, but the captions are kept for the .srt."""

    def setUp(self):
        self.s = load_server()
        self.now = int(time.time() * 1000)

    def test_not_live_without_a_url(self):
        r = post(self.s, id="s1", text="Amen.", lang="en")
        self.assertEqual(r.get_json(), {"ok": True, "live": False})
        self.assertFalse(self.s.app.test_client().get("/caps").get_json()["youtube"])

    def test_srt_is_timed_from_the_stream_start(self):
        start = self.now - 60000
        post(self.s, id="a", text="Before the stream.", lang="en", t=start - 5000)
        post(self.s, id="b", text="First line.", lang="en", t=start + 12500)
        post(self.s, id="c", text="Second line.", lang="en", t=start + 15000)
        post(self.s, id="d", text="Long after.", lang="en", t=start + 40000)
        self.assertEqual(srt(self.s, start),
                         "1\n00:00:12,500 --> 00:00:15,000\nFirst line.\n\n"
                         "2\n00:00:15,000 --> 00:00:22,000\nSecond line.\n\n"
                         "3\n00:00:40,000 --> 00:00:47,000\nLong after.\n")

    def test_srt_without_a_start_begins_at_the_first_line(self):
        post(self.s, id="b", text="First line.", lang="en", t=self.now - 3000)
        self.assertTrue(srt(self.s).startswith("1\n00:00:00,000 --> 00:00:07,000\nFirst line."))

    def test_a_missing_or_absurd_time_becomes_now(self):
        post(self.s, id="b", text="Line.", lang="en", t=T0 - 10**9)
        kept = self.s._YT_LOG["b"]["t"]
        self.assertLess(abs(kept - int(time.time() * 1000)), 5000)

    def test_bad_requests(self):
        self.assertEqual(post(self.s, id="", text="x").status_code, 400)
        self.assertEqual(post(self.s, id="a", text="  ").status_code, 400)
        r = self.s.app.test_client().get("/youtube/captions.srt?start=soon")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()


# ---- the replay's voice track ---------------------------------------------------
# A clip is mp3 frames; MPEG-1 Layer III, 128 kb/s, 44.1 kHz, stereo, no CRC: 417
# bytes and 1152/44100 s (26.1 ms) a frame.
FRAME = bytes((0xFF, 0xFB, 0x90, 0x00)) + bytes(413)
FSEC = 1152 / 44100


def clip(frames):
    return FRAME * frames


@need_flask
class VoiceTrack(unittest.TestCase):
    def setUp(self):
        self.s = load_server()
        self.rec = self.s.RECORDING
        self.rec.clear()
        self.rec.enabled = True
        self.start = time.time() - 120

    def voice(self, start=None):
        start = self.start if start is None else start
        return self.s.app.test_client().get(f"/youtube/voice.mp3?start={int(start * 1000)}")

    def test_clips_land_where_they_were_voiced_with_silence_between(self):
        self.rec.add(clip(40), text="First.", lang="en", spoke_at=self.start + 10)   # ~1.04 s
        self.rec.add(clip(40), text="Second.", lang="en", spoke_at=self.start + 20)
        r = self.voice()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/mpeg")
        body = r.get_data()
        self.assertEqual(len(body) % len(FRAME), 0)              # whole frames only
        frames = len(body) // len(FRAME)
        # the second clip ends at 20 s plus its own length, to within a frame
        self.assertAlmostEqual(frames * FSEC, 20 + 40 * FSEC, delta=FSEC)
        rec = sys.modules["recorder"]
        self.assertEqual(rec.mp3_duration(body), round(frames * FSEC, 3))
        # the first clip starts at 10 s: that many frames of silence come first
        self.assertEqual(rec.silence(rec._first_frame(FRAME), 10)[0], body[:round(10 / FSEC) * len(FRAME)])

    def test_overlapping_clips_follow_each_other(self):
        self.rec.add(clip(200), lang="en", spoke_at=self.start + 1)    # ~5.2 s
        self.rec.add(clip(40), lang="en", spoke_at=self.start + 2)     # voiced while the first played
        body = self.voice().get_data()
        self.assertAlmostEqual(len(body) // len(FRAME) * FSEC, 1 + 240 * FSEC, delta=FSEC)

    def test_only_the_youtube_language_and_only_after_the_start(self):
        self.rec.add(clip(40), lang="en", spoke_at=self.start - 30)    # before the stream
        self.rec.add(clip(40), lang="ro", spoke_at=self.start + 5)     # the other direction
        self.assertEqual(self.voice().status_code, 409)
        self.rec.add(clip(40), lang="en", spoke_at=self.start + 5)
        body = self.voice().get_data()
        self.assertAlmostEqual(len(body) // len(FRAME) * FSEC, 5 + 40 * FSEC, delta=FSEC)

    def test_start_is_required(self):
        self.assertEqual(self.s.app.test_client().get("/youtube/voice.mp3").status_code, 400)


class Silence(unittest.TestCase):
    """The silence takes the shape of the clip's frames, without CRC or padding."""

    def test_frames_match_the_clip(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "recorder_t", os.path.join(os.path.dirname(__file__), "..", "app", "recorder.py"))
        rec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rec)
        # a mono MPEG-2 24 kHz 48 kb/s frame with CRC and padding on
        hdr = bytes((0xFF, 0xF2, 0x64, 0xC0))
        frame = rec._first_frame(hdr + bytes(200))
        self.assertEqual(frame[0], bytes((0xFF, 0xF3, 0x64, 0xC0)))   # CRC off, padding off
        self.assertEqual(frame[2], 576 // 8 * 48000 // 24000)        # 144 bytes
        pad, secs = rec.silence(frame, 1.0)
        self.assertEqual(len(pad), 42 * 144)                         # round(1 / 0.024)
        self.assertAlmostEqual(secs, 42 * 0.024)
        self.assertEqual(rec.mp3_duration(pad), round(secs, 3))
