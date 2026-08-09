"""Forward-testing log (§10, M6): record at call time, measure afterwards.

Non-negotiables encoded here:
- A call row is written the moment a token is FIRST mentioned — never
  retroactively. No look-ahead anywhere.
- Tokens that never get a pool end as status 'no_pool' and stay in every
  denominator; silently dropping them would be survivorship bias.
- MFE/MAE are computed from the 4 sampled checkpoints only — a spike
  between checkpoints is invisible. The report states this.
- 'rug' (liq_gone) means liquidity fell below the floor or the pair
  vanished AFTER the call had liquidity >= floor. A price crash with
  intact liquidity is NOT a rug. A vanished pair counts as -100% in MAE.

All functions take an injectable `now` for testability.
"""

import logging
import statistics
from datetime import datetime, timedelta, timezone

from tracker.models import EnrichedToken

log = logging.getLogger(__name__)

DEFAULT_CHECKPOINTS_MIN = (15, 60, 240, 1440)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def create_call(conn, source_id: int, contract_address: str,
                mention_ts: str | None, market: EnrichedToken | None,
                checkpoints_min=DEFAULT_CHECKPOINTS_MIN,
                now: datetime | None = None,
                late_discovery: bool = False,
                is_baseline: bool = False) -> int | None:
    """Open a forward-test entry for a token's first mention.

    Returns the call_id, or None if a call already exists OR the mention
    was ingested too late (older than the first checkpoint interval) —
    its '+15min' lies in the past and cannot be forward-measured. That
    exclusion is outcome-independent, hence bias-free."""
    if conn.execute("SELECT 1 FROM calls WHERE contract_address=?",
                    (contract_address,)).fetchone():
        return None
    now_dt = _now(now)
    mention_dt = datetime.fromisoformat(mention_ts) if mention_ts else now_dt
    if (now_dt - mention_dt) > timedelta(minutes=min(checkpoints_min)):
        log.info("no forward-test entry for %s: mention ingested %.0f min late",
                 contract_address,
                 (now_dt - mention_dt).total_seconds() / 60)
        return None

    has_baseline = market is not None and market.price_usd is not None
    cur = conn.execute(
        """INSERT INTO calls (source_id, contract_address, ts_utc, mcap_at_call,
                              price_at_call, liquidity_at_call, baseline_ts,
                              status, late_discovery, is_baseline)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (source_id, contract_address, _iso(mention_dt),
         market.mcap if has_baseline else None,
         market.price_usd if has_baseline else None,
         market.liquidity_usd if has_baseline else None,
         _iso(now_dt) if has_baseline else None,
         int(late_discovery), int(is_baseline)),
    )
    call_id = cur.lastrowid
    for minutes in checkpoints_min:
        conn.execute(
            "INSERT INTO call_checkpoints (call_id, checkpoint_min, due_ts)"
            " VALUES (?, ?, ?)",
            (call_id, minutes, _iso(mention_dt + timedelta(minutes=minutes))),
        )
    conn.commit()
    return call_id


def due_checkpoint_addresses(conn, now: datetime | None = None) -> list[str]:
    """Distinct addresses with at least one due, unmeasured checkpoint."""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT c.contract_address
           FROM call_checkpoints k JOIN calls c ON c.call_id = k.call_id
           WHERE k.measured_ts IS NULL AND k.due_ts <= ?""",
        (_iso(_now(now)),))]


def record_measurements(conn, results: dict[str, EnrichedToken | None],
                        rug_floor_usd: float,
                        now: datetime | None = None) -> int:
    """Write measurements for every due checkpoint of the given addresses.

    `results` follows the DexScreener client contract: an address that is
    absent was a FAILED request (its checkpoints stay unmeasured and are
    retried next cycle); a None value means the API knows no pair."""
    now_dt = _now(now)
    now_iso = _iso(now_dt)
    n = 0
    for addr, e in results.items():
        call = conn.execute(
            """SELECT call_id, price_at_call, liquidity_at_call, baseline_ts
               FROM calls WHERE contract_address=?""", (addr,)).fetchone()
        if call is None:
            continue
        call_id, price0, liq0, baseline_ts = call
        due = conn.execute(
            """SELECT checkpoint_min FROM call_checkpoints
               WHERE call_id=? AND measured_ts IS NULL AND due_ts <= ?
               ORDER BY checkpoint_min""", (call_id, now_iso)).fetchall()
        if not due:
            continue

        # late baseline: a pre-pool call gets its baseline from the first
        # checkpoint that finds a price (baseline_ts records when)
        if baseline_ts is None and e is not None and e.price_usd is not None:
            price0, liq0 = e.price_usd, e.liquidity_usd
            conn.execute(
                """UPDATE calls SET price_at_call=?, liquidity_at_call=?,
                   mcap_at_call=?, baseline_ts=? WHERE call_id=?""",
                (e.price_usd, e.liquidity_usd, e.mcap, now_iso, call_id))
            baseline_ts = now_iso

        # rug reference: did this call ever have liquidity >= floor?
        prev_max_liq = conn.execute(
            """SELECT MAX(liquidity_usd) FROM call_checkpoints
               WHERE call_id=? AND measured_ts IS NOT NULL""",
            (call_id,)).fetchone()[0]
        had_liq = max(liq0 or 0.0, prev_max_liq or 0.0) >= rug_floor_usd

        if e is None:
            price = mcap = liq = None
            # only a pair that existed before can be "missing"
            pair_missing = 1 if baseline_ts is not None else 0
        else:
            price, mcap, liq = e.price_usd, e.mcap, e.liquidity_usd
            pair_missing = 0
        liq_gone = 1 if had_liq and (e is None or (liq or 0.0) < rug_floor_usd) else 0

        # If several checkpoints are overdue at once (downtime catch-up),
        # there is only ONE observation: it goes into the latest due slot;
        # earlier slots are closed as missed (NULL values) so their label
        # ("price at +15m") never lies. MFE/MAE skip NULLs anyway.
        for i, (checkpoint_min,) in enumerate(due):
            is_observation = i == len(due) - 1
            conn.execute(
                """UPDATE call_checkpoints SET measured_ts=?, price_usd=?,
                   mcap=?, liquidity_usd=?, pair_missing=?, liq_gone=?
                   WHERE call_id=? AND checkpoint_min=?""",
                (now_iso,
                 price if is_observation else None,
                 mcap if is_observation else None,
                 liq if is_observation else None,
                 pair_missing if is_observation else 0,
                 liq_gone if is_observation else 0,
                 call_id, checkpoint_min))
            n += 1
        _update_outcome(conn, call_id)
    conn.commit()
    return n


