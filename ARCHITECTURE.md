# Architecture

## How it works

The bot is a **custom four-service pipeline stitched together over a Twilio Media Streams
WebSocket**. `main.py` opens a public tunnel, starts a FastAPI server, and asks Twilio to
dial the test line with TwiML containing `<Connect><Stream>` — which gives us a
bidirectional 8 kHz μ-law audio socket for the duration of the call. From there a single
`MediaSession` object owns the conversation: inbound frames go straight to a Deepgram
streaming WebSocket, finalized transcripts accumulate into a turn buffer, and when Deepgram
signals an endpoint (or 650 ms of silence elapses) the buffered text is appended to a Claude
conversation and Claude produces the patient's next line. That line is synthesized by
ElevenLabs directly to `ulaw_8000` — the exact format Twilio wants, so there is no
transcoding anywhere in the hot path — then paced back over the socket in 20 ms frames.
Because we send audio at real time rather than dumping it into Twilio's buffer, playback
stays interruptible, which is what makes barge-in testing possible. Twilio records the call
itself (dual channel), and after hangup `main.py` polls the API for the MP3 and writes a
timestamped transcript alongside it. The conversation role mapping is deliberately inverted:
the *agent under test* occupies Claude's `user` role and our bot is the `assistant`, which
frames every request as "given what they just said, what does this patient say next".

## Why these choices

**Why a custom pipeline instead of a Realtime API or Twilio ConversationRelay.** A
speech-to-speech Realtime API would have been fewer moving parts, and I'd reach for it in
production. It was the wrong call here because this bot's job is not to converse well — it's
to *probe*. Scenario 09 needs to interrupt mid-sentence at a controlled word count;
scenario 15 needs the caller's audio attenuated to 35% with injected dead air; scenario 12
needs a deliberate 3.5-second silence after the opening line. Those are all manipulations of
the audio and turn-taking layer, and a bundled speech-to-speech model hides exactly that
layer. Owning the pipeline also means I can see the agent's transcript separately from my
own, which is what makes the transcripts usable as bug evidence rather than just recordings.
The cost is latency — STT → LLM → TTS is roughly 1.5–2.5 s per turn versus sub-second for
Realtime — which is noticeable but acceptable for a caller who is meant to sound a little
hesitant anyway.

**Twilio** because outbound PSTN with a bidirectional media socket and built-in call
recording is a solved problem there, and the dual-channel recording gives evaluators the
same artifact a human reviewer would want. **Deepgram over Whisper** because Whisper needs
complete audio chunks; it cannot tell me *when the agent stopped talking*, and endpointing
is the entire basis of turn-taking here. Deepgram's `speech_final` and `UtteranceEnd` events
are what drive the loop. **ElevenLabs** because the rubric says calls get listened to before
code gets read, and a robotic caller invalidates every behavioral scenario — an agent may
respond differently to something that sounds synthetic. Its native μ-law 8 kHz output was
the deciding technical factor. **Claude Sonnet** for the brain: fast enough not to add dead
air between turns, and strong enough to hold a persona under pressure — scenario 16 requires
believing a fabricated medical rename and pushing back when contradicted, which a smaller
model tends to abandon. **FastAPI plus a throwaway tunnel over a deployed host** because the only thing
being tested is a phone call; a deployment pipeline would add an hour and change nothing
about the results.

**Notable implementation decisions.** Python 3.13 removed `audioop`, so `audio.py`
implements the G.711 μ-law codec directly (the reference Sun/BSD algorithm) — needed for
the gain control scenario 15 depends on. Scenario behaviour is data, not code: each
`Scenario` is a frozen dataclass carrying its persona, agenda, watch-list, and pipeline
knobs (`interrupt_after_words`, `quiet`, `language`, `pause_after_opening_ms`), so adding a
17th test means adding a record, not touching the media layer. Claude ends calls by emitting
a `[END_CALL]` sentinel that `bot.py` strips before synthesis, which keeps hangup logic in
the prompt where the conversational judgment already lives. And `config.py` hard-fails if
`TARGET_PHONE_NUMBER` is anything but the assessment line — a bot that autodials should not
be one config typo away from calling a stranger.

## What the calls changed

Almost nothing about turn-taking survived contact with the real agent, and that iteration
is the part of this project I'd point at first.

**The bot answered the IVR.** Deepgram endpoints on every pause, and a phone tree pauses
between prompts, so "this call may be recorded" looked exactly like a finished turn. The
bot opened one second into the disclaimer and then talked over the language menu; the two
sides were misaligned for the rest of the call. Fixed by treating automated preamble as
non-speech and requiring a longer settled gap (2.2s) before the opening line than
mid-call (1.2s).

**Then it answered half-sentences.** Their agent delivers one thought in bursts with
multi-second gaps — *"The earliest slot on Thursday is"* … *"9:45 with Dr. X"*. A flat
silence threshold can't tell that from a finished turn. Two mechanisms fixed it: terminal
punctuation as a completeness signal (Deepgram punctuates a sentence it considers
finished, and leaves a truncated one bare), so a fragment waits 3.0s instead of 1.2s; and
discarding a generated reply outright if the agent resumes while Claude is still thinking.

**The caller didn't sound like one.** Early turns averaged 36.7 words, peaking at 71 —
nobody stacks three questions into one breath on a phone. Hard length rules plus dropping
`max_tokens` to 110 brought it to 11.1 words. That single change did more for realism than
any voice tuning.

**Latency was worth measuring rather than guessing.** I assumed TTS was the bottleneck and
added streaming; it saved 0.14s. The real budget was 1.6s of silence-waiting plus 1.6s of
Claude. I also added prompt caching expecting a speedup — it cached correctly and changed
latency not at all, because generation dominates. It stayed for the cost saving, not as a
performance win. Final median response is ~2s.

**Failures that don't crash are the expensive ones.** When the ElevenLabs quota ran out
mid-batch, nothing errored — the bot generated fine lines, sent no audio, and the far end
hung up on silence. Ten scenarios burned before anyone noticed. There's now a preflight
that synthesizes one line before dialling, and an abort that stops the batch rather than
working through the remaining scenarios mutely. Same category: a dropped Deepgram socket
made the bot deaf for a whole call and read, in the transcript, as *their* agent going
silent — which is how a pipeline bug becomes a false bug report.

## Known limitations

- **Turn-taking is heuristic.** A 650 ms silence window plus Deepgram's endpoint signal
  works well, but an agent that pauses mid-sentence can get cut off. This is tunable
  (`ENDPOINT_DELAY`) and was tuned by listening to early calls rather than guessing.
- **No echo cancellation.** Not needed on a digital call leg, but our own audio would
  confuse the transcriber if the far end looped it back.
- **The tunnel is a single point of failure.** Fine for a test harness; `PUBLIC_URL`
  exists as the escape hatch for anyone who'd rather not depend on it.
- **Bug detection is human.** The bot generates evidence — transcripts and recordings tied
  to a stated bug target — and I read them. Automating that judgment would mean grading an
  LLM's medical-safety behaviour with another LLM, which I don't trust for a report that
  claims a patient-safety defect.
