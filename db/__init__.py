import sqlite3
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    value REAL,
    prev_value REAL,
    change_pct REAL,
    error TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_macro_key_time ON macro_snapshots(key, fetched_at);

CREATE TABLE IF NOT EXISTS news_fetches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','failed')),
    error TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_fetch_topic ON news_fetches(topic, fetched_at);

CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_id INTEGER NOT NULL REFERENCES news_fetches(id),
    title TEXT NOT NULL,
    link TEXT,
    source TEXT,
    published TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    llm_status TEXT NOT NULL CHECK (llm_status IN ('ok','failed','skipped')),
    provider TEXT,
    base_score REAL,
    adjustment INTEGER NOT NULL DEFAULT 0,
    final_score REAL,
    coverage REAL,
    missing TEXT,        -- JSON list
    summary TEXT,        -- JSON list (3 lines)
    risk_comment TEXT,
    error TEXT,
    inputs TEXT          -- JSON: LLM에 준 입력 스냅샷 (사후 검증용)
);
"""


def connect(path=None):
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path=None):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
