# Our Decisions & Context — PrettyGoodAI Voice Bot

## What We're Building

A Python voice bot that calls +1-805-439-8008 (Pretty Good AI's test line) and
simulates realistic patient conversations to find bugs in their AI agent.

Pretty Good AI is a voice AI platform built specifically for medical practices
using athenaOne EHR. Their agent handles: appointment scheduling, medication
refills, insurance verification, referrals, billing, and general practice questions.

---

## Tech Stack Decisions

| Purpose | Tool | Why |
|---|---|---|
| Outbound phone calls + recording | Twilio | Industry standard, best documentation, Claude Code knows it best |
| Text-to-speech (patient voice) | ElevenLabs | Most natural voice — evaluators listen first before reading code |
| Speech-to-text (hear the agent) | Deepgram | Real-time streaming STT, better than Whisper for live calls, great medical vocab |
| AI brain (generate responses) | Claude claude-sonnet-4-6 (Anthropic) | Fast enough for conversational latency, smart enough for complex medical scenarios |
| Webhook server | FastAPI | Async, lightweight, perfect for Twilio webhooks |
| Public tunnel | ngrok | Exposes local server to Twilio — right tradeoff for a 6-hour challenge |

---

## Target: 16 Calls, Each 4-6 Minutes Long

We are going beyond the minimum (10 calls, 1-3 min) to:
- 16 calls (minimum was 10)
- 4-6 minutes each (minimum was 1-3 min)
- Highly specific scenarios designed to expose real bugs

---

## Fake Patient Identities

### Female Patient (use for calls 1-13, 15, 16)
```
Name: Sarah Johnson
Date of Birth: March 15, 1989
Phone: 555-0142
Insurance: Aetna (primary) / United Healthcare (husband's plan — unsure which is active)
Pharmacy: CVS on Main Street
```

### Male Patient (use for calls 10, 14 when male voice is used)
```
Name: David Johnson
Date of Birth: July 22, 1985
Phone: 555-0198
Insurance: Cigna
Pharmacy: Walgreens on Oak Street
```

---

## Voice Behavior Rules (inject into EVERY Claude system prompt)

These make the bot sound like a real human patient, not a scripted bot:

- Speak in short, natural sentences — NOT paragraphs
- Use filler words occasionally: "um", "uh", "actually", "wait", "so", "I mean"
- Sometimes ask follow-up questions instead of accepting the first answer
- Express mild confusion or hesitation when appropriate
- NEVER reveal you are a bot — stay in character always
- If asked for personal info, use the fake identity above
- React emotionally when the scenario calls for it (frustration, relief, confusion)
- Don't resolve the call too quickly — keep asking relevant follow-ups to reach 4-5 minutes
- If the agent gives a vague answer, push back: "Sorry, can you be more specific?"
- Occasionally self-correct mid-sentence: "Wait, actually I meant Tuesday, not Thursday"
- Add small talk naturally: "Oh okay, and is there parking at the office?"

---

## Architecture Notes (for ARCHITECTURE.md in the repo)

**Why Twilio over Plivo/Vonage/Telnyx:**
Most tutorials, Stack Overflow answers, and AI-generated code defaults to Twilio.
Since Claude Code is writing this, Twilio gives the cleanest, most reliable output.
The $20 balance is also fully reimbursable.

**Why ElevenLabs over Google TTS / AWS Polly / Azure:**
Voice quality is evaluated BEFORE code quality per the challenge rubric.
A robotic voice = instant rejection. ElevenLabs voices are indistinguishable from
human at conversational speed. The higher cost ($5-8) is worth it for this use case.

**Why Deepgram over Whisper (OpenAI):**
Whisper requires complete audio chunks — it can't stream. In a live call we need
transcription AS the agent speaks so we can respond quickly. Deepgram Nova-2
streams in real time and has best-in-class accuracy for medical terminology.

**Why Claude claude-sonnet-4-6 over GPT-4o:**
Fast enough for conversational latency, smart enough for complex medical scenarios.
Also appropriate since we're testing a competitor's product — using Anthropic felt right.

**Why FastAPI + ngrok over deploying to a server:**
This is a 6-hour challenge, not a production deployment. ngrok gives us a public URL
in 30 seconds. FastAPI handles async Twilio webhooks cleanly. Deploying to AWS/GCP
would waste 1-2 hours for no benefit to this evaluation.

**Why NOT Twilio ConversationRelay or built-in AI:**
Less control over voice quality, harder to inject scenario-specific patient prompts,
and the custom pipeline gives cleaner separation of concerns for the bug-finding logic.
We want full control over how each scenario plays out.

---

## File Structure to Build

```
/voice_bot
  main.py              # Entry point — python main.py --scenario 1
  bot.py               # Core conversation loop
  scenarios.py         # All 16 scenario prompts and configs
  voice.py             # ElevenLabs TTS integration
  transcriber.py       # Deepgram STT integration
  recorder.py          # Saves transcript (.txt) and audio (.mp3)
  server.py            # FastAPI webhook server for Twilio callbacks
  .env                 # Real keys — NEVER COMMIT TO GITHUB
  .env.example         # Template with variable names only — safe to commit
  .gitignore           # Must include .env
  requirements.txt
  README.md
  ARCHITECTURE.md
  /recordings          # Auto-created — stores MP3s (call-01.mp3 etc)
  /transcripts         # Auto-created — stores TXT files (call-01.txt etc)
  BUG_REPORT.md        # Bug report
```

---

## How to Run (must work as single command after setup)

```bash
# Run a specific scenario
python main.py --scenario 1

# Run all 16 scenarios sequentially with 60s gap between calls
python main.py --all

# Run with specific voice gender override
python main.py --scenario 7 --voice female
python main.py --scenario 10 --voice male
```

---

## Bug Report Format

```
BUG-[number]
Call: call-[number] | File: transcript-[number].txt | Timestamp: [e.g. 2:14]
Severity: Critical / High / Medium / Low
Category: Scheduling / Medical Advice / Medication Safety / Insurance / Escalation / Knowledge / Audio / Empathy

What happened:
[Exact description of what the agent said or did wrong]

What should have happened:
[What a correct, well-designed agent should have done]

Why it matters:
[Patient safety / compliance / trust impact]

Quote (if applicable):
"[exact words the agent said]"
```

---

## CRITICAL SECURITY NOTE FOR CLAUDE CODE

⚠️ The .env file contains real API keys and credentials.
NEVER commit .env to GitHub under any circumstances.
Always ensure .gitignore includes .env before any git operations.
Only .env.example (with empty placeholder values) should ever be committed.
