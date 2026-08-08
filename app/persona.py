"""Per-user override of how ATHENA presents itself — a different assistant
name, a short personality note, and/or a different ElevenLabs voice/avatar
image, per family member (2026-08-08, requested by Tasos). Small and
deliberately separate from app/voice.py (which is about the STT/TTS backends
themselves, not who wants which name/voice) and app/security.py (device
tokens, not preferences) — same reasoning as app/creative.py and
app/health.py being their own focused modules rather than growing an
existing one.

Voice and avatar choices are deliberately a curated admin-picked list (see
persona_voice_choices/persona_avatar_choices in system_settings), not a free
text field — a family member (especially a kid) picks from a small approved
set rather than pasting in an arbitrary ElevenLabs voice ID or image URL
from anywhere on the internet.
"""
from __future__ import annotations

from .db import connect, utcnow

DEFAULT_ASSISTANT_NAME = "ATHENA"


def get_persona(user_id: str) -> dict[str, str]:
    with connect() as conn:
        row = conn.execute(
            "SELECT assistant_name, persona_note, voice_id, avatar_url FROM user_personas WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return {"assistant_name": "", "persona_note": "", "voice_id": "", "avatar_url": ""}
    return {
        "assistant_name": row["assistant_name"], "persona_note": row["persona_note"],
        "voice_id": row["voice_id"], "avatar_url": row["avatar_url"],
    }


def set_persona(user_id: str, assistant_name: str = "", persona_note: str = "", voice_id: str = "", avatar_url: str = "") -> dict[str, str]:
    with connect() as conn:
        conn.execute(
            """INSERT INTO user_personas(user_id,assistant_name,persona_note,voice_id,avatar_url,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 assistant_name=excluded.assistant_name, persona_note=excluded.persona_note,
                 voice_id=excluded.voice_id, avatar_url=excluded.avatar_url, updated_at=excluded.updated_at""",
            (user_id, assistant_name.strip()[:40], persona_note.strip()[:500], voice_id.strip()[:120], avatar_url.strip()[:500], utcnow()),
        )
    return get_persona(user_id)


def display_name(user_id: str) -> str:
    """The name to introduce ATHENA as, in this user's system prompt — falls
    back to the product's real name if they haven't set their own."""
    return get_persona(user_id)["assistant_name"] or DEFAULT_ASSISTANT_NAME
