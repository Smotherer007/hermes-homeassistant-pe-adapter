#!/usr/bin/env python3
"""Voice adapter between an ESPHome voice device (e.g. Home Assistant Voice PE) and Hermes Agent.

The device is the microphone and the speaker; Hermes is the brain. One *turn*:

    wake word (on device) -> mic audio over the ESPHome API -> Mistral STT -> text
    text -> `hermes -z` (one-shot, final answer only) -> Mistral TTS -> playback on device

Nothing local is required: both speech directions are HTTP calls to a hosted endpoint
(Mistral Voxtral by default), and the agent runs elsewhere on the network.

Audio transport: the device asks for a server via `VoiceAssistantRequest`; replying
with port 0 keeps the audio on the API connection (`AUDIO_MODE_API`), so no extra
socket is needed:

    mic in   : VoiceAssistantAudio messages, 16 kHz s16 mono
    speaker  : a FLAC file at 48 kHz that the device fetches and plays itself

The LED ring of the Voice PE reacts to the events sent here (STT/TTS/RUN phases),
so a turn looks the same as it does with Home Assistant.

Run modes
    turn            one conversation turn, then exit (default)
    daemon          keep listening, one turn after another
    probe           print what the device reports and exit
    say             play a WAV file on the device and exit
    listen          wait for the wake word, capture one sentence, print the transcript

Progress is written to stdout as one JSON object per line, so a caller can read it
without parsing prose.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import os
import signal
import pathlib
import re
import shlex
import struct
import subprocess
import sys
import threading
import tempfile
import time
import wave

import aiohttp
from aiohttp import web
from aioesphomeapi import APIClient, VoiceAssistantEventType

try:  # WebRTC VAD: 20 ms frames at 16 kHz, mode 2 = aggressive but speech-safe
    import webrtcvad
except ImportError:  # pragma: no cover - the extension installs it via uv
    webrtcvad = None

SAMPLE_RATE = 16_000

# One device, one bridge: several daemons would each arm the wake word and each
# receive the audio, which produces duplicated and empty turns.
PID_FILE = pathlib.Path("/tmp/hermes-pe-bridge.pid")


def acquire_pid_file() -> bool:
    """True when this process owns the bridge slot."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)     # raises when the process is gone
            return False
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass                # stale file: take over
    PID_FILE.write_text(str(os.getpid()))
    return True


def release_pid_file() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def load_config(path: pathlib.Path) -> dict:
    text = pathlib.Path(path).read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # secrets.yaml is YAML, but a flat key: "value" file needs no parser.
        values = {}
        for line in text.splitlines():
            match = re.match(r'^([A-Za-z0-9_]+):\s*"?(.*?)"?\s*$', line)
            if match:
                values[match.group(1)] = match.group(2)
        return values


def frame_rms(pcm: bytes) -> float:
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    return math.sqrt(sum(s * s for s in samples) / count)


class Vad:
    """A real voice-activity detector when webrtcvad is available, an RMS fallback otherwise."""

    def __init__(self, speech_rms: float) -> None:
        self.speech_rms = speech_rms
        self.engine = webrtcvad.Vad(2) if webrtcvad is not None else None
        self.noise = 0.0

    @property
    def kind(self) -> str:
        return "webrtcvad" if self.engine is not None else "rms"

    def threshold(self) -> float:
        return max(self.speech_rms, min(self.noise * 2.2, 1200))

    def check(self, frame: bytes) -> tuple[bool, float]:
        """(is_speech, level) for one 20 ms frame."""
        level = frame_rms(frame)
        if self.engine is not None:
            try:
                return self.engine.is_speech(frame, SAMPLE_RATE), level
            except Exception:
                pass
        if self.noise == 0:
            self.noise = max(level, 1.0)
        elif level < self.noise * 1.2:
            self.noise = self.noise * 0.995 + max(level, 1.0) * 0.005
        return level > self.threshold(), level


