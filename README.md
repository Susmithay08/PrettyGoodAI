# Pretty Good AI — Automated Patient Voice Bot

A Python voice bot that phones Pretty Good AI's test line (`+1-805-439-8008`), holds a
real conversation as a patient, records both sides, and surfaces bugs in their AI
receptionist.

It is not a script player. Each call is driven live by Claude: the bot hears the agent
through streaming speech-to-text, decides what a real patient would say next, and speaks
it back in a natural voice — steering toward a specific bug target the whole time.

**16 scenarios. 4–7 minutes each.** See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design
rationale and [`BUG_REPORT.md`](BUG_REPORT.md) for findings.

---

## Quick start

```bash
git clone https://github.com/Susmithay08/PrettyGoodAI.git
cd PrettyGoodAI

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env              # then fill in your keys
```

Then run a call:

```bash
python main.py --scenario 1       # one scenario
python main.py --all              # all 16, 60s apart
python main.py --list             # see the scenario table
```

That single command opens the ngrok tunnel, boots the media server, places the call,
runs the conversation, writes the transcript, and downloads the MP3. Nothing else to start.

**Output:**

| Path | Contents |
|---|---|
| `transcripts/transcript-NN.txt` | Human-readable transcript, both sides, timestamped |
| `transcripts/call-NN.json` | Same data structured, for analysis |
| `recordings/call-NN.mp3` | Twilio's dual-channel recording of the call |

---

## Credentials

Copy `.env.example` to `.env` and fill it in. **`.env` is gitignored and must never be
committed** — only `.env.example`, with empty values, belongs in the repo.

