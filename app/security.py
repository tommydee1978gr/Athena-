from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import MASTER_KEY_PATH, SESSION_TTL_SECONDS
from .db import connect, utcnow

_password_hasher = PasswordHasher()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def password_hash(password: str) -> str:
    return _password_hasher.hash(password)


def password_verify(encoded: str, password: str) -> bool:
    try:
        return _password_hasher.verify(encoded, password)
    except VerifyMismatchError:
        return False


def _fernet() -> Fernet:
    MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not MASTER_KEY_PATH.exists():
        MASTER_KEY_PATH.write_bytes(Fernet.generate_key())
        os.chmod(MASTER_KEY_PATH, 0o600)
    return Fernet(MASTER_KEY_PATH.read_bytes().strip())


def encrypt_text(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_text(value: bytes) -> str:
    try:
        return _fernet().decrypt(value).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Encrypted data cannot be decrypted with the current master key") from exc


def encrypt_json(value: Any) -> bytes:
    return encrypt_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def decrypt_json(value: bytes) -> Any:
    return json.loads(decrypt_text(value))


def create_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash,user_id,csrf_token,expires_at,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (token_hash(token), user_id, csrf, (now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(), now.isoformat(), now.isoformat()),
        )
    return token, csrf


def get_session(token: str | None):
    if not token:
        return None
    digest = token_hash(token)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        row = conn.execute(
            """SELECT s.token_hash AS session_hash,s.csrf_token,s.expires_at,u.*
               FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (digest, now),
        ).fetchone()
        if row:
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, digest))
        return row


def delete_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))


def issue_confirmation(user_id: str, session_hash: str, action: str, payload: Any, ttl_seconds: int = 180) -> str:
    token = secrets.token_urlsafe(40)
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO confirmations(token_hash,user_id,session_hash,action,payload_hash,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (token_hash(token), user_id, session_hash, action, payload_hash(payload), (now + timedelta(seconds=ttl_seconds)).isoformat(), now.isoformat()),
        )
    return token


def consume_confirmation(token: str | None, user_id: str, session_hash: str, action: str, payload: Any) -> bool:
    if not token:
        return False
    digest = token_hash(token)
    now = datetime.now(timezone.utc).isoformat()
    expected = payload_hash(payload)
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM confirmations WHERE token_hash=? AND user_id=? AND session_hash=?
               AND action=? AND payload_hash=? AND expires_at>? AND used_at IS NULL""",
            (digest, user_id, session_hash, action, expected, now),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE confirmations SET used_at=? WHERE token_hash=?", (now, digest))
    return True


def create_satellite_token(user_id: str, label: str, room: str = "") -> str:
    """A long-lived credential for a physical wake-word satellite device —
    deliberately not a session token: no 12h expiry (a device in the kitchen
    shouldn't need someone to log back in), but scoped to exactly one user
    and only usable on the satellite websocket, never as a general session."""
    token = secrets.token_urlsafe(40)
    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO satellite_tokens(token_hash,user_id,label,room,created_at,last_seen_at) VALUES(?,?,?,?,?,?)",
            (token_hash(token), user_id, label.strip()[:80], room.strip()[:80], now, None),
        )
    return token


def resolve_satellite_token(token: str | None):
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT st.token_hash,st.room,u.* FROM satellite_tokens st JOIN users u ON u.id=st.user_id WHERE st.token_hash=? AND u.active=1",
            (token_hash(token),),
        ).fetchone()
        if row:
            conn.execute("UPDATE satellite_tokens SET last_seen_at=? WHERE token_hash=?", (utcnow(), row["token_hash"]))
        return row


def list_satellite_tokens(user_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT token_hash,label,room,created_at,last_seen_at FROM satellite_tokens WHERE user_id=? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
    return [{"id": row["token_hash"][:16], "label": row["label"], "room": row["room"], "created_at": row["created_at"], "last_seen_at": row["last_seen_at"]} for row in rows]


def revoke_satellite_token(user_id: str, token_id_prefix: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM satellite_tokens WHERE user_id=? AND token_hash LIKE ?", (user_id, f"{token_id_prefix}%"))
    return cur.rowcount > 0


def audit(user_id: str | None, action: str, details: dict[str, Any], remote_addr: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(id,user_id,action,details_json,remote_addr,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, action, json.dumps(details, ensure_ascii=False, default=str), remote_addr, utcnow()),
        )