class Utterance:
    """Collect mic audio and decide when the sentence is over.

    Uses an explicit silence counter rather than timestamps: a frame either counts
    as speech or it does not, and `silence_ms` of non-speech frames ends the turn.
    That is what makes the turn stop when the room is quiet, instead of drifting
    until the hard cap.
    """

    def __init__(self, silence_ms: int, max_seconds: float, speech_rms: float = 300) -> None:
        self.silence_ms = silence_ms
        self.max_seconds = max_seconds
        self.speech_rms = speech_rms   # absolute floor: below this it is never speech
        self.chunks: list[bytes] = []
        self.bytes_seen = 0
        self.speaking = False
        self.loud_run_ms = 0.0
        self.silence_run_ms = 0.0
        self.cap_hit = False
        self.speech_started_once = False
        self.frames_total = 0
        self.frames_speech = 0
        self.vad = Vad(speech_rms)
        self.carry = b""
        self.trace: list[tuple] = []
        self.player_key = None
        self.tts_end_sent = False

    @property
    def seconds(self) -> float:
        return self.bytes_seen / (SAMPLE_RATE * 2)

    def threshold(self) -> float:
        # Relative to the quietest thing the microphone hears, not to a table value and
        # not to the wake-word chime: the chime is loud, the room is not.
        return max(self.speech_rms, min(self.noise * 2.2, 1200))

    def push(self, chunk: bytes) -> bool:
        """Return True when the utterance is complete.

        Frames are cut from a carry buffer instead of from each chunk: an audio
        message is ~32 ms but a frame is 20 ms, so slicing per chunk would drop a
        third of the audio - including the moment the silence starts.
        """
        self.chunks.append(chunk)
        self.bytes_seen += len(chunk)
        self.carry += chunk

        frame_bytes = FRAME_SAMPLES * 2
        while len(self.carry) >= frame_bytes:
            frame = self.carry[:frame_bytes]
            self.carry = self.carry[frame_bytes:]
            self._judge(frame)
            if self._finished():
                return True

        if self.seconds >= self.max_seconds:
            self.cap_hit = True
            emit("speech_end", reason="max_seconds", at=round(self.seconds, 2))
            return True
        return False

    def _finished(self) -> bool:
        if self.speaking and self.silence_run_ms >= self.silence_ms:
            emit("speech_end", reason="silence", silence_ms=round(self.silence_run_ms), at=round(self.seconds, 2))
            return True
        return False

    def _judge(self, frame: bytes) -> None:
        self.frames_total += 1
        loud, level = self.vad.check(frame)
        if loud:
            self.frames_speech += 1

        if loud:
            self.loud_run_ms += FRAME_MS
            self.silence_run_ms = 0.0
            if not self.speaking and self.loud_run_ms >= 100:
                self.speaking = True
                emit("speech_start", at=round(self.seconds, 2), detector=self.vad.kind)
        else:
            self.loud_run_ms = 0.0
            if self.speaking:
                self.silence_run_ms += FRAME_MS

        self.trace.append((round(self.seconds, 2), round(level, 1), loud,
                           round(self.silence_run_ms), round(self.vad.noise, 1)))

    def wav(self) -> bytes:
        pcm = b"".join(self.chunks)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        with wave.open(path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm)
        return pathlib.Path(path).read_bytes()


def build_stt_fields(wav_name: str, language: str, model: str, dialect: str, bias: list[str] | None) -> dict:
    """The multipart fields for one recording, in either endpoint dialect.

    `openai` is what the local containers speak: a `prompt` with the vocabulary hint and
    a fixed `response_format`. `mistral` is Voxtral: a two-letter language code and
    repeated `context_bias` entries instead of a prompt.
    """
    fields: dict = {"model": model} if model else {}
    if dialect == "mistral":
        if len(language) == 2:
            fields["language"] = language.lower()
        if bias:
            fields["__context_bias__"] = bias
    else:
        fields["language"] = language
        fields["response_format"] = "json"
        if bias:
            fields["prompt"] = ", ".join(bias)
    return fields


async def transcribe(
    session: aiohttp.ClientSession,
    base_url: str,
    wav: bytes,
    language: str,
    model: str = "whisper-1",
    dialect: str = "openai",
    key: str = "",
    bias: list[str] | None = None,
) -> str:
    """One recording -> text. The hosted API also needs a bearer token."""
    fields = build_stt_fields("turn.wav", language, model, dialect, bias)
    form = aiohttp.FormData()
    form.add_field("file", wav, filename="turn.wav", content_type="audio/wav")
    for name, value in fields.items():
        if name == "__context_bias__":
            for term in value:
                form.add_field("context_bias", term)
        else:
            form.add_field(name, value)

    headers = {"Authorization": f"Bearer {key}"} if key else None
    async with session.post(f"{base_url}/v1/audio/transcriptions", data=form, headers=headers) as response:
        payload = await response.json(content_type=None)
    # A 401 from a hosted endpoint arrives as a body, not as an exception.
    if isinstance(payload.get("detail"), (str, list)):
        raise RuntimeError(f"transcription failed: {payload['detail']}")
    return (payload.get("text") or "").strip()


def transcribe_options(options: argparse.Namespace) -> dict:
    """The recognizer settings every call site needs, so the three stay in sync."""
    return {
        "model": options.stt_model,
        "dialect": options.stt_dialect,
        "key": options.stt_key,
        "bias": json.loads(options.stt_bias) if options.stt_bias else None,
    }


def build_tts_body(text: str, model: str, voice: str, dialect: str, extra: dict | None = None) -> dict:
    """The speech request body for one sentence.

    Voxtral calls the voice `voice_id` and wraps the audio in JSON; the local servers
    call it `voice` and answer with the bytes themselves.
    """
    body: dict = {"model": model, "response_format": "wav"}
    if dialect == "mistral":
        body["input"] = text
        if voice:
            body["voice_id"] = voice
    else:
        body["input"] = text
        body["voice"] = voice or "default"
    body.update(extra or {})
    return body


