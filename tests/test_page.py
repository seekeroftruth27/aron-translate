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


def pad_source():
    """enqueueAudio and the silence after a clip, cut from the real page."""
    html = read("index.html")
    a = html.index("function enqueueAudio(")
    b = html.index("// Follow the playhead down the transcript.", a)
    return html[a:b]


PAD_HARNESS = """
(function(){
  var queued = 0, curRate = 1, clauses = true, fallbackMode = false, timers = {}, nextTimer = 1;
  var AU = {q: [], broken: false, clipStart: true, tail: false, frame: null, padTimer: 0};
  function fbEnqueue(){} function initAudioOut(){} function pumpAppend(){} function maybeStart(){} function applyDrift(){}
  function clauseMode(){ return clauses; }
  function queuedAudioS(){ return queued; }
  function setTimeout(f, ms){ timers[nextTimer] = {f: f, ms: ms}; return nextTimer++; }
  function clearTimeout(id){ delete timers[id]; }
  function b64Bytes(b){ return Uint8Array.from(b); }     // the page's base64; plain byte lists here
  %SOURCE%
  return {
    set: function(o){ if(o.queued != null) queued = o.queued; if(o.clauses != null) clauses = o.clauses; if(o.rate) curRate = o.rate; },
    feed: function(b64, last){ enqueueAudio(b64, last, null); },
    fire: function(){ var ids = Object.keys(timers); var t = timers[ids[0]]; delete timers[ids[0]]; AU.padTimer = 0; t.f(); },
    timers: function(){ return JSON.stringify(Object.keys(timers).map(function(k){ return timers[k].ms; })); },
    q: function(){ return JSON.stringify(AU.q.map(function(x){ return {n: x.buf.length, el: x.el, head: Array.from(x.buf.slice(0, 4))}; })); },
    frame: function(b){ var f = mp3Frame(b64Bytes(b)); return f && JSON.stringify({header: Array.from(f.header), s: f.s, bytes: f.bytes}); },
    silence: function(b, s){ return JSON.stringify(Array.from(mp3Silence(mp3Frame(b64Bytes(b)), s))); }
  };
})()
"""


def _mp3(header, frames=3, id3=True):
    """A clip's opening bytes, as a list: an ID3 tag (if asked) and `frames` frames of junk audio."""
    ver = (header[1] >> 3) & 3
    samples, rate = (1152, 44100) if ver == 3 else (576, 24000)
    length = samples // 8 * 128 * 1000 // rate + ((header[2] >> 1) & 1)
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + b"T" * 10 if id3 else b""
    return list(tag + (bytes(header) + b"\x55" * (length - 4)) * frames)


SONIOX = (0xFF, 0xF3, 0xC4, 0xC4)        # MPEG-2 Layer III, 128 kb/s, 24 kHz, mono, no CRC
ELEVEN = (0xFF, 0xFA, 0x92, 0x64)        # MPEG-1 Layer III, 128 kb/s, 44.1 kHz, padded, with CRC


