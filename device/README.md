# The device side

The adapter talks to a stock ESPHome firmware, but a Voice PE that never saw a Home
Assistant needs three things baked in before it can be used on its own: a fixed name, WiFi
credentials, and an API key. `pi-voice-pe.patch` adds exactly those and nothing else.

That is why the patch exists at all: without it the device sits in onboarding, waiting for
a Home Assistant that sets the key, and the adapter has nothing to authenticate against.

## What the patch changes

* `name: pi-voice-pe`, `name_add_mac_suffix: false`, so the device answers at
  `pi-voice-pe.local` instead of `home-assistant-voice-<mac>.local`.
* `wifi: ssid` and `password` from `!secret`, so the image associates on its own instead of
  waiting for Improv over BLE to provision it.
* `api: encryption: key: !secret api_key`, a fixed key. Upstream comments this as "Uses key
  set by Home Assistant"; without Home Assistant nobody would set one.

Nothing about the wake word, the LED ring or the media player changes. The stock Voice PE
firmware already does all of that.

## Build and flash

```bash
git clone --depth 1 https://github.com/esphome/home-assistant-voice-pe.git
cd home-assistant-voice-pe
cp home-assistant-voice.yaml pi-voice-pe.yaml
cp /path/to/hermes-homeassistant-pe-adapter/device/pi-voice-pe.patch .

git apply -p1 < pi-voice-pe.patch        # or: patch -p1 < pi-voice-pe.patch
cp /path/to/device/secrets.example.yaml secrets.yaml
chmod 600 secrets.yaml                   # fill in ssid, password and a fresh api_key

uvx esphome compile pi-voice-pe.yaml     # ~4 minutes, the toolchain is cached
uvx esphome run pi-voice-pe.yaml --device /dev/ttyACM0
```

`/dev/ttyACM*` needs permission. On an immutable distribution a udev rule with
`TAG+="uaccess"` for vendor `303a` does that; `setfacl` does not, because the device node is
recreated on every replug.

Do not open `/dev/ttyACM*` while a turn is running: touching the USB CDC resets the
ESP32-S3, the API connection drops, and the wake word is disarmed.

## The key has to match

`api_key` in `secrets.yaml` on the ESPHome side and the same value in the file `config.json`
points at (`secrets` key of the adapter) are the same secret. If they differ, the adapter
logs a handshake failure and nothing else happens.

Generate one with `openssl rand -base64 32`.

## After flashing

The device boots with `volume: 0.05`, which sounds like no audio at all. Set it once:

```yaml
# in the adapter config
"volume": 0.7
```

The wake word is the firmware default, `Okay Nabu`. Two clients cannot share the device:
it accepts a single ESPHome API subscription, so only one adapter may run per device.
