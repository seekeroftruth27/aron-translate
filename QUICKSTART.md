# Quick Start

> New to Áron? The illustrated step-by-step guide is **[QUICKSTART.pdf](QUICKSTART.pdf)**.
> This page is the operator's reference.

Everything runs in the cloud — there's nothing to install beyond Python, and no
special hardware. Two audiences: **A. Operators** (machine is already set up —
just run it) and **B. New machine** (standing it up from scratch).

---

## A. Operate it (already set up)

1. **Double-click `Start Church Translate.bat`** (Windows). A console window opens,
   builds the Python env on first run, and the browser opens at
   `http://localhost:5000`. *(On other platforms: `python app/server.py` — see
   section B.)*
2. Pick a **Translate into** language (stage 2). This is fixed for the whole
   session — every segment, in whatever language it's spoken, is translated
   into this one target.
3. Click **Start Listening** (or press Space). The button turns red.
4. Speak, in any supported language. The **Original** panel fills as it's
   transcribed; the **Translation** panel fills with the chosen target
   language when the cloud returns it.
5. Optional: press **Speak** to also hear the translation aloud (adds
   latency; uses ElevenLabs or Soniox, whichever key is configured — the Soniox
   key already covers Soniox's voices).
6. To stop: click **Stop**, then close the console window.

What a service needs is in one row: **Start listening**, **Translate into**,
**Mic** (mute it for music), **Speak**, **Project**, **Clear wall** and, last,
**Settings**, which holds everything else by pipeline stage: **1 Listen** (input / gain / **Pace** / **Gate music** /
**Extra ears**), **2 Translate** (**Model** and the **Decision Router**), **3
Wall** (the projector's knobs, web captions, ProPresenter) and **4 Voice**. The
lights across the top show each link of the pipe at a glance; **Download**
(the session record, the spoken audio) sits beside them.

**How big each piece is (Pace).** Pace, in Settings › Listen, sets how much
text goes to the translator at a time: **Pronto** (~3–7 words), **Moderato**
(~7–10, the default), **Lento** (~10–14). Smaller pieces reach the screen
sooner; bigger ones give the translator more context. A change applies at the
next cut. Under the **Decision Router** Pace is greyed out: Soniox's own
segments set the size.

**Which model translates.** The **Model** menu picks the translator.
`claude-haiku-4-5` is the default and among the fastest (~0.6 s per caption).
One entry uses no model at all and captions Soniox's own translation:
**none, fast** shows only final text, a few words at a time, about 1 s sooner
than any model path, and nothing on the wall changes once shown — but Soniox
translates Romanian idioms word for word. Nothing is spoken under it.

- **Source language** is an accuracy *hint* for Soniox, not a restriction —
  leave it on **Auto-detect** (the default) unless you know only one language
  will be spoken and want to pin it for slightly better accuracy. Soniox
  streams continuously and marks phrase ends itself (lowest latency); it
  falls back automatically to per-segment transcription if the streaming
  socket ever fails.
- **Text-to-speech** shows ElevenLabs controls: a **Voice** dropdown (from
  your ElevenLabs account — one voice covers every target language, since
  ElevenLabs' multilingual voices speak whatever language the text is in), an
  **EL model** selector (v2 balanced · turbo/flash faster · v3 most
  expressive but slower · v4 newest and most natural · v4 Turbo, the
  default, nearly as fast as flash), and **Stability / Style / Speed** sliders
  (both v4 models take Stability only, so the other two hide).
  All apply *live* to the next spoken line (lower stability + higher style =
  more expressive, less monotone). **Expression** (v3 and v4 models) lets the
  translator give the voice a delivery hint — [gently], [solemnly],
  [joyfully]… — when the words call for one; the hint is never shown on the
  wall, and Debug lists each one chosen.

**The session record.** From the first Start Listening, the app writes a record
of the session into the `recordings` folder as it goes (`session-<date>_<time>`):
a `session.html` page you can open after the service, with the settings at the
start and every change after with its time, the models in use, and every line
with what the extra ears heard, who translated it (Soniox or the model) and
why the Decision Router sent it on. **Download › Session record** (or `H`)
downloads the same page.

**Keeping the service.** With Speak on, the spoken translation is recorded as
it goes (the switch is **Record speech**, in Settings › Voice).
**Download › Spoken audio** — or the `A` key — downloads it as one `.zip`: the
whole service as a single mp3, subtitle files timed to it, a timestamped map of
every clip, and a source-beside-translation transcript. **Auto-save on exit**
(on by default) writes the same thing into the `recordings` folder when you
close the app, so there is nothing to remember; **Save now** does it mid-service
and **Discard** starts a clean recording for the next one. The clips are held by
the app itself, not the browser tab, so a reload never loses them, and nothing
is uploaded anywhere.

**Costs.** The box in the top-right corner is the estimated spend of this run
across every API; hover for the breakdown. **Debug** also shows ElevenLabs'
remaining credits and this run's usage (STT minutes, TTS characters,
translation calls). *(If it shows "key lacks 'user read' scope", your ElevenLabs
key is scoped to STT+TTS only — the remaining-credit line needs user read, but
the this-run tally still works.)*

### Tuning the mic / feed (do this once per venue)

Watch the meter: green bar = level, **yellow** = speech threshold, **blue** =
noise floor. The system auto-tracks the room; you nudge four knobs:

1. **Input** — pick the microphone or soundboard/SDI capture device.
2. Stay silent ~2 s so the **blue** noise floor settles.
3. Speak normally — the green bar should clearly cross the **yellow** line.
   If it barely does, raise **Gain** until speech sits around **−20 to −12 dBFS**
   (don't slam 0 = clipping).
4. **Sensitivity** = how far above the noise floor counts as speech. Raise it if
   room noise starts segments; lower it if the starts of words get clipped.
5. **Hold** = silence before a phrase is cut. Raise it if captions cut
   mid-sentence; lower it if they lag or run on.

*(These mic knobs apply only when Soniox is running per-segment, e.g. after an
automatic fallback. Soniox's normal streaming mode does its own endpointing
and ignores the Silero VAD knobs.)*

Each translated line has a **▶** button to replay its speech (instant if Speak
was on — the already-synthesized clips are reused; synthesized on demand if
Speak was off). The **?** button at the top right opens the illustrated guide,
`QUICKSTART.pdf`. The **Diagnostics** button beside it opens a diagnostics
panel (engine/capability status + a live event log, with a Copy button), and
**Clear** wipes both caption panels without stopping the session. **Clear wall**
(or `W`) wipes only what the room is reading — projector, website, ProPresenter —
and keeps the transcript.

Settings — including your **Source language** and **Translate into** picks —
are remembered between sessions — in the browser and on the server too
(`operator-settings.json` in the recordings folder), so a different browser,
a private window or a cleared cache still opens on your settings.

### If the page closes mid-service

The operator page runs the service: it listens to the microphone, asks for each
translation and plays the voice. **Closing it (or the browser) stops the
translation.** The black console window — the server — keeps running, and the
projector and website keep the last captions on screen (the projector fades
them after 15 s of silence). Keep the page open for the whole service; you can
minimise it or cover it. Plug a laptop in and turn off sleep: sleep stops the
microphone too.

**Saved for you when it closes:**
- **The speech recording** — written to the `recordings` folder (with Auto-save
  on exit on). If the browser crashes, is force-quit or the computer sleeps, so
  the page never gets to say goodbye, the server notices the audio stream drop
  and saves it itself when no page has come back within 30 seconds.
- **The session record** — its last rows too, including a caption still being
  translated, and it is marked closed.
- **Your settings** — kept on the server as you change them.

**Reopen `http://localhost:5000` and press Start** (or the Space bar) to carry on.
What comes back:
- **the transcript** — if you are back within **20 minutes**, the Original and
  Translation columns are refilled and a note says "Resumed the service from …";
  the session record carries on as the **same** service, not a second file.
  Click **Start fresh** in that note to empty the columns and begin a new
  session record instead (the room's screens are left alone — **Clear wall** is
  for those);
- your settings, even in a different browser;
- the translator's context, if you are back within **5 minutes** — it carries on
  with the sermon's thread; after that it starts fresh;
- the speech recording — it continues as the same service, in the same folder;
- the cost so far.

What does not: anything said while the page was closed was never heard, so it is
not translated. Restored rows are text only — no ▶ replay and no editing. After
more than 20 minutes away, or a server restart, the page starts a new service.
Only a session a page has left is picked up: a second tab opened beside a page
that is still running starts on its own.

---

## B. Set up a new machine

All you need is Python and the API keys.

### 1. Python 3.9+
Install from <https://www.python.org/downloads/> (on Windows, tick **"Add
python.exe to PATH"**).

### 2. API keys
Copy `.env.example` to `.env` in the project root and fill in:

```bash
cp .env.example .env
# then edit .env:
#   SONIOX_API_KEY=...             (required: STT, its own translation, voice)
#   ANTHROPIC_API_KEY=sk-ant-...   (optional: Claude translators)
#   OPENAI_API_KEY=...             (optional: gpt-6 translators, the Router, OpenAI ear)
#   GEMINI_API_KEY=...             (optional: Gemini translators, Gemini Live ear)
#   ELEVENLABS_API_KEY=...         (optional: ElevenLabs voice and ear)
```

The Soniox key alone does all three stages — speech-to-text, translation
(Soniox's own, shown as **none, fast**), and a spoken voice. That is the whole
minimal setup; everything below is optional. Each LLM key adds its translators
to the **Model** menu. The server reads `.env` directly — no shell setup needed.

- Anthropic key: <https://console.anthropic.com> (per-token billing, **separate
  from Claude.ai Pro**).
- Soniox key: <https://console.soniox.com>.
- ElevenLabs key + voice id: <https://elevenlabs.io> (voice ids live in your
  Voice Library).

### 3. Install deps and run
```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app/server.py
```

Then open <http://localhost:5000>. On Windows you can instead double-click
**`Start Church Translate.bat`**, which does the venv build and key prompt for
you on first run.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **Model menu greyed out** | That provider has no key in `.env` (step 2), or the `.env` beside the app isn't the one you edited — after unpacking a new version into a new folder, copy your `.env` over. A Windows environment variable of the same name (e.g. `OPENAI_API_KEY`) overrides `.env` even when it is empty. With no LLM key the app translates with **none, fast**. |
| **The operator page was closed mid-service** | Reopen `http://localhost:5000` and press **Start**. The transcript comes back (within 20 minutes), and your settings, the speech recording and (within 5 minutes) the translator's context carry on. See *If the page closes mid-service* above. |
| **`Soniox key not configured`** | Add `SONIOX_API_KEY` to `.env` and relaunch — it's the only STT engine. |
| **Speak does nothing / "TTS not configured"** | With only a Soniox key, Speak works straight away (it defaults to the Soniox voice). If silent, **ElevenLabs** is selected under **Speech** without a voice id — switch **Speech** to **Soniox**, or add `ELEVENLABS_API_KEY` + `CT_EL_VOICE`. |
| **Can't start the server — "address already in use"** | An old server is still running on port 5000. Stop it (or set `CT_PORT`) and relaunch. |
| **Start button does nothing** | Hard-refresh the page (**Ctrl+Shift+R**). If still dead, press **F12 → Console** and read the red error. |
| **Mic blocked / "Mic error"** | Browsers only allow mic capture over **`localhost`** or **HTTPS**. Opening the page via a LAN IP over `http://` will fail — keep it on `localhost`, or set up HTTPS before using another device. |
| **Captions cut mid-sentence** | Move **Pace** one step slower (Pronto → Moderato → Lento). **Captions lag** → one step faster. (On the per-segment fallback path, **Hold** does this job.) |
| **Source and translation rows don't line up** under **none, fast** | Fixed 2026-09-24 — update to the current version. |
