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
  var MD_GUARD_MS = 5000, changes = [], listening = true;
  function translatingNow(){ return serviceMode === 'speaking'; }
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
    heldBack: function(){ return speakerHeldBack(); },
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


@need_js
class SpeakerHeldBack(unittest.TestCase):
    """Muted by hand and someone starts speaking: the Mic pulses. A Gate's own
    mute is left alone, since it lets go by itself."""
    ONE = {"score": 1.3, "fast": 1.3, "offN": 0, "clear": True, "back": False}
    ROOM = {"score": 0.7, "fast": 0.7, "offN": 0, "clear": False, "back": False}

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var gate = " + PRAYER_HARNESS.replace("%PIN%", pin_source())
                     .replace("%SOURCE%", prayer_source()))
        self.t = 0

    def state(self, **o):
        self.js.eval("gate.state(%s)" % json.dumps(o))

    def seconds(self, n, window):
        for _ in range(n):
            self.t += 1000
            self.js.eval("gate.decide(%d, %s)" % (self.t, json.dumps(window)))
        return self.js.eval("gate.heldBack()")

    def test_muted_by_hand_and_he_speaks(self):
        self.state(mode="singing", by=False)
        self.assertFalse(self.seconds(2, self.ONE))
        self.assertTrue(self.seconds(1, self.ONE))

    def test_it_lasts_through_a_pause_between_sentences(self):
        self.state(mode="singing", by=False)
        self.seconds(4, self.ONE)
        self.assertTrue(self.seconds(2, self.ROOM))
        self.assertFalse(self.seconds(2, self.ROOM))

    def test_not_when_a_gate_muted_it(self):
        self.state(mode="singing", by=True)
        self.assertFalse(self.seconds(4, self.ONE))

    def test_not_while_the_mic_is_on(self):
        self.state(mode="speaking", by=False)
        self.assertFalse(self.seconds(4, self.ONE))

    def test_not_while_music_plays(self):
        self.state(mode="singing", by=False, music=True)
        self.assertFalse(self.seconds(4, self.ONE))

    def test_not_for_a_room_praying(self):
        self.state(mode="singing", by=False)
        self.assertFalse(self.seconds(6, self.ROOM))


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


def direction_source():
    """From `let autoFlip` to renderLangUI: Auto and Swap, the direction's two controls."""
    html = read("index.html")
    a = html.index("let autoFlip=true;")
    b = html.index("// Button face, menu ticks, which controls are live", a)
    return html[a:b]


DIRECTION_HARNESS = """
(function(){
  var activeSrc = 'hu', lastPairSrc = 'hu', lastSwapAt = 0, saved = 0;
  var LANG_NAMES = {hu: 'Hungarian', en: 'English'};
  var performance = {now: function(){ return 1; }};
  function sessionPair(){ return ['hu', 'en']; }
  function otherSide(c){ return c === 'hu' ? 'en' : 'hu'; }
  function saveSettings(){ saved++; }
  function renderLangUI(){}
  function log(){}
  %SOURCE%
  return {
    swap: swapDirection,
    auto: function(on){ setAutoFlip(on); },
    state: function(){ return JSON.stringify({auto: autoFlip, src: activeSrc, saved: saved}); }
  };
})()
"""


@need_js
class SwapTakesDirectionOffAuto(unittest.TestCase):
    """Swap and Auto are one split button with the Mic's rule: doing it by hand
    turns Auto off, and only Auto turns it back on."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var d = " + DIRECTION_HARNESS.replace("%SOURCE%", direction_source()))

    def state(self):
        return json.loads(self.js.eval("d.state()"))

    def test_a_swap_turns_auto_off_and_is_kept(self):
        self.js.eval("d.swap()")
        st = self.state()
        self.assertEqual(st["src"], "en")
        self.assertFalse(st["auto"])
        self.assertEqual(st["saved"], 1)

    def test_auto_stays_off_until_tapped(self):
        self.js.eval("d.swap(); d.swap()")
        self.assertFalse(self.state()["auto"])
        self.js.eval("d.auto(true)")
        self.assertTrue(self.state()["auto"])
        self.js.eval("d.swap()")
        self.assertFalse(self.state()["auto"])


def hold_source():
    """From FOLLOW_MS to audioSlice: whether the clauses voice holds a clip, and
    the pace it reads at."""
    html = read("index.html")
    a = html.index("const FOLLOW_MS=")
    b = html.index("function audioSlice(", a)
    return html[a:b]


HOLD_HARNESS = """
(function(){
  var now = 0, queued = 0, curRate = 1, fallbackMode = false, clauseRate = 1;
  var AUQ = {timer: 0, lastText: null, freeAt: 0}, AU = {el: null};
  function clauseMode(){ return true; }
  function applyDrift(){}
  function voiceDelayS(){ return 12; }
  function epochNow(){ return now; }
  function queuedAudioS(){ return queued; }
  function drainAudioTickets(){}
  function setTimeout(){ return 1; }
  %SOURCE%
  return {
    set: function(o){ now = o.now; queued = o.queued || 0; AUQ.lastText = o.last; AUQ.freeAt = o.freeAt || 0; AUQ.timer = 0; },
    hold: function(spokeMs, bytes){ return holdTicket({spokeMs: spokeMs, slices: [[new Array((bytes || 0) + 1).join('A'), false]]}); },
    pace: function(){ return clauseRate; }
  };
})()
"""


@need_js
class ASentencesNextClipFollowsOn(unittest.TestCase):
    """A clip carrying on the sentence the voice was just speaking plays straight
    on; anything else waits for its moment, his first word + the delay."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var h = " + HOLD_HARNESS.replace("%SOURCE%", hold_source()))

    def held(self, **o):
        self.js.eval("h.set(%s)" % json.dumps(o))
        return self.js.eval("h.hold(%d)" % 10000)     # due at 22 000 ms

    def test_a_new_sentence_waits_for_its_moment(self):
        self.assertTrue(self.held(now=15000, last="there must be prayer.", freeAt=15500))

    def test_the_rest_of_a_sentence_follows_while_the_voice_speaks(self):
        self.assertFalse(self.held(now=15000, last="beyond a life of obedience,", freeAt=15500))

    def test_and_within_the_grace_after_it_falls_silent(self):
        self.assertFalse(self.held(now=16800, last="beyond a life of obedience,", freeAt=15500))

    def test_but_not_long_after(self):
        self.assertTrue(self.held(now=17200, last="beyond a life of obedience,", freeAt=15500))

    def test_a_closing_quote_after_the_full_stop_still_ends_the_sentence(self):
        self.assertTrue(self.held(now=15000, last='and He will be gracious to me."', freeAt=15500))


