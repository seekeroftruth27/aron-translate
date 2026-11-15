"""The music gate's decisions (app/index.html, mdDecide): when a song closes the
gate and when speech opens it again.

The thresholds and mdDecide are cut out of the real page and run in the embedded
engine (mini-racer, see tests/test_page.py); only the stand-ins around them and the
five lines that turn a window into the median (from mdRun) are written here. Each
test feeds one YAMNet window every half second, as the page does, as the share of
music in it (music / (music + speech)).

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v
"""
import json
import os
import unittest

try:
    from py_mini_racer import MiniRacer
except ImportError:                                         # pragma: no cover
    MiniRacer = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOP_MS = 500


def gate_source():
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as f:
        html = f.read()
    a = html.index("const MD_ON=0.60")
    b = html.index("let yamSession=null")
    c = html.index("function mdDecide(now){")
    d = html.index("// The prayer detector's verdicts")
    return html[a:b] + html[c:d]


def pin_source():
    """The operator's pin (operatorMode, pinHeard, pinCheck), cut from the real page."""
    with open(os.path.join(ROOT, "app", "index.html"), encoding="utf-8") as f:
        html = f.read()
    a = html.index("// ---- the operator's pin")
    return html[a:html.index("// ---- end of the operator's pin", a)]


HARNESS = """
(function(){
  var serviceMode = 'speaking', lastModeAt = -1e9, moves = [], modeByDetector = false;
  var autoMode = true, autoPrayer = false, events = [];
  var MODE_LABEL = {speaking: 'Speaking', singing: 'Music', prayer: 'Prayer'};
  var mdRaw = [], mdScore = 0, mdOnSince = 0, mdOffSince = 0, mdFeat = null;
  function renderHearChip(){}
  function mdRenderDiag(){}
  function renderModeUI(){}
  function log(){}
  function jEvent(kind, data){ events.push(kind); }
  var clock = 0;
  function setServiceMode(m, why, by){
    if(m === serviceMode) return;
    serviceMode = m; lastModeAt = clock; modeByDetector = by || false; moves.push([clock, m, why]);
  }
  %PIN%
  %SOURCE%
  return {
    click: function(m, at){ clock = at; operatorMode(m, 'operator'); return serviceMode; },
    gates: function(music, prayer){ autoMode = music; autoPrayer = prayer; pinCheck(); },
    pin: function(){ return modePin; },
    by: function(){ return modeByDetector; },
    window: function(raw, at){                  // the tail of mdRun, then the decision
      clock = at;
      if(raw !== null){
        mdRaw.push(raw); mdSeq++;
        while(mdRaw.length > MD_MEDIAN_N) mdRaw.shift();
        var sorted = mdRaw.slice().sort(function(a, b){ return a - b; });
        mdScore = sorted[sorted.length >> 1];
      }
      mdDecide(at);
      return serviceMode;
    },
    mode: function(){ return serviceMode; },
    consts: function(){
      return [MD_ON, MD_OFF, MD_ON_MS, MD_OFF_MS, MD_MEDIAN_N, MD_OFF_FAST_N,
              MD_FAST_ON_N, MD_FAST_ON_AT, MD_GUARD_MS].join(',');
    },
    moves: function(){ return JSON.stringify(moves); }
  };
})()
"""

need_js = unittest.skipUnless(MiniRacer, "needs mini-racer (pip install -r requirements-dev.txt)")


