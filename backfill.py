"""One-shot M2 backfill: enrich every address collected so far.

Usage:  python backfill.py

Steps:
1. repair first_mention_ts from the actual MIN(mentions.ts_utc)
2. mark system mints (config ignore_mints) as 'ignored'
3. mark addresses failing the base58 shape check as 'invalid_address'
4. batch-query DexScreener for everything still unenriched; write tokens
   + one snapshot each; addresses without pairs -> 'invalid_no_pairs'

mcap_at_first_mention stays NULL for tokens whose first mention is older
than the proxy window — the current mcap is NOT the mcap back then and
pretending otherwise would poison later run-up statistics.
"""

import logging
import sys

from tracker import db, extract
from tracker.config import load_config
from tracker.enrich.dexscreener import DexScreenerClient

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("backfill")


def main() -> None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)  # applies the M2 migration

    # 1. first_mention_ts = earliest mention in OUR database (the §14 M2 field)
    repaired = conn.execute("""
        UPDATE tokens SET first_mention_ts = (
            SELECT MIN(m.ts_utc) FROM mentions m
            WHERE m.contract_address = tokens.contract_address)
        WHERE EXISTS (
            SELECT 1 FROM mentions m
            WHERE m.contract_address = tokens.contract_address
              AND m.ts_utc < tokens.first_mention_ts)
    """).rowcount
    conn.commit()
    log.info("first_mention_ts repaired for %d tokens", repaired)

    # 2. system mints
    for mint in cfg.ignore_mints:
        conn.execute(
            "UPDATE tokens SET enrich_status='ignored' WHERE contract_address=?"
            " AND (enrich_status IS NULL OR enrich_status != 'ignored')", (mint,))
    conn.commit()

    # 3. shape check, 4. queue
    queue = []
    n_bad_shape = 0
    for (addr,) in conn.execute(
            "SELECT contract_address FROM tokens WHERE enrich_status IS NULL"):
        if extract.is_valid_solana_address(addr):
            queue.append(addr)
        else:
            db.set_enrich_status(conn, addr, "invalid_address")
            n_bad_shape += 1
    # same retry rule as the live loop: young no-pairs tokens get another look
    retry = [a for a in db.get_retryable_no_pairs(conn, cfg.no_pairs_retry_window_h)
             if a not in queue]
    queue.extend(retry)
    log.info("queue: %d addresses to enrich (%d marked invalid_address, "
             "%d ignored mints, %d no-pairs retries)",
             len(queue), n_bad_shape, len(cfg.ignore_mints), len(retry))

    client = DexScreenerClient(
        cfg.enrich_api_base, cfg.enrich_timeout_s,
        cfg.enrich_min_request_interval_s, cfg.enrich_max_retries_429,
        cfg.enrich_max_addresses_per_call, cfg.chain,
    )
    n_ok = n_no_pairs = 0
    results = client.fetch_tokens(queue)
    for addr, e in results.items():
        if e is None:
            db.set_enrich_status(conn, addr, "invalid_no_pairs")
            n_no_pairs += 1
        else:
            db.apply_enrichment(conn, addr, e, cfg.first_mention_proxy_window_min)
            db.insert_snapshot(conn, addr, e)
            n_ok += 1
    n_failed = len(queue) - len(results)

    log.info("backfill done: %d enriched, %d without pairs (marked invalid), "
             "%d request failures (left NULL, will retry on next run)",
             n_ok, n_no_pairs, n_failed)

    # summary over the whole table
    print("\nenrich_status distribution:")
    for status, n in conn.execute(
            "SELECT COALESCE(enrich_status, '(pending)') AS s, COUNT(*) AS n"
            " FROM tokens GROUP BY s ORDER BY n DESC"):
        print(f"  {status:>18}  {n}")
    n_snaps = conn.execute("SELECT COUNT(*) FROM token_snapshots").fetchone()[0]
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE pool_created_at IS NOT NULL").fetchone()[0]
    n_mafm = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE mcap_at_first_mention IS NOT NULL").fetchone()[0]
    n_mapc = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE mcap_at_pool_creation IS NOT NULL").fetchone()[0]
    print(f"\nsnapshots written: {n_snaps}")
    print(f"tokens with pool_created_at:        {n_pool}")
    print(f"tokens with mcap_at_first_mention:  {n_mafm}")
    print(f"tokens with mcap_at_pool_creation:  {n_mapc}")
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted — safe to re-run, already-marked tokens are skipped")
        sys.exit(1)
