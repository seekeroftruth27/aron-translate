"""The server's endpoints, with no keys and no provider: the pages load, the
capability and status reports have their shape, the caption store keeps order,
settings and sessions round-trip, and bad input is refused.

    python -m unittest discover -s tests -v
"""
import unittest

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
        self.assertEqual(set(self.c.get("/pipe-status").get_json()), {"projectors", "web"})


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
