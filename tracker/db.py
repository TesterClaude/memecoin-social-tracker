"""SQLite storage — full §5 schema.

M1 only writes sources/mentions/tokens; token_snapshots, signals and calls
are created empty so M2 needs no migration. All timestamps UTC ISO-8601.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id         INTEGER PRIMARY KEY,
    platform          TEXT NOT NULL,
    handle            TEXT NOT NULL,
    tier              TEXT,
    credibility_score REAL,
    first_seen        TEXT NOT NULL,
    notes             TEXT,
    UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS mentions (
    mention_id        INTEGER PRIMARY KEY,
    platform          TEXT NOT NULL,
    source_id         INTEGER NOT NULL REFERENCES sources(source_id),
    external_id       TEXT NOT NULL,
    ts_utc            TEXT,
    raw_text          TEXT NOT NULL,
    ticker            TEXT,
    contract_address  TEXT,
    chain             TEXT,
    sentiment         REAL,
    confidence        REAL,
    author_age_days   INTEGER,
    author_followers  INTEGER,
    engagement_json   TEXT,
    is_duplicate      INTEGER NOT NULL DEFAULT 0,
    dedupe_hash       TEXT NOT NULL,
    UNIQUE (platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_mentions_dedupe ON mentions(dedupe_hash);
CREATE INDEX IF NOT EXISTS idx_mentions_contract ON mentions(contract_address);

CREATE TABLE IF NOT EXISTS tokens (
    contract_address  TEXT PRIMARY KEY,
    chain             TEXT NOT NULL,
    ticker            TEXT,
    name              TEXT,
    first_mention_ts  TEXT,
    first_seen_price  REAL,
    first_seen_mcap   REAL,
    socials_json      TEXT,
    launch_ts         TEXT
);

CREATE TABLE IF NOT EXISTS token_snapshots (
    contract_address  TEXT NOT NULL,
    ts_utc            TEXT NOT NULL,
    price_usd         REAL,
    mcap              REAL,
    liquidity_usd     REAL,
    vol_5m            REAL,
    vol_1h            REAL,
    txns_buy          INTEGER,
    txns_sell         INTEGER,
    holders           INTEGER,
    PRIMARY KEY (contract_address, ts_utc)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id         INTEGER PRIMARY KEY,
    contract_address  TEXT NOT NULL,
    ts_utc            TEXT NOT NULL,
    signal_type       TEXT NOT NULL,
    score             REAL,
    components_json   TEXT,
    triggered_alert   INTEGER NOT NULL DEFAULT 0,
    outcome_json      TEXT
);

CREATE TABLE IF NOT EXISTS calls (
    call_id           INTEGER PRIMARY KEY,
    source_id         INTEGER NOT NULL REFERENCES sources(source_id),
    contract_address  TEXT NOT NULL,
    ts_utc            TEXT NOT NULL,
    mcap_at_call      REAL,
    outcome_mfe       REAL,
    outcome_mae       REAL
);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def get_or_create_source(conn: sqlite3.Connection, platform: str, handle: str) -> int:
    row = conn.execute(
        "SELECT source_id FROM sources WHERE platform=? AND handle=?",
        (platform, handle),
    ).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO sources (platform, handle, first_seen) VALUES (?, ?, ?)",
        (platform, handle, _now_utc()),
    )
    conn.commit()
    return cur.lastrowid


def mention_exists(conn: sqlite3.Connection, platform: str, external_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM mentions WHERE platform=? AND external_id=?",
        (platform, external_id),
    ).fetchone() is not None


def is_known_hash(conn: sqlite3.Connection, dedupe_hash: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM mentions WHERE dedupe_hash=? LIMIT 1", (dedupe_hash,)
    ).fetchone() is not None


def insert_mention(
    conn: sqlite3.Connection,
    *,
    platform: str,
    source_id: int,
    external_id: str,
    ts_utc: str | None,
    raw_text: str,
    ticker: str | None,
    contract_address: str | None,
    chain: str,
    views: int | None,
    is_duplicate: bool,
    dedupe_hash: str,
) -> None:
    engagement = json.dumps({"views": views}) if views is not None else None
    conn.execute(
        """INSERT OR IGNORE INTO mentions
           (platform, source_id, external_id, ts_utc, raw_text, ticker,
            contract_address, chain, engagement_json, is_duplicate, dedupe_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, source_id, external_id, ts_utc, raw_text, ticker,
         contract_address, chain, engagement, int(is_duplicate), dedupe_hash),
    )
    conn.commit()


def upsert_token_first_seen(
    conn: sqlite3.Connection,
    contract_address: str,
    chain: str,
    ticker: str | None,
    ts_utc: str | None,
) -> bool:
    """Insert the token on first sighting. Returns True if it was new."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO tokens (contract_address, chain, ticker, first_mention_ts)
           VALUES (?, ?, ?, ?)""",
        (contract_address, chain, ticker, ts_utc or _now_utc()),
    )
    conn.commit()
    return cur.rowcount > 0