async def synthesize(
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    text: str,
    extra: dict,
    voice: str = "",
    dialect: str = "openai",
    key: str = "",
) -> bytes:
    body = build_tts_body(text, model, voice, dialect, extra)
    headers = {"Authorization": f"Bearer {key}"} if key else None
    async with session.post(f"{base_url}/v1/audio/speech", json=body, headers=headers) as response:
        payload = await response.json(content_type=None) if "json" in response.headers.get("content-type", "") else None
        if payload is None:
            response.raise_for_status()
            return await response.read()
    if response.status != 200:
        raise RuntimeError(f"TTS failed with {response.status}: {payload}")
    encoded = payload.get("audio_data")
    if not encoded:
        raise RuntimeError(f"TTS returned no audio_data: {payload}")
    return base64.b64decode(encoded)


def local_ip() -> str:
    """The address the device can reach us on (no traffic is sent)."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: a UDP connect sends nothing
        return sock.getsockname()[0]
    finally:
        sock.close()


def wav_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as handle:
        return handle.getnframes() / handle.getframerate()


def to_flac(wav: bytes, sample_rate: int) -> bytes:
    """The device's media player decodes FLAC only, so convert before serving."""
    process = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "flac",
         "-ar", str(sample_rate), "-ac", "1", "pipe:1"],
        input=wav,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(f"flac conversion failed: {process.stderr.decode('utf-8', 'replace')[:200]}")
    return process.stdout


def merge_wavs(parts: list[bytes]) -> bytes:
    """Concatenate WAV files that share format; the first header wins."""
    if not parts:
        return b""
    with wave.open(io.BytesIO(parts[0]), "rb") as first:
        params = first.getparams()
        frames = [first.readframes(first.getnframes())]
    for part in parts[1:]:
        with wave.open(io.BytesIO(part), "rb") as handle:
            frames.append(handle.readframes(handle.getnframes()))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setparams(params)
        for chunk in frames:
            out.writeframes(chunk)
    return buffer.getvalue()