@need_js
class ClipEndsInSilence(unittest.TestCase):
    """A MediaSource that runs dry halts 0.1-0.15 s short of its last sample and
    plays the rest only after the next append, glued to the next clip ("hear… ts",
    2026-11-22). So a finished clip with nothing behind it gets silence after it,
    just before the voice would run out, and nothing else does."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var p = " + PAD_HARNESS.replace("%SOURCE%", pad_source()))

    def q(self):
        return json.loads(self.js.eval("p.q()"))

    def feed(self, last=False, b64=None):
        self.js.eval("p.feed(%s, %s)" % (json.dumps(b64 if b64 is not None else _mp3(SONIOX)), json.dumps(last)))

    def test_the_frame_shape_of_soniox_and_elevenlabs(self):
        sx = json.loads(self.js.eval("p.frame(%s)" % json.dumps(_mp3(SONIOX))))
        self.assertEqual(sx, {"header": [0xFF, 0xF3, 0xC4, 0xC4], "s": 0.024, "bytes": 384})
        el = json.loads(self.js.eval("p.frame(%s)" % json.dumps(_mp3(ELEVEN, id3=False))))
        self.assertEqual(el["header"], [0xFF, 0xFB, 0x90, 0x64])      # no CRC, no padding
        self.assertEqual(el["bytes"], 417)

    def test_the_same_silence_as_the_recorder(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("recorder_pad", os.path.join(ROOT, "app", "recorder.py"))
        rec = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rec)
        for header, frame_s in ((SONIOX, 576 / 24000), (ELEVEN, 1152 / 44100)):
            clip = _mp3(header)
            mine = bytes(json.loads(self.js.eval("p.silence(%s, %r)" % (json.dumps(clip), frame_s))))
            theirs, _ = rec.silence(rec._first_frame(bytes(clip)), frame_s)
            self.assertEqual(mine, theirs)                   # one silent frame, byte for byte

    def test_silence_goes_on_just_before_the_voice_runs_out(self):
        self.js.eval("p.set({queued: 3})")
        self.feed(); self.feed(last=True)
        self.assertEqual(len(self.q()), 2)                    # not yet: 3 s still to play
        self.assertEqual(json.loads(self.js.eval("p.timers()")), [2600])
        self.js.eval("p.set({queued: 0.4}); p.fire()")
        q = self.q()
        self.assertEqual(len(q), 3)
        self.assertEqual(q[-1]["head"], [0xFF, 0xF3, 0xC4, 0xC4])
        self.assertEqual(q[-1]["n"], 384 * 13)               # at least 0.3 s, in 24 ms frames
        self.assertIsNone(q[-1]["el"])                       # no line of its own

    def test_not_when_the_next_clip_comes_in_time(self):
        self.js.eval("p.set({queued: 3})")
        self.feed(last=True)
        self.js.eval("p.set({queued: 5})")
        self.feed()                                           # the next clip's first slice
        self.assertEqual(json.loads(self.js.eval("p.timers()")), [])
        self.feed(last=True)
        self.js.eval("p.set({queued: 0.2}); p.fire()")
        self.assertEqual(len(self.q()), 4)                    # one pad, after the second clip only

    def test_not_in_the_middle_of_a_clip(self):
        self.js.eval("p.set({queued: 0.1})")
        self.feed()                                           # still streaming: the voice runs dry mid-word
        self.assertEqual(len(self.q()), 1)
        self.assertEqual(json.loads(self.js.eval("p.timers()")), [])

    def test_at_once_when_it_is_already_running_out(self):
        self.js.eval("p.set({queued: 0.2})")
        self.feed()
        self.assertEqual(len(self.q()), 1)
        self.feed(last=True, b64="")                          # a clip may close with an empty slice
        self.assertEqual(len(self.q()), 2)

    def test_the_wait_follows_the_pace(self):
        self.js.eval("p.set({queued: 4.4, rate: 1.25})")
        self.feed(last=True)
        self.assertEqual(json.loads(self.js.eval("p.timers()")), [3200])

    def test_not_for_the_continuous_stream(self):
        self.js.eval("p.set({queued: 0.2, clauses: false})")
        self.feed(last=True)
        self.assertEqual(len(self.q()), 1)


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
  var now = 0, queued = 0, curRate = 1, fallbackMode = false, clauseRate = 1, logged = [];
  var AUQ = {timer: 0, lastText: null, freeAt: 0}, AU = {el: null};
  function clauseMode(){ return true; }
  function applyDrift(){}
  function voiceDelayS(){ return 12; }
  function epochNow(){ return now; }
  function queuedAudioS(){ return queued; }
  function drainAudioTickets(){}
  function setTimeout(){ return 1; }
  function log(kind, msg){ logged.push(msg); }
  function jEvent(){}
  %SOURCE%
  return {
    set: function(o){ now = o.now; queued = o.queued || 0; AUQ.lastText = o.last; AUQ.freeAt = o.freeAt || 0; AUQ.timer = 0; },
    hold: function(spokeMs, text){
      var slot = {spokeMs: spokeMs, text: text === undefined ? 'He spoke.' : text, slices: [['AAAA', false]]};
      var held = holdTicket(slot);
      return JSON.stringify({held: held, lead: slot.lead || 0, drop: !!slot.drop});
    },
    pace: function(){ return clauseRate; },
    dropping: function(){ return natDropping; },
    logged: function(){ return JSON.stringify(logged); }
  };
})()
"""

DUE = 22000      # his first word at 10 s + the 12 s delay


