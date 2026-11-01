import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    local_date TEXT NOT NULL,
    text TEXT NOT NULL,
    mood TEXT NULL CHECK (mood IN ('good','okay','rough','low','sad')),
    energy_score INTEGER NULL CHECK (energy_score BETWEEN 1 AND 10),
    energy_is_approximate INTEGER NOT NULL DEFAULT 0,
    source_id TEXT UNIQUE NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    text, content='entries', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO entries_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('weekly','monthly')),
    period_key TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    version INTEGER NOT NULL,
    is_latest INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reflections_period ON reflections(kind, period_key, version);

CREATE TABLE IF NOT EXISTS focus_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done','failing')),
    created_at TEXT NOT NULL,
    completed_at TEXT NULL,
    source_reflection_id INTEGER NULL
);

CREATE TABLE IF NOT EXISTS ai_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('correction','note')),
    text TEXT NOT NULL,
    reflection_id INTEGER NULL,
    claim_id TEXT NULL,
    created_at TEXT NOT NULL,
    deleted_at TEXT NULL,
    conversation_id INTEGER NULL
);

CREATE TABLE IF NOT EXISTS reflection_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('weekly','monthly')),
    period_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NULL,
    last_error TEXT NULL,
    reflection_id INTEGER NULL,
    created_at TEXT NOT NULL,
    queued_at TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, period_key)
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    last_message_at TEXT NOT NULL,
    ended_at TEXT NULL,
    summarized_at TEXT NULL,
    summary_attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    text TEXT NOT NULL,
    cited_entry_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(reflection_jobs)")}
    if "queued_at" not in columns:  # database created before this column existed
        conn.execute("ALTER TABLE reflection_jobs ADD COLUMN queued_at TEXT NOT NULL DEFAULT ''")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(ai_memory)")}
    if "conversation_id" not in columns:  # database created before conversation summaries
        conn.execute("ALTER TABLE ai_memory ADD COLUMN conversation_id INTEGER NULL")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    if "summary_attempts" not in columns:  # database created before summary retries were counted
        conn.execute("ALTER TABLE conversations ADD COLUMN summary_attempts INTEGER NOT NULL DEFAULT 0")
    if "last_attempt_at" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN last_attempt_at TEXT NULL")
    conn.commit()
