from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from .config import DB_PATH


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
              id TEXT PRIMARY KEY,
              username TEXT UNIQUE NOT NULL COLLATE NOCASE,
              display_name TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','adult','child','guest')),
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              csrf_token TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_failures(
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL COLLATE NOCASE,
              remote_addr TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_credentials(
              provider TEXT PRIMARY KEY,
              value_enc BLOB NOT NULL,
              configured_by TEXT REFERENCES users(id) ON DELETE SET NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS connections(
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              provider TEXT NOT NULL,
              account_label TEXT NOT NULL DEFAULT '',
              token_enc BLOB NOT NULL,
              scopes TEXT NOT NULL DEFAULT '',
              expires_at TEXT,
              status TEXT NOT NULL DEFAULT 'connected',
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(user_id, provider)
            );

            CREATE TABLE IF NOT EXISTS oauth_states(
              state_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              provider TEXT NOT NULL,
              redirect_uri TEXT NOT NULL,
              verifier_enc BLOB,
              requested_scopes TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_permissions(
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              capability TEXT NOT NULL,
              allowed INTEGER NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(user_id, capability)
            );

            CREATE TABLE IF NOT EXISTS confirmations(
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              session_hash TEXT NOT NULL,
              action TEXT NOT NULL,
              payload_hash TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              used_at TEXT,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_proposals(
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              action TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','executed','failed','cancelled')),
              result_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log(
              id TEXT PRIMARY KEY,
              user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
              action TEXT NOT NULL,
              details_json TEXT NOT NULL,
              remote_addr TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memories(
              id TEXT PRIMARY KEY,
              owner_id TEXT REFERENCES users(id) ON DELETE CASCADE,
              namespace TEXT NOT NULL CHECK(namespace IN ('private','family_shared','project_shared','system')),
              text TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              embedding_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations(
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','system')),
              content TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS family_tasks(
              id TEXT PRIMARY KEY,
              created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              assigned_to TEXT REFERENCES users(id) ON DELETE SET NULL,
              title TEXT NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','done','cancelled')),
              due_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS location_consent(
              user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              enabled INTEGER NOT NULL DEFAULT 0,
              share_with_family INTEGER NOT NULL DEFAULT 0,
              retention_hours INTEGER NOT NULL DEFAULT 24,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS locations(
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              latitude REAL NOT NULL,
              longitude REAL NOT NULL,
              accuracy REAL,
              source TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS voiceprints(
              user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              embedding_enc BLOB NOT NULL,
              sample_count INTEGER NOT NULL,
              threshold REAL NOT NULL DEFAULT 0.65,
              consent_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS release_workspaces(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              artist TEXT NOT NULL,
              audio_path TEXT NOT NULL,
              artwork_path TEXT,
              metadata_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              package_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS release_tracks(
              id TEXT PRIMARY KEY,
              release_id TEXT NOT NULL REFERENCES release_workspaces(id) ON DELETE CASCADE,
              track_number INTEGER NOT NULL,
              title TEXT NOT NULL,
              audio_path TEXT NOT NULL,
              validation_json TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              UNIQUE(release_id, track_number)
            );

            CREATE TABLE IF NOT EXISTS media_files(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              original_name TEXT NOT NULL,
              relative_path TEXT NOT NULL UNIQUE,
              media_type TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_settings(
              key TEXT PRIMARY KEY,
              value_enc BLOB NOT NULL,
              updated_by TEXT REFERENCES users(id) ON DELETE SET NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creative_projects(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'song' CHECK(kind IN ('song','videoclip','combo','other')),
              status TEXT NOT NULL DEFAULT 'idea' CHECK(status IN ('idea','writing','generating','mixing','done','published','abandoned')),
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creative_prompts(
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL REFERENCES creative_projects(id) ON DELETE CASCADE,
              platform TEXT NOT NULL CHECK(platform IN ('suno','higgsfield','openart','other')),
              prompt_text TEXT NOT NULL,
              outcome TEXT NOT NULL DEFAULT 'untried' CHECK(outcome IN ('untried','worked','discarded')),
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_channels(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK(kind IN ('ntfy')),
              config_enc BLOB NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS satellite_tokens(
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              room TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              last_seen_at TEXT
            );

            -- Same shape/purpose as satellite_tokens (a long-lived per-user device
            -- credential, not a session token) but kept separate rather than reused:
            -- a health-data webhook and a voice satellite are different trust
            -- boundaries, and conflating them would mean one token type could
            -- silently authenticate the other's endpoint too.
            CREATE TABLE IF NOT EXISTS health_webhook_tokens(
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              created_at TEXT NOT NULL,
              last_seen_at TEXT
            );

            -- One row per data point pushed by a Health Connect-exporting app
            -- (e.g. Health Connect Webhook, fed by Health Sync from Huawei Health).
            -- value_json holds the metric's own fields verbatim (schema varies per
            -- metric_type — steps has count/start_time/end_time, weight has
            -- kilograms/time, etc.) rather than forcing every metric into the same
            -- rigid columns; recorded_at is pulled out for indexed date-range queries.
            CREATE TABLE IF NOT EXISTS health_metrics(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              metric_type TEXT NOT NULL,
              value_json TEXT NOT NULL,
              recorded_at TEXT NOT NULL,
              received_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mcp_servers(
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              transport TEXT NOT NULL CHECK(transport IN ('stdio','http')),
              config_enc BLOB NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              requires_confirmation INTEGER NOT NULL DEFAULT 1,
              created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routines(
              id TEXT PRIMARY KEY,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title TEXT NOT NULL,
              trigger_type TEXT NOT NULL CHECK(trigger_type IN ('cron','interval_minutes')),
              trigger_config_json TEXT NOT NULL,
              action_type TEXT NOT NULL CHECK(action_type IN ('ask','notify')),
              action_config_json TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              source TEXT NOT NULL DEFAULT 'user' CHECK(source IN ('user','athena_proposed')),
              last_run_at TEXT,
              last_result_json TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_connections_user_provider ON connections(user_id, provider);
            CREATE INDEX IF NOT EXISTS idx_login_failures_lookup ON login_failures(username, remote_addr, created_at);
            CREATE INDEX IF NOT EXISTS idx_memories_namespace_owner ON memories(namespace, owner_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_status ON family_tasks(assigned_to, status);
            CREATE INDEX IF NOT EXISTS idx_locations_user_created ON locations(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_media_owner_created ON media_files(owner_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_release_tracks_release ON release_tracks(release_id, track_number);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_action_proposals_user_status ON action_proposals(user_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_creative_projects_owner ON creative_projects(owner_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_creative_prompts_project ON creative_prompts(project_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_notification_channels_owner ON notification_channels(owner_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_routines_owner_enabled ON routines(owner_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_mcp_servers_enabled ON mcp_servers(enabled);
            CREATE INDEX IF NOT EXISTS idx_satellite_tokens_user ON satellite_tokens(user_id);
            CREATE INDEX IF NOT EXISTS idx_health_webhook_tokens_user ON health_webhook_tokens(user_id);
            CREATE INDEX IF NOT EXISTS idx_health_metrics_owner_type ON health_metrics(owner_id, metric_type, recorded_at);

            -- Memory search: SQLite FTS5 keyword index, external-content table synced to
            -- `memories` via triggers. No embedding model, no network, no download — a
            -- HuggingFace hiccup can never hang memory search again (see app/memory.py).
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
              text, content='memories', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
              INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
              INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', old.rowid, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
              INSERT INTO memories_fts(memories_fts, rowid, text) VALUES('delete', old.rowid, old.text);
              INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
            END;
            """
        )
        # One-time backfill for rows that existed before the FTS5 index was introduced —
        # triggers only cover inserts/updates/deletes from this point forward. Cheap and
        # idempotent: skipped once the index is already populated.
        fts_count = conn.execute("SELECT COUNT(*) AS c FROM memories_fts").fetchone()["c"]
        memories_count = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
        if fts_count == 0 and memories_count > 0:
            conn.execute("INSERT INTO memories_fts(rowid, text) SELECT rowid, text FROM memories")
