"""The server's endpoints, with no keys and no provider: the pages load, the
capability and status reports have their shape, the caption store keeps order,
settings and sessions round-trip, and bad input is refused.

    python -m unittest discover -s tests -v
"""
import json
import time
import unittest
from unittest import mock

from support import load_server, need_flask

SESSION = "session-2026-11-02_10-00-00"


@need_flask
class Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        self.c = self.server.app.test_client()


class Pages(Endpoints):
    def test_the_operator_page_loads(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<html", r.data)

    def test_the_projector_page_loads(self):
        r = self.c.get("/projector")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<html", r.data)

    def test_the_help_guide_is_a_pdf(self):
        r = self.c.get("/help")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"%PDF"))

    def test_an_unknown_path_is_a_json_404(self):
        r = self.c.get("/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())


class Reports(Endpoints):
    def test_caps_shape(self):
        caps = self.c.get("/caps").get_json()
        for key in ("languages", "target_languages", "tts", "stt", "publish",
                    "propresenter", "propresenter_follow_mic", "propresenter_quiet_s"):
            self.assertIn(key, caps)
        self.assertIn("auto", caps["languages"])
        self.assertNotIn("auto", caps["target_languages"])
        self.assertIn("ro", caps["target_languages"])

    def test_without_keys_nothing_is_offered(self):
        caps = self.c.get("/caps").get_json()
        self.assertFalse(caps["stt"])
        self.assertFalse(caps["tts"])
        self.assertFalse(caps["publish"])
        self.assertFalse(caps["propresenter"])

    def test_status_names_the_three_keys(self):
        st = self.c.get("/status").get_json()
        self.assertEqual(set(st["keys"]), {"anthropic", "soniox", "elevenlabs"})
        self.assertFalse(st["keys"]["soniox"]["present"])

    def test_usage_starts_at_zero(self):
        usage = self.c.get("/usage").get_json()
        self.assertEqual(usage["session"]["claude_calls"], 0)
        self.assertEqual(usage["cost"]["total"], 0)

    def test_pipe_status_shape(self):
        self.assertEqual(set(self.c.get("/pipe-status").get_json()), {"projectors", "web", "watch"})


class Refusals(Endpoints):
    def test_web_captions_are_refused_when_not_configured(self):
        self.assertEqual(self.c.post("/publish", json={"text": "hi"}).status_code, 503)

    def test_stt_wants_audio(self):
        self.assertEqual(self.c.post("/stt", data=b"").status_code, 400)

    def test_voices_say_why_there_are_none(self):
        self.assertEqual(self.c.get("/voices").get_json()["voices"], [])

    def test_unknown_record_action(self):
        self.assertEqual(self.c.post("/record", json={"action": "boom"}).status_code, 400)


class Recording(Endpoints):
    def test_status_and_clear(self):
        st = self.c.get("/record").get_json()
        self.assertEqual(st["clips"], 0)
        self.assertEqual(self.c.post("/record", json={"action": "clear"}).get_json()["clips"], 0)

    def test_on_and_off(self):
        self.assertFalse(self.c.post("/record", json={"action": "off"}).get_json()["enabled"])
        self.assertTrue(self.c.post("/record", json={"action": "on"}).get_json()["enabled"])

    def test_saving_an_empty_recording_is_refused(self):
        self.c.post("/record", json={"action": "clear"})
        self.assertEqual(self.c.post("/record", json={"action": "save"}).status_code, 409)


class Settings(Endpoints):
    def test_round_trip_and_newer_wins(self):
        self.assertEqual(self.c.post("/settings", json={"at": 50, "settings": {"a": 1}, "keys": {}})
                         .get_json(), {"ok": True})
        self.assertEqual(self.c.get("/settings").get_json()["settings"], {"a": 1})
        older = self.c.post("/settings", json={"at": 10, "settings": {"a": 2}, "keys": {}}).get_json()
        self.assertEqual(older.get("kept"), "newer")
        self.assertEqual(self.c.get("/settings").get_json()["settings"], {"a": 1})

    def test_junk_is_refused(self):
        self.assertEqual(self.c.post("/settings", json={"nope": 1}).status_code, 400)
        self.assertEqual(self.c.post("/settings", data=b"not json").status_code, 400)


class Sessions(Endpoints):
    def test_a_bad_id_is_refused(self):
        for sid in ("", "../x", "session-1", SESSION + "x"):
            self.assertEqual(self.c.post("/session", json={"id": sid}).status_code, 400, sid)

    def test_a_record_is_accepted(self):
        r = self.c.post("/session", json={"id": SESSION, "rows": [], "events": []})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_a_patch_to_an_unknown_session_is_404(self):
        r = self.c.post("/session", json={"id": "session-2000-01-01_00-00-00", "patch": True})
        self.assertEqual(r.status_code, 404)

    def test_a_closing_tail_is_laid_over_the_last_push(self):
        sid = "session-2026-11-02_11-00-00"
        self.c.post("/session", json={"id": sid, "rows": [{"n": 1}, {"n": 2}, {"n": 3}]})
        r = self.c.post("/session", json={"id": sid, "patch": True, "rows": [{"n": "3b"}],
                                          "rows_from": 2, "closed": True})
        self.assertEqual(r.status_code, 200)
        rec = self.server.SESSIONS[sid]
        self.assertEqual([x["n"] for x in rec["rows"]], [1, 2, "3b"])
        self.assertTrue(rec["closed"])


class PushWatchdog(Endpoints):
    """The server sees both ends of the push -- the words Soniox finalizes and the captions
    /caption stores -- and reports speech that waits too long for a stored caption."""

    def setUp(self):
        self.server = load_server()
        self.c = self.server.app.test_client()
        s = self.server
        s.CT_PUSH_WATCH_S, s.CT_PUSH_WATCH_WORDS = 12, 6
        s._WD.update(since=None, words=0, open=None, episodes=[], seq=0, thread=True)  # no timer thread

    def words(self, n, **kw):
        return [dict({"text": " w%d" % i, "is_final": True}, **kw) for i in range(n)]

    def test_no_episode_under_the_wait_or_the_word_count(self):
        s = self.server
        s._wd_speech(self.words(8), now=100)
        s._wd_check(now=105)                       # 5 s: normal
        self.assertIsNone(s._wd_report(now=105)["gap_s"])
        s._wd_stream_closed(now=106)
        s._wd_speech(self.words(3), now=200)       # too few words
        s._wd_check(now=300)
        self.assertEqual(s._wd_report(now=300)["episodes"], [])

    def test_speech_with_no_stored_caption_opens_an_episode(self):
        s = self.server
        s._wd_speech(self.words(4), now=100)
        s._wd_speech(self.words(4), now=104)
        s._wd_check(now=113)
        r = s._wd_report(now=113)
        self.assertEqual(r["gap_s"], 13.0)
        self.assertEqual(len(r["episodes"]), 1)
        self.assertEqual(r["episodes"][0]["words"], 8)
        self.assertIsNone(r["episodes"][0]["recovered"])
        s._wd_check(now=120)                       # still the same episode
        self.assertEqual(len(s._wd_report(now=120)["episodes"]), 1)

    def test_a_stored_caption_closes_it_with_its_length(self):
        s = self.server
        s._wd_speech(self.words(8), now=100)
        s._wd_check(now=115)
        s._wd_caption(now=131)
        r = s._wd_report(now=140)
        self.assertIsNone(r["gap_s"])
        self.assertTrue(r["episodes"][0]["recovered"])
        self.assertEqual(r["episodes"][0]["secs"], 31.0)
        s._wd_check(now=200)                       # nothing owed any more
        self.assertEqual(len(s._wd_report(now=200)["episodes"]), 1)

    def test_a_closed_stream_ends_the_watch(self):
        s = self.server
        s._wd_speech(self.words(8), now=100)
        s._wd_stream_closed(now=105)               # muted, held for music, stopped
        s._wd_check(now=200)
        self.assertEqual(s._wd_report(now=200)["episodes"], [])
        s._wd_speech(self.words(8), now=300)
        s._wd_check(now=315)
        s._wd_stream_closed(now=330)
        e = s._wd_report(now=330)["episodes"][0]
        self.assertIs(e["recovered"], False)
        self.assertEqual(e["ended"], "the speech stream closed")

    def test_only_finalized_original_words_count(self):
        s = self.server
        toks = (self.words(10, translation_status="translation")        # Soniox's own translation
                + [{"text": "<end>", "is_final": True}, {"text": "<fin>", "is_final": True},
                   {"text": " ,", "is_final": True}]                    # no letters
                + [{"text": " draft", "is_final": False}] * 10)         # not final yet
        s._wd_speech(toks, now=100)
        s._wd_check(now=200)
        self.assertEqual(s._wd_report(now=200)["episodes"], [])

    def test_off_when_the_wait_is_zero(self):
        s = self.server
        s.CT_PUSH_WATCH_S = 0
        s._wd_speech(self.words(20), now=100)
        s._wd_check(now=500)
        self.assertEqual(s._wd_report(now=500)["episodes"], [])

    def test_the_caption_post_and_pipe_status_are_wired(self):
        s = self.server
        s._wd_speech(self.words(8), now=s.time.monotonic() - 30)
        s._wd_check()
        w = self.c.get("/pipe-status").get_json()["watch"]
        self.assertIsNotNone(w["gap_s"])
        self.c.post("/caption", json={"id": "z", "text": "Hello", "done": True})
        w = self.c.get("/pipe-status").get_json()["watch"]
        self.assertIsNone(w["gap_s"])
        self.assertTrue(w["episodes"][0]["recovered"])
        # an empty text, or a clear, is not a caption
        s._wd_speech(self.words(8), now=s.time.monotonic() - 30)
        s._wd_check()
        self.c.post("/caption", json={"id": "z2", "text": "", "done": False})
        self.c.post("/caption", json={"action": "clear"})
        self.assertIsNotNone(self.c.get("/pipe-status").get_json()["watch"]["gap_s"])


class PoolPermission(Endpoints):
    """The page talks to this server at other loopback addresses (127.0.0.2 ...) so a
    stalled request cannot hold the room's captions in line; the browser then needs the
    server's permission to read the answers, and gives it to the page's own address only."""

    def setUp(self):
        self.server = load_server()
        self.c = self.server.app.test_client()
        self.own = "http://localhost:%d" % self.server.PORT

    def test_the_pages_own_address_may_read_answers(self):
        r = self.c.options("/project", headers={"Origin": self.own,
                                                "Access-Control-Request-Method": "POST",
                                                "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], self.own)
        self.assertIn("POST", r.headers["Access-Control-Allow-Methods"])
        self.assertIn("Content-Type", r.headers["Access-Control-Allow-Headers"])
        r = self.c.get("/pipe-status", headers={"Origin": self.own})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], self.own)

    def test_another_site_gets_no_permission(self):
        for origin in ("https://example.com", "http://localhost:1", "http://127.0.0.2:%d" % self.server.PORT):
            r = self.c.get("/pipe-status", headers={"Origin": origin})
            self.assertNotIn("Access-Control-Allow-Origin", r.headers, origin)

    def test_a_request_without_an_origin_is_unchanged(self):
        r = self.c.get("/pipe-status")
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_the_voice_lanes_may_post_and_read_their_streams(self):
        # the voice and the translator are posted to 127.0.0.5-7 (voiceFetch)
        for path in ("/speak-stream", "/translate"):
            r = self.c.options(path, headers={"Origin": self.own,
                                              "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "content-type"})
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], self.own, path)
            self.assertIn("POST", r.headers["Access-Control-Allow-Methods"], path)


class VoiceTiming(Endpoints):
    """A clip's first audio line says how long its request has been on the server, so the
    page can tell time queued in the browser from time spent waiting here."""

    def setUp(self):
        self.server = load_server()

    def lines(self, meta):
        def fake(text, stats=None, **opts):
            stats["ttfb_ms"] = 40
            yield b"one"
            yield b"two"
        with mock.patch.object(self.server, "synthesize_stream", fake):
            return [json.loads(x) for x in self.server._tts_chunk_lines("Amin.", {"provider": "soniox"}, meta)]

    def test_the_first_line_counts_from_the_request(self):
        out = self.lines({"t_in": time.monotonic() - 0.5})
        self.assertGreaterEqual(out[0]["in_ms"], 500)
        self.assertLess(out[0]["in_ms"], 5000)
        self.assertEqual(out[0]["ttfb_ms"], 40)
        self.assertNotIn("in_ms", out[1])

    def test_a_one_chunk_clip_carries_it_too(self):
        def fake(text, stats=None, **opts):
            yield b"only"
        with mock.patch.object(self.server, "synthesize_stream", fake):
            out = [json.loads(x) for x in self.server._tts_chunk_lines(
                "Amin.", {"provider": "soniox"}, {"t_in": time.monotonic()})]
        self.assertTrue(out[0]["last"])
        self.assertIn("in_ms", out[0])

    def test_no_stamp_no_number(self):
        self.assertNotIn("in_ms", self.lines({"src": "x"})[0])
        self.assertNotIn("in_ms", self.lines(None)[0])


class CaptionStore(Endpoints):
    """/caption keeps what the networked projector polls; posts may overtake each other."""

    def setUp(self):
        # Its send-order counters live in the module, so each test gets a new server.
        self.server = load_server()
        self.c = self.server.app.test_client()

    def feed(self):
        return self.c.get("/caption-feed?since=-1&pid=t").get_json()

    def put(self, sid, text, done=False, n=None):
        body = {"id": sid, "text": text, "done": done}
        if n is not None:
            body["n"] = n
        return self.c.post("/caption", json=body).get_json()

    def test_a_caption_appears_and_updates_in_place(self):
        self.put("a", "Hel")
        self.put("a", "Hello", done=True)
        segs = self.feed()["segs"]
        self.assertEqual(segs, [{"id": "a", "text": "Hello", "done": True}])

    def test_a_text_plain_post_is_read_as_the_same_json(self):
        # The page posts from 127.0.0.1 as a no-cors text/plain request (its own
        # connection pool, no preflight); the body is the same JSON.
        r = self.c.post("/caption", data=json.dumps({"id": "p", "text": "Plain", "done": True, "n": 7}),
                        headers={"Content-Type": "text/plain;charset=UTF-8"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.feed()["segs"], [{"id": "p", "text": "Plain", "done": True}])

    def test_a_body_that_is_not_json_is_ignored(self):
        before = self.feed()["rev"]
        for body in ("not json", "[1,2]", ""):
            r = self.c.post("/caption", data=body, headers={"Content-Type": "text/plain"})
            self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.feed()["rev"], before)

    def test_clear_empties_the_store_and_bumps_the_epoch(self):
        self.put("a", "x")
        before = self.feed()
        self.c.post("/caption", json={"action": "clear"})
        after = self.feed()
        self.assertEqual(after["segs"], [])
        self.assertGreater(after["rev"], before["rev"])

    def test_nothing_changed_is_reported_cheaply(self):
        rev = self.put("a", "x")["rev"]
        r = self.c.get(f"/caption-feed?since={rev}&pid=t").get_json()
        self.assertFalse(r["changed"])
        self.assertNotIn("segs", r)

    def test_a_late_partial_never_replaces_its_final(self):
        self.put("a", "Hello there.", done=True, n=2)
        self.put("a", "Hello th", done=False, n=1)         # older post, arrives last
        self.assertEqual(self.feed()["segs"][0]["text"], "Hello there.")

    def test_nothing_sent_before_a_clear_comes_back_after_it(self):
        self.c.post("/caption", json={"action": "clear", "n": 5})
        self.put("old", "stale", n=3)
        self.assertEqual(self.feed()["segs"], [])

    def test_a_clear_that_lands_late_wipes_only_what_came_before_it(self):
        self.put("new", "kept", n=9)
        self.c.post("/caption", json={"action": "clear", "n": 5})
        self.assertEqual([s["id"] for s in self.feed()["segs"]], ["new"])

    def test_pace_is_clamped(self):
        self.c.post("/caption", json={"type": "pace", "wps": 5000, "dwell": -1,
                                      "highlight": 99, "lines": 99, "font": 1})
        pace = self.feed()["pace"]
        self.assertEqual(pace, {"wps": 1000, "dwell": 0, "highlight": 12, "lines": 12, "font": 30})

    def test_a_feed_poll_counts_as_a_projector(self):
        self.c.get("/caption-feed?since=-1&pid=one")
        self.c.get("/caption-feed?since=-1&pid=two")
        self.assertGreaterEqual(self.c.get("/pipe-status").get_json()["projectors"], 2)


if __name__ == "__main__":
    unittest.main()
