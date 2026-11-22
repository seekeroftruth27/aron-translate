"""Which voice reads clauses (app/server.py _clauses_wanted), and what the page
is told about it (/caps).

    python -m unittest discover -s tests -v
"""
import unittest

from support import load_server, need_flask


@need_flask
class VoiceModeTest(unittest.TestCase):
    def wanted(self, srv, provider, model=None, lag=2.5):
        opts = {"provider": provider, "el_model": model}
        return srv._clauses_wanted({"voice_lag": lag}, opts)

    def test_clauses_by_default_for_both_voices(self):
        srv = load_server()
        self.assertEqual(self.wanted(srv, "elevenlabs", "eleven_v4"), 2.5)
        self.assertEqual(self.wanted(srv, "elevenlabs", "eleven_v4_turbo"), 2.5)
        self.assertEqual(self.wanted(srv, "soniox"), 2.5)

    def test_a_model_without_stitching_keeps_its_clips(self):
        srv = load_server()
        self.assertIsNone(self.wanted(srv, "elevenlabs", "eleven_v3"))

    def test_each_voice_can_go_back_on_its_own(self):
        srv = load_server(CT_EL_VOICE_MODE="stream", CT_SX_VOICE_MODE="clips")
        self.assertIsNone(self.wanted(srv, "elevenlabs", "eleven_v4"))
        self.assertIsNone(self.wanted(srv, "soniox"))
        self.assertTrue(srv.EL_STREAM)

    def test_the_old_stream_switch_still_means_clips(self):
        srv = load_server(CT_EL_STREAM="0")
        self.assertEqual(srv.EL_VOICE_MODE, "clips")
        self.assertIsNone(self.wanted(srv, "elevenlabs", "eleven_v4"))

    def test_a_bad_lag_reads_as_none_queued(self):
        srv = load_server()
        self.assertEqual(srv._clauses_wanted({"voice_lag": "?"}, {"provider": "soniox"}), 0.0)

    def test_the_page_is_told(self):
        srv = load_server(CT_VOICE_DELAY_S="7")
        caps = srv.app.test_client().get("/caps").get_json()
        self.assertEqual(caps["el_voice_mode"], "clauses")
        self.assertEqual(caps["sx_voice_mode"], "clauses")
        self.assertEqual(caps["voice_delay_s"], 7.0)
        self.assertEqual(caps["voice_director"], "off")      # no key in the tests: nobody picks tags
        self.assertEqual(caps["el_stability"], 0.65)


if __name__ == "__main__":
    unittest.main()
