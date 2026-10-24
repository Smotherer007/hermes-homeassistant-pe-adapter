# hermes-homeassistant-pe-adapter

A voice adapter that plugs a Home Assistant Voice PE (or any ESPHome device with
`voice_assistant`) into [Hermes Agent](https://github.com/NousResearch/hermes-agent).
Wake word, speech to text, one agent turn, spoken answer, wake word again.

No local models. Both speech directions are HTTP calls to a hosted endpoint, and the
agent runs wherever Hermes runs. That makes the adapter small enough for a box without a
GPU and keeps the device useful after the agent moved to the network.

```
Voice PE (wake word, mic, speaker)
   | ESPHome native API over TLS/Noise
   v
hermes-pe-adapter (this project)
   |-- wake word is handled on the device, the adapter only sees the captured sentence
   |-- STT: Mistral Voxtral transcribe  (audio -> text)
   |-- agent: `hermes -z "<text>"`      (one-shot, final answer only)
   `-- TTS: Mistral Voxtral speech      (text -> FLAC 48 kHz -> device media player)
```

## Why an adapter instead of a Home Assistant integration

The device already speaks a complete protocol: it announces a wake word, streams 16 kHz
microphone audio over the ESPHome API, and reacts to LED phases (`STT_VAD_START` through
`RUN_END`) that the firmware maps to its ring. It also plays a URL from its own media
player, so the adapter never has to stream speaker audio back.

That means the adapter does not need Home Assistant at all. It needs the device's API key
and a command that turns text into an answer.

## Install

```bash
git clone git@github.com:Smotherer007/hermes-homeassistant-pe-adapter.git
cd hermes-homeassistant-pe-adapter
cp config.example.json config.json      # then edit it
export MISTRAL_API_KEY=...              # or put the key in the environment of the unit
uv run --with aioesphomeapi --with aiohttp --with webrtcvad-wheels \
  python src/hermes_pe/bridge.py --config config.json turn
```

Nothing has to be installed into the system Python: `uv` resolves the three dependencies
per run. For a permanent install use `uv pip install -e .` and the `hermes-pe` entry point.

The device's API key lives next to the device, not in this repository:

```yaml
# ~/.config/voice-pe/secrets.yaml   (chmod 600)
api_key: "..."                        # the `api: encryption: key` from the ESPHome yaml
```

## Configuration

Every flag exists as a config key, and an explicit flag wins over the file.

```json
{
  "host": "voice-pe.local",
  "secrets": "~/.config/voice-pe/secrets.yaml",
  "language": "de",
  "stt_url": "https://api.mistral.ai",
  "stt_model": "voxtral-mini-transcribe-3",
  "stt_dialect": "mistral",
  "tts_url": "https://api.mistral.ai",
  "tts_model": "voxtral-mini-tts-3-0",
  "tts_dialect": "mistral",
  "tts_voice": "voice_481",
  "answer_cmd": "hermes -z {text}"
}
```

* `host` is the mDNS name or the IP of the device. Use the IP if the adapter runs in a
  container, where mDNS usually does not resolve.
* `secrets` points at the file holding `api_key`.
* Paths in the config may use `~` and environment variables; they are expanded before
  use, so a unit that starts in `/` still finds `~/.config/voice-pe/secrets.yaml`.
* `answer_cmd` receives the transcript in place of `{text}`. Its stdout is what gets
  spoken, so anything that prints an answer works: `hermes -z {text}`,
  `hermes -z {text} --continue`, or a script of your own.
* `answer_cmd` is split like a shell command but executed without a shell, so a
  transcript containing quotes stays text. A systemd unit does not inherit your
  interactive PATH, so give the absolute path of the agent binary there.
* `tts_voice` takes a preset slug such as `voice_481`, or the UUID of a saved voice.
* `stt_bias` is a list of terms the recognizer should spell correctly. It is sent as
  `context_bias` per term to Mistral and as one `prompt` to an OpenAI style endpoint.

Keys are read from the environment (`MISTRAL_API_KEY`) and never from the config file, so
a repository, a unit file or a container definition cannot leak them.

## Audio back to the device

`audio_mode` decides how the answer reaches the speaker. The default is `url` because it
is the only mode the stock Home Assistant Voice PE firmware plays.

* `url` (default): the answer is converted to FLAC at 48 kHz and published by a small
  HTTP server in this adapter; `TTS_END` carries the URL and the device fetches the file
  with its `media_player`. This is the path Home Assistant itself uses.
* `api`: the answer is streamed as 16 kHz mono PCM with `send_voice_assistant_audio`.
  Only works on a firmware that binds a `speaker` to `voice_assistant` (ESPHome writes
  incoming audio into `speaker_buffer_` under `#ifdef USE_SPEAKER`). On the stock Voice PE
  it is silently dropped, and `TTS_END` without a URL logs `"No url in TTS_END event"` and
  returns early: events and LEDs run through, the room stays quiet.

Two settings exist because the address a device can reach is not always the address the
adapter sees:

```json
{
  "audio_mode": "url",
  "tts_port": 8899,
  "tts_public_base": "http://192.168.1.10:8899"
}
```

`tts_port` pins the port the file server listens on, `tts_public_base` is what the device
is told. Behind NAT, in a container, or on a host with several interfaces these differ
from `local_ip()`. In a container the port also has to be published, for example
`ports: ["8899:8899"]`. Without both, the log shows `tts_url` and then no `http_get`,
which means the device never fetched the file.

## Run modes

```bash
python src/hermes_pe/bridge.py --config config.json turn     # one turn, then exit (default)
python src/hermes_pe/bridge.py --config config.json daemon   # turns forever, wake word re-armed
python src/hermes_pe/bridge.py --config config.json probe    # print what the device reports
python src/hermes_pe/bridge.py --config config.json listen   # capture one sentence, print text
python src/hermes_pe/bridge.py --config config.json say --file answer.wav
```

`turn` is what a supervisor or a container should use. `daemon` keeps one ESPHome
subscription open, which is what keeps the wake word armed on the device.

## Deployment

`docs/INSTALL.md` has the three ways this runs: a systemd user unit on a normal host, a
watchdog script plus cron where there is no init system (containers with s6 or tini), and
what a container needs in terms of published ports.

## Endpoint dialects

Two shapes are supported, chosen by `stt_dialect` / `tts_dialect`:

* `openai`: what local servers speak. `voice` as the voice field, raw audio bytes as the
  speech response, `prompt` as the vocabulary hint, `response_format` in the request.
* `mistral`: Voxtral. The speech response is JSON with base64 in `audio_data`, the voice
  field is `voice_id`, and the vocabulary hint becomes repeated `context_bias` entries
  together with a two letter `language`.

```bash
export MISTRAL_API_KEY=...
curl -s https://api.mistral.ai/v1/audio/voices?page_size=100 \
  -H "Authorization: Bearer $MISTRAL_API_KEY" | jq '.items[] | {id, name, type}'
```

Saved voices (including cloned ones) are only addressable by their UUID. Presets have a
slug such as `voice_481` (German) and can be used by slug.

A speech request therefore looks like this:

```json
{
  "model": "voxtral-mini-tts-3-0",
  "input": "Alles erledigt.",
  "voice_id": "01a16d08-63da-7067-a298-9e691bcdf78d",
  "response_format": "wav"
}
```

The answer arrives as `{"audio_data": "<base64 wav>"}`, which the adapter decodes, merges
into one file per answer, and converts to FLAC at 48 kHz because that is the only format
the device's media player decodes.

## What the device insists on

These cost hours when they are unknown. They are also in `docs/PROTOCOL.md`.

* The device accepts exactly one ESPHome API subscription. Two clients split the
  microphone audio and both time out, so a second adapter must not run next to the first.
* The media player decodes FLAC at 48 kHz only. A WAV URL fails silently.
* A fresh device sits at `volume: 0.05`, which sounds like no audio at all. Set the
  volume once and keep it.
* Do not open `/dev/ttyACM*` while a turn is running. Touching the USB CDC resets the
  ESP32-S3 and disarms the wake word.
* `TTS_END` must be sent exactly once per turn. A second one replays the sentence.

## Status

Working end to end against a Voice PE: wake word, transcription, agent turn, spoken
answer, LED phases. The ACP backend (`hermes-acp`, persistent session, streaming) is
designed but not implemented; today every turn is one `hermes -z` call, which is the
right trade for voice because the answer has to arrive before the user walks away.

## License

MIT. The device transport started as part of the pi voice extension by the same author
and was extracted here to be agent independent.
