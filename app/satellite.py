"""Always-on wake-word satellite protocol.

A satellite (ESP32-S3, Raspberry Pi, whatever) holds a per-device token
(security.create_satellite_token) and opens exactly one websocket:

    wss://<host>/ws/voice/satellite?token=<token>

It streams raw 16-bit PCM mono audio at 16000 Hz as binary frames, at
whatever chunk size is convenient (recommended: ~100-300ms per frame). Two
control messages exist, sent as JSON text frames:

    {"event": "end_of_command"}   — the satellite decided the user stopped
                                     talking (its own VAD); ends capture early
    {"event": "ping"}             — keepalive; server replies {"event":"pong"}

Server → satellite events (JSON text frames), in the order they occur:

    {"event": "wake_detected"}
    {"event": "listening"}         — command capture has started
    {"event": "transcript", "text": "..."}
    {"event": "answer", "text": "..."}   — followed by a binary audio frame
                                            (mp3 if ElevenLabs is configured,
                                            wav otherwise) if TTS succeeded
    {"event": "error", "message": "..."}
    {"event": "idle"}              — back to listening for the wake phrase

This module owns the state machine; main.py just accepts the websocket and
hands the connection to run_satellite_session().
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
import uuid
import wave
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .config import TEMP_DIR
from .orchestrator import ask
from .voice import VoiceBackendError, elevenlabs_configured, elevenlabs_synthesize, elevenlabs_transcribe, transcribe

logger = logging.getLogger("athena.satellite")

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit PCM
WAKE_CHECK_WINDOW_SECONDS = 2.5
MAX_COMMAND_SECONDS = 15
SILENCE_TIMEOUT_SECONDS = 2.0  # if no new audio arrives this long during capture, treat it as end-of-command


def _write_wav(pcm: bytes, path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)


async def _transcribe_best_effort(pcm: bytes, language: str | None = "el", model_name: str = "tiny") -> str | None:
    # language defaults to Greek, not None/auto-detect: on short, often noisy
    # 2-3s clips (exactly what both the wake-word check and a spoken command
    # are), Whisper's language auto-detection is unreliable and was the main
    # reason wake-word/command recognition felt broken — it would frequently
    # guess the wrong language on a short "Αθηνά" and transcribe garbage.
    if not pcm:
        return None
    path = TEMP_DIR / f"satellite-{uuid.uuid4().hex}.wav"
    _write_wav(pcm, path)
    try:
        if elevenlabs_configured():
            try:
                result = await elevenlabs_transcribe(path, language)
                return result.get("text", "")
            except VoiceBackendError as exc:
                logger.warning("satellite: ElevenLabs STT failed (%s), trying local", exc.message)
        try:
            result = await transcribe(path, model_name=model_name, language=language)
            return result.get("text", "")
        except Exception as exc:
            logger.warning("satellite: local STT unavailable: %s", exc)
            return None
    finally:
        path.unlink(missing_ok=True)


SPEECH_CHARS_PER_SECOND = 14.0  # rough Greek TTS speaking rate, used only to size the barge-in guard below
MIN_SPEAKING_HOLD_SECONDS = 1.0


async def _speak_back(ws: WebSocket, text: str) -> float:
    """Sends the TTS reply and returns how many seconds the caller should
    keep ignoring incoming mic audio for — sending the bytes only queues
    them, it does not wait for the satellite to actually finish playing
    them out loud, so without this the wake-word check would resume
    immediately and can hear the tail of ATHENA's own answer, mistake it
    for a new wake phrase, and answer itself again (reported live as
    "speaks again before finishing, sounds like two voices at once")."""
    if not text or not elevenlabs_configured():
        return 0.0
    try:
        audio = await elevenlabs_synthesize(text)
        await ws.send_bytes(audio)
        return max(MIN_SPEAKING_HOLD_SECONDS, len(text) / SPEECH_CHARS_PER_SECOND)
    except VoiceBackendError as exc:
        logger.info("satellite: TTS reply skipped (%s)", exc.message)
        return 0.0


def _seconds_of_pcm(buf: bytes) -> float:
    return len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH)


async def run_satellite_session(ws: WebSocket, user: dict[str, Any], wake_phrase: str, *, language: str = "el", wake_model: str = "tiny", command_model: str = "small") -> None:
    await ws.accept()
    await ws.send_json({"event": "idle"})
    state = "listening"
    rolling = bytearray()
    command = bytearray()
    # While now < listen_after, ATHENA is still (or is estimated to still be)
    # speaking its previous answer — incoming mic audio is discarded rather
    # than wake-checked, so the satellite can't hear its own voice, decide
    # that was "Αθηνά" again, and answer itself on top of the answer still
    # playing (reported live as "starts talking again before it's done,
    # sounds like two voices at once").
    listen_after = 0.0

    async def receive_with_timeout(timeout: float | None):
        try:
            if timeout is None:
                return await ws.receive()
            return await asyncio.wait_for(ws.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    try:
        while True:
            message = await receive_with_timeout(SILENCE_TIMEOUT_SECONDS if state == "capturing" else None)

            if message is None:
                # Silence timeout during capture — the user stopped talking.
                if state == "capturing":
                    listen_after = time.monotonic() + await _finish_command(ws, user, command, language, command_model)
                    command = bytearray()
                    state = "listening"
                    rolling.clear()
                continue

            if message["type"] == "websocket.disconnect":
                break

            if "text" in message and message["text"] is not None:
                try:
                    import json

                    payload = json.loads(message["text"])
                except ValueError:
                    continue
                if payload.get("event") == "ping":
                    await ws.send_json({"event": "pong"})
                elif payload.get("event") == "end_of_command" and state == "capturing":
                    listen_after = time.monotonic() + await _finish_command(ws, user, command, language, command_model)
                    command = bytearray()
                    state = "listening"
                    rolling.clear()
                continue

            chunk = message.get("bytes")
            if not chunk:
                continue

            if state == "listening":
                if time.monotonic() < listen_after:
                    continue  # still speaking (or estimated to be) — drop this frame, don't even buffer it
                rolling.extend(chunk)
                if _seconds_of_pcm(rolling) >= WAKE_CHECK_WINDOW_SECONDS:
                    heard = await _transcribe_best_effort(bytes(rolling), language, wake_model)
                    if heard and wake_phrase.casefold() in heard.casefold():
                        await ws.send_json({"event": "wake_detected"})
                        await ws.send_json({"event": "listening"})
                        state = "capturing"
                        command.clear()
                    else:
                        # Slide the window instead of clearing it outright, so a
                        # wake phrase spoken right at a boundary is not missed.
                        keep_from = max(0, len(rolling) - int(SAMPLE_RATE * SAMPLE_WIDTH * 1.0))
                        rolling = bytearray(rolling[keep_from:])
            else:  # capturing
                command.extend(chunk)
                if _seconds_of_pcm(command) >= MAX_COMMAND_SECONDS:
                    listen_after = time.monotonic() + await _finish_command(ws, user, command, language, command_model)
                    command = bytearray()
                    state = "listening"
                    rolling.clear()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("satellite session crashed for user %s", user.get("id"))
        try:
            await ws.send_json({"event": "error", "message": "internal error"})
        except Exception:
            pass


async def _finish_command(ws: WebSocket, user: dict[str, Any], command: bytearray, language: str = "el", model_name: str = "small") -> float:
    """Returns how many seconds the caller should keep ignoring mic input for
    (see listen_after in run_satellite_session) — 0.0 if nothing was spoken
    back, so the caller can resume wake-checking immediately."""
    text = await _transcribe_best_effort(bytes(command), language, model_name)
    if not text or not text.strip():
        await ws.send_json({"event": "idle"})
        return 0.0
    await ws.send_json({"event": "transcript", "text": text})
    hold_seconds = 0.0
    try:
        result = await ask(user, text, voice=True)
        answer = result.get("answer", "")
    except Exception as exc:
        logger.exception("satellite: ask() failed")
        answer = ""
        await ws.send_json({"event": "error", "message": str(exc)})
    if answer:
        await ws.send_json({"event": "answer", "text": answer})
        hold_seconds = await _speak_back(ws, answer)
    await ws.send_json({"event": "idle"})
    return hold_seconds