class _Hold(unittest.TestCase):
    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var h = " + HOLD_HARNESS.replace("%SOURCE%", hold_source()))

    def go(self, now, queued=0, last="Amen.", freeAt=0, text="He spoke."):
        """A clip whose first word he said at 10 s, offered at `now` ms with
        `queued` s of speech still ahead of it."""
        self.js.eval("h.set(%s)" % json.dumps({"now": now, "queued": queued, "last": last, "freeAt": freeAt}))
        return json.loads(self.js.eval("h.hold(10000, %s)" % json.dumps(text)))

    def late_by(self, s, text="He spoke."):
        """A new sentence that can start `s` seconds after its moment (the voice
        busy until then)."""
        return self.go(DUE + s * 1000 - 2000, queued=2, text=text)


@need_js
class ASentenceStartsInHisPause(_Hold):
    """A sentence starts at its moment less NAT_EARLY, or a natural gap after the
    voice falls quiet, whichever is later (2026-11-22)."""

    def test_well_before_its_moment_it_waits(self):
        self.assertTrue(self.go(15000)["held"])                 # its start, 18 000, is 3 s off

    def test_up_to_4_s_early_with_silence_in_front(self):
        r = self.go(16600)                                      # 1.4 s before 18 000
        self.assertFalse(r["held"])
        self.assertAlmostEqual(r["lead"], 1.4, places=3)

    def test_a_closing_quote_after_the_full_stop_still_ends_the_sentence(self):
        self.assertTrue(self.go(15000, last='and He will be gracious to me."', freeAt=15500)["held"])

    def test_when_late_it_starts_after_a_sentence_gap_not_on_the_last_sound(self):
        r = self.go(25000, queued=2)                            # the voice busy until 27 000, 5 s late
        self.assertFalse(r["held"])
        self.assertAlmostEqual(r["lead"], 0.3, places=3)

    def test_far_behind_the_gap_narrows_but_stays(self):
        r = self.go(30000, queued=4)                            # quiet at 34 000, 12 s late
        self.assertAlmostEqual(r["lead"], 0.2, places=3)


@need_js
class TheRestOfASentenceFollowsOn(_Hold):
    """A clip carrying on the sentence the voice was just speaking follows after a
    short clause gap, whatever its moment."""

    def test_while_the_voice_speaks(self):
        r = self.go(15000, queued=1, last="beyond a life of obedience,", freeAt=16000)
        self.assertFalse(r["held"])
        self.assertAlmostEqual(r["lead"], 0.08, places=3)

    def test_within_the_grace_after_it_falls_silent(self):
        self.assertFalse(self.go(16800, last="beyond a life of obedience,", freeAt=15500)["held"])

    def test_long_after_it_waits_like_a_new_sentence(self):
        self.assertTrue(self.go(15000, last="beyond a life of obedience,", freeAt=13000)["held"])

    def test_it_keeps_the_pace(self):
        for _ in range(3):
            self.late_by(20)
        p = self.js.eval("h.pace()")
        self.go(DUE + 25000, queued=1, last="beyond a life of obedience,", freeAt=DUE + 26000)
        self.assertEqual(self.js.eval("h.pace()"), p)


@need_js
class HardlyFaster(_Hold):
    """Under 10 s behind it reads at 1x; past that a little faster, never past
    1.1x, set where a sentence begins, back to 1x once under 5 s behind."""

    def pace(self, late):
        self.assertFalse(self.late_by(late)["held"])
        return self.js.eval("h.pace()")

    def test_on_time_and_a_little_late_read_at_1x(self):
        for late in (0, 3, 8, 10):
            self.assertEqual(self.pace(late), 1)

    def test_past_10_s_a_little_faster(self):
        self.assertEqual(self.pace(12.3), 1.02)
        self.assertEqual(self.pace(15.3), 1.06)

    def test_never_past_1_1(self):
        self.assertEqual(self.pace(29), 1.1)

    def test_it_holds_until_under_5_s_then_back_to_1x(self):
        self.pace(20)
        self.assertEqual(self.pace(7), 1.1)
        self.assertEqual(self.pace(4.5), 1)