def split_sentences(text: str, max_chars: int = 160) -> list[str]:
    """Split a reply into speakable pieces so TTS can start before the end is built."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    sentences: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        while len(part) > max_chars:
            cut = part.rfind(" ", 0, max_chars)
            cut = cut if cut > 40 else max_chars
            sentences.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            sentences.append(part)
    return sentences or [text.strip()]


def to_device_pcm(wav: bytes) -> bytes:
    """Resample whatever the TTS server returned to the 16 kHz mono s16 the device wants."""
    process = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "pipe:1"],
        input=wav,
        capture_output=True,
    )
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {process.stderr.decode('utf-8', 'replace')[:300]}")
    return process.stdout


def pcm_chunks(pcm: bytes, ms: int = 100) -> list[bytes]:
    """Split 16 kHz mono s16 PCM into playback sized chunks.

    The device plays what arrives on the API connection as it arrives. Pushing 40
    seconds in one call overruns its buffer, so the audio goes out in 100 ms pieces
    and the sender paces itself against real time.
    """
    size = int(SAMPLE_RATE * 2 * ms / 1000)
    return [pcm[offset:offset + size] for offset in range(0, len(pcm), size)]


def build_answer_argv(command: str, transcript: str) -> list[str]:
    """Split the command template first, then insert the transcript as one argument.

    Splitting the *substituted* string would turn a spoken sentence into several
    arguments: `hermes -z {text}` with "Wie lange kochen?" became
    `hermes -z Wie lange kochen?`, and the agent saw "Wie" as its prompt and the rest as
    trailing arguments. The transcript is data, never part of the command line.
    """
    head, placeholder, tail = command.partition("{text}")
    argv = shlex.split(head)
    if placeholder:
        argv.append(transcript)
        argv.extend(shlex.split(tail))
    else:
        # A template without the placeholder still gets the text, or the agent would
        # be asked the same question forever.
        argv.append(transcript)
    return argv


async def answer(command: str, transcript: str, timeout: float = 180) -> str:
    """Ask the agent (or any command) for a spoken reply.

    The command is split like a shell would but run without a shell, so a transcript
    containing quotes or backticks can never become a second command. That also means
    PATH has to be right: a systemd unit does not inherit an interactive shell's PATH,
    which is why the error names the binary instead of failing silently.
    """
    argv = build_answer_argv(command, transcript)
    if not argv:
        raise RuntimeError("answer_cmd is empty: nothing would produce a reply")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            f"answer command not found: {argv[0]}. Use an absolute path, or set PATH in the "
            f"service that runs this adapter (systemd does not inherit your shell PATH)."
        ) from error
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as error:
        process.kill()
        raise RuntimeError(f"answer command timed out after {timeout:.0f}s: {argv[0]}") from error
    return stdout.decode("utf-8", "replace").strip()


class TtsServer:
    """Serves the generated WAV so the device can fetch it (HA does the same)."""

    def __init__(self) -> None:
        self.runner: web.AppRunner | None = None
        self.port: int = 0
        self.files: dict[str, bytes] = {}

    async def start(self) -> int:
        app = web.Application()
        app.router.add_get("/tts/{name}", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001 - the only way to learn the port
        return self.port

    async def handle(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        data = self.files.get(name)
        emit("http_get", path=request.path, found=data is not None,
             bytes=len(data) if data else 0, agent=request.headers.get("User-Agent", "")[:40])
        if data is None:
            return web.Response(status=404)
        content_type = "audio/flac" if name.endswith(".flac") else "audio/wav"
        return web.Response(body=data, content_type=content_type)

    def publish(self, name: str, data: bytes) -> None:
        self.files[name] = data

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


class Bridge:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.client: APIClient | None = None
        self.turn_done: asyncio.Event | None = None
        self.utterance: Utterance | None = None
        self.sent_listening = False
        self.tts_server = TtsServer()
        self.local_ip = local_ip()
        self.chunks = 0
        self.trace: list[tuple] = []
        # Playback state (media player entity key and the per-turn TTS_END guard)
        self.player_key: int | None = None
        self.tts_end_sent = False
        # Set by main() when SIGTERM/SIGINT arrives; run_daemon watches it so the
        # process cannot outlive the session and keep its subscription open.
        self.stop_event = asyncio.Event()

    async def connect(self) -> None:
        config = load_config(pathlib.Path(self.options.secrets))
        key = config.get("api_key", "")
        self.client = APIClient(self.options.host, self.options.port, None, noise_psk=key or None)
        await self.client.connect(login=True)
        await self.tts_server.start()
        info = await self.client.device_info()
        emit("connected", device=info.name, version=info.esphome_version)

    async def announce_start(self) -> None:
        """The response message must reach the device before any pipeline event,
        otherwise it aborts the turn immediately.

        The order matters for the LED ring: RUN_START opens the run, and the
        firmware's control_leds script switches phase on every event from here.
        """
        await asyncio.sleep(0.3)
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_WAKE_WORD_END)
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_START)

    def event(self, event_type: VoiceAssistantEventType, data: dict[str, str] | None = None) -> None:
        """Emit a pipeline event; the device maps these onto its LED phases."""
        if self.client is not None:
            self.client.send_voice_assistant_event(event_type, data)

    async def run_turn(self) -> None:
        assert self.client is not None
        self.turn_done = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def handle_start(wake_word_phrase: str, flags: int, audio_settings, conversation_id) -> int:
            emit("wake", phrase=wake_word_phrase or "okay nabu", flags=flags)
            self.chunks = 0
            self.sent_listening = False
            self.utterance = Utterance(self.options.silence_ms, self.options.max_seconds, self.options.speech_rms)
            asyncio.get_running_loop().create_task(self.announce_start())
            return 0  # API audio mode: mic in and TTS out on the existing connection

        async def handle_audio(data: bytes, conversation_id) -> None:
            if self.utterance is None or self.turn_done is None or self.turn_done.is_set():
                return

            self.chunks += 1
            was_speaking = self.utterance.speaking
            finished = self.utterance.push(data)

            # The device's LED phases hang off these two events, so they must track
            # what the detector actually sees, not a fixed timer.
            if self.utterance.speaking and not was_speaking and not self.sent_listening:
                self.sent_listening = True
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START)
            if self.chunks == 1 or self.chunks % 50 == 0:
                emit("audio", chunks=self.chunks, seconds=round(self.utterance.seconds, 2),
                     speaking=self.utterance.speaking, rms=round(frame_rms(data), 1),
                     noise=round(self.utterance.noise, 1), threshold=round(self.utterance.threshold(), 1),
                     silence_ms=round(self.utterance.silence_run_ms))
            if finished:
                self.turn_done.set()

        async def handle_stop(aborted: bool) -> None:
            emit("stop", aborted=aborted, seconds=round(self.utterance.seconds, 2) if self.utterance else 0)
            if self.turn_done is not None and not self.turn_done.is_set():
                self.turn_done.set()

        unsubscribe = self.client.subscribe_voice_assistant(
            handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio,
        )

        try:
            await asyncio.wait_for(self.turn_done.wait(), timeout=self.options.timeout)
        except asyncio.TimeoutError:
            emit("error", reason="no wake word within timeout")
            unsubscribe()
            return

        assert self.utterance is not None
        if not self.utterance.speaking:
            pathlib.Path(self.options.dump).write_bytes(self.utterance.wav())
            emit("error", reason="no speech detected", chunks=self.chunks,
                 seconds=round(self.utterance.seconds, 2) if self.utterance else 0)
            unsubscribe()
            return

        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END)   # LED: thinking
        wav = self.utterance.wav()
        pathlib.Path(self.options.dump).write_bytes(wav)                  # keep the raw turn for diagnosis
        async with aiohttp.ClientSession() as http:
            transcript = await transcribe(
                http, self.options.stt_url, wav, self.options.language, **transcribe_options(self.options)
            )
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_END, {"text": transcript})
            emit("transcript", text=transcript, seconds=round(self.utterance.seconds, 2), wav_bytes=len(wav))
            if not transcript:
                emit("error", reason="empty transcript")
                unsubscribe()
                return

            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START, {"text": transcript})
            reply = self.options.answer if self.options.answer else await answer(
                self.options.answer_cmd, transcript, self.options.answer_timeout,
            )
            emit("reply", text=reply[:500])

            # Announce early streaming, exactly like Home Assistant: this is what the
            # firmware's on_intent_progress checks to switch the LED ring to "replying".
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_PROGRESS, {"tts_start_streaming": "1"})
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END, {
                "conversation_id": "",
                "continue_conversation": "False",
                "speech": reply,
            })

            sentences = split_sentences(reply)
            name = f"{int(time.time() * 1000)}.wav"
            parts: list[bytes] = []
            for index, sentence in enumerate(sentences, start=1):
                part = await synthesize(
                    http, self.options.tts_url, self.options.tts_model, sentence, self.options.tts_extra,
                    voice=self.options.tts_voice, dialect=self.options.tts_dialect, key=self.options.tts_key,
                )
                parts.append(part)
                emit("tts", part=index, of=len(sentences), bytes=len(part))

        merged = merge_wavs(parts)
        duration = wav_seconds(merged)
        started = time.monotonic()

        if self.options.audio_mode == "api":
            # Everything stays on the ESPHome connection: no URL, no second port, and
            # therefore nothing the device has to be able to reach. This is the only
            # mode that works when the adapter runs inside a container whose IP is not
            # routable from the LAN.
            pcm = to_device_pcm(merged)
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": reply})
            chunks = pcm_chunks(pcm)
            for index, chunk in enumerate(chunks, start=1):
                self.client.send_voice_assistant_audio(chunk)
                if index % 10 == 0 or index == len(chunks):
                    emit("tts_stream", sent=index, of=len(chunks), bytes=len(chunk) * index)
                await asyncio.sleep(0.08)
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END, {"url": ""})
            emit("tts_played", mode="api", seconds=round(duration, 2), pcm_bytes=len(pcm))
        else:
            flac = to_flac(merged, 48000)
            name = name.replace(".wav", ".flac")
            self.tts_server.publish(name, flac)
            emit("tts_file", flac_bytes=len(flac), seconds=round(duration, 2))

            # The device fetches the file itself and plays it through its media player.
            url = f"http://{self.local_ip}:{self.tts_server.port}/tts/{name}"
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": reply})
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END, {"url": url})
            emit("tts_url", url=url, seconds=round(duration, 2))

        # Keep the run open until the device finished speaking, then close it so the
        # LED ring returns to idle at the right moment.
        await asyncio.sleep(max(0.8, duration + 1.0 - (time.monotonic() - started)))
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
        emit("done", seconds=round(self.utterance.seconds, 2), played=round(duration, 2))
        unsubscribe()

    async def listen_once(self) -> None:
        """Wait for the wake word, capture the sentence, print the transcript, stop."""
        assert self.client is not None
        self.turn_done = asyncio.Event()

        async def handle_start(wake_word_phrase: str, flags: int, audio_settings, conversation_id) -> int:
            emit("wake", phrase=wake_word_phrase or "okay nabu")
            self.chunks = 0
            self.sent_listening = False
            self.utterance = Utterance(self.options.silence_ms, self.options.max_seconds, self.options.speech_rms)
            emit("detector", kind=self.utterance.vad.kind, silence_ms=self.options.silence_ms)
            asyncio.get_running_loop().create_task(self.announce_start())
            return 0

        async def handle_audio(data: bytes, conversation_id) -> None:
            if self.utterance is None or self.turn_done is None or self.turn_done.is_set():
                return
            self.chunks += 1
            was_speaking = self.utterance.speaking
            finished = self.utterance.push(data)
            if self.utterance.speaking and not was_speaking and not self.sent_listening:
                self.sent_listening = True
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START)
            if self.chunks % 25 == 0:
                emit("audio", chunks=self.chunks, seconds=round(self.utterance.seconds, 2),
                     speaking=self.utterance.speaking, silence_ms=round(self.utterance.silence_run_ms),
                     frames=self.utterance.frames_total, speech_frames=self.utterance.frames_speech)
            if finished:
                self.turn_done.set()

        async def handle_stop(aborted: bool) -> None:
            if self.turn_done is not None and not self.turn_done.is_set():
                self.turn_done.set()

        unsubscribe = self.client.subscribe_voice_assistant(
            handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio,
        )
        try:
            await asyncio.wait_for(self.turn_done.wait(), timeout=self.options.timeout)
            if self.utterance is None or not self.utterance.speaking:
                emit("vad_stats", frames=self.utterance.frames_total if self.utterance else 0,
                     speech_frames=self.utterance.frames_speech if self.utterance else 0,
                     seconds=round(self.utterance.seconds, 2) if self.utterance else 0)
                emit("error", reason="no speech detected")
                return
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END)
            wav = self.utterance.wav()
            pathlib.Path(self.options.dump).write_bytes(wav)
            async with aiohttp.ClientSession() as http:
                transcript = await transcribe(
                http, self.options.stt_url, wav, self.options.language, **transcribe_options(self.options)
            )
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_END, {"text": transcript})
            self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
            pathlib.Path("/tmp/pe-trace.json").write_text(json.dumps(self.utterance.trace[:2000]))
            emit("transcript", text=transcript, seconds=round(self.utterance.seconds, 2),
                 reason="silence" if not self.utterance.cap_hit else "max_seconds",
                 dump=self.options.dump,
                 frames=self.utterance.frames_total, speech_frames=self.utterance.frames_speech)
        except asyncio.TimeoutError:
            emit("error", reason="no wake word within timeout")
        finally:
            if callable(unsubscribe):
                unsubscribe()

    async def run_daemon(self) -> None:
        """Keep the wake word armed and answer turns on command from stdin.

        One ESPHome voice-assistant subscription lives for the whole process, so the
        device never disarms its wake word between turns. After a transcript the
        bridge sends STT_VAD_END, which puts the LED ring into its "thinking"
        animation, and waits for a line on stdin:

            {"cmd": "answer", "file": "/tmp/answer.wav"}   speak this file in the session
            {"cmd": "skip"}                                stay silent, back to idle
        """
        assert self.client is not None
        loop = asyncio.get_running_loop()
        inbox: asyncio.Queue[str] = asyncio.Queue()

        def read_stdin() -> None:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    loop.call_soon_threadsafe(inbox.put_nowait, line)

        threading.Thread(target=read_stdin, daemon=True, name="stdin").start()

        self.turn_done = asyncio.Event()

        async def watch_stop() -> None:
            """End the run and the process when SIGTERM arrives.

            SIGTERM used to only set an event nobody awaited, so the daemon kept its
            voice-assistant subscription after the session asked it to go away. The
            next session then had two subscribers, and the microphone audio was split
            between them (empty and duplicated transcripts).
            """
            await self.stop_event.wait()
            try:
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
            finally:
                sys.stdout.flush()
                os._exit(0)

        asyncio.get_running_loop().create_task(watch_stop())

        async def handle_start(wake_word_phrase: str, flags: int, audio_settings, conversation_id) -> int:
            emit("wake", phrase=wake_word_phrase or "okay nabu")
            self.chunks = 0
            self.sent_listening = False
            self.utterance = Utterance(self.options.silence_ms, self.options.max_seconds, self.options.speech_rms)
            self.tts_end_sent = False
            self.turn_done.clear()
            asyncio.get_running_loop().create_task(self.announce_start())
            return 0

        async def handle_audio(data: bytes, conversation_id) -> None:
            if self.utterance is None or self.turn_done.is_set():
                return
            was_speaking = self.utterance.speaking
            finished = self.utterance.push(data)
            if self.utterance.speaking and not was_speaking and not self.sent_listening:
                self.sent_listening = True
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START)
            if finished:
                self.turn_done.set()

        async def handle_stop(aborted: bool) -> None:
            self.turn_done.set()

        unsubscribe = self.client.subscribe_voice_assistant(
            handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio,
        )
        # A previous run may have been killed mid-turn: close it so the LED ring
        # falls back to idle instead of blinking forever.
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
        emit("armed", silence_ms=self.options.silence_ms, max_seconds=self.options.max_seconds)

        try:
            while True:
                self.turn_done.clear()
                self.utterance = None
                try:
                    await asyncio.wait_for(self.turn_done.wait(), timeout=self.options.timeout)
                except asyncio.TimeoutError:
                    emit("idle", reason="no wake word; still armed")
                    continue

                if self.utterance is None or not self.utterance.speaking:
                    emit("idle", reason="no speech detected")
                    continue

                # LED: thinking — the device keeps this phase until the turn ends.
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END)
                wav = self.utterance.wav()
                pathlib.Path(self.options.dump).write_bytes(wav)
                async with aiohttp.ClientSession() as http:
                    transcript = await transcribe(
                http, self.options.stt_url, wav, self.options.language, **transcribe_options(self.options)
            )
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_END, {"text": transcript})
                emit("transcript", text=transcript, seconds=round(self.utterance.seconds, 2),
                     reason="silence" if not self.utterance.cap_hit else "max_seconds",
                     dump=self.options.dump, frames=self.utterance.frames_total,
                     speech_frames=self.utterance.frames_speech)
                if not transcript:
                    self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
                    continue

                command: dict = {"cmd": "skip"}
                try:
                    line = await asyncio.wait_for(inbox.get(), timeout=self.options.answer_timeout)
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        command = parsed
                except asyncio.TimeoutError:
                    emit("idle", reason="no answer within timeout")
                except json.JSONDecodeError:
                    emit("idle", reason="unparsable command")

                if command.get("cmd") == "answer" and command.get("file"):
                    await self.play_in_session(pathlib.Path(str(command["file"])), transcript)
                self.event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)
                emit("turn_end")
        finally:
            if callable(unsubscribe):
                unsubscribe()

    async def play_in_session(self, wav_path: pathlib.Path, transcript: str) -> None:
        """Speak a file inside the open run, and wait for the device to finish.

        Only one TTS_END is sent (the one carrying the URL): a second one without a
        URL stops the playback in the middle of a sentence. The end of the playback
        comes from the media player state instead of an estimate, because the device
        fetches the file first and starts later.
        """
        assert self.client is not None
        if not wav_path.exists():
            emit("error", reason=f"answer file missing: {wav_path}")
            return

        if self.player_key is None:
            entities, _ = await self.client.list_entities_services()
            from aioesphomeapi import MediaPlayerInfo
            player = next((entity for entity in entities if isinstance(entity, MediaPlayerInfo)), None)
            self.player_key = player.key if player else None
        if self.player_key is None:
            emit("error", reason="device has no media player to speak through")
            return

        wav = wav_path.read_bytes()
        duration = wav_seconds(wav)
        flac = to_flac(wav, 48000)
        name = f"{int(time.time() * 1000)}.flac"
        self.tts_server.publish(name, flac)
        url = f"http://{self.local_ip}:{self.tts_server.port}/tts/{name}"

        if self.tts_end_sent:
            emit("error", reason="refusing a second TTS_END in the same turn")
            return
        self.tts_end_sent = True

        finished = asyncio.Event()
        started = False

        def on_state(state) -> None:
            nonlocal started
            if getattr(state, "key", None) != self.player_key:
                return
            current = str(getattr(state, "state", ""))
            emit("player_state", state=current)
            if current == "2":       # PLAYING
                started = True
            elif current == "1" and started:   # IDLE again
                finished.set()

        unsubscribe = self.client.subscribe_states(on_state)
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": transcript[:200]})
        self.event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END, {"url": url})
        emit("playing", url=url, seconds=round(duration, 2))

        try:
            await asyncio.wait_for(finished.wait(), timeout=duration + 30)
            emit("played", seconds=round(duration, 2))
        except asyncio.TimeoutError:
            emit("error", reason="device never reported the end of playback")
        finally:
            if callable(unsubscribe):
                unsubscribe()

    async def say_file(self, wav_path: pathlib.Path, volume: float | None) -> None:
        """Play a WAV through the device's media player (no voice-assistant session)."""
        assert self.client is not None
        from aioesphomeapi import MediaPlayerCommand, MediaPlayerInfo

        entities, _ = await self.client.list_entities_services()
        player = next((entity for entity in entities if isinstance(entity, MediaPlayerInfo)), None)
        if player is None:
            emit("error", reason="device has no media player")
            return

        wav = pathlib.Path(wav_path).read_bytes()
        duration = wav_seconds(wav)
        flac = to_flac(wav, 48000)
        name = f"{int(time.time() * 1000)}.flac"
        self.tts_server.publish(name, flac)

        stopped = asyncio.Event()

        def on_state(state) -> None:
            if getattr(state, "key", None) != player.key:
                return
            current = str(getattr(state, "state", ""))
            emit("player_state", state=current, volume=getattr(state, "volume", None))
            # 1 = IDLE, 2 = PLAYING
            if current == "1" and self.played_once:
                stopped.set()
            if current == "2":
                self.played_once = True

        self.played_once = False
        self.client.subscribe_states(on_state)

        if volume is not None:
            self.client.media_player_command(player.key, volume=volume)

        url = f"http://{self.local_ip}:{self.tts_server.port}/tts/{name}"
        self.client.media_player_command(player.key, command=MediaPlayerCommand.PLAY, media_url=url)
        emit("playing", url=url, seconds=round(duration, 2), flac_bytes=len(flac))

        try:
            await asyncio.wait_for(stopped.wait(), timeout=duration + 15)
        except asyncio.TimeoutError:
            emit("error", reason="device did not report the end of playback")
        emit("done", played_seconds=round(duration, 2))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()


