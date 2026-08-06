from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx

from .config import MODEL_DIR, TEMP_DIR
from .db import connect, utcnow
from .security import decrypt_json, encrypt_json

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" — a safe default until Tommy picks his own


class VoiceBackendError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _elevenlabs_config() -> dict[str, Any] | None:
    from .integrations import get_app_config  # local import: integrations imports nothing from voice, but this keeps the module graph one-directional

    return get_app_config("elevenlabs")


def elevenlabs_configured() -> bool:
    cfg = _elevenlabs_config()
    return bool(cfg and cfg.get("api_key"))


_MD_BOLD_ITALIC = re.compile(r"\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*")
_MD_HEADER = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_MD_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_WHITESPACE = re.compile(r"[ \t]{2,}")


def strip_markdown_for_speech(text: str) -> str:
    """The same answer text gets shown on screen *and* spoken — screen
    formatting (bold, bullets, backticks, headers) has no business reaching
    a TTS engine; read aloud it comes out as noise ("asterisk asterisk OK
    asterisk asterisk"), not silence. This does not fix an assistant that
    answers in a bulleted status-report style in the first place — see the
    system prompt for that — it just keeps literal markup out of the audio."""
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_BOLD_ITALIC.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    text = _MD_WHITESPACE.sub(" ", text)
    return text.strip()


async def elevenlabs_synthesize(text: str, voice_id: str | None = None) -> bytes:
    cfg = _elevenlabs_config()
    if not cfg or not cfg.get("api_key"):
        raise VoiceBackendError("not_configured", "ElevenLabs is not configured — add an API key in Integrations")
    text = strip_markdown_for_speech(text)
    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id or cfg.get("default_voice_id") or ELEVENLABS_DEFAULT_VOICE_ID)
    # Without voice_settings ElevenLabs falls back to a flat, low-expressiveness
    # default that reads as robotic/grating — these are the values ElevenLabs'
    # own docs recommend as a natural-sounding starting point, not the raw API
    # default. All four are user-overridable via system_settings("voice").
    settings = {
        "stability": float(cfg.get("stability", 0.45)),
        "similarity_boost": float(cfg.get("similarity_boost", 0.8)),
        "style": float(cfg.get("style", 0.35)),
        "use_speaker_boost": bool(cfg.get("use_speaker_boost", True)),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                url,
                headers={"xi-api-key": cfg["api_key"], "Accept": "audio/mpeg", "Content-Type": "application/json"},
                json={"text": text, "model_id": cfg.get("model_id", "eleven_multilingual_v2"), "voice_settings": settings},
            )
    except httpx.RequestError as exc:
        raise VoiceBackendError("provider_unavailable", str(exc)) from exc
    if response.status_code >= 400:
        raise VoiceBackendError("error", f"ElevenLabs TTS returned HTTP {response.status_code}: {response.text[:300]}")
    return response.content


