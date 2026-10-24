# Deployment notes

Lessons from bringing this adapter up against a real Home Assistant Voice PE, on a host
that runs as an app container. Each one cost real time, each one is written as
symptom -> cause -> rule so it can be checked quickly.

## 1. `daemon` is a transport, not an answering server

**Symptom.** Wake word fires, log shows `transcript`, the LED ring stays on "thinking",
and nothing is ever spoken. No error in the log.

**Cause.** `run_daemon()` is built for the pi extension, which drives it over stdin:
after a transcript it blocks on `inbox.get()` waiting for

```
{"cmd": "answer", "file": "/tmp/answer.wav"}
{"cmd": "skip"}
```

Started detached (`</dev/null`) nobody writes that line, so the turn ends in
`{"event": "idle", "reason": "no answer within timeout"}` after `answer_timeout`
seconds — long after the person at the device gave up.

**Rule.** For standalone operation use a mode that answers on its own: `serve` keeps the
subscription open and answers every question, `turn` does one question and exits (loop it
if you must, at the cost of a reconnect gap). `daemon` is correct only when something
feeds its stdin. `scripts/hermes-pe-keepalive.sh` therefore starts `serve`.

A log line for the input without a line for the output is the signature of this class of
bug. Check that before looking at credentials or the network.

## 2. Sourcing an env file does not export it

**Symptom.** Every transcription fails with `Invalid API Key`, while the same key works
from a shell and the config file looks right.

**Cause.** `. "$ENVFILE"` in a POSIX shell assigns shell variables. Without `allexport`
the values are not in the environment of the process that is started afterwards, so the
adapter starts with no `MISTRAL_API_KEY` at all and sends `Authorization: Bearer `
(empty).

**Rule.** Always

```sh
set -a
. "$ENVFILE"
set +a
```

and verify afterwards that the child really has the variable:

```sh
tr '\0' '\n' < /proc/<pid>/environ | grep -c '^MISTRAL_API_KEY='
```

## 3. A watchdog must not find itself

**Symptom.** The watchdog never starts the daemon (or starts it again and again).

**Cause.** `pgrep -f "<pattern>"` matches the shell that runs the watchdog, because the
pattern is part of that shell's own command line. And `/proc/<pid>/comm` is truncated to
15 characters, so a script named `hermes-pe-listen.sh` appears as `hermes-pe-liste` —
a `comm`-based check fails just as silently.

**Rule.** Compare the full argv of each candidate process:

```sh
[ "$(tr '\0' ' ' < "/proc/$pid/cmdline")" = "/bin/sh $LISTEN " ]
```

and skip `$$`. Then prove it: run the watchdog twice and confirm the second run is
silent and exactly one process exists.

## 4. One voice-assistant subscription only

**Symptom.** Two clients connected, both wait forever, transcripts arrive empty or cut.

**Cause.** The device splits microphone audio between subscriptions. Two bridge
processes (a leftover one-shot plus the new daemon, or two watchdogs that did not see
each other) both look healthy and both time out.

**Rule.** Before starting a mode that holds the subscription, check that no other one
does. Every deployment script here is idempotent for that reason.

## 5. A bridge address is not a LAN address

**Symptom.** `url` mode completes, the log shows `tts_url`, and no `http_get` follows.

**Cause.** The adapter announced `http://10.x.y.z:<port>/tts/...` — its own address
inside the container network. A device on `192.168.x.y` has no route there. The port
must be published by the host, and `tts_public_base` must name the address the device
can reach.

**Rule.** Probe before diagnosing: bind the port and fetch it through the host's LAN
address — the hairpin path works, so a `404` from the adapter proves publish and
listener are both fine:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://<host-lan-ip>:8899/tts/nonexistent
```

A published port lives in the host app compose, which the app manager re-renders from
the store manifest on restart — so a hand edit survives a restart, not an update.
Alternatives are a sidecar container (`restart: unless-stopped`) or a reverse proxy app;
both survive updates. Inside the container network other apps should address this one by
its compose name (`<app>_web_1`), not by IP: the IP changes on recreate.

## 6. State the latency the user feels

The chain is wake word -> 0.9 s silence detection -> STT -> agent turn -> TTS for every
sentence -> file -> device fetch -> playback. Nothing is spoken until all of it is done.
Measured warm: ~4.5 s agent, ~2-3 s TTS, so 8-15 s of silence with the LED ring on
"thinking" — and much longer when the agent decides to use tools or the model is cold.

That silence is indistinguishable from a hang, so: keep answers short at the command
level (`answer_cmd`), measure it, and say the number out loud. Real streaming (sentence
by sentence while the agent writes) needs the ACP backend, which is not implemented.

## 7. Diagnose the right life of the container

A log from a previous container life is indistinguishable from a current one by content
alone. Check the file's mtime against the process start time before concluding anything
about a failure, and confirm the process exists at all — a recreated container leaves
`/opt/data` intact, including logs and cron jobs, but no processes.