@need_js
class OneSteadyPace(unittest.TestCase):
    """The clauses voice reads at one pace that rises quickly when sentences
    start late, falls slowly, ignores small wobbles, and changes only at a
    sentence's start (2026-11-21)."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var h = " + HOLD_HARNESS.replace("%SOURCE%", hold_source()))

    def start(self, late, queued=0, last="Amen.", b64=0):
        """A new sentence (his first word at 10 s, due at 22 s) whose turn comes `late` s late."""
        self.js.eval("h.set(%s)" % json.dumps({"now": 22000 + late * 1000, "queued": queued, "last": last}))
        self.assertFalse(self.js.eval("h.hold(10000, %d)" % b64))
        return self.js.eval("h.pace()")

    def test_on_time_sentences_read_at_1x(self):
        for _ in range(5):
            self.assertEqual(self.start(0), 1)

    def test_a_small_wobble_does_not_change_it(self):
        self.assertEqual(self.start(1), 1)
        self.assertEqual(self.start(-0.0), 1)

    def test_late_sentences_raise_it_quickly(self):
        paces = [self.start(6) for _ in range(4)]
        self.assertGreater(paces[0], 1)
        self.assertEqual(paces, sorted(paces))
        self.assertGreaterEqual(paces[-1], 1.1)

    def test_it_falls_slowly_and_never_past_1x(self):
        for _ in range(6):
            high = self.start(8)
        after = [self.start(0) for _ in range(5)]
        self.assertGreater(after[-1], 1)
        self.assertLess(high - after[-1], 0.15)
        for _ in range(80):
            low = self.start(0)
        self.assertEqual(low, 1)

    def test_capped_at_1_3(self):
        for _ in range(30):
            p = self.start(30)
        self.assertEqual(p, 1.3)

    def test_a_pile_up_reads_faster_for_that_sentence_only(self):
        self.assertEqual(self.start(0, b64=350000), 1.4)    # its own 16 s of audio
        self.assertEqual(self.start(0), 1)

    def test_the_rest_of_a_sentence_keeps_the_pace(self):
        for _ in range(4):
            p = self.start(8)
        self.js.eval("h.set(%s)" % json.dumps({"now": 15000, "last": "beyond a life of obedience,", "freeAt": 15500}))
        self.assertFalse(self.js.eval("h.hold(10000)"))
        self.assertEqual(self.js.eval("h.pace()"), p)


def delay_source():
    """From STYLED_MIN_S to voiceDueS: how far behind him the clauses voice sits."""
    html = read("index.html")
    a = html.index("const STYLED_MIN_S=")
    b = html.index("// Seconds until a caption's moment", a)
    return html[a:b]


DELAY_HARNESS = """
(function(){
  var caps = {voice_delay_s: 12, voice_style_s: 0, voice_director: 'claude'};
  var exprTags = {checked: false}, delayEl = {value: '12', dataset: {}};
  %SOURCE%
  return {
    set: function(o){ exprTags.checked = !!o.tags; if(o.slider != null){ delayEl.value = String(o.slider); delayEl.dataset.saved = '1'; } },
    delay: function(){ return voiceDelayS(); }
  };
})()
"""


@need_js
class SentenceWait(unittest.TestCase):
    """Settings › Voice › Sentence wait sets how far behind him the voice sits,
    never under 8 s while the Expression director styles the clips."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var d = " + DELAY_HARNESS.replace("%SOURCE%", delay_source()))

    def delay(self, **o):
        self.js.eval("d.set(%s)" % json.dumps(o))
        return self.js.eval("d.delay()")

    def test_the_servers_delay_until_the_slider_is_moved(self):
        self.assertEqual(self.delay(), 12)

    def test_the_slider(self):
        self.assertEqual(self.delay(slider=7), 7)

    def test_expression_tags_wait_at_least_8_s(self):
        self.assertEqual(self.delay(slider=6, tags=True), 8)
        self.assertEqual(self.delay(slider=10, tags=True), 10)
