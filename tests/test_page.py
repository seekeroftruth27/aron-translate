"""The operator page (app/index.html): it parses, and the ProPresenter box follows
the Mic as the guide says.

The page's JavaScript runs in an embedded engine (the `mini-racer` package, in
requirements-dev.txt); these tests are skipped where it is not installed. The
follow-the-Mic code is cut out of the real page and run against stand-ins for
the rest of the page, so a change to it is tested, not a copy of it.

    pip install -r requirements-dev.txt
    python -m unittest discover -s tests -v
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from py_mini_racer import MiniRacer
except ImportError:                                         # pragma: no cover
    MiniRacer = None
need_js = unittest.skipUnless(MiniRacer, "needs mini-racer (pip install -r requirements-dev.txt)")


def read(name):
    with open(os.path.join(ROOT, "app", name), encoding="utf-8") as f:
        return f.read()


def scripts(html):
    """The inline <script> bodies of a page."""
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)]


@need_js
class PagesParse(unittest.TestCase):
    def check(self, name):
        found = scripts(read(name))
        self.assertTrue(found, name)
        for i, src in enumerate(found):
            try:
                MiniRacer().eval("new Function(%s)" % json.dumps(src))   # compiles, never runs
            except Exception as exc:
                self.fail(f"{name} script {i}: {exc}")

    def test_operator_page(self):
        self.check("index.html")

    def test_projector_page(self):
        self.check("projector.html")

    def test_session_page(self):
        self.check("session.html")


def follow_mic_source():
    """From `let ppMicState` to the Mic button's comment: the box's whole logic."""
    html = read("index.html")
    a = html.index("let ppMicState=null;")
    b = html.index("// The Mic button in the ribbon is the mode's face", a)
    return html[a:b]


HARNESS = """
(function(){
  var calls = [], ticks = [];
  var caps = {}, listening = false, mode = 'speaking', now = 0, lastSpeechAt = 0;
  var performance = {now: function(){ return now; }};
  function translatingNow(){ return mode === 'speaking'; }
  function projectClear(){ calls.push('clear'); }
  function fetch(url, opts){
    calls.push('show:' + JSON.parse(opts.body).action);
    return {then: function(){ return {catch: function(){}}; }};
  }
  function log(){}
  function setInterval(fn){ ticks.push(fn); }
  %SOURCE%
  return {
    caps: function(c){ caps = c; },
    set: function(o){
      if('listening' in o) listening = o.listening;
      if('mode' in o) mode = o.mode;
      if('now' in o) now = o.now;
      if('speech' in o) lastSpeechAt = o.speech;
    },
    follow: function(on){ ppFollowMic(on); },
    tick: function(){ ticks.forEach(function(f){ f(); }); },
    calls: function(){ return JSON.stringify(calls); },
    reset: function(){ calls.length = 0; }
  };
})()
"""


@need_js
class FollowTheMic(unittest.TestCase):
    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var box = " + HARNESS.replace("%SOURCE%", follow_mic_source()))
        self.caps(propresenter_follow_mic=True, propresenter_quiet_s=10)

    def caps(self, **c):
        self.js.eval("box.caps(%s)" % json.dumps(c))

    def set(self, **o):
        self.js.eval("box.set(%s)" % json.dumps(o))

    def follow(self, on):
        self.js.eval("box.follow(%s)" % json.dumps(on))

    def tick(self):
        self.js.eval("box.tick()")

    def calls(self):
        return json.loads(self.js.eval("box.calls()"))

    def listen(self, at=0):
        """Start listening with the Mic on, as the page does (setListenUI)."""
        self.set(listening=True, mode="speaking", now=at, speech=at)
        self.follow(True)

    def seconds(self, s, speech=None):
        self.set(now=s * 1000, **({} if speech is None else {"speech": speech * 1000}))
        self.tick()

    # --- the Mic -----------------------------------------------------------
    def test_mic_on_shows_the_box(self):
        self.listen()
        self.assertEqual(self.calls(), ["show:show"])

    def test_mute_clears_it_and_unmute_shows_it_again(self):
        self.listen()
        self.set(mode="singing")
        self.follow(False)
        self.set(mode="speaking")
        self.follow(True)
        self.assertEqual(self.calls(), ["show:show", "clear", "show:show"])

    def test_stop_clears_it(self):
        self.listen()
        self.set(listening=False)
        self.follow(False)
        self.assertEqual(self.calls(), ["show:show", "clear"])

    def test_the_same_state_is_sent_once(self):
        self.listen()
        self.follow(True)
        self.follow(True)
        self.set(mode="singing")
        self.follow(False)
        self.follow(False)
        self.assertEqual(self.calls(), ["show:show", "clear"])

    def test_nothing_is_sent_without_the_server_flag(self):
        self.caps(propresenter_follow_mic=False)
        self.listen()
        self.set(mode="singing")
        self.follow(False)
        self.assertEqual(self.calls(), [])

    def test_the_propresenter_switch_plays_no_part(self):
        """The box is driven by the server's flag alone: the page's own switch
        for sending caption text is not consulted."""
        self.assertNotIn("ppStream", follow_mic_source())
        self.listen()                                       # the harness has no ppStream at all
        self.assertEqual(self.calls(), ["show:show"])

    # --- silence -----------------------------------------------------------
    def test_ten_seconds_of_silence_clear_it_and_speech_brings_it_back(self):
        self.listen()
        self.seconds(9)
        self.assertEqual(self.calls(), ["show:show"])      # not yet
        self.seconds(11)
        self.assertEqual(self.calls(), ["show:show", "clear"])
        self.seconds(30)
        self.assertEqual(self.calls(), ["show:show", "clear"])      # cleared once, not repeated
        self.seconds(31, speech=31)
        self.assertEqual(self.calls(), ["show:show", "clear", "show:show"])

    def test_speech_keeps_the_box_up(self):
        self.listen()
        for s in range(5, 60, 5):
            self.seconds(s, speech=s)
        self.assertEqual(self.calls(), ["show:show"])

    def test_zero_means_silence_never_clears_it(self):
        self.caps(propresenter_follow_mic=True, propresenter_quiet_s=0)
        self.listen()
        self.seconds(3600)
        self.assertEqual(self.calls(), ["show:show"])

    def test_the_limit_is_the_servers_setting(self):
        self.caps(propresenter_follow_mic=True, propresenter_quiet_s=3)
        self.listen()
        self.seconds(2)
        self.assertEqual(self.calls(), ["show:show"])
        self.seconds(4)
        self.assertEqual(self.calls(), ["show:show", "clear"])

    def test_silence_while_muted_sends_nothing_more(self):
        self.listen()
        self.set(mode="singing")
        self.follow(False)
        self.seconds(60)
        self.assertEqual(self.calls(), ["show:show", "clear"])

    def test_unmuting_after_a_quiet_stretch_shows_the_box(self):
        self.listen()
        self.seconds(11)                                    # silent: cleared
        self.set(mode="singing")
        self.follow(False)
        self.set(mode="speaking", speech=12000)             # the page restamps speech on resume
        self.seconds(12)
        self.follow(True)
        self.assertEqual(self.calls()[-1], "show:show")

    def test_silence_is_not_counted_when_not_listening(self):
        self.set(listening=False, now=100000)
        self.tick()
        self.assertEqual(self.calls(), [])


def prayer_source():
    """The detector block and pgDecide, cut from the real page."""
    html = read("index.html")
    a = html.index("// ---- prayer detector")
    b = html.index("// ---- end of prayer detector", a)
    c = html.index("function pgDecide(now){")
    d = html.index("function pgRenderDiag()", c)
    return html[a:b] + html[c:d]


PRAYER_HARNESS = """
(function(){
  var now = 0, lastModeAt = -1e9, serviceMode = 'speaking', modeByDetector = false, music = false;
  var MD_GUARD_MS = 5000, changes = [];
  var performance = {now: function(){ return now; }};
  function musicHeard(){ return music; }
  function renderHearChip(){}
  function pgRenderDiag(){}
  function setServiceMode(m, why, by){
    serviceMode = m; lastModeAt = now; modeByDetector = by || false; changes.push(m);
  }
  %SOURCE%
  var seed = 1;
  function noise(){ seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 1073741824 - 1; }
  return {
    state: function(o){
      if('mode' in o) serviceMode = o.mode;
      if('by' in o) modeByDetector = o.by;
      if('lastModeAt' in o) lastModeAt = o.lastModeAt;
      if('music' in o) music = o.music;
    },
    decide: function(t, o){ now = t; for(var k in o) pg[k] = o[k]; pgDecide(t); },
    feed: function(seconds, amp){
      for(var s = 0; s < seconds * 10; s++){
        var f = new Float32Array(1600);
        for(var i = 0; i < 1600; i++) f[i] = noise() * amp;
        now += 100; pgFeed(f, 16000);
      }
      return pg.feat !== null;
    },
    mode: function(){ return serviceMode; },
    changes: function(){ return JSON.stringify(changes); }
  };
})()
"""


@need_js
class PrayerGate(unittest.TestCase):
    """A few words of prayer on the wall are fine; a speaker held back is not."""
    QUIET = {"score": 0.7, "fast": 0.7, "offN": 0, "clear": False, "back": False}

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var gate = " + PRAYER_HARNESS.replace("%SOURCE%", prayer_source()))

    def state(self, **o):
        self.js.eval("gate.state(%s)" % json.dumps(o))

    def decide(self, t, **o):
        self.js.eval("gate.decide(%d, %s)" % (t, json.dumps({**self.QUIET, **o})))

    def mode(self):
        return self.js.eval("gate.mode()")

    def held(self, at=100000):
        """In a hold the prayer detector placed a second ago, so inside the anti-strobe guard."""
        self.state(mode="prayer", by="prayer", lastModeAt=at - 1000)

    def test_a_low_score_holds_after_two_seconds(self):
        self.decide(100000)
        self.assertEqual(self.mode(), "speaking")
        self.decide(102000)
        self.assertEqual(self.mode(), "prayer")

    def test_one_clear_window_starts_the_wait_again(self):
        self.decide(100000)
        self.decide(101000, clear=True)
        self.decide(102000)
        self.assertEqual(self.mode(), "speaking")

    def test_the_guard_still_spaces_out_holds(self):
        self.state(lastModeAt=100000)
        self.decide(100000)
        self.decide(102000)
        self.assertEqual(self.mode(), "speaking")
        self.decide(105000)
        self.assertEqual(self.mode(), "prayer")

    def test_music_outranks_a_hold(self):
        self.state(music=True)
        self.decide(100000)
        self.decide(102000)
        self.assertEqual(self.mode(), "speaking")

    def test_a_speaker_coming_back_ends_a_hold_inside_the_guard(self):
        self.held()
        self.decide(100000, back=True)
        self.assertEqual(self.mode(), "speaking")

    def test_the_fast_score_ends_a_hold_inside_the_guard(self):
        self.held()
        self.decide(100000, offN=1, fast=1.2)
        self.assertEqual(self.mode(), "speaking")

    def test_a_mute_by_the_operator_stays(self):
        self.state(mode="prayer", by=False)
        self.decide(100000, back=True)
        self.assertEqual(self.mode(), "prayer")

    def test_the_floor_follows_the_feed(self):
        for amp in (0.3, 0.003):
            js = MiniRacer()
            js.eval("var gate = " + PRAYER_HARNESS.replace("%SOURCE%", prayer_source()))
            self.assertTrue(js.eval("gate.feed(12, %f)" % amp), amp)
            self.assertFalse(js.eval("gate.feed(4, %f)" % (amp / 100)), amp)


if __name__ == "__main__":
    unittest.main()
