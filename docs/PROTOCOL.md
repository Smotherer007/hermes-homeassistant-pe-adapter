# Protocol notes

What the adapter sends to the device, and which details are not negotiable.

## One turn, in order

The device drives the sequence. The adapter answers each step and never sends an event
early, because every event changes the LED ring and a duplicate re-triggers an action.

```
device:  wake word -> VoiceAssistantRequest
adapter: AUDIO_MODE_API on port 0, then
         STT_VAD_START            ring: listening
device:  VoiceAssistantAudio (16 kHz s16 mono), ended by the device
adapter: STT_VAD_END              ring: thinking
         STT_END {text}           transcript known
         INTENT_START {text}      agent turn starts here
         INTENT_PROGRESS {tts_start_streaming: 1}
         INTENT_END {speech}      text of the answer
         TTS_START {text}         ring: replying
         TTS_END {url}            exactly once, the device fetches and plays it
         RUN_END                  ring: idle, wake word armed again
```

`run` mode in `bridge.py` implements exactly this order.
`daemon` mode keeps the subscription open across turns, which is what keeps
`micro_wake_word` armed; the device only listens while one API client is subscribed.

## Audio

The adapter never streams speaker audio. It publishes one FLAC file over a small local
HTTP server, and `TTS_END` carries the URL; the device downloads and plays it. That keeps
the ESPHome connection free of large binary messages.

Two rules follow from the firmware:

* FLAC at 48 kHz is the only format the media player decodes. A WAV URL fails silently,
  so a missing answer sound is usually a container or sample rate problem, not a volume
  problem.
* The device starts at `volume: 0.05`. Set the volume once; a value of 0 in a config
  means "leave it as it is".

Microphone audio arrives as `VoiceAssistantAudio` messages at 16 kHz signed 16 bit mono.
Speech detection runs in `bridge.py` on a short window with `webrtcvad` plus an RMS floor,
so a fan or a door does not start a turn.

## Things that break it

* Two API clients. The device splits the microphone audio between subscriptions and both
  wait for a wake word that never arrives. The adapter takes a pid lock for that reason.
* Touching `/dev/ttyACM*` while a turn runs. The USB CDC reset reboots the ESP32-S3, the
  API connection drops, and the wake word is disarmed.
* `TTS_END` twice. The sentence is spoken twice.
* An event order that ends a turn without `RUN_END`. The ring stays in "thinking" and the
  device stops listening.

## Endpoint shapes

Transcription, `openai` dialect:

```
POST {stt_url}/v1/audio/transcriptions   multipart
file, model, language, response_format=json, prompt
-> {"text": "..."}
```

Transcription, `mistral` dialect:

```
POST {stt_url}/v1/audio/transcriptions   multipart, Authorization: Bearer
file, model, language (two letters), context_bias (repeated)
-> {"text": "...", "language": null, "segments": []}
```

Speech, `openai` dialect:

```
POST {tts_url}/v1/audio/speech           json
{model, input, voice, response_format}
-> raw audio bytes
```

Speech, `mistral` dialect:

```
POST {tts_url}/v1/audio/speech           json, Authorization: Bearer
{model, input, voice_id, response_format}
-> {"audio_data": "<base64>"}
```

Both mistakes are silent in different ways: a wrong voice field is answered with a
different speaker, and an undecoded JSON envelope is stored as an audio file that plays
as noise. `synthesize()` decides by content type, not by dialect, so a proxy that rewrites
the envelope still works.
