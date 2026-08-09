"""Launch baseline: an independent sample of new tokens, discovered from
DexScreener's token-profiles feed regardless of channel mentions.

Purpose (§10): called tokens can only be judged against the base rate of
comparable tokens nobody called. Honesty notes:
- The profiles feed contains tokens whose deployers set up a profile — a
  biased sample leaning toward marketed tokens, NOT the universe of all
  new pools (that would need an on-chain indexer).
- Baseline forward checkpoints anchor at DISCOVERY time; channel calls
  anchor at mention time. Both are "the moment the observer saw it".
- Already-mentioned tokens are still admitted (excluding them would bias
  the baseline toward never-mentioned = worse tokens); they keep their
  existing call and are flagged via mentioned_ts.
"""

import json
import logging
import re
import statistics
from datetime import datetime, timezone

from tracker import db, forward
from tracker.models import EnrichedToken

log = logging.getLogger(__name__)

BASELINE_PLATFORM = "baseline"
BASELINE_HANDLE = "dexscreener_profiles"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_known(conn, contract_address: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM baseline_tokens WHERE contract_address=?",
        (contract_address,)).fetchone() is not None


def admit(conn, contract_address: str, e: EnrichedToken | None,
          profile_links_json: str | None, checkpoints_min,
          now_iso: str | None = None) -> None:
    """Insert one baseline token and open its forward-test entry."""
    now_iso = now_iso or _now_iso()
    mentioned = conn.execute(
        "SELECT MIN(ts_utc) FROM mentions WHERE contract_address=?",
        (contract_address,)).fetchone()[0]
    conn.execute(
        """INSERT OR IGNORE INTO baseline_tokens
           (contract_address, chain, ticker, name, discovered_ts,
            first_seen_price, first_seen_mcap, socials_json, dex_id,
            pair_address, pool_created_at, enriched_at, enrich_status,
            mentioned_ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contract_address,
         e.chain_id if e else "solana",
         e.symbol if e else None,
         e.name if e else None,
         now_iso,
         e.price_usd if e else None,
         e.mcap if e else None,
         (e.socials_json if e else None) or profile_links_json,
         e.dex_id if e else None,
         e.pair_address if e else None,
         e.pool_created_at if e else None,
         now_iso if e else None,
         "ok" if e else None,
         mentioned),
    )
    conn.commit()
    source_id = db.get_or_create_source(conn, BASELINE_PLATFORM, BASELINE_HANDLE)
    forward.create_call(conn, source_id, contract_address, now_iso, e,
                        checkpoints_min, is_baseline=True)
    if e is not None:
        db.insert_snapshot(conn, contract_address, e)


def run_discovery(conn, cfg, client, ignore_mints: frozenset[str]) -> int:
    """One discovery pass: poll the profiles feed, resolve up to
    max_new_per_cycle unknown addresses, admit fresh ones. Returns the
    number of admitted tokens."""
    profiles = client.fetch_token_profiles()
    if profiles is None:
        return 0  # failed request — next interval tries again

    candidates = []
    for p in profiles:
        addr = p["address"]
        if addr in ignore_mints or is_known(conn, addr):
            continue
        candidates.append(p)
        if len(candidates) >= cfg.baseline_max_new_per_cycle:
            break
    if not candidates:
        sync_mentioned_flags(conn)
        return 0

    resolved = client.fetch_tokens([p["address"] for p in candidates])
    admitted = 0
    for p in candidates:
        addr = p["address"]
        if addr not in resolved:
            continue  # request failed for this chunk — retry next interval
        e = resolved[addr]
        if e is not None and e.pool_created_at:
            age_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(e.pool_created_at)
                       ).total_seconds() / 60
            if age_min > cfg.baseline_max_pool_age_min:
                # profiles feed also surfaces older tokens — not "new
                # launches", skip without admitting
                continue
        # e is None = profiled but no pool yet: the pre-pool case is
        # exactly worth tracking, admitted with no_pool machinery
        admit(conn, addr, e, p["links_json"], cfg.forward_checkpoints_min)
        admitted += 1
    sync_mentioned_flags(conn)
    if admitted:
        log.info("baseline: admitted %d new tokens", admitted)
    return admitted


def sync_mentioned_flags(conn) -> int:
    """Set mentioned_ts for baseline tokens that have since appeared in
    mentions — the coverage join, run once per discovery pass."""
    cur = conn.execute("""
        UPDATE baseline_tokens SET mentioned_ts = (
            SELECT MIN(m.ts_utc) FROM mentions m
            WHERE m.contract_address = baseline_tokens.contract_address)
        WHERE mentioned_ts IS NULL AND EXISTS (
            SELECT 1 FROM mentions m
            WHERE m.contract_address = baseline_tokens.contract_address)
    """)
    conn.commit()
    return cur.rowcount


# -- reporting ----------------------------------------------------------------

def _aggregate_outcomes(rows: list) -> dict:
    """rows: (status, outcome_mfe, rugged) of completed calls."""
    n = len(rows)
    mfes = [r[1] for r in rows if r[1] is not None]
    return {
        "n_done": n,
        "median_mfe": statistics.median(mfes) if mfes else None,
        "rug_share": sum(1 for r in rows if r[2]) / n if n else None,
        "no_pool_share": sum(1 for r in rows if r[0] == "no_pool") / n if n else None,
    }


def group_comparison(conn) -> dict:
    """Called (regular channel calls) vs. baseline over completed calls,
    plus coverage of the baseline by tracked channels."""
    called = conn.execute("""
        SELECT c.status, c.outcome_mfe,
               EXISTS(SELECT 1 FROM call_checkpoints k
                      WHERE k.call_id = c.call_id AND k.liq_gone = 1)
        FROM calls c
        WHERE c.is_baseline = 0 AND c.late_discovery = 0
          AND c.status != 'open'""").fetchall()
    baseline = conn.execute("""
        SELECT c.status, c.outcome_mfe,
               EXISTS(SELECT 1 FROM call_checkpoints k
                      WHERE k.call_id = c.call_id AND k.liq_gone = 1)
        FROM baseline_tokens b
        JOIN calls c ON c.contract_address = b.contract_address
        WHERE c.status != 'open'""").fetchall()
    total, mentioned = conn.execute(
        """SELECT COUNT(*),
                  COALESCE(SUM(CASE WHEN mentioned_ts IS NOT NULL THEN 1 ELSE 0 END), 0)
           FROM baseline_tokens""").fetchone()
    return {
        "called": _aggregate_outcomes(called),
        "baseline": _aggregate_outcomes(baseline),
        "baseline_total": total,
        "baseline_mentioned": mentioned,
        "coverage": mentioned / total if total else None,
    }


# -- serial deployers ---------------------------------------------------------

_HANDLE_RE = re.compile(
    r"https?://(?:www\.)?(?:x\.com|twitter\.com)/(@?[A-Za-z0-9_]{1,15})(?:[/?#]|$)",
    re.IGNORECASE)
_NON_HANDLES = {"i", "intent", "search", "home", "hashtag", "share"}


def twitter_handles(socials_json: str | None) -> list[str]:
    """Distinct X handles from a socials/links JSON blob (list of dicts
    with 'url' values). Status links resolve to their author handle."""
    if not socials_json:
        return []
    try:
        entries = json.loads(socials_json)
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    handles: dict[str, None] = {}
    for entry in entries:
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str):
            continue
        m = _HANDLE_RE.match(url.strip())
        if m:
            handle = m.group(1).lstrip("@").lower()
            if handle and handle not in _NON_HANDLES:
                handles.setdefault(handle, None)
    return list(handles)


def serial_deployers(conn, min_tokens: int = 2) -> list[dict]:
    """X handles appearing in the socials of >= min_tokens distinct tokens
    (across tokens AND baseline_tokens), with the forward outcomes of
    their tokens. Facts only — a serial handle is not automatically a
    scammer, but the pattern is worth seeing."""
    by_handle: dict[str, set] = {}
    for table in ("tokens", "baseline_tokens"):
        for addr, socials in conn.execute(
                f"SELECT contract_address, socials_json FROM {table}"
                " WHERE socials_json IS NOT NULL"):
            for h in twitter_handles(socials):
                by_handle.setdefault(h, set()).add(addr)

    out = []
    for handle, addrs in by_handle.items():
        if len(addrs) < min_tokens:
            continue
        placeholders = ",".join("?" * len(addrs))
        rows = conn.execute(
            f"""SELECT c.status, c.outcome_mfe,
                       EXISTS(SELECT 1 FROM call_checkpoints k
                              WHERE k.call_id = c.call_id AND k.liq_gone = 1)
                FROM calls c
                WHERE c.contract_address IN ({placeholders})
                  AND c.status != 'open'""", list(addrs)).fetchall()
        out.append({"handle": handle, "n_tokens": len(addrs),
                    **_aggregate_outcomes(rows)})
    out.sort(key=lambda d: -d["n_tokens"])
    return out
