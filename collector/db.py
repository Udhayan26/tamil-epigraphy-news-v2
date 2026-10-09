"""SQLite storage: candidate URLs, verified articles, and resumable progress state."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    url          TEXT PRIMARY KEY,
    collector    TEXT NOT NULL,
    found_at     TEXT NOT NULL,
    hint_title   TEXT,
    hint_date    TEXT,
    wayback_ts   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | matched | rejected | failed
    tries        INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status);

CREATE TABLE IF NOT EXISTS articles (
    url            TEXT PRIMARY KEY,
    title          TEXT,
    pub_date       TEXT,
    year           INTEGER,
    domain         TEXT,
    source_country TEXT,
    language       TEXT,
    groups         TEXT,      -- JSON list of group keys
    matched_terms  TEXT,      -- JSON list
    places         TEXT,      -- JSON list
    snippet        TEXT,
    collector      TEXT,
    archive_url    TEXT,
    match_basis    TEXT,      -- full-text | title-only
    fetched_at     TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- state ----
    def get_state(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.conn.commit()

    # ---- candidates ----
    def add_candidate(self, url: str, collector: str, hint_title: str = "",
                      hint_date: str = "", wayback_ts: str = "") -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO candidates(url,collector,found_at,hint_title,hint_date,wayback_ts) "
            "VALUES(?,?,?,?,?,?)",
            (url, collector, now(), hint_title, hint_date, wayback_ts),
        )
        return cur.rowcount > 0

    def add_candidates(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            if self.add_candidate(r["url"], r["collector"], r.get("title", ""),
                                  r.get("date", ""), r.get("wayback_ts", "")):
                n += 1
        self.conn.commit()
        return n

    def pending(self, limit: int, max_tries: int = 3) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM candidates WHERE status IN ('pending','failed') AND tries < ? "
            "ORDER BY tries ASC, RANDOM() LIMIT ?",
            (max_tries, limit),
        ).fetchall()

    def count_pending(self, max_tries: int = 3) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE status IN ('pending','failed') AND tries < ?",
            (max_tries,),
        ).fetchone()[0]

    def mark(self, url: str, status: str, error: str = "") -> None:
        self.conn.execute(
            "UPDATE candidates SET status=?, tries=tries+1, last_error=? WHERE url=?",
            (status, error[:300], url),
        )

    # ---- articles ----
    def save_article(self, a: dict) -> None:
        cols = ["url", "title", "pub_date", "year", "domain", "source_country", "language",
                "groups", "matched_terms", "places", "snippet", "collector", "archive_url",
                "match_basis", "fetched_at"]
        vals = [a.get(c) for c in cols]
        self.conn.execute(
            f"INSERT OR REPLACE INTO articles({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            vals,
        )

    def commit(self) -> None:
        self.conn.commit()

    def stats(self) -> dict:
        c = self.conn
        return {
            "candidates": c.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
            "pending": self.count_pending(),
            "matched": c.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
            "rejected": c.execute("SELECT COUNT(*) FROM candidates WHERE status='rejected'").fetchone()[0],
            "failed": c.execute("SELECT COUNT(*) FROM candidates WHERE status='failed'").fetchone()[0],
        }
