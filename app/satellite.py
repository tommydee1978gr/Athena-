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


async def _transcribe_best_effort(pcm: bytes, language: str | None = None) -> str | None:
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
            result = await transcribe(path, model_name="tiny", language=language)
            return result.get("text", "")
        except Exception as exc:
            logger.warning("satellite: local STT unavailable: %s", exc)
            return None
    finally:
        path.unlink(missing_ok=True)


async def _speak_back(ws: WebSocket, text: str) -> None:
    if not text or not elevenlabs_configured():
        return
    try:
        audio = await elevenlabs_synthesize(text)
        await ws.send_bytes(audio)
    except VoiceBackendError as exc:
        logger.info("satellite: TTS reply skipped (%s)", exc.message)


def _seconds_of_pcm(buf: bytes) -> float:
    return len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH)


async def run_satellite_session(ws: WebSocket, user: dict[str, Any], wake_phrase: str) -> None:
    await ws.accept()
    await ws.send_json({"event": "idle"})
    state = "listening"
    rolling = bytearray()
    command = bytearray()

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
                    await _finish_command(ws, user, command)
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
                    await _finish_command(ws, user, command)
                    command = bytearray()
                    state = "listening"
                    rolling.clear()
                continue

            chunk = message.get("bytes")
            if not chunk:
                continue

            if state == "listening":
                rolling.extend(chunk)
                if _seconds_of_pcm(rolling) >= WAKE_CHECK_WINDOW_SECONDS:
                    heard = await _transcribe_best_effort(bytes(rolling))
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
                    await _finish_command(ws, user, command)
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


async def _finish_command(ws: WebSocket, user: dict[str, Any], command: bytearray) -> None:
    text = await _transcribe_best_effort(bytes(command))
    if not text or not text.strip():
        await ws.send_json({"event": "idle"})
        return
    await ws.send_json({"event": "transcript", "text": text})
    try:
        result = await ask(user, text)
        answer = result.get("answer", "")
    except Exception as exc:
        logger.exception("satellite: ask() failed")
        answer = ""
        await ws.send_json({"event": "error", "message": str(exc)})
    if answer:
        await ws.send_json({"event": "answer", "text": answer})
        await _speak_back(ws, answer)
    await ws.send_json({"event": "idle"})