def _update_outcome(conn, call_id: int) -> None:
    """Recompute MFE/MAE from measured checkpoints; finalise status once
    the last checkpoint is in. MFE >= 0, MAE <= 0 (percent vs baseline);
    a vanished pair counts as -100."""
    price0 = conn.execute(
        "SELECT price_at_call FROM calls WHERE call_id=?", (call_id,)).fetchone()[0]
    measured = conn.execute(
        """SELECT price_usd, pair_missing FROM call_checkpoints
           WHERE call_id=? AND measured_ts IS NOT NULL""", (call_id,)).fetchall()
    mfe = mae = None
    if price0:
        pcts = []
        for price, missing in measured:
            if missing:
                pcts.append(-100.0)
            elif price is not None:
                pcts.append((price / price0 - 1) * 100)
        if pcts:
            mfe = max(0.0, max(pcts))
            mae = min(0.0, min(pcts))
    unmeasured = conn.execute(
        """SELECT COUNT(*) FROM call_checkpoints
           WHERE call_id=? AND measured_ts IS NULL""", (call_id,)).fetchone()[0]
    status = "open" if unmeasured else ("done" if price0 else "no_pool")
    conn.execute("UPDATE calls SET outcome_mfe=?, outcome_mae=?, status=?"
                 " WHERE call_id=?", (mfe, mae, status, call_id))


def channel_stats(conn) -> list[dict]:
    """Per-channel forward-test statistics over COMPLETED calls, plus an
    '(all)' row. no_pool calls stay in every denominator. late_discovery
    calls (first mention was an OUTCOME post) are EXCLUDED from the main
    numbers and reported separately — they would poison channel stats."""
    rows = conn.execute(
        """SELECT s.handle, c.status, c.outcome_mfe, c.outcome_mae,
                  EXISTS(SELECT 1 FROM call_checkpoints k
                         WHERE k.call_id = c.call_id AND k.liq_gone = 1),
                  c.late_discovery
           FROM calls c JOIN sources s ON s.source_id = c.source_id
           WHERE c.is_baseline = 0""").fetchall()

    def aggregate(name: str, subset: list) -> dict:
        regular = [r for r in subset if not r[5]]
        completed = [r for r in regular if r[1] in ("done", "no_pool")]
        mfes = [r[2] for r in completed if r[2] is not None]
        maes = [r[3] for r in completed if r[3] is not None]
        n = len(completed)
        return {
            "channel": name,
            "calls_completed": n,
            "calls_open": sum(1 for r in regular if r[1] == "open"),
            "late_discovery": sum(1 for r in subset if r[5]),
            "median_mfe": statistics.median(mfes) if mfes else None,
            "median_mae": statistics.median(maes) if maes else None,
            "rug_share": sum(1 for r in completed if r[4]) / n if n else None,
            "no_pool_share": sum(1 for r in completed if r[1] == "no_pool") / n
                             if n else None,
            "over_50_share": sum(1 for r in completed
                                 if r[2] is not None and r[2] >= 50) / n if n else None,
        }

    by_channel: dict[str, list] = {}
    for r in rows:
        by_channel.setdefault(r[0], []).append(r)
    stats = [aggregate(ch, subset) for ch, subset in
             sorted(by_channel.items(), key=lambda kv: -len(kv[1]))]
    stats.append(aggregate("(all)", rows))
    return stats


def unposted_final_calls(conn) -> list[dict]:
    """Finalised calls whose +24h outcome has not been posted yet, with
    the origin alert message id (None if there was no NEW_CALL alert)."""
    rows = conn.execute(
        """SELECT c.call_id, c.contract_address, c.status, c.outcome_mfe,
                  c.outcome_mae, t.alert_message_id,
                  EXISTS(SELECT 1 FROM call_checkpoints k
                         WHERE k.call_id = c.call_id AND k.liq_gone = 1),
                  (SELECT k.mcap FROM call_checkpoints k
                   WHERE k.call_id = c.call_id AND k.measured_ts IS NOT NULL
                   ORDER BY k.checkpoint_min DESC LIMIT 1)
           FROM calls c
           LEFT JOIN tokens t ON t.contract_address = c.contract_address
           WHERE c.status != 'open' AND c.outcome_posted = 0""").fetchall()
    return [{"call_id": r[0], "contract_address": r[1], "status": r[2],
             "mfe": r[3], "mae": r[4], "alert_message_id": r[5],
             "rugged": bool(r[6]), "mcap_final": r[7]} for r in rows]


def mark_outcome_posted(conn, call_id: int) -> None:
    conn.execute("UPDATE calls SET outcome_posted=1 WHERE call_id=?", (call_id,))
    conn.commit()
