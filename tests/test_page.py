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


def pin_source():
    """The operator's Auto / Manual (operatorMode, micToAuto, ...), cut from the real page."""
    html = read("index.html")
    a = html.index("// ---- the operator's Auto / Manual")
    return html[a:html.index("// ---- end of the operator's Auto / Manual", a)]


PRAYER_HARNESS = """
(function(){
  var now = 0, lastModeAt = -1e9, serviceMode = 'speaking', modeByDetector = false, music = false;
  var MD_GUARD_MS = 5000, changes = [];
  var autoMode = false, autoPrayer = true;
  var MODE_LABEL = {speaking: 'Speaking', singing: 'Music', prayer: 'Prayer'};
  var performance = {now: function(){ return now; }};
  function musicHeard(){ return music; }
  function renderHearChip(){}
  function pgRenderDiag(){}
  function renderModeUI(){}
  function log(){}
  function jEvent(){}
  function setServiceMode(m, why, by){
    if(m === serviceMode) return;
    if(by && micManual) return;                 // as the page's: Manual refuses the detectors
    serviceMode = m; lastModeAt = now; modeByDetector = by || false; changes.push(m);
  }
  %PIN%
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
    click: function(m, t){ now = t; operatorMode(m, 'operator'); },
    manual: function(){ return micManual; },
    auto: function(t){ now = t; micToAuto(); },
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
        self.js.eval("var gate = " + PRAYER_HARNESS.replace("%PIN%", pin_source())
                     .replace("%SOURCE%", prayer_source()))

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

    # --- Auto / Manual ------------------------------------------------------
    def test_an_unmute_during_a_prayer_hold_stays_on(self):
        """A click used to buy the 5 s guard; then the prayer gate held again."""
        self.held(at=100000)
        self.js.eval("gate.click('speaking', 100500)")
        self.assertEqual(self.mode(), "speaking")
        self.assertTrue(self.js.eval("gate.manual()"))
        for t in range(101000, 131000, 1000):                    # 30 s more of the praying room
            self.decide(t)
        self.assertEqual(self.mode(), "speaking")

    def test_back_on_auto_the_next_prayer_holds(self):
        self.held(at=100000)
        self.js.eval("gate.click('speaking', 100500)")
        self.decide(101000, back=True)                            # one voice: still Manual
        self.assertTrue(self.js.eval("gate.manual()"))
        self.js.eval("gate.auto(102000)")
        self.decide(110000)                                       # a prayer, well past the guard
        self.decide(112000)
        self.assertEqual(self.mode(), "prayer")

    def test_the_floor_follows_the_feed(self):
        for amp in (0.3, 0.003):
            js = MiniRacer()
            js.eval("var gate = " + PRAYER_HARNESS.replace("%PIN%", pin_source())
                    .replace("%SOURCE%", prayer_source()))
            self.assertTrue(js.eval("gate.feed(12, %f)" % amp), amp)
            self.assertFalse(js.eval("gate.feed(4, %f)" % (amp / 100)), amp)


if __name__ == "__main__":
    unittest.main()


def append_source():
    """pumpAppend, maybeStart and evictBehind, cut from the real page."""
    html = read("index.html")
    a = html.index("function pumpAppend(){")
    b = html.index("function applyDrift(){", a)
    return html[a:b]


APPEND_HARNESS = """
(function(){
  var appended = [], removed = [], logs = [], full = 0, buffered = [[0, 40]], t = 20;
  var KEEP_BEHIND_S = 30, SPANS = [], lastSpanEl = null;
  var AU = {q: [], started: true, broken: false,
            el: {get currentTime(){ return t; },
                 buffered: {get length(){ return buffered.length; },
                            start: function(i){ return buffered[i][0]; },
                            end: function(i){ return buffered[i][1]; }}},
            sb: {updating: false,
                 appendBuffer: function(b){
                   if(full > 0){ full--; var e = new Error('full'); e.name = 'QuotaExceededError'; throw e; }
                   appended.push(b.id);
                 },
                 remove: function(a, b){ removed.push([a, b]); buffered[0][0] = b; }}};
  function audioReady(){ return true; }
  function bufEnd(){ return buffered[0][1]; }
  function audioFallback(w){ logs.push('fallback ' + w); }
  function log(l, m){ logs.push(m); }
  %SOURCE%
  return {
    push: function(id){ AU.q.push({buf: {id: id, length: 100}, el: null}); },
    full: function(n){ full = n; },
    at: function(s){ t = s; },
    pump: function(){ pumpAppend(); },
    state: function(){ return JSON.stringify({appended: appended, removed: removed, queued: AU.q.length, logs: logs}); }
  };
})()
"""


@need_js
class AppendingVoice(unittest.TestCase):
    """The room's voice: slices go into one MediaSource. When it reports itself full,
    the slice that failed is retried after space is freed; nothing is skipped."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var v = " + APPEND_HARNESS.replace("%SOURCE%", append_source()))

    def state(self):
        return json.loads(self.js.eval("v.state()"))

    def test_a_full_buffer_frees_space_and_retries_the_same_slice(self):
        for i in (1, 2, 3):
            self.js.eval("v.push(%d)" % i)
        self.js.eval("v.full(1)")
        self.js.eval("v.pump()")                  # slice 1 bounces: space is freed
        st = self.state()
        self.assertEqual(st["appended"], [])
        self.assertEqual(st["removed"], [[0, 15]])  # everything up to 5 s behind the playhead
        self.assertEqual(st["queued"], 3)           # nothing dropped
        for _ in range(3):                          # each updateend pumps again
            self.js.eval("v.pump()")
        self.assertEqual(self.state()["appended"], [1, 2, 3])

    def test_with_nothing_to_free_the_slice_is_let_go_and_said(self):
        self.js.eval("v.at(3)")                     # the playhead near the start: nothing behind it
        self.js.eval("v.push(1); v.push(2); v.full(1); v.pump()")
        st = self.state()
        self.assertEqual(st["queued"], 1)           # slice 1 let go, slice 2 still waiting
        self.assertTrue(any("voice buffer full" in m for m in st["logs"]))
        self.js.eval("v.pump()")
        self.assertEqual(self.state()["appended"], [2])