@need_js
class MusicGate(unittest.TestCase):
    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var gate = " + HARNESS.replace("%PIN%", pin_source()).replace("%SOURCE%", gate_source()))
        self.at = 100 * HOP_MS      # past the anti-strobe guard of a fresh page

    def feed(self, *raws):
        """One window per value; returns the mode after the last one."""
        mode = None
        for raw in raws:
            self.at += HOP_MS
            mode = self.js.eval("gate.window(%s, %d)" % (json.dumps(raw), self.at))
        return mode

    def moves(self):
        return json.loads(self.js.eval("gate.moves()"))

    def song_from_speech(self):
        self.feed(*[0.02] * 12)

    def seconds_to(self, mode, raw, limit=30):
        """Seconds of windows at `raw` until the gate is in `mode` (None: not within the limit)."""
        for n in range(1, int(limit * 1000 / HOP_MS) + 1):
            if self.feed(raw) == mode:
                return n * HOP_MS / 1000
        return None

    # --- a song starts -----------------------------------------------------
    def test_a_song_closes_the_gate_in_about_two_seconds(self):
        self.song_from_speech()
        took = self.seconds_to("singing", 0.95)
        self.assertIsNotNone(took)
        self.assertLessEqual(took, 2.5)
        self.assertGreaterEqual(took, 1.5)

    def test_one_loud_window_does_not(self):
        self.song_from_speech()
        self.assertEqual(self.feed(0.99, 0.02, 0.02), "speaking")

    def test_three_windows_are_not_enough(self):
        self.song_from_speech()
        self.assertEqual(self.feed(0.95, 0.95, 0.95), "speaking")

    def test_a_song_that_is_not_clear_waits_for_the_timer(self):
        """Music at 70% is a song, but not one to mute on four windows."""
        self.song_from_speech()
        took = self.seconds_to("singing", 0.7)
        self.assertIsNotNone(took)
        self.assertGreater(took, 3.5)

    # --- speech starts over a song ----------------------------------------
    def in_song(self):
        self.song_from_speech()
        self.seconds_to("singing", 0.95)
        self.feed(*[0.95] * 12)          # well past the guard

    def test_speech_after_a_song_opens_the_gate_in_about_two_seconds(self):
        self.in_song()
        took = self.seconds_to("speaking", 0.02)
        self.assertIsNotNone(took)
        self.assertLessEqual(took, 2.5)

    def test_two_speech_like_windows_inside_a_song_do_not(self):
        """A breath between phrases, a soft verse: the gate stays shut."""
        self.in_song()
        self.assertEqual(self.feed(0.1, 0.1, 0.95, 0.95), "singing")
        self.assertEqual(self.feed(0.2, 0.1), "singing")

    def test_three_do_not_either(self):
        self.in_song()
        self.assertEqual(self.feed(0.1, 0.1, 0.1), "singing")

    # --- the guard --------------------------------------------------------
    def test_the_gate_holds_for_the_guard_after_a_move(self):
        self.song_from_speech()
        self.seconds_to("singing", 0.95)
        t = self.moves()[-1][0]
        # four speech windows would release it, but not inside MD_GUARD_MS
        while self.at - t < 4000:
            self.assertEqual(self.feed(0.02), "singing")
        self.assertEqual(self.seconds_to("speaking", 0.02), 1.0)    # the guard ends at 5 s

    # --- the operator's pin --------------------------------------------------
    def click(self, mode):
        self.at += HOP_MS
        return self.js.eval("gate.click(%s, %d)" % (json.dumps(mode), self.at))

    def pin(self):
        return self.js.eval("gate.pin()")

    def mode(self):
        return self.js.eval("gate.mode()")

    def test_an_unmute_during_a_song_is_not_muted_again(self):
        """It used to last the 5 s guard, then Gate music muted again, and again."""
        self.in_song()
        self.assertEqual(self.click("speaking"), "speaking")
        self.assertEqual(self.pin(), "speaking")
        self.assertEqual(self.feed(*[0.95] * 60), "speaking")      # 30 s more of the song
        self.assertEqual(self.pin(), "speaking")

    def test_the_pin_lets_go_on_speech_and_the_next_song_mutes(self):
        self.in_song()
        self.click("speaking")
        self.feed(*[0.95] * 20)
        self.feed(*[0.02] * 12)                                     # the pastor, alone
        self.assertIsNone(self.pin())
        self.assertEqual(self.mode(), "speaking")
        took = self.seconds_to("singing", 0.95)                     # the next song
        self.assertIsNotNone(took)
        self.assertLessEqual(took, 2.5)

    def test_unmuting_as_a_song_starts_holds_too(self):
        """1 pressed while still on, before the detector mutes: it pins it on."""
        self.song_from_speech()
        self.feed(0.95, 0.95)
        self.assertEqual(self.click("speaking"), "speaking")
        self.assertEqual(self.feed(*[0.95] * 40), "speaking")

    def test_a_mute_before_a_song_is_taken_over_and_ends_with_it(self):
        self.song_from_speech()
        self.assertEqual(self.click("singing"), "singing")
        self.assertEqual(self.pin(), "singing")
        self.feed(*[0.95] * 8)                                      # the band starts
        self.assertIsNone(self.pin())
        self.assertTrue(self.js.eval("gate.by()"))                  # Gate music's hold now
        self.feed(*[0.95] * 12)
        self.assertIsNotNone(self.seconds_to("speaking", 0.02, limit=5))    # the song ends

    def test_a_mute_with_no_music_stays_muted(self):
        """It used to be lifted after 5 s of speech."""
        self.song_from_speech()
        self.click("singing")
        self.assertEqual(self.feed(*[0.02] * 60), "singing")
        self.assertEqual(self.pin(), "singing")

    def test_no_pin_when_no_gate_could_move_it(self):
        self.js.eval("gate.gates(false, false)")
        self.click("singing")
        self.assertIsNone(self.pin())
        self.click("speaking")
        self.assertIsNone(self.pin())

    def test_a_mute_is_not_pinned_by_gate_prayer_alone(self):
        """Gate prayer never lifts an operator's mute, so there is nothing to pin against."""
        self.js.eval("gate.gates(false, true)")
        self.click("singing")
        self.assertIsNone(self.pin())

    def test_switching_gate_music_off_drops_the_pin(self):
        self.in_song()
        self.click("speaking")
        self.js.eval("gate.gates(false, false)")
        self.assertIsNone(self.pin())

    def test_an_unmute_waits_for_every_gate_that_is_on(self):
        """Gate prayer on too: the music detector hearing speech is not enough."""
        self.js.eval("gate.gates(true, true)")
        self.in_song()
        self.click("speaking")
        self.feed(*[0.02] * 12)
        self.assertEqual(self.pin(), "speaking")

    def test_the_thresholds_the_page_runs(self):
        """So a change to them has to change this test too."""
        self.assertEqual(self.js.eval("gate.consts()"), "0.6,0.4,2500,1500,9,4,4,0.8,5000")


if __name__ == "__main__":
    unittest.main()