DEFAULT_STT_BASE = "https://api.mistral.ai"
DEFAULT_STT_MODEL = "voxtral-mini-transcribe-3"
DEFAULT_TTS_BASE = "https://api.mistral.ai"
DEFAULT_TTS_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_ANSWER_CMD = "hermes -z {text}"

#: Vocabulary the recognizer is biased towards. Costs nothing, fixes the spelling of
#: the terms that are actually said out loud on this machine.
DEFAULT_BIAS = [
    "Hermes", "Pi", "TypeScript", "Node.js", "Python", "JSON", "API", "Commit",
    "Repository", "Docker", "Container", "Terminal", "git", "npm", "Pull Request", "Branch",
]


def build_parser() -> argparse.ArgumentParser:
    home = pathlib.Path.home()
    key = os.environ.get("MISTRAL_API_KEY", "")
    parser = argparse.ArgumentParser(description="Voice adapter between an ESPHome voice device and Hermes Agent")
    parser.add_argument("--config", default="",
                        help="JSON file with defaults; explicit flags still win")
    parser.add_argument("--host", default="voice-pe.local")
    parser.add_argument("--port", type=int, default=6053)
    parser.add_argument("--secrets", default=str(home / ".config" / "voice-pe" / "secrets.yaml"))
    parser.add_argument("--stt-url", default=DEFAULT_STT_BASE)
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL)
    parser.add_argument("--stt-dialect", default="mistral", choices=["openai", "mistral"])
    parser.add_argument("--stt-key", default=os.environ.get("MISTRAL_API_KEY", ""),
                        help="bearer token for a hosted recognizer; prefer the environment over argv")
    parser.add_argument("--stt-bias", default=json.dumps(DEFAULT_BIAS),
                        help="JSON array of vocabulary keyphrases (prompt for openai, context_bias for mistral)")
    parser.add_argument("--tts-url", default=DEFAULT_TTS_BASE)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--tts-dialect", default="mistral", choices=["openai", "mistral"])
    parser.add_argument("--tts-voice", default=os.environ.get("MISTRAL_TTS_VOICE", ""),
                        help="voice id or name: a preset slug such as voice_481, or the uuid of a saved voice")
    parser.add_argument("--tts-key", default=os.environ.get("MISTRAL_API_KEY", ""),
                        help="bearer token for a hosted voice; prefer the environment over argv")
    parser.add_argument("--language", default="de")
    parser.add_argument("--silence-ms", type=int, default=900)
    parser.add_argument("--speech-rms", type=float, default=300.0, help="absolute RMS floor for speech")
    parser.add_argument("--max-seconds", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=120.0, help="how long to wait for a wake word")
    parser.add_argument("--answer-timeout", type=float, default=600.0, help="how long to wait for the answer")
    parser.add_argument("--answer", default="", help="fixed answer instead of running --answer-cmd")
    parser.add_argument("--answer-cmd", default=DEFAULT_ANSWER_CMD, help="command that produces the spoken answer")
    parser.add_argument("--tts-extra", default="", help="JSON merged into the TTS request")
    parser.add_argument("--audio-mode", default="api", choices=["api", "url"],
                        help="api streams PCM over the ESPHome connection (works in a container); "
                             "url serves a FLAC file the device fetches (needs a reachable IP)")
    parser.add_argument("--dump", default="/tmp/pe-last.wav", help="always write the raw turn here")
    parser.add_argument("--debug", action="store_true", help="log library traffic to stderr")
    parser.add_argument("--file", default="", help="WAV to play in say mode")
    parser.add_argument("--volume", type=float, default=None,
                        help="media player volume before playing, 0..1 (omitted = leave as is)")
    parser.add_argument("mode", choices=("turn", "daemon", "probe", "say", "listen"), nargs="?", default="turn")
    return parser