@need_js
class LeftOutWholeNeverHalved(_Hold):
    """Past 30 s behind a whole sentence is left out, with the rest of it; a piece
    carrying on a sentence only past 40 s."""

    def test_a_sentence_30_s_late_is_left_out(self):
        self.assertTrue(self.late_by(31)["drop"])
        self.assertIn("left out a sentence, 31 s behind him", self.js.eval("h.logged()"))

    def test_and_the_rest_of_it_too(self):
        self.assertTrue(self.late_by(31, text="for we are His workmanship,")["drop"])
        self.assertTrue(self.js.eval("h.dropping()"))
        self.assertTrue(self.go(DUE, text="created for good works.")["drop"])     # whatever its time
        self.assertFalse(self.js.eval("h.dropping()"))
        self.assertFalse(self.go(16600)["drop"])                                  # the next sentence plays

    def test_a_clip_with_no_words_ends_the_run(self):
        self.late_by(31, text=None)
        self.assertFalse(self.js.eval("h.dropping()"))

    def test_a_piece_carrying_on_waits_until_40_s(self):
        last = dict(last="beyond a life of obedience,")
        self.assertFalse(self.go(DUE + 33000, queued=1, freeAt=DUE + 34000, **last)["drop"])
        self.assertTrue(self.go(DUE + 40000, queued=1, freeAt=DUE + 41000, **last)["drop"])


def drain_source():
    """From audioSlice to resetAudioTickets: how a held clip is fed or left out."""
    html = read("index.html")
    a = html.index("function audioSlice(")
    b = html.index("// Where each batch's speech sits on the one timeline", a)
    return html[a:b]


DRAIN_HARNESS = """
(function(){
  var fed = [], verdict = {held: false, lead: 0, drop: false}, curRate = 1, fallbackMode = false;
  var AUQ = {next: 0, serving: 0, held: new Map(), timer: 0, lastText: null, freeAt: 0};
  var AU = {q: [], frame: null, broken: false}, clauseRate = 1, natPace = 1, natDropping = false, paceAt = null;
  function holdTicket(slot){ if(verdict.held) return true; slot.lead = verdict.lead; if(verdict.drop) slot.drop = true; return false; }
  function enqueueAudio(b64, last, el){ fed.push('clip:' + b64); }
  function queuedAudioS(){ return 0; }
  function epochNow(){ return 0; }
  function b64Bytes(b){ return [1, 2, 3]; }
  function mp3Frame(u){ return {s: 0.024, bytes: 384, header: [255]}; }
  function mp3Silence(f, seconds){ return {silence: seconds}; }
  function clearSpeaking(){}
  function clearTimeout(){}
  %SOURCE%
  return {
    run: function(v, slices){
      verdict = v; fed = []; AU.q = []; AUQ.held.clear(); AUQ.serving = 0; AUQ.next = 0;
      AUQ.held.set(0, {slices: slices, done: true, el: null, spokeMs: 1});
      drainAudioTickets();
      return JSON.stringify({fed: fed, q: AU.q.map(function(x){ return x.buf.silence; })});
    }
  };
})()
"""


@need_js
class ThePauseIsSilenceInTheTimeline(unittest.TestCase):
    """The gap goes into the timeline as silence in front of the clip, so the
    voice never runs dry in it; a clip left out never reaches the speaker."""

    def setUp(self):
        self.js = MiniRacer()
        self.js.eval("var d = " + DRAIN_HARNESS.replace("%SOURCE%", drain_source()))

    def run_(self, **v):
        verdict = {"held": False, "lead": 0, "drop": False, **v}
        return json.loads(self.js.eval("d.run(%s, [['AAAA', false], ['BBBB', true]])" % json.dumps(verdict)))

    def test_silence_goes_in_first(self):
        r = self.run_(lead=0.3)
        self.assertEqual(r["q"], [0.3])
        self.assertEqual(r["fed"], ["clip:AAAA", "clip:BBBB"])

    def test_no_gap_no_silence(self):
        self.assertEqual(self.run_()["q"], [])

    def test_a_clip_left_out_is_not_heard(self):
        r = self.run_(drop=True)
        self.assertEqual(r["fed"], [])
        self.assertEqual(r["q"], [])

    def test_a_held_clip_waits(self):
        self.assertEqual(self.run_(held=True)["fed"], [])


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
