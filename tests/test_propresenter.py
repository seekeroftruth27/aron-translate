"""ProPresenter: /project (show, clear, caption text) and the /caps flags that tell
the page how to drive the Message.

    python -m unittest discover -s tests -v
"""
import unittest

from support import FakeProPresenter, load_server, need_flask

MESSAGE = "LIVE Translation"
QUOTED = "LIVE%20Translation"


def post(server, **body):
    return server.app.test_client().post("/project", json=body)


@need_flask
class NotConfigured(unittest.TestCase):
    def test_project_is_refused(self):
        self.assertEqual(post(load_server(), action="show").status_code, 503)

    def test_caps_say_so(self):
        caps = load_server().app.test_client().get("/caps").get_json()
        self.assertFalse(caps["propresenter"])
        self.assertFalse(caps["propresenter_follow_mic"])

    def test_url_without_a_message_is_not_configured(self):
        s = load_server(CT_PROPRESENTER_URL="http://127.0.0.1:1")
        self.assertEqual(post(s, action="show").status_code, 503)


@need_flask
class FollowTheMic(unittest.TestCase):
    """CT_PROPRESENTER_FOLLOW_MIC=1: the Message is only shown and cleared."""

    def setUp(self):
        self.pp = FakeProPresenter().__enter__()
        self.addCleanup(self.pp.__exit__)
        self.s = load_server(CT_PROPRESENTER_URL=self.pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                             CT_PROPRESENTER_FOLLOW_MIC=1)

    def test_show_triggers_the_message_with_no_token_values(self):
        r = post(self.s, action="show")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pp.requests, [("POST", f"/v1/message/{QUOTED}/trigger", [])])

    def test_clear_clears_the_message(self):
        r = post(self.s, action="clear")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pp.requests, [("GET", f"/v1/message/{QUOTED}/clear", None)])

    def test_the_message_may_be_named_by_uuid(self):
        uid = "3f370d4d-a5da-4ca2-afd8-a72632731508"
        s = load_server(CT_PROPRESENTER_URL=self.pp.url, CT_PROPRESENTER_MESSAGE=uid,
                        CT_PROPRESENTER_FOLLOW_MIC=1)
        post(s, action="show")
        self.assertEqual(self.pp.requests[-1][1], f"/v1/message/{uid}/trigger")

    def test_caps_advertise_it(self):
        caps = self.s.app.test_client().get("/caps").get_json()
        self.assertTrue(caps["propresenter"])
        self.assertTrue(caps["propresenter_follow_mic"])

    def test_a_trailing_slash_and_a_bare_host_are_tolerated(self):
        host = self.pp.url.replace("http://", "")
        for url in (self.pp.url + "/", host):
            self.pp.requests.clear()
            s = load_server(CT_PROPRESENTER_URL=url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                            CT_PROPRESENTER_FOLLOW_MIC=1)
            self.assertEqual(post(s, action="show").status_code, 200, url)
            self.assertEqual(len(self.pp.requests), 1, url)


@need_flask
class CaptionText(unittest.TestCase):
    """Without follow-the-Mic the Message carries each caption in its text token."""

    def setUp(self):
        self.pp = FakeProPresenter().__enter__()
        self.addCleanup(self.pp.__exit__)
        self.s = load_server(CT_PROPRESENTER_URL=self.pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                             CT_PROPRESENTER_TOKEN="Message", CT_PROPRESENTER_TYPE="off")

    def test_text_goes_into_the_named_token(self):
        r = post(self.s, text="Bună ziua")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pp.requests,
                         [("POST", f"/v1/message/{QUOTED}/trigger",
                           [{"name": "Message", "text": {"text": "Bună ziua"}}])])

    def test_empty_text_is_rejected_and_sends_nothing(self):
        self.assertEqual(post(self.s, text="").status_code, 400)
        self.assertEqual(self.pp.requests, [])

    def test_text_is_cut_at_a_thousand_characters(self):
        post(self.s, text="x" * 1500)
        self.assertEqual(len(self.pp.requests[0][2][0]["text"]["text"]), 1000)

    def test_follow_mic_is_off_by_default(self):
        caps = self.s.app.test_client().get("/caps").get_json()
        self.assertFalse(caps["propresenter_follow_mic"])