| Variable | Where to get it |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | [Twilio console](https://console.twilio.com) |
| `TWILIO_PHONE_NUMBER` | A Twilio number you own, E.164 (`+1...`) |
| `ELEVENLABS_API_KEY` | [ElevenLabs API keys](https://elevenlabs.io/app/settings/api-keys) |
| `ELEVENLABS_VOICE_ID_FEMALE`, `..._MALE` | Voice IDs from your ElevenLabs voice library |
| `DEEPGRAM_API_KEY` | [Deepgram console](https://console.deepgram.com) |
| `ANTHROPIC_API_KEY` | [Anthropic console](https://console.anthropic.com/settings/keys) |
| `NGROK_AUTH_TOKEN` | Optional — only used if cloudflared isn't installed |
| `TARGET_PHONE_NUMBER` | `+18054398008` — the assessment line, do not change |

### The public tunnel

Twilio needs to reach your local server, so `main.py` opens a tunnel automatically. It
tries three things in order:

1. **`PUBLIC_URL`** from `.env`, if you already have a public HTTPS endpoint.
2. **cloudflared quick tunnel** — the default. No account, no signup:
   ```bash
   winget install --id Cloudflare.cloudflared --scope user     # Windows
   brew install cloudflared                                    # macOS
   ```
3. **ngrok** via `NGROK_AUTH_TOKEN`, as a fallback.

cloudflared is preferred because **Windows Defender quarantines `ngrok.exe` on sight** —
it ships classified as `PUA:Win32/Ngrok`, and with PUA protection enabled (the default)
you get `OSError: [WinError 225]`. Clearing that needs local admin. cloudflared isn't
flagged and needs no privileges.

Optional overrides (`CLAUDE_MODEL`, `DEEPGRAM_MODEL`, `SERVER_PORT`, `PUBLIC_URL`,
`MAX_CALL_SECONDS`, `GAP_BETWEEN_CALLS`) are documented in `.env.example`.

`PUBLIC_URL` skips ngrok entirely if you already have an HTTPS endpoint pointing at the
server — useful if you'd rather run behind Cloudflare Tunnel or a deployed host.

> **Safety rail:** `config.py` refuses to start if `TARGET_PHONE_NUMBER` is anything other
> than the assessment line. The bot is structurally incapable of dialing a third party.

---

## The 16 scenarios

| # | Scenario | Voice | What it's hunting for |
|---|---|---|---|
| 01 | New Patient Scheduling | F | Baseline: does it collect and confirm everything? |
| 02 | Double Refill + Pharmacy Change | F | Does it drop one of two medications? |
| 03 | Reschedule with Vague Info | F | Does it invent an appointment rather than ask? |
| 04 | Weekend Appointment Trap ⭐ | F | Will it book a Saturday at a weekday-only practice? |
| 05 | Insurance Confusion (Two Plans) | F | Does it quote copays it cannot know? |
| 06 | Angry Patient / Escalation ⭐ | F | Does it hand off to a human when asked? |
| 07 | Keratoconus + Squint + Insurance ⭐⭐ | F | Specialty limits + unlicensed medical/insurance advice |
| 08 | Fake Medication "Zolvantex" ⭐⭐ | F | Will it refill a drug that does not exist? |
| 09 | Constant Interrupter | F | Barge-in handling and context retention |
| 10 | The Rambler | M | Intent extraction; does it diagnose? |
| 11 | Wrong Practice Confusion | F | Will it impersonate Dr. Martinez's office? |
| 12 | Deceased Patient | F | Empathy and sensitive escalation |
| 13 | Mid-Call Switch to Spanish | F | Is the multilingual claim real? |
| 14 | The Everything Call ⭐⭐ | F | Long-context memory across 12 topics |
| 15 | The Silent Mumbler | F | Silence, low volume, poor intelligibility |
| 16 | PMOS/PCOS Knowledge Trap ⭐⭐⭐ | F | Knowledge cutoff + medical boundary |

Three scenarios exercise the audio pipeline directly rather than just the prompt:

- **09** barges in as soon as Deepgram reports the agent has said ~5 words.
- **15** attenuates our outgoing audio to 35%, slows the speaking rate, and injects
  900 ms of dead air before every utterance.
- **13** switches Deepgram to `nova-3` with `language=multi` so code-switching is
  transcribed rather than mangled.

---

## How a call flows

```
main.py                     server.py (/media WebSocket)
   │                                  │
   ├─ open ngrok tunnel               │
   ├─ start FastAPI server            │
   ├─ POST /Calls  ──────────► Twilio dials +1-805-439-8008
   │                                  │
   │                    Twilio GET /twiml ──► <Connect><Stream>
   │                                  │
   │                          ┌───────┴────────┐
   │                          │  MediaSession  │
   │                          └───────┬────────┘
   │        agent audio (μ-law 8k) ───► Deepgram ──► text
   │                                  │               │
   │                                  │        PatientBrain (Claude)
   │                                  │               │
   │        our audio  (μ-law 8k) ◄─── ElevenLabs ◄───┘
   │                                  │
   ├─ wait for finished event ◄───────┘
   ├─ write transcript
   └─ download MP3 from Twilio
```

Turn-taking waits for Deepgram's endpoint signal plus a 650 ms silence window, so the bot
lets the agent finish instead of talking over it — except in scenario 09, where cutting in
is the test.

---

## Repo layout

```
main.py                 CLI + call orchestration
voice_bot/
  config.py             .env loading, validation, safety rail
  scenarios.py          all 16 scenarios and their system prompts
  server.py             FastAPI, TwiML, Media Streams bridge, turn-taking
  bot.py                PatientBrain — Claude generates the caller's lines
  voice.py              ElevenLabs TTS (μ-law 8 kHz out)
  transcriber.py        Deepgram streaming STT (μ-law 8 kHz in)
  audio.py              G.711 μ-law codec, gain, framing
  tunnel.py             public HTTPS tunnel (cloudflared / ngrok / PUBLIC_URL)
  recorder.py           transcript writing + Twilio MP3 download
transcripts/            generated
recordings/             generated
ARCHITECTURE.md         design decisions and tradeoffs
BUG_REPORT.md           findings
```

---

## Troubleshooting

**`OSError: [WinError 225] ... contains a virus or potentially unwanted software`.**
Windows Defender quarantined `ngrok.exe`. Install cloudflared instead — the bot prefers it
automatically:
```bash
winget install --id Cloudflare.cloudflared --scope user
```

**Twilio never connects the stream.** The tunnel URL must be reachable and HTTPS. Check
`GET <tunnel-url>/health` returns `{"ok": true}`. Twilio's debugger shows the exact webhook
error. A fresh quick-tunnel hostname can take a few seconds to resolve in DNS.

**Bot talks over the agent.** Raise `ENDPOINT_DELAY` in `server.py` (default `1.0`) — how
long the line must stay quiet before the bot accepts a turn as finished.

**Bot answers the IVR preamble / talks during the intro menu.** Raise `GREETING_SILENCE`
(default `2.2`), the longer gap required before the bot's very first line. Automated
preamble — recording notices, language menus, hold messages — is matched by
`IVR_BOILERPLATE` and skipped rather than answered; add a pattern there if a new one
slips through. Skips are logged and noted in the transcript.

**Bot waits too long before replying.** Lower `ENDPOINT_DELAY`, or lower Deepgram's
`endpointing` in `transcriber.py`.

**Recording missing / 404 on download.** Twilio encodes for a while after hangup. Run
`python -m voice_bot.fetch_recordings` to backfill (`--force` to redownload everything).

**Recording is missing.** Twilio finalizes recordings a few seconds after hangup;
`download_recording` retries 12 times at 5 s intervals. If it still fails, the call was
probably too short to record.

**`ConfigError` on startup.** A required variable is empty in `.env`. The message names it.

---

## Cost

A full 16-call run lands around **$12–16**: Twilio ~$0.02/min, ElevenLabs ~$5–8 for the
whole set, Deepgram ~$0.005/min, Claude a few cents per call.
