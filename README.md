# Áron — live any-language captions

> "He shall speak for you to the people, and he shall be your mouth,"
> Exodus 4:16

Real-time speech translation for a church service. Speech is captured,
transcribed, translated, and shown as **live captions**. The operator picks
**one target language** and Soniox auto-detects whatever language is actually
being spoken (any of its 60), so any speaker gets captioned into that single
chosen target, with optional spoken output.

**Languages** are three lists (`scripts/languages.py`): Soniox *hears* 60 and
translates among them itself (**none, fast**); a translator Model *captions*
the ones it was measured to write — gpt-6-sol 150, gpt-6.1-terra 145, gemini-3.5-flash-lite 126,
claude-haiku-4-5 62, any
Model not yet measured the 29 offered since launch
(`validation/results/2026-10-25-caption-languages/`, full table in
[docs/languages.md](docs/languages.md)); and each voice *speaks*
its own list (Soniox 63, ElevenLabs v3/v4 85, v2 29). A language Soniox does
not hear is one-way only (no bilingual pair, no Decision Router); one the voice cannot
say is captioned and not spoken. The language menu marks the first with an
asterisk ("Target not supported by Soniox translation: use a more capable
Model") and the second with "no voice".

Everything runs in the **cloud** — there are no local models to install and no
special hardware. You need Python and a couple of API keys.

```
speech ─► VAD chunk / Soniox stream ─► [STT] ──► text ─► [translate] ─► captions
         (browser)                     Soniox         Claude              (chosen target)
                                        (cloud, any                        (+ TTS: ElevenLabs
                                         source language)                   or Soniox)
```

Source language is auto-detected per segment (or pinned as a hint, for a bit
more accuracy when only one language will be spoken); the target is fixed for
the whole session via the **Translate into** picker. The two caption panels
have fixed roles — left is always **Original** (whatever's heard, in whatever
language), right is always **Translation** (in the chosen target) — so a
listener just watches the Translation column regardless of who's speaking or
in what language.

## Models in the pipeline

| Stage | Service | Notes |
|---|---|---|
| Speech-to-text | **Soniox** (realtime) | Multilingual — auto-detects any of 60+ languages, or pin a source-language hint for slightly better accuracy. Streams continuously and does its own phrase endpointing (the low-latency default); falls back automatically to per-segment (Silero VAD) POSTs if the streaming socket fails. Returns punctuation. Needs `SONIOX_API_KEY`. |
| Translation | **`claude-haiku-4-5`** (default) / **`claude-sonnet-5-5`**, and optional **OpenAI**, **xAI**, **Google Gemini** models — every model is named in the app exactly as its API spells it | Selectable per request from the UI's **Model** dropdown: Haiku for speed, Sonnet 5.5 for stronger contextual inference (homophones, word-boundary mistakes, proper-name recovery, who an ambiguous "sunt"/"am" refers to) at higher cost, with its thinking turned off (`"between_tools"`; it rejects `"disabled"`) — at adaptive `low` it scored no better, was no faster, and now and then reasoned into the caption itself (`validation/results/2026-10-19-sonnet-no-thinking/`). Three more backends join behind the **same system prompt and caption-safety hardening**, each shown only when its key is set (a saved pick with no key bounces back to the Claude default): **OpenAI** `gpt-6-luna` (`OPENAI_API_KEY`), **xAI** `grok-4.20-non-reasoning` and `grok-4.3` with its reasoning pinned off (`XAI_API_KEY`), and **Google Gemini** — `gemini-3.5-flash-lite` (default; ~0.54 s to first word, the tighter latency tail a congregation notices) or `gemini-3.8-flash` (slower but better at rebuilding the hyphens Romanian STT drops, e.g. `va dat`→`v-a dat`), both pinned to their lowest reasoning level (`GEMINI_API_KEY`; optional prompt caching via `CT_GEMINI_CACHE`). One Church/Scripture-aware system prompt works for any target language; each segment carries a `→Language:` tag (e.g. `→Spanish:`) and a rolling window of recent segments — kept per target language — as context. Claude needs `ANTHROPIC_API_KEY` (billed per-token, separate from a Claude.ai Pro subscription); every LLM key is optional — with none, the app captions with **none, fast**. One picker entry calls no model at all: **`none, fast`** makes Soniox's own live translation the caption (see *Soniox's own translation* below). |
| Text-to-speech | **ElevenLabs** or **Soniox** | Optional (the **Speak** button), and interchangeable — the **Speech** dropdown switches backend and model live, and only backends with a key behind them are offered. Both return 24 kHz PCM directly and speak whichever target language is active, so neither needs per-language voice mapping.<br>**ElevenLabs** — one multilingual voice (`CT_EL_VOICE`) speaks whatever language the text is in. Bills **per character** ($0.04–0.08/1k). The **Speech** menu offers **ElevenLabs v4** and **ElevenLabs v4 Turbo** (the default, and faster) beside **Soniox v2**; the older ElevenLabs models are no longer offered. Stability is `CT_EL_STABILITY` (default 0.5); v4 takes no other knob. **Expression** (`CT_EL_EXPRESSION=1`, off by default; v3 and v4 models) lets the translator lead a reply with a delivery tag for the voice (`[gently]`, `[solemnly]`, `[joyfully]`…), taken off before the caption is shown. Each clip carries a **running context** — the text and request ids of the clips the voice said just before — so a caption cut mid-sentence is read as the continuation it is, not as a fresh opening (all but v3, which refuses it; ~10 ms of first byte). With v4 or v4 Turbo, the voice goes further (**Stream**, on by default; `CT_EL_STREAM=0` turns it off): every caption goes into one open connection (ElevenLabs' Text to Dialogue WebSocket) and the whole service is read as one continuous speech instead of a clip per caption, at about 0.3 s more delay (`scripts/voice_stream.py`). While the voice runs behind, sentences are held for the next caption so it reads them with what follows in hand. Stability does not apply there. Supports **soundcheck voice capture**, and is the only backend that can remix the speaker's native accent out of the clone — see below.<br>**Soniox** — reuses `SONIOX_API_KEY` from speech-to-text, so if STT works, spoken output needs nothing further; 70 built-in voices, 63 languages. Bills **per generated hour** (~$0.70/hr, roughly 4–8× cheaper at normal speaking rates, and insensitive to how verbose a translation runs). Knob: speed; "Tighten pauses" (`reduce_silence`, ~8–15% shorter audio, which also trims drift) is on by default (`CT_SX_TIGHTEN=0` turns it off). Unlike ElevenLabs it does not infer language from the text, so the app sends the active target language with each request. Also supports **soundcheck voice capture**, though its clones keep the speaker's accent — see below. |

## Architecture

The **browser** captures audio and runs in-browser voice-activity detection
(self-hosted Silero, fully offline) or streams continuously to Soniox. The
**server** is a thin Flask broker that forwards each segment to Soniox, the
text to Claude, and the translation to whichever TTS backend is selected — it
loads no models and runs on just Flask + requests + numpy + soundfile.

- `app/server.py` — Flask. `POST /stt` (audio → source text, `?source=` is an
  accuracy hint), `POST /translate` (text → translation into `target_lang`,
  optional TTS), a Soniox streaming socket at `/stt-stream`, the projector
  page and its feed (`/projector`, `/caption-feed`, `POST /caption`), the
  optional relays (`POST /publish` for the website, `POST /project` for
  ProPresenter), `/cut-models` (classifier weights the Pace parser uses;
  `POST /sat` only if `wtpsplit` is installed), `POST /route` (the Decision
  Router's check), and `/caps` · `/status` · `/usage` (with the cost estimate) ·
  `/pipe-status` (projector pages open, the web relay) for the UI. Loads `.env` for the keys.
- `app/index.html` — live-caption UI. Across the top, a **light** for each link
  in the pipe — Mic, Soniox, the Decision Router, the translator (its hollow
  second light is the prompt cache), the voice, the projector, and the web relay
  while it is on — then the estimated spend, Download, Diagnostics, Help and
  light/dark. Under it a **ribbon** of what a service needs (Start/Stop, the
  languages, Mic, Speak, Project, Clear wall, and **Settings** last), a strip
  with the input level and the current Pace, Voice and Wall, and the
  Original/Translation transcript. Everything else is in the Settings drawer.
  Settings persist in the browser and on the server (`operator-settings.json`
  beside the recordings), so a new browser opens on them, not on defaults.
  The page drives the pipeline, so closing it stops the translation. On the way
  out it saves the recording and sends the session record's last rows by
  beacon; if the browser dies without that, the server sees the audio stream
  drop without a Stop and saves both itself after 30 s with no page back.
  Reopening within 20 min resumes the service: the transcript is refilled and the
  same session record carries on (with a Start fresh option), and the settings,
  the recording and (within 5 min) the translator's context carry on too — see QUICKSTART, *If the page closes mid-service*.
- `Start Church Translate.bat` — one-click Windows launcher: builds a small
  Python env on first run, prompts for the API keys, and opens the browser.

## Design principles

**Translation quality outranks latency.** This is a *captioning* system, not a
chat box — a faithful, whole-sentence translation a beat late beats a fast but
fractured one. Wherever the two trade off, the pipeline waits for the better
result, and the latency cost is made explicit (a knob, a default, a comment)
rather than silently spent:

- The **sentence assembler** (`app/index.html`, `sxCut`) decides where Soniox's
  running transcript is cut into packets for the translator. It prefers a whole
  sentence; failing that, a clause mark the speaker actually paused after; and a
  run-on that Soniox never punctuates is cut anyway at a word count. The **Pace**
  bar is the operator's lever on this trade, and as of v2.11.0 **it is the
  parser**: a small classifier scores every word gap, and each Pace stop is the
  word range it cuts within — **Pronto** (~3–7 words a piece), **Moderato**
  (7–10, the default), **Lento** (10–14), plus a far-left **Soniox** stop that
  cuts on Soniox's punctuation alone for a speaker it punctuates well. Each stop
  also sets Soniox's own endpoint silence. (The old experimental Cut rule menu
  is gone — the classifier cut is now the only rule; without its weights the
  assembler falls back to the punctuation rule. The removed candidates remain
  replayable in the lab harness.) What each number measures, and how it was set:
  `validation/PARAMETERS.md`. The translator is told packets arrive mid-thought
  and stitches them (a caption that starts lower-case also drops the previous
  caption's full stop).
- Captions always keep the **full** translation; only the optional spoken-audio
  channel ever speeds up or skips ahead, and only when it falls behind the speaker.

**Stages only talk forwards.** STT hands sentences to the translator; the
translator hands text to the caption, the projector and TTS; TTS hands audio to
the player. No stage reads a later stage's state to decide what to do — the
translator never knows how far behind the voice is, and playback lag is handled
entirely on the audio side. Every bug that snowballed came from a backward edge
(a lag signal steering the model, a queue growing while a call was out), so when
a feature needs one, find another way.

When adding a feature, prefer the option that yields the better caption.

**Wet ink (experimental).** The lab behind the two-day Stream-mode campaign
(`validation/results/2026-09-16-stream-lab/`, `2026-09-17-stream-dispatch/`)
found the shipped path's latency is mostly the recognizer finalizing words ~5 s
after first hearing them, while its tentative words are 97 % already right at
half a second. Stream mode — the model segmenting the raw tail into phrase-sized
captions — was built, measured and removed: the seams and the churn were not
worth the machinery. What stayed is **wet ink**, now always on with Soniox streaming: the
assembler cuts from the tentative words too and settles the finals against what
it sent (`validation/results/2026-09-18-wet-ink/`), so the same whole sentences
leave ~4 s sooner. Measured on a 40-minute sermon at
Pronto (`validation/results/2026-09-24-soniox-tradeoff/`): a word reaches the
translator a median 1.6 s after it is spoken (p90 3.4 s), and the caption lands
~0.6 s later with Haiku. (The **Live draft** — Soniox's own translation shown
grey on the wall, then replaced by the model's — was removed on 2026-10-01:
the wall rewording itself under the reader was too messy on a projector, and
with wet ink it bought no speed.)

**Soniox's own translation (no model).** The top entry of the Model picker,
**none, fast**, skips the LLM and captions Soniox's live translation directly.
A slower form, **none**, left the menu on 2026-09-24 (a saved pick of it opens as
**none, fast**); it stays in the table as the measurement it was:

| Model entry | What the wall shows | Spoken → shown (median / p90) |
|---|---|---|
| **none, fast** | Soniox's *final* translation, one segment (~6 words) at a time, the moment it lands; nothing on the wall ever changes | 1.3 / 2.3 s |
| **none** *(removed from the menu; `replay-native` only)* | Soniox's translation a sentence at a time, its tentative words shown grey until they firm up | 4.4 / 7.9 s |
| *(for comparison)* Pronto + Haiku | the model's caption | ~2.2 / ~4.2 s |

The trade is quality: on a spoken battery of Romanian figurative speech Soniox
got 2 of 21 idioms right against Haiku's 16 ("a da ortul popii" came out as
"kill the priest"); on plain preaching it is close to Haiku. With **Speak** on, **none, fast**
captions are spoken too (through `/speak-stream`, the same voice and timeline). Soniox's `context` (fixed translations for listed phrases)
only fixes idioms given verbatim, so the app does not send it. Details:
`validation/results/2026-09-24-soniox-tradeoff/`.

## Catching mis-hears — the Decision Router and extra ears (optional)

Two accuracy features sit around the translator, both off the hot path, both
aimed at the same failure: Soniox mishearing a word in a way that changes the
meaning. Neither slows the normal pipeline — it never waits on them.

**The Decision Router.** With it on (Settings › Translate, under the **Model**
menu), one translator (whichever the **Model** menu names)
does the captioning, and a fast check reads each finished segment and only
*sends on* the doubtful ones — a likely mis-hearing, a shaky translation, or a
cuss word — for a closer look, keeping Soniox's own reading for the rest. Each
caption carries a small **router chip** and a who-translated label so the
operator can see what happened, and the check's own latency shows in its light's
tooltip and the Diagnostics waterfall. Soniox's own segments set the captions
while it is on, so Pace is not used. The offensive-words check is narrowed to actual
swearing — Bible and preaching language passes (on a whole sermon it sent on
~16% of segments, down from ~24% when it flagged anything sensitive). The whole
check runs under one deadline (`CT_ROUTER_TIMEOUT_MS`, default 1200 ms); past
it, Soniox's text stands. `CT_ROUTER_MODEL` can pin the checker to a model other
than the caption's.

**Extra ears.** Alongside Soniox, up to three more realtime recognizers can hear
the same audio — ElevenLabs, OpenAI, and Gemini Live — not to caption, but as
*witnesses*. When the Decision Router sends a segment on as a likely mis-hearing, each
ear's reading of those same words goes to the translator as a clue to what was
actually said. Such a segment waits briefly for the ears to catch up — until
each has the segment's last word *and a word past it*, at most `CT_EAR_WAIT_MS`
after the seal (default 700 ms; set in `.env`). Only the Decision Router's sent-on mis-hearings wait; everything else takes the
ears as they stand. On a whole 82-minute sermon with 27 meaning-changing
Soniox mis-hears, the translator got 20 right with no ears and 26 with all three
at +0.7 s, with no harm on the control segments; Gemini Live is the single
strongest ear and the least noisy. Each ear's reading appears under its row on
the page once final (the ear has reached the row's last word) — nothing streams
mid-word. `CT_EARS=0` hides and disables the lot.

Turning these on, and the model/confidence-hint choices that go with them, are
covered step by step — with the measured trade-offs — in
**[`docs/accuracy-tuning.md`](docs/accuracy-tuning.md)**.

## What it costs

The **≈ $** at the end of the lights is this server run's estimated spend across
every API, at the list prices in `config/prices.json` (hover for the
breakdown). Token counts are the providers' own; audio and characters are what
was sent. Rough hourly costs: Soniox speech-to-text $0.12, Soniox voice $0.70,
a cached `gpt-6-sol` (fast) caption ~$0.002, ears $0.39–1.02 each. Prompt
caching is on for every translator (the translator's hollow second light shows
it per caption).

## Run it

You need Python 3.9+ and a **Soniox** key, which alone covers speech-to-text,
translation (Soniox's own, the **none, fast** model) and spoken output. Add an
LLM key (Anthropic, OpenAI, Google or xAI) for better translation, and an
OpenAI key for the Decision Router. Start here if you are new: [`QUICKSTART.pdf`](QUICKSTART.pdf).

**Windows (one click).** Double-click **`Start Church Translate.bat`**. The first
run builds a tiny native-Windows Python env, asks for your API keys (saved to
`.env`), and opens the browser. Later launches start instantly.

**Linux (download).** Get `church-translate-linux-x86_64.tar.gz` (most PCs) or
`church-translate-linux-arm64.tar.gz` (Raspberry Pi 4/5 and other 64-bit ARM)
from the [latest release](https://github.com/seekeroftruth27/aron-translate/releases/latest),
then:

```bash
tar -xzf church-translate-linux-x86_64.tar.gz
./church-translate
```

The first run asks for your Soniox key in the terminal and saves it in a `.env`
beside the program; it then opens the browser. It needs glibc 2.35 or newer
(Ubuntu 22.04+, Debian 12+, Raspberry Pi OS Bookworm+, Fedora 36+). To check a
download, `sha256sum -c SHA256SUMS --ignore-missing` against the release's
`SHA256SUMS`.

**Linux / macOS (from source).** You need Python 3.9+ with `venv` (on
Debian/Ubuntu/Raspberry Pi OS: `sudo apt install python3-venv`). Copy
`.env.example` to `.env` and fill in your keys (leave a line blank for its
default), then:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python app/server.py
```

The same three lines work on Windows from source, with
`.venv\Scripts\activate` in place of `. .venv/bin/activate`. Set `CT_PORT` in
`.env` to serve on a port other than 5000.

Then open **http://localhost:5000**, pick a target language, click **Start**,
and talk. First-time setup and operator tuning: **[QUICKSTART.md](QUICKSTART.md)**.

## Speaking in the speaker's own voice (optional)

**Capture voice** records a 30-second reference take and speaks the translation
in that voice — including in languages the speaker doesn't know. A voice
captured in English will speak Romanian, Spanish or Korean at the same latency
as a built-in voice. Available on either speech backend, but they do not offer
the same thing:

| | Soniox | ElevenLabs |
|---|---|---|
| Clone | The speaker exactly as captured | The speaker exactly as captured |
| Accent | Carried into every language, always | Optional **Neutral accent** pass remixes it towards native pronunciation in the target language |
| Ready in | ~3 seconds | ~4 seconds, or ~40 seconds with the accent pass |
| Ceiling | 20 captured voices per **organisation** | By ElevenLabs plan |

**The accent problem, and the one lever for it.** A clone reproduces the
speaker's accent along with their timbre, so a Romanian speaker cloned and then
asked to speak English sounds like a Romanian speaking English — faithful to the
person, but not what a service translating *into* English wants. Soniox has no
lever for this; its clone is whatever it captured. ElevenLabs does: tick
**Neutral accent** and the capture is cloned, then remixed towards native
pronunciation in the current target language, and the remix is kept in place of
the raw clone. **Strength** (light / medium / strong) is how far the remix may
travel from the recording — both ends are failure modes, since light leaves the
accent in and strong can stop sounding like the speaker at all. It is not
deterministic: if a take lands wrong, recapture and try the next step up. If the
accent pass fails, the plain clone is kept rather than the take being thrown
away, and the app says so.

The remix is conditioned on a short demo script as well as the description, and
that script is **written in the target language first**, through the translator
the app already runs. ElevenLabs will generate one for you, but measured against
the live API it writes it in English even when asked for a native Spanish
voice — which teaches the remix to speak English with a Spanish accent, the
exact thing being removed. If the translator is unreachable the app falls back
to ElevenLabs' own script and logs that it did; the accent pass gets weaker,
the capture still works.

Do it at **soundcheck**, not mid-service:

- A capture reproduces pace, accent and breathing, not just tone. It wants one
  clean take of one speaker — quiet room, no music, no congregation. Read a
  paragraph at normal preaching pace; 20–30 seconds is plenty (2 minutes is
  Soniox's hard ceiling). Use **Test** to hear a line in the target language
  before committing, and recapture if it isn't right.
- **Ask the speaker first.** This is a different thing from captioning them: it
  makes their voice say words they did not say, in a language they may not
  speak, and a mistranslation will sound like they personally said it. Their
  call, not the operator's — and worth deciding whether the congregation is
  told.
- Both backends cap how many captures you may hold — Soniox at **20 per
  organisation**, ElevenLabs by plan — so delete captures you no longer need
  (the **Delete** button next to the voice picker). Takes that are too short or
  near-silent are refused before upload so they don't consume a slot.

## The session record (for looking back after a service)

From the first **Start Listening**, the page keeps a record of the session and
the local server writes it to `recordings/session-<date>_<time>/` every few
seconds, so a crash or a closed window loses at most the last few seconds.
The folder holds two files: `session.json` (the data) and `session.html`, a
page that opens from disk and shows:

- **Setup**: every setting as it stood when listening started (languages, Pace,
  cut rule, Decision Router, extra ears, Wait for ears, translator, wall, voice, …),
  and the model ids in use (Soniox STT, each extra ear's model, the Decision
  Router's model, translator default and hearing fallback, pins, median latency, usage,
  estimated spend).
- **Changes**: every setting changed after that, with the time and the old and
  new value; listening started/stopped, service mode switches, and Clear.
- **Transcript**: every row (Clear empties the screen, not the record), with
  the time it was sealed, the source language, what each extra ear heard
  (tagged *sent* where the translator was given that reading), the caption,
  and its tags: who translated it (Soniox or the model id), why the Decision Router
  sent it on (translation / misheard / sensitive) with its scores and check time,
  Soniox's own English when a model replaced it, the voice tag the translator
  added, a hand edit (time and the text before), errors, and the timings:
  spoken → shown, sealed → shown, translator time. Filters show only the
  Decision Router's rows, the model's rows, rows with an ear's reading, edits or
  errors, and the settings changes appear in line at their time.
- **Event log**: the Diagnostics log for the whole session, with the server's
  own warnings in among it: a reply that copied the Decision Router's clue
  (Soniox's English, sent with a doubted segment) had the clue cut out before
  anything showed or spoke it.

**Download › Session record** (or `H`) downloads the same `session.html`. A speech recording's
save and its `.zip` carry both files too. `CT_SESSION_SAVE=0` stops the
writing to disk; the download still works.

## Keeping the service (audio + timed transcript)

Every clip the app speaks is kept as it is generated, so the whole service can
be saved off afterwards. **Download › Spoken audio** (or `A`) downloads one `.zip`:

| file | what it is |
| --- | --- |
| `audio.mp3` | the spoken translation, whole service, one file |
| `captions.srt` / `.vtt` | the same words as subtitles, timed to that mp3 |
| `segments.json` | every clip: its text, the source line it came from, where it sits in the mp3 (seconds *and* byte offset), and the wall-clock time it was spoken |
| `transcript.html` | source beside translation, with both clocks |
| `session.json` / `.html` | the session record (above): settings and their changes, models, every row with its tags |

Two timelines, because they answer different questions: `t` is where a line
sits **in the file**, `at`/`wall` is **when it was said**. The gap between them
grows all service — the recording holds only the spoken parts, so the silences
exist in wall-clock and not in the audio. Timings come from the mp3's own frame
headers, not from a nominal bitrate, so a cue lands on the words in any player.

The clips pile up in the **local server**, not the browser tab, so a reload
never costs you the recording, and none of it leaves the machine. In
**Settings › Voice**: **Record speech** turns the buffer on and off,
**Save now** writes it to the `recordings` folder mid-service, **Discard**
starts a fresh one between services, and **Auto-save on exit** writes it out
when the app closes so nobody has to remember. Defaults, folder and size cap
are `CT_RECORD*` in `.env` (see `.env.example`).

## On the web — captions, live voice, and an archive (optional)

The service doesn't have to stay in the room. Turn on **Web captions** (Settings › Wall) and each
finalized translation is streamed to a page on **your own web host**, so people
read it on their phones from anywhere — a QR code in the bulletin is enough. It's a
server-to-server relay: the operator app POSTs each caption (and any hand-edit) to
your host, phones poll it about once a second, and **Clear wall** (or clearing
the transcript) wipes their screens too. While it is on, a **Web** light shows
whether the last relay reached your site. The publish token stays on the server; the operator app never goes on the
public internet, and your host only ever sees finalized caption text.

- **Live voice — "Listen."** With TTS on, the same stream carries the *spoken*
  translation: each finished sentence's mp3 is sent alongside its text, and a
  **Listen** button appears on the phone page (it shows only once a service has
  audio; the tap is also what lets a phone play sound). Tapping it hears the
  service live, ~a sentence behind the captions, and joins live rather than
  replaying what's already scrolled past.
- **Archive.** Clearing at the end of a service rolls it — captions and,
  if any, its clips — into a dated folder (`YYYY-MM-DD_HHMM`) so past services can
  be read back and replayed. Browsing is behind an unguessable key
  (`archive.php?k=…`), the stored files are never directly reachable, and the
  newest 20 services are kept automatically.

Nothing here is on by default and nothing runs locally for it: with the Web toggle
off — or with no relay configured — the app behaves exactly as before.

**Setting it up (once).** Put the `web/captions/` folder on your PHP host, create
`config.php` from `config.sample.php` (set `PUBLISH_TOKEN`, and `ARCHIVE_KEY` for
the archive), and point the app at it in `.env`:

```
CT_PUBLISH_URL=https://yourchurch.org/captions/publish.php
CT_PUBLISH_TOKEN=<the same token as config.php's PUBLISH_TOKEN>
```

Full walkthrough — including the audio and archive options — is in
[`web/captions/README.md`](web/captions/README.md).

**Pushing updates to the host.** You can upload the folder however you like (SFTP,
cPanel, `scp`), or use the bundled script, which is configurable per host and only
ever sends the code — never your `config.php` or the live data (`captions.json`,
`clips/`, `archive/`). Point it at your host in `.env`:

```
CT_DEPLOY_SSH=user@yourhost.com          # SSH key auth (no password)
CT_DEPLOY_PATH=public_html/captions      # where the relay lives on the host
CT_DEPLOY_PORT=22                        # optional
```

then:

```bash
python scripts/deploy_captions.py --dry-run   # preview what will be sent
python scripts/deploy_captions.py             # upload
```

It needs the `ssh`/`scp` clients (built into Windows 10+, macOS and Linux) and a
working SSH key to the host — test with `ssh -o BatchMode=yes user@yourhost.com echo ok`.

## Projecting to ProPresenter (optional)

If ProPresenter drives your auditorium screen, point a ProPresenter **web
object** at Áron's own caption strip — the recommended way: ProPresenter
renders the page itself, so you get the full **typewriter reveal, the rolling
window, and dimmed older sentences**, none of which the alternative below (a
plain-text ProPresenter Message) can show, since a Message can only ever hold
a flat string.

The app serves a standalone page at **`http://<operator-ip>:5000/projector`**
— point a ProPresenter **web object** at it (or just open it in a browser on any
machine on the LAN, e.g. the ProPresenter PC). It shows **one flowing paragraph**
that fills each line before starting the next, bottom-anchored so the reading line
stays put, and a word-level diff so a correction moves only the words that
differ. It's fed live from
the server (a `/caption-feed` poll, so it works cross-machine with no ProPresenter
API and no BroadcastChannel), and the in-app **Project** button opens the same page.

Size and place it for a lower-third with these knobs (all optional, `0` = off):

```
CT_PROJECTOR_MAX_H_PX=200    # clip the strip to a fixed band height (px); newest at the bottom
CT_PROJECTOR_BOTTOM_PX=20    # lift the band up off the bottom edge (px)
CT_PROJECTOR_LEFT_PX=0       # left margin (px); 0 = default 6% of the window width
CT_PROJECTOR_RIGHT_PX=0      # right margin (px); 0 = default 6% of the window width
CT_PROJECTOR_FONT_PX=68      # fixed caption text size (px); 0 = scale with the window
CT_PROJECTOR_BG=transparent  # a CSS colour (e.g. black) or "transparent"
CT_PROJECTOR_W=0             # fixed width  (px); 0 = fill the window
CT_PROJECTOR_H=0             # fixed height (px); 0 = fill the window
CT_PROJECTOR_LINES=3         # rolling window: lines kept on the wall before the top scrolls off (default for the lines menu; 2 lines show at 115% size)
CT_PROJECTOR_DWELL_MS=1000   # hold each line this long before it scrolls up (0 = scroll freely)
CT_PROJECTOR_HIGHLIGHT=12    # recent sentences kept bright (12 = all/one-colour); lower to dim older ones
CT_PROJECTOR_LATEST_ONLY=0   # 1 = keep only the current message (retires earlier sentences)
CT_PROJECTOR_STALE_S=60      # a caption older than this is never served to a projector (0 = keep until Clear)
```

By default the strip is a **rolling window** of the last `CT_PROJECTOR_LINES` lines:
previous sentences stay above the reading line until they scroll off the top, and each line is held a beat (`CT_PROJECTOR_DWELL_MS`) before it scrolls up so a
big paragraph doesn't fly past too fast to read. Set `CT_PROJECTOR_DWELL_MS=0` to
scroll freely. The **Wall** settings (Settings › Wall) change the
reveal live and override these defaults: words per second the typewriter shows (10,
20, 40, or the whole sentence at once), the per-line hold (1 s, ½ s, none), and how many
recent sentences stay **bright** (all — the calm one-colour default — or just the last
1/2/3, dimming older ones for emphasis). A
projector opened late starts from what was said in the last `CT_PROJECTOR_STALE_S`
seconds, never the sound check from before the service; **Clear wall** in the ribbon
(key W) wipes the wall on demand, transcript kept. The
typewriter is cosmetic -- the text exists the moment the translation lands, and
every word not yet shown is latency on top of it: on the wet-ink replay
(`validation/scripts/projector_motion.js`) the last word of a sentence reaches the
wall a median 2.0 s after its final at the defaults, 1.0 s with the hold off, 0.5 s at
20 words/s, and at once with "sentence at once". The choice persists on the operator's
machine and is kept in the server's caption store, so a projector opened later, or on
another computer, starts with it. `CT_PROJECTOR_LATEST_ONLY=1` keeps only the message being read, but
retiring a short previous sentence mid-line makes the next one jump left, so it's off
by default.

> **Transparency:** `CT_PROJECTOR_BG=transparent` only shows through where a
> compositor puts the page over other content (ProPresenter's web object). A plain
> browser paints an opaque **white** surface behind a transparent page, so testing
> the URL in a normal browser will look white — that is expected, not a fault; use
> `black` to preview the strip in a browser.

### Or: drive a ProPresenter Message directly (no web object)

If your ProPresenter can't host a web object (an older version, or a locked-down
show), Áron can instead push the live translation into a ProPresenter **Message**
over its local network API, driven **server-to-server** from Áron's Flask broker
(ProPresenter's API sends no CORS headers, so a browser can't call it directly) —
the same shape as the web relay. A Message can only ever hold plain text, so this
path can't show the typewriter reveal or the dimmed-sentence highlighting the web
object above gets for free; it can still do a **rolling word-by-word window**
(below), just without those finer effects.

**Set up (once):**

1. **In ProPresenter** → *Preferences → Network*: enable the network, and note the
   **host and port** it shows (e.g. `192.168.1.50:1025`). Áron must be on the same
   LAN.
2. **In ProPresenter** → *Messages*: create a Message with a single **text token**
   (name it, e.g. `Caption`), and style/position it as your lower-third caption.
3. **Point Áron at it** in `.env`, then restart:
   ```
   CT_PROPRESENTER_URL=http://192.168.1.50:1025
   CT_PROPRESENTER_MESSAGE=<the Message's name, uuid, or index>
   CT_PROPRESENTER_TOKEN=Caption
   ```
   A **ProPresenter** switch then appears in Settings › Wall (only when configured).
   Tick it to project; the choice is remembered, and unticking it hides the message.

**The Message as the translation box, following the Mic.** If the box on screen
is the Message itself — for example a web object in the Message's theme carries the
captions — the Message needs no text. Set:

```
CT_PROPRESENTER_MESSAGE=LIVE Translation
CT_PROPRESENTER_FOLLOW_MIC=1
```

Áron then shows the Message when the translation turns on (**Mic on** while
listening) and clears it when it turns off (**Muted**, by you or by Gate music, or
**Stop**), so the box is up while someone speaks and gone for songs. After
`CT_PROPRESENTER_QUIET_S` seconds (default 10) without speech while the Mic is on it
clears too, and the next speech brings it back; set it to `0` to keep the box up
through any silence. Once per change, never per caption.

To tie the box to a ProPresenter **Look** as well, name it:

```
CT_PROPRESENTER_LOOK=Translation
```

The box then comes on only when the translation is on **and** that Look is live,
and goes off when the ProPresenter operator switches to another Look (switching
back brings it back within about two seconds). Use the Look's name as shown in
ProPresenter's Looks, or its uuid; list several with commas. Case doesn't matter.
A Look you've changed on the fly without saving may come up under another name,
so save the Look you use. It needs ProPresenter 7.9 or later (`/v1/look/current`);
on an older one the box stays off and the log says why. It works with the **ProPresenter** switch in Settings
left off: that switch only sends caption text, which this mode never does. The Message is triggered with no token values, so give it no text token.

Finalized captions only go to the screen — the same policy as the website. It's
independent of the web relay and the built-in Project window: run any combination.
With the model set to **none, fast** each caption is a Soniox segment of about six
words arriving every ~2 s, so the default whole-line mode would flash short
fragments; use the rolling `word` mode below.

**Continuous rolling captions (optional):** by default each finalized line
*replaces* the whole message at once, so a short sentence flashes up and the box
blanks between captions. Set `CT_PROPRESENTER_TYPE=word` and Áron instead streams
a **continuous word-by-word rolling window that stays full**: each finalized
caption's words flow in a word every `CT_PROPRESENTER_TYPE_MS`, the lines fill and
*stay* full, and when a line rolls off the top it first **scrolls up one row and
lingers** before disappearing as the next line fills at the bottom. (ProPresenter has no typewriter of its own, so Áron re-sends
the message token as the text grows; a single reveal thread paces it and speeds up
when it falls behind the speaker, so the caption never lags the room.)

```
CT_PROPRESENTER_TYPE=word     # off (default) | word
CT_PROPRESENTER_TYPE_MS=120   # milliseconds between reveals
CT_PROPRESENTER_TYPE_WORDS=1  # words revealed per step (raise for a chunkier reveal)
CT_PROPRESENTER_LINES=2       # resting caption lines (the rolling-window height)
CT_PROPRESENTER_COLS=42       # approx. characters per line, for wrapping/rolling
CT_PROPRESENTER_SCROLL_MS=220 # how long the departing line lingers a row up (0 = off/instant)
```

For `word` mode, set the Message's text box up in ProPresenter to match:
**vertically bottom-aligned**, **"Shrink text to fit" off**, theme **transitions
set to none** (otherwise each rapid re-trigger re-runs the build animation and
flickers). **Box height:** with the scroll on (`CT_PROPRESENTER_SCROLL_MS` > 0)
make the box **`CT_PROPRESENTER_LINES` + 1 rows tall** — the extra top row is where
the departing line rises before it goes; with the scroll off (`=0`, instant drop)
`CT_PROPRESENTER_LINES` rows is enough. `CT_PROPRESENTER_COLS` is an estimate of
how many characters fit on one line; nudge it until Áron's line breaks match what
ProPresenter actually wraps on your screen. These knobs apply only in `word` mode;
`off` never wraps or truncates. Startup logs `ProPresenter ON -> … [typing
1w/120ms, 2x42]` when typing mode is active.

> **Note:** built to the current official ProPresenter API — verified against the
> published OpenAPI: `POST /v1/message/{id}/trigger` with a text token
> `{"name": <token>, "text": {"text": <line>}}`, and `GET /v1/message/{id}/clear`.
> The `{id}` may be the message's name, uuid or index, and `CT_PROPRESENTER_TOKEN`
> must match the text token's name inside the message. The server logs
> `ProPresenter ON -> …` at startup when configured; if projection doesn't show,
> the app console logs `[project]` with the HTTP status.

## Config (editable, no code)

- `config/translation_prompt.txt` — the translator's system prompt. Reasons
  from Claude's own knowledge first (standard biblical names, common idioms,
  homophone/word-boundary recovery); the glossary below is only for topping
  that up with this congregation's own vocabulary.
- `config/glossary.tsv` — **supplementary** names and idioms, in two
  `## NAMES` / `## IDIOMS` sections in one file. Claude already knows
  standard biblical names and common Romanian idioms on its own; add entries
  here only for this congregation's own rare/local names or regional
  phrasing that general knowledge wouldn't already cover. The idioms section
  is empty since 2026-10-19: the current translators know the mined idioms
  unaided, and the Router sends a word-for-word idiom to the translator.

(Hallucinated modern profanity is handled by the translation prompt itself —
it treats out-of-place expletives as STT mis-hears — while legitimate graphic
biblical vocabulary is translated faithfully. The prompt also picks, between
equally faithful wordings, the one that can't be heard as an unintended double
meaning: Romanian *"am lucrat la sapă"* is literally *"I worked with a hoe"*,
where *"I worked the fields"* says the same thing and can't raise a laugh from
the back row.)

## Measuring it

Every number quoted in `CHANGELOG.md` came from a script in `validation/`:
quality corpora (figurative, homophone, bidirectional register/names/Scripture),
latency and cost sweeps, the segmentation labs that replay a captured Soniox
stream through the assembler's own cut rule, and provider probes. Results worth
keeping are committed beside the scripts. Runtime code stays in `scripts/`; the
batteries import it from there. `validation/` lives in the development repo
only: its test material is real recorded sermons and services, which are not
ours to publish.

## Cloud design notes

How the cloud stages fit together and why, with the credit, latency, and
deployment considerations behind them.

**Credits — how we avoid draining them.** STT bills per **minute of audio**,
TTS per **character**. The VAD is the credit firewall: only detected *speech*
segments (or Soniox's own endpointing) get billed, so silence and pauses cost
nothing. **Idle stop** auto-stops after N minutes of no speech as a safety net
for songs/announcements. **TTS only when Speak is on**, cached per line for
Replay so replays don't re-synthesize. Retries are capped on the paid stages
so a flaky network can't loop on billed calls. The **Diagnostics** panel's usage
meter shows ElevenLabs remaining credits (when that backend is in use) plus a
this-run tally (STT minutes,
TTS characters, translation calls) as a visible burn-rate guardrail.

**Latency: the round-trip question.** The app orchestrates each stage
(browser → STT → browser → MT → browser → TTS → browser), so the box↔cloud
round-trip is paid up to 3× per utterance. But processing dominates, not
network: Soniox shows a word ~0.5 s after it is spoken, the assembler then waits
for a cut (~1.1 s median at Pronto), translation takes ~0.6 s with Haiku (0.5–0.9 s
across the fast models; Sonnet 5 ~1.8 s), TTS ~0.5–1 s; each round-trip itself is
tens–low-hundreds of ms. The wait for a good cut is the largest share. Net extra from
the hub pattern is ~0.3–0.6 s — real but small next to the seconds of
inference already in the path. For live *captions* (where quality outranks
latency) this is an acceptable trade.

**Deployment.** Nothing heavy runs locally, so the handoff is light — the
server's only jobs are to hold the API keys and proxy audio/text to the
providers. Two shapes: **(A) Hosted web app** — deploy the static frontend +
thin key-holding proxy to a cloud host (Render / Fly / Railway / small VPS);
zero local install, HTTPS comes free (letting the page open from other
devices on the network), but needs one maintainer for uptime/keys/billing and
simple auth so a leaked URL can't run up the bill. **(B) Local thin proxy** —
ship as a Windows `.exe` or the one-command native-Windows app
(`Start Church Translate.bat`); no external host to maintain, but keys sit on
the AV machine and captions only work on that one machine (`localhost`).

**Caveats before the church relies on it.** Cost is per-character (TTS) and
per-minute (STT) — continuous Sunday services add up, so price it against
expected hours before committing. Fully cloud means a network blip kills
captions mid-sermon; there is no offline fallback. It sends sermon audio to
third parties (Soniox / ElevenLabs / Anthropic) — likely fine for a church,
but a conscious decision.

## Roadmap

- [x] Cut from Soniox's tentative words instead of waiting for finals (wet ink).
- [x] A no-model mode that shows only final text, as fast as Soniox segments it
      (**none, fast**).
- [x] A cut rule that beats fixed word counts without losing clean cuts in a
      real service — the Pace bar is now the classifier parser, each stop a word
      range (`validation/results/2026-10-10-pace-words/`, research in
      `validation/HANDOFF-cut-rule.md`).
- [ ] Soundboard/SDI line-in instead of laptop mic (device picker already
      supports it).
- [ ] HTTPS so the captions page can be opened from other devices on the network.

## Licence

Áron is [MIT licensed](LICENSE) — use it, adapt it, run it in your own
congregation. If it helps your church, that's the point.

The voice-activity-detection files vendored in `app/vad/` are third-party
(ONNX Runtime Web, `@ricky0123/vad-web`, Silero VAD — all MIT) and keep their
own notices; see [app/vad/NOTICE](app/vad/NOTICE). The cloud services the app
calls are governed by their own terms, and you bring your own API keys, so
usage of those is billed to you.

---

> **Local-model history.** Earlier versions also ran local STT/TTS (SpeD /
> Parakeet via NeMo, Kokoro / Piper / MMS, VoxLingua language-ID) plus a batch of
> offline analysis tooling, and later a two-engine cloud STT setup (Soniox +
> ElevenLabs Scribe) before Scribe was dropped in favor of Soniox alone. That
> experimental sandbox is preserved on a branch of the development repo; this
> tree keeps only the clean cloud pipeline.