def resolve_path(value: str) -> str:
    """Expand ~ and environment variables, then make the path absolute.

    A config file is written by a human, so it contains "~/.config/voice-pe/secrets.yaml".
    pathlib does not expand that on its own, and a relative path breaks as soon as the
    working directory changes (systemd units start in /).
    """
    if not value:
        return value
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))


def normalize_paths(options: argparse.Namespace) -> argparse.Namespace:
    options.secrets = resolve_path(options.secrets)
    options.dump = resolve_path(options.dump)
    if options.file:
        options.file = resolve_path(options.file)
    return options


def apply_config_defaults(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    """Fill the parser defaults from --config, leaving every explicit flag untouched.

    A config file keeps a device name, a voice id and an answer command out of the
    command line, which is what a systemd unit or a container wants.
    """
    if "--config" not in argv:
        return
    index = argv.index("--config")
    if index + 1 >= len(argv):
        return
    path = pathlib.Path(argv[index + 1])
    if not path.is_file():
        print(f"config file not found: {path}", file=sys.stderr)
        return
    data = json.loads(path.read_text())
    defaults: dict = {}
    for action in parser._actions:
        if action.dest not in data:
            continue
        value = data[action.dest]
        if isinstance(value, (list, dict)):
            value = json.dumps(value)
        elif action.type in (int, float) and not isinstance(value, bool):
            value = action.type(value)
        elif isinstance(value, bool):
            value = value
        else:
            value = str(value)
        defaults[action.dest] = value
    parser.set_defaults(**defaults)


async def main() -> int:
    parser = build_parser()
    apply_config_defaults(parser, sys.argv[1:])
    options = normalize_paths(parser.parse_args())

    # An aborted tool call (Esc) must leave the device in idle, not in "thinking".
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    if options.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    options.tts_extra = json.loads(options.tts_extra) if options.tts_extra else {}
    bridge = Bridge(options)
    bridge.stop_event = stop

    try:
        await bridge.connect()
        if options.mode == "say":
            if not options.file:
                emit("error", reason="say needs --file")
                return 1
            await bridge.say_file(pathlib.Path(options.file), options.volume)
            return 0
        if options.mode == "listen":
            await bridge.listen_once()
            return 0
        if options.mode == "probe":
            emit("probe", host=options.host)
            return 0
        if options.mode == "daemon":
            if not acquire_pid_file():
                emit("error", reason=f"another bridge already owns {PID_FILE}")
                return 1
            try:
                await bridge.run_daemon()
            finally:
                release_pid_file()
            return 0
        await bridge.run_turn()
        return 0
    except Exception as error:  # noqa: BLE001 - the extension reads the JSON, not a traceback
        emit("error", reason=str(error)[:300])
        return 1
    finally:
        await bridge.close()


def run() -> int:
    """Console-script entry point (`hermes-pe ...`)."""
    return asyncio.run(main())


if __name__ == "__main__":
    sys.exit(run())