@need_flask
class LookGate(unittest.TestCase):
    """CT_PROPRESENTER_LOOK: the box follows the Mic only while that Look is live."""

    UUID = "9b1c5f0e-2d43-4b7a-8e61-0a7c3d2f4e15"

    def server(self, look, wanted="Translation"):
        pp = FakeProPresenter(look=look).__enter__()
        self.addCleanup(pp.__exit__)
        s = load_server(CT_PROPRESENTER_URL=pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                        CT_PROPRESENTER_FOLLOW_MIC=1, CT_PROPRESENTER_LOOK=wanted)
        return s, pp

    @staticmethod
    def look(name, uuid="0f0e0d0c-0000-4000-8000-000000000000"):
        # The shape of the published example: name/uuid under "id".
        return {"id": {"uuid": uuid, "name": name, "index": 0}, "screens": []}

    def test_show_on_the_chosen_look_triggers_the_message(self):
        s, pp = self.server(self.look("Translation"))
        r = post(s, action="show")
        self.assertEqual(r.get_json(), {"ok": True})
        self.assertEqual(pp.requests[-1], ("POST", f"/v1/message/{QUOTED}/trigger", []))

    def test_show_on_another_look_is_held_and_sends_nothing(self):
        s, pp = self.server(self.look("Worship"))
        r = post(s, action="show")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"ok": True, "held": "look", "look": "Worship"})
        self.assertEqual([q[1] for q in pp.requests], ["/v1/look/current"])

    def test_names_ignore_case_and_may_be_a_list_or_a_uuid(self):
        for wanted in ("translation", "Worship, TRANSLATION", self.UUID):
            s, _ = self.server(self.look("Translation", self.UUID), wanted)
            self.assertEqual(post(s, action="show").get_json(), {"ok": True}, wanted)

    def test_a_flat_look_reply_is_read_too(self):
        s, _ = self.server({"uuid": self.UUID, "name": "Translation", "index": 0, "screens": []})
        self.assertEqual(post(s, action="show").get_json(), {"ok": True})

    def test_look_endpoint_reports_the_live_look(self):
        s, _ = self.server(self.look("Worship"))
        self.assertEqual(s.app.test_client().get("/project/look").get_json(),
                         {"active": False, "look": "Worship"})

    def test_clear_never_asks_for_the_look(self):
        s, pp = self.server(self.look("Worship"))
        post(s, action="clear")
        self.assertEqual(pp.requests, [("GET", f"/v1/message/{QUOTED}/clear", None)])

    def test_caps_advertise_it_only_with_follow_mic(self):
        s, _ = self.server(self.look("Translation"))
        self.assertTrue(s.app.test_client().get("/caps").get_json()["propresenter_look"])
        with FakeProPresenter() as pp:
            t = load_server(CT_PROPRESENTER_URL=pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                            CT_PROPRESENTER_LOOK="Translation")
            self.assertFalse(t.app.test_client().get("/caps").get_json()["propresenter_look"])

    def test_without_a_look_show_does_not_ask(self):
        with FakeProPresenter() as pp:
            s = load_server(CT_PROPRESENTER_URL=pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                            CT_PROPRESENTER_FOLLOW_MIC=1)
            post(s, action="show")
            self.assertEqual([q[1] for q in pp.requests], [f"/v1/message/{QUOTED}/trigger"])
            self.assertEqual(s.app.test_client().get("/project/look").get_json(),
                             {"active": True, "look": ""})

    def test_an_unreachable_look_query_is_a_502_and_shows_nothing(self):
        with FakeProPresenter(status=404) as pp:   # an older ProPresenter: no /v1/look/current
            s = load_server(CT_PROPRESENTER_URL=pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                            CT_PROPRESENTER_FOLLOW_MIC=1, CT_PROPRESENTER_LOOK="Translation")
            r = post(s, action="show")
            self.assertEqual(r.status_code, 502)
            self.assertIn("404", r.get_json()["error"])
            self.assertEqual([q[1] for q in pp.requests], ["/v1/look/current"])


@need_flask
class ProPresenterMisbehaves(unittest.TestCase):
    """A ProPresenter hiccup is reported to the page, never raised."""

    def test_an_error_status_becomes_502(self):
        with FakeProPresenter(status=500) as pp:
            s = load_server(CT_PROPRESENTER_URL=pp.url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                            CT_PROPRESENTER_FOLLOW_MIC=1)
            for action in ("show", "clear"):
                r = post(s, action=action)
                self.assertEqual(r.status_code, 502, action)
                self.assertIn("500", r.get_json()["error"])

    def test_nothing_listening_becomes_502(self):
        with FakeProPresenter() as pp:
            url = pp.url
        s = load_server(CT_PROPRESENTER_URL=url, CT_PROPRESENTER_MESSAGE=MESSAGE,
                        CT_PROPRESENTER_FOLLOW_MIC=1)
        self.assertEqual(post(s, action="show").status_code, 502)


@need_flask
class QuietSetting(unittest.TestCase):
    """CT_PROPRESENTER_QUIET_S: seconds of silence that clear the box. 0 = never."""

    def quiet(self, **env):
        s = load_server(CT_PROPRESENTER_URL="http://127.0.0.1:1",
                        CT_PROPRESENTER_MESSAGE=MESSAGE, **env)
        return s.app.test_client().get("/caps").get_json()["propresenter_quiet_s"]

    def test_default_is_ten_seconds(self):
        self.assertEqual(self.quiet(), 10)

    def test_zero_means_never(self):
        self.assertEqual(self.quiet(CT_PROPRESENTER_QUIET_S=0), 0)

    def test_a_value_is_used(self):
        self.assertEqual(self.quiet(CT_PROPRESENTER_QUIET_S=25), 25)

    def test_junk_falls_back_to_the_default(self):
        self.assertEqual(self.quiet(CT_PROPRESENTER_QUIET_S="soon"), 10)

    def test_it_is_clamped(self):
        self.assertEqual(self.quiet(CT_PROPRESENTER_QUIET_S=-5), 0)
        self.assertEqual(self.quiet(CT_PROPRESENTER_QUIET_S=999999), 3600)


if __name__ == "__main__":
    unittest.main()