async def elevenlabs_transcribe(path: Path, language: str | None = None) -> dict[str, Any]:
    cfg = _elevenlabs_config()
    if not cfg or not cfg.get("api_key"):
        raise VoiceBackendError("not_configured", "ElevenLabs is not configured — add an API key in Integrations")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle, "audio/wav")}
                data = {"model_id": "scribe_v1"}
                if language:
                    data["language_code"] = language
                response = await client.post(ELEVENLABS_STT_URL, headers={"xi-api-key": cfg["api_key"]}, files=files, data=data)
    except httpx.RequestError as exc:
        raise VoiceBackendError("provider_unavailable", str(exc)) from exc
    if response.status_code >= 400:
        raise VoiceBackendError("error", f"ElevenLabs STT returned HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    return {"text": payload.get("text", ""), "language": payload.get("language_code"), "language_probability": payload.get("language_probability")}

_whisper_models: dict[tuple[str, str, str], Any] = {}
_whisper_lock = threading.Lock()
_speaker_model = None
_speaker_lock = threading.Lock()


def runtime_status() -> dict[str, Any]:
    packages = {
        "faster_whisper": importlib.util.find_spec("faster_whisper") is not None,
        "piper": importlib.util.find_spec("piper") is not None,
        "speechbrain": importlib.util.find_spec("speechbrain") is not None,
        "torch": importlib.util.find_spec("torch") is not None,
        "torchaudio": importlib.util.find_spec("torchaudio") is not None,
    }
    local_ready = all(packages.values())
    elevenlabs_ready = elevenlabs_configured()
    return {
        "status": "ready" if (local_ready or elevenlabs_ready) else "error",
        "backend": "elevenlabs" if elevenlabs_ready else ("local" if local_ready else "unavailable"),
        "elevenlabs_configured": elevenlabs_ready,
        "packages": packages,
        "models": {
            "whisper_cached": (MODEL_DIR / "whisper").exists() and any((MODEL_DIR / "whisper").iterdir()),
            "piper_cached": (MODEL_DIR / "piper").exists() and any((MODEL_DIR / "piper").glob("*.onnx")),
            "speechbrain_cached": (MODEL_DIR / "speechbrain").exists() and any((MODEL_DIR / "speechbrain").iterdir()),
        },
    }


def _whisper_model(model_name: str, device: str, compute_type: str):
    key = (model_name, device, compute_type)
    if key in _whisper_models:
        return _whisper_models[key]
    with _whisper_lock:
        if key not in _whisper_models:
            from faster_whisper import WhisperModel
            _whisper_models[key] = WhisperModel(model_name, device=device, compute_type=compute_type, download_root=str(MODEL_DIR / "whisper"))
    return _whisper_models[key]


def _transcribe_sync(path: Path, model_name: str, device: str, compute_type: str, language: str | None) -> dict[str, Any]:
    model = _whisper_model(model_name, device, compute_type)
    segments, info = model.transcribe(str(path), language=language or None, vad_filter=True)
    items = []
    for segment in segments:
        items.append({"start": float(segment.start), "end": float(segment.end), "text": segment.text.strip()})
    return {"text": " ".join(item["text"] for item in items).strip(), "language": info.language, "language_probability": float(info.language_probability), "segments": items}


async def transcribe(path: Path, model_name: str = "small", device: str = "cpu", compute_type: str = "int8", language: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_transcribe_sync, path, model_name, device, compute_type, language)


def _piper_voice_files(voice: str) -> tuple[Path, Path]:
    directory = MODEL_DIR / "piper"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{voice}.onnx", directory / f"{voice}.onnx.json"


def _ensure_piper_voice(voice: str) -> tuple[Path, Path]:
    model, config = _piper_voice_files(voice)
    if model.exists() and config.exists():
        return model, config
    command = [sys.executable, "-m", "piper.download_voices", "--data-dir", str(model.parent), voice]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    if completed.returncode != 0 or not model.exists() or not config.exists():
        raise RuntimeError(f"Piper voice download failed: {completed.stderr[-1000:]}")
    return model, config


def _synthesize_sync(text: str, voice: str, speaker_id: int | None, length_scale: float) -> Path:
    model, _ = _ensure_piper_voice(voice)
    output = TEMP_DIR / f"tts-{uuid.uuid4().hex}.wav"
    from piper import PiperVoice, SynthesisConfig

    piper_voice = PiperVoice.load(str(model))
    synthesis_config = SynthesisConfig(length_scale=length_scale, speaker_id=speaker_id)
    with wave.open(str(output), "wb") as wav_file:
        piper_voice.synthesize_wav(text, wav_file, syn_config=synthesis_config)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Piper synthesis did not produce audio")
    return output


async def synthesize(text: str, voice: str = "el_GR-rapunzelina-low", speaker_id: int | None = None, length_scale: float = 1.0) -> Path:
    return await asyncio.to_thread(_synthesize_sync, strip_markdown_for_speech(text), voice, speaker_id, length_scale)


def _load_speaker_model():
    global _speaker_model
    if _speaker_model is not None:
        return _speaker_model
    with _speaker_lock:
        if _speaker_model is None:
            from speechbrain.inference.classifiers import EncoderClassifier
            _speaker_model = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=str(MODEL_DIR / "speechbrain"), run_opts={"device": "cpu"})
    return _speaker_model


def _speaker_embedding_sync(path: Path) -> list[float]:
    import torch
    import torchaudio

    signal, sample_rate = torchaudio.load(str(path))
    if signal.shape[0] > 1:
        signal = signal.mean(dim=0, keepdim=True)
    if sample_rate != 16000:
        signal = torchaudio.functional.resample(signal, sample_rate, 16000)
    model = _load_speaker_model()
    with torch.no_grad():
        embedding = model.encode_batch(signal).squeeze().cpu().tolist()
    return [float(x) for x in embedding]


async def speaker_embedding(path: Path) -> list[float]:
    return await asyncio.to_thread(_speaker_embedding_sync, path)


def _normalize(vector: list[float]) -> list[float]:
    norm = sum(x * x for x in vector) ** 0.5
    return [x / max(norm, 1e-12) for x in vector]


def _average(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        raise ValueError("At least one voice sample is required")
    length = len(vectors[0])
    if any(len(v) != length for v in vectors):
        raise ValueError("Voice embeddings have inconsistent dimensions")
    return _normalize([sum(v[i] for v in vectors) / len(vectors) for i in range(length)])


def _similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return -1.0
    aa = _normalize(a)
    bb = _normalize(b)
    return sum(x * y for x, y in zip(aa, bb))


async def enroll_voice(user_id: str, paths: list[Path], threshold: float = 0.65) -> dict[str, Any]:
    vectors = [await speaker_embedding(path) for path in paths]
    embedding = _average(vectors)
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO voiceprints(user_id,embedding_enc,sample_count,threshold,consent_at,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET embedding_enc=excluded.embedding_enc,sample_count=excluded.sample_count,threshold=excluded.threshold,consent_at=excluded.consent_at,updated_at=excluded.updated_at""",
            (user_id, encrypt_json(embedding), len(paths), threshold, now, now),
        )
    return {"status": "enrolled", "sample_count": len(paths), "threshold": threshold}


async def verify_voice(user_id: str, path: Path) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM voiceprints WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return {"status": "authorization_required", "verified": False, "reason": "voice_not_enrolled"}
    enrolled = decrypt_json(row["embedding_enc"])
    current = await speaker_embedding(path)
    score = _similarity(enrolled, current)
    threshold = float(row["threshold"])
    return {"status": "ready", "verified": score >= threshold, "score": score, "threshold": threshold}


def remove_voiceprint(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM voiceprints WHERE user_id=?", (user_id,))
