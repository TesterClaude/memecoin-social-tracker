"""SQLite storage — full §5 schema.

M1 only writes sources/mentions/tokens; token_snapshots, signals and calls
are created empty so M2 needs no migration. All timestamps UTC ISO-8601.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from tracker.models import EnrichedToken

# mcap_at_pool_creation can only be estimated while the h24 price-change
# window still covers the pool's entire life
_POOL_ESTIMATE_MAX_AGE_H = 24

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

CREATE TABLE IF NOT EXISTS call_checkpoints (
    call_id           INTEGER NOT NULL REFERENCES calls(call_id),
    checkpoint_min    INTEGER NOT NULL,
    due_ts            TEXT NOT NULL,
    measured_ts       TEXT,
    price_usd         REAL,
    mcap              REAL,
    liquidity_usd     REAL,
    pair_missing      INTEGER NOT NULL DEFAULT 0,
    liq_gone          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (call_id, checkpoint_min)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_open
    ON call_checkpoints(due_ts) WHERE measured_ts IS NULL;
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# M2 columns added to the §5 tokens table; applied idempotently on connect
_TOKENS_M2_COLUMNS = {
    "dex_id": "TEXT",
    "pair_address": "TEXT",
    "pool_created_at": "TEXT",
    "mcap_at_first_mention": "REAL",
    "mcap_at_pool_creation": "REAL",
    "enriched_at": "TEXT",
    # NULL = never enriched, 'ok' = enriched, 'ignored' = system mint,
    # 'invalid_address' = fails base58 shape, 'invalid_no_pairs' = API knows
    # no pair. invalid*/ignored are never queried (again).
    "enrich_status": "TEXT",
}


# M6 columns added to the §5 calls table
_CALLS_M6_COLUMNS = {
    "price_at_call": "REAL",
    "liquidity_at_call": "REAL",
    # when the baseline was captured — equals enrichment time for normal
    # calls, later for pre-pool calls whose first price arrives with a
    # checkpoint. NULL = no price ever seen.
    "baseline_ts": "TEXT",
    # 'open' -> 'done' | 'no_pool' once the last checkpoint is measured
    "status": "TEXT NOT NULL DEFAULT 'open'",
    # 1 = the token's FIRST mention was an OUTCOME post: the call was
    # discovered late and must be excluded from main channel statistics
    "late_discovery": "INTEGER NOT NULL DEFAULT 0",
    # 1 = the +24h outcome reply was posted (or nothing to post)
    "outcome_posted": "INTEGER NOT NULL DEFAULT 0",
}

_MENTIONS_ALERT_COLUMNS = {
    # NEW_CALL | OUTCOME | LIST | COMMENTARY; NULL for pre-feature rows
    "message_type": "TEXT",
    # all hrefs of the message block as a JSON array — CAs often live only
    # here; without them, stored messages could never be re-classified
    "links_json": "TEXT",
}

_TOKENS_ALERT_COLUMNS = {
    # Telegram message_id of the NEW_CALL alert, for reply threading
    "alert_message_id": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in (("tokens", _TOKENS_M2_COLUMNS),
                           ("tokens", _TOKENS_ALERT_COLUMNS),
                           ("calls", _CALLS_M6_COLUMNS),
                           ("mentions", _MENTIONS_ALERT_COLUMNS)):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, col_type in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
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
    message_type: str | None = None,
    links: list[str] | None = None,
) -> None:
    engagement = json.dumps({"views": views}) if views is not None else None
    links_json = json.dumps(links) if links else None
    conn.execute(
        """INSERT OR IGNORE INTO mentions
           (platform, source_id, external_id, ts_utc, raw_text, ticker,
            contract_address, chain, engagement_json, is_duplicate, dedupe_hash,
            message_type, links_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, source_id, external_id, ts_utc, raw_text, ticker,
         contract_address, chain, engagement, int(is_duplicate), dedupe_hash,
         message_type, links_json),
    )
    conn.commit()


def set_alert_message_id(conn: sqlite3.Connection, contract_address: str,
                         message_id: int) -> None:
    conn.execute("UPDATE tokens SET alert_message_id=? WHERE contract_address=?",
                 (message_id, contract_address))
    conn.commit()


def get_alert_message_id(conn: sqlite3.Connection,
                         contract_address: str) -> int | None:
    row = conn.execute("SELECT alert_message_id FROM tokens WHERE contract_address=?",
                       (contract_address,)).fetchone()
    return row[0] if row else None


def get_token_ticker(conn: sqlite3.Connection,
                     contract_address: str) -> str | None:
    row = conn.execute("SELECT ticker FROM tokens WHERE contract_address=?",
                       (contract_address,)).fetchone()
    return row[0] if row else None


def ticker_collision_count(conn: sqlite3.Connection, ticker: str,
                           since_iso: str) -> int:
    """Distinct contract addresses mentioned with this ticker since the
    given timestamp — >1 means ticker collision (§7: key on CA, never ticker)."""
    return conn.execute(
        """SELECT COUNT(DISTINCT contract_address) FROM mentions
           WHERE ticker = ? COLLATE NOCASE AND contract_address IS NOT NULL
             AND ts_utc >= ?""",
        (ticker, since_iso)).fetchone()[0]


