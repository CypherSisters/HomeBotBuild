# Voice ducking, when PuffPAI lowers your music, and when it must not

*Code: `src/puffpai/voice/ducking.py`, called from
`src/puffpai/voice/service.py:handle_wake`. Config: the tracked user-unit
drop-in `systemd/user/puffpai-voice.service.d/10-duck.conf`.*

## The rule

> The music dips **only** when you are actually talking to Puff.

Concretely:

| Situation | What happens to playback |
| --- | --- |
| You sing along, or talk in the room | **Nothing.** No dip, no mute. |
| Puff transcribes that speech and decides it wasn't addressed | **Nothing.** |
| You say "Hey PuffPAI, …" | Dips to `PUFF_DUCK_ADDRESSED_LEVEL` (45%) |
| Puff speaks the reply | Stays dipped (the reply plays at full over it) |
| Hands-free follow-up window (`PUFF_FOLLOWUP=1`, 8 s) | Stays dipped while open |
| The exchange ends | Restored to full volume |
| A wake we probably mis-heard over music | Light 70% dip on your **next** attempt |

## Why it does not duck on every wake trigger

The wake model (`hey_puffpai.onnx`) is a **speech** detector, not a name
detector, its own comment says it "fires at ~1.0 on any full-scale audio (TV,
radio, speech)". Ducking the instant it fires, i.e. *before* transcribing and
*before* knowing whether anyone addressed Puff, dips the music for every
stretch of room speech that `parse_address()` later rejects: dozens of dips an
hour against a handful of genuinely addressed turns.

## Why it does not simply duck after the address check

Moving the duck to after `parse_address()` breaks **waking over music**: the
command still transcribes but her wake *name* is dropped, so the address gate
rejects it (`'is it?'`, `'What time is it?'`).

The mechanism: Whisper decodes the trigger buffer plus the continuation as
**one** utterance, so a degraded continuation degrades the whole decode and eats
the leading name. Probably compounded by the XVF3800's residual-echo suppressor
attenuating near-end speech while the far end is active (double-talk).

## Acoustic echo cancellation, already wired, by topology

On the reference hardware (ReSpeaker XVF3800 as both speaker and mic):

```
/proc/asound/card2/stream0 : Playback Running (iface 1 OUT) + Capture Running (iface 2 IN)
pactl list short sinks     : alsa_output.usb-…reSpeaker_XVF3800…analog-stereo   RUNNING
pactl list short sources   : alsa_input.usb-…reSpeaker_XVF3800…analog-stereo    RUNNING
pactl list short modules   : (no module-echo-cancel, none loaded)
```

The Pulse default **sink and the capture source are the same USB device**, so
the XVF3800 DSP's AEC far-end reference *is* the audio we play, no PulseAudio
`module-echo-cancel` and no loopback wiring is needed. Measured on
that setup: with jazz at full volume the mic reads RMS p50=25 / max=135, *below* the
quiet room's own noise floor and below `PUFF_WAKE_GATE` (150).

**So ducking is not needed to keep the box's own music out of the mic.** It is
needed only for double-talk, and a light dip is enough for that.

**Exception:** Spotify (librespot) drives bare ALSA
(`/etc/asound.conf` → `hw:APE,0`), not the XVF3800. Their audio is outside both
the AEC reference *and* the Pulse ducking lever.

## The peek path

An un-addressed pass counts as a **possible missed wake** when the transcript is
empty, its mean `avg_logprob` is at or below `PUFF_STT_WEAK_LOGPROB` (−0.8), or
a fragment of her name is present but landed too late in the utterance for
`parse_address()` to accept it. If that happens *while something is playing*, a
peek is armed for `PUFF_DUCK_PEEK_ARM` seconds; the next wake trigger inside
that window ducks to `PUFF_DUCK_PEEK_LEVEL` (70%) **before** recording.

Why arm the *next* attempt rather than retry the current one: by the time we
know a transcript was weak, the audio it came from is already captured. Lowering
the music then cannot change that transcript, only the next capture. People
repeat themselves when ignored, and that repeat is what the peek protects. A
name-biased Whisper re-decode of the same audio was considered and rejected: it
can hallucinate the wake word, which would mean answering un-addressed room
speech, the exact thing the address gate exists to prevent.

Singing along and ordinary conversation transcribe confidently and contain no
name, so they never arm a peek and never dip anything.

## Levers

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUFF_DUCK_ADDRESSED_LEVEL` | `45` | Dip level (%) for an addressed exchange |
| `PUFF_DUCK_PEEK_LEVEL` | `70` | Lighter dip for peek retries / listen-ducking |
| `PUFF_DUCK_PEEK_ARM` | `20` | Seconds a peek stays armed |
| `PUFF_DUCK_ON_LISTEN` | `0` | `1` = duck on every wake trigger |
| `PUFF_STT_WEAK_LOGPROB` | `-0.8` | Confidence at/below which we suspect a missed wake |
| `PUFF_DUCK_PCT` | n/a | Legacy name for the addressed level; still honoured |

**Fallback**, if waking over loud music is unreliable in your room:
`PUFF_DUCK_ON_LISTEN=1` ducks on every wake trigger, before recording (at 70%
rather than 45%), with no code change.

## Verifying it in the room

You cannot self-test a wake through the speakers, the AEC eats anything Puff
plays, so a synthesized phrase produces no wake event. It needs a real voice.

1. Start music (World Radio or Jellyfin, **not** Spotify, which bypasses the
   Pulse lever). Sing along and hold a normal conversation near the appliance
   for a minute. **The volume must not change at all.**
2. Say "Hey PuffPAI, what time is it?", the music should dip, the answer be
   clearly audible, and the music return when she finishes.
3. Turn the music up loud and say it again. She should still answer. If she
   misses it the first time, say it once more: the peek path makes the second
   attempt land.

In `voice.log`, a `ducked …` line must only ever appear next to an
`ADDRESSED` line or a `peek:` reason, never after a bare `addressed=false`.
