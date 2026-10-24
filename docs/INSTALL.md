# Installing it somewhere

The adapter is one process that talks to the device over the ESPHome API and to Mistral
over HTTPS. How it is started is the only part that differs between machines.

Three things have to be true everywhere:

* the device's `api_key` is in a file the adapter can read, and `config.json` points at it
* `MISTRAL_API_KEY` is in the environment of the process, never in the config file
* only one adapter runs per device, because the device accepts a single API subscription

## Where it runs

Any machine the device can reach on the network can run the adapter, and so can a
machine that cannot be reached by the device, as long as `audio_mode` is `url` and the
TTS port is the one the device can reach. Two settings cover the difference:

```json
{
  "audio_mode": "url",
  "tts_port": 8899,
  "tts_public_base": "http://192.168.1.10:8899"
}
```

`tts_public_base` is what the device is told, `tts_port` is what the adapter listens on.
They are the same value unless the adapter sits behind NAT, in a container, or on a host
with several addresses.

## A normal host: systemd

```ini
# ~/.config/systemd/user/hermes-pe.service
[Unit]
Description=Hermes Voice PE adapter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/hermes-homeassistant-pe-adapter
EnvironmentFile=%h/.config/voice-pe/env
ExecStart=%h/hermes-homeassistant-pe-adapter/.venv/bin/python -u -m hermes_pe.bridge --config config.json daemon
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now hermes-pe.service
loginctl enable-linger "$USER"        # keep it running without an open session
journalctl --user -u hermes-pe -f
```

`EnvironmentFile` is the right place for `MISTRAL_API_KEY`. `chmod 600` it.

## No systemd: a watchdog and cron

Some containers have no init system at all (s6-overlay, tini), so the unit above cannot be
used. `scripts/hermes-pe-keepalive.sh` does the same job from cron: start the adapter when
it is not running, stay silent when it is.

```bash
mkdir -p ~/.config/voice-pe
printf 'MISTRAL_API_KEY=%s\n' "$YOUR_KEY" > ~/.config/voice-pe/env
chmod 600 ~/.config/voice-pe/env

# every two minutes
( crontab -l 2>/dev/null; echo "*/2 * * * * $PWD/scripts/hermes-pe-keepalive.sh" ) | crontab -
```

The script refuses to start the adapter without a key on purpose: an adapter without one
transcribes every sentence to "Invalid API key" and looks like a broken microphone.

Two details in it exist because they cost real time:

* `pgrep -f` matches its own caller, because the pattern is part of the shell's command
  line. Without a check on the process name, the watchdog finds itself and never starts
  anything.
* the daemon is started with `setsid`, so the scheduler exiting does not take it down.

## A container

Whatever starts the container, the following has to be inside it: the repo, the venv, the
config, the secrets file and the `MISTRAL_API_KEY`. The TTS port has to be published if
`audio_mode` is `url`:

```yaml
services:
  web:
    ports:
      - "8899:8899"
```

A published port is what makes `tts_public_base` point at the host instead of at the
container address, which the device cannot route.

## Checking it works

```bash
# transport: connects, prints what the device reports, exits
.venv/bin/python -m hermes_pe.bridge --config config.json probe

# one turn: wake word, question, spoken answer, exits
.venv/bin/python -m hermes_pe.bridge --config config.json turn
```

A working turn logs `transcript`, `reply`, `tts`, then either `tts_url` followed by
`http_get` (url mode) or `tts_played` (api mode), then `done`. If `tts_url` appears
without a following `http_get`, the device could not reach the address in it.