def mention_chain_position(conn: sqlite3.Connection, contract_address: str,
                           before_ts: str) -> tuple[int, str | None, float | None]:
    """(position, first_channel, minutes_after_first) of a mention at
    before_ts within the mention chain of this CA. Position 1 = first."""
    first = conn.execute(
        """SELECT s.handle, m.ts_utc FROM mentions m
           JOIN sources s ON s.source_id = m.source_id
           WHERE m.contract_address = ? AND m.ts_utc < ?
           ORDER BY m.ts_utc LIMIT 1""",
        (contract_address, before_ts)).fetchone()
    if first is None:
        return 1, None, None
    n_prior = conn.execute(
        """SELECT COUNT(DISTINCT source_id) FROM mentions
           WHERE contract_address = ? AND ts_utc < ?""",
        (contract_address, before_ts)).fetchone()[0]
    minutes = (datetime.fromisoformat(before_ts)
               - datetime.fromisoformat(first[1])).total_seconds() / 60
    return n_prior + 1, first[0], minutes


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


def get_enrich_status(conn: sqlite3.Connection, contract_address: str) -> str | None:
    row = conn.execute(
        "SELECT enrich_status FROM tokens WHERE contract_address=?",
        (contract_address,),
    ).fetchone()
    return row[0] if row else None


def set_enrich_status(conn: sqlite3.Connection, contract_address: str, status: str) -> None:
    conn.execute(
        "UPDATE tokens SET enrich_status=?, enriched_at=? WHERE contract_address=?",
        (status, _now_utc(), contract_address),
    )
    conn.commit()


def get_retryable_no_pairs(conn: sqlite3.Connection, window_hours: int) -> list[str]:
    """no-pairs tokens whose FIRST MENTION is younger than the window.

    A token called before its pool exists (insider chatter, §11 #21) gets
    'invalid_no_pairs' on the first lookup — but the pool may appear minutes
    later. These are re-checked every cycle until the window closes; after
    that they drop out of this query and stay invalid for good."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=window_hours)).isoformat(timespec="seconds")
    return [r[0] for r in conn.execute(
        "SELECT contract_address FROM tokens"
        " WHERE enrich_status='invalid_no_pairs' AND first_mention_ts > ?",
        (cutoff,))]


def _age_hours(ts_iso: str | None, now: datetime) -> float | None:
    if not ts_iso:
        return None
    try:
        return (now - datetime.fromisoformat(ts_iso)).total_seconds() / 3600
    except ValueError:
        return None


def apply_enrichment(
    conn: sqlite3.Connection,
    contract_address: str,
    e: EnrichedToken,
    first_mention_proxy_window_min: int,
) -> None:
    """Write market state into tokens. The two write-once fields:

    - mcap_at_first_mention: current mcap, but ONLY if enrichment happens
      within the proxy window of the first mention — for older tokens the
      current mcap is not the mcap back then, so it stays NULL (honest).
    - mcap_at_pool_creation: estimated from the h24 price change, only
      while the pool is younger than 24h (see estimate_mcap_at_creation).
    """
    from tracker.enrich.dexscreener import estimate_mcap_at_creation

    now = datetime.now(timezone.utc)
    row = conn.execute(
        """SELECT first_mention_ts, mcap_at_first_mention, mcap_at_pool_creation,
                  first_seen_price, first_seen_mcap
           FROM tokens WHERE contract_address=?""",
        (contract_address,),
    ).fetchone()
    if row is None:
        return
    first_mention_ts, mcap_at_fm, mcap_at_pc, first_price, first_mcap = row

    if mcap_at_fm is None and e.mcap is not None:
        mention_age_h = _age_hours(first_mention_ts, now)
        if mention_age_h is not None \
                and mention_age_h * 60 <= first_mention_proxy_window_min:
            mcap_at_fm = e.mcap

    if mcap_at_pc is None:
        mcap_at_pc = estimate_mcap_at_creation(
            e.mcap, e.price_change_h24, _age_hours(e.pool_created_at, now))

    conn.execute(
        """UPDATE tokens SET
             ticker            = COALESCE(ticker, ?),
             name              = COALESCE(name, ?),
             chain             = ?,
             dex_id            = ?,
             pair_address      = ?,
             pool_created_at   = ?,
             socials_json      = ?,
             first_seen_price  = COALESCE(first_seen_price, ?),
             first_seen_mcap   = COALESCE(first_seen_mcap, ?),
             mcap_at_first_mention = ?,
             mcap_at_pool_creation = ?,
             enriched_at       = ?,
             enrich_status     = 'ok'
           WHERE contract_address = ?""",
        (e.symbol, e.name, e.chain_id, e.dex_id, e.pair_address,
         e.pool_created_at, e.socials_json, e.price_usd, e.mcap,
         mcap_at_fm, mcap_at_pc,
         now.isoformat(timespec="seconds"), contract_address),
    )
    conn.commit()


def insert_snapshot(conn: sqlite3.Connection, contract_address: str,
                    e: EnrichedToken) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO token_snapshots
           (contract_address, ts_utc, price_usd, mcap, liquidity_usd,
            vol_5m, vol_1h, txns_buy, txns_sell)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contract_address, _now_utc(), e.price_usd, e.mcap, e.liquidity_usd,
         e.vol_5m, e.vol_1h, e.txns_buy, e.txns_sell),
    )
    conn.commit()
