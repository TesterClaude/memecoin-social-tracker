"""M1 entrypoint: poll t.me/s/ channels -> extract -> store -> alert raw hits.

Ctrl+C stops cleanly. One unreachable channel never kills the loop.
"""

import logging
import time

from tracker import db, extract, forward
from tracker.alert import AlertBot, format_alert
from tracker.collector.telegram_preview import TelegramPreviewCollector
from tracker.config import load_config
from tracker.enrich.dexscreener import DexScreenerClient
from tracker.models import EnrichedToken, ExtractionResult, RawMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("run")


def process_message(conn, cfg, msg: RawMessage,
                    ignore_mints: frozenset[str] = frozenset(),
                    ) -> tuple[ExtractionResult, bool, list[str]] | None:
    """Store a new message. Returns (result, alertable, first_seen_addresses)
    for fresh messages, None if the message was already ingested. alertable
    is False for duplicates (forward waves) and no-hit messages;
    first_seen_addresses are addresses never stored in tokens before —
    these open forward-test entries (M6)."""
    if db.mention_exists(conn, msg.platform, msg.external_id):
        return None

    addresses = extract.extract_addresses(msg.text, msg.links)
    # system mints from swap links are noise: never stored as the mention's
    # contract, never alerted, never enriched
    addresses = [a for a in addresses if a not in ignore_mints]
    result = ExtractionResult(
        addresses=addresses,
        tickers=extract.extract_tickers(msg.text, cfg.ticker_min_len, cfg.ticker_max_len),
        dedupe_hash=extract.dedupe_hash(msg.text),
    )
    # Copy-paste forward wave: same normalised text already seen in another
    # channel. Stored (N forwards, one idea) but not re-alerted.
    # Guard on the NORMALISED text: URL-only posts normalise to "" and would
    # otherwise all collide on the empty-string hash, suppressing real alerts.
    is_dup = bool(extract.normalize_text(msg.text)) \
        and db.is_known_hash(conn, result.dedupe_hash)

    source_id = db.get_or_create_source(conn, msg.platform, msg.channel)
    db.insert_mention(
        conn,
        platform=msg.platform,
        source_id=source_id,
        external_id=msg.external_id,
        ts_utc=msg.ts_utc,
        raw_text=msg.text,
        ticker=result.tickers[0] if result.tickers else None,
        contract_address=result.addresses[0] if result.addresses else None,
        chain=cfg.chain,
        views=msg.views,
        is_duplicate=is_dup,
        dedupe_hash=result.dedupe_hash,
    )
    first_seen = []
    for addr in result.addresses:
        if db.upsert_token_first_seen(
                conn, addr, cfg.chain,
                result.tickers[0] if result.tickers else None, msg.ts_utc):
            first_seen.append(addr)

    return result, (result.has_hit and not is_dup), first_seen


def enrich_addresses(conn, cfg, client: DexScreenerClient,
                     addresses: set[str]) -> dict[str, EnrichedToken]:
    """Batch-enrich addresses seen this cycle. Local shape check first, so
    regex false positives never hit the API; invalid tokens are marked and
    not queried again — EXCEPT no-pairs tokens still inside the retry
    window (their pool may not exist yet), which are re-checked every
    cycle. 'ok' tokens ARE re-queried on every re-mention — that is what
    builds the token_snapshots time series."""
    to_query = []
    for addr in addresses:
        status = db.get_enrich_status(conn, addr)
        if status is not None and status != "ok":
            continue  # ignored/invalid_*; young no-pairs re-enter below
        if not extract.is_valid_solana_address(addr):
            db.set_enrich_status(conn, addr, "invalid_address")
            log.info("marked %s invalid_address (not a 32-byte pubkey)", addr)
            continue
        to_query.append(addr)

    retry = [a for a in db.get_retryable_no_pairs(conn, cfg.no_pairs_retry_window_h)
             if a not in to_query]
    if retry:
        log.info("re-checking %d young no-pairs tokens (retry window %dh)",
                 len(retry), cfg.no_pairs_retry_window_h)
    to_query.extend(retry)
    if not to_query:
        return {}

    retry_set = set(retry)
    market: dict[str, EnrichedToken] = {}
    for addr, e in client.fetch_tokens(to_query).items():
        if e is None:
            if addr not in retry_set:  # retries already carry the status
                db.set_enrich_status(conn, addr, "invalid_no_pairs")
                log.info("marked %s invalid_no_pairs (dexscreener knows no pair)", addr)
        else:
            if addr in retry_set:
                log.info("token %s recovered: pool exists now, enriched", addr)
            db.apply_enrichment(conn, addr, e, cfg.first_mention_proxy_window_min)
            db.insert_snapshot(conn, addr, e)
            market[addr] = e
    return market


def main() -> None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    collector = TelegramPreviewCollector(
        cfg.user_agent, cfg.request_timeout_s, cfg.backoff_on_429_s,
        cfg.stale_after_days,
    )
    bot = AlertBot(cfg.bot_token, cfg.alert_chat_id, cfg.alerts_send_delay_s) \
        if cfg.alerts_enabled else None
    enricher = DexScreenerClient(
        cfg.enrich_api_base, cfg.enrich_timeout_s,
        cfg.enrich_min_request_interval_s, cfg.enrich_max_retries_429,
        cfg.enrich_max_addresses_per_call, cfg.chain,
    ) if cfg.enrich_enabled else None
    ignore = frozenset(cfg.ignore_mints)

    log.info("collector started: %d channels, poll every %ds, alerts %s, "
             "enrichment %s", len(cfg.channels), cfg.poll_interval_s,
             "on" if bot else "off", "on" if enricher else "off")

    if cfg.forward_enabled and enricher is None:
        log.warning("forward_testing.enabled without enrichment: entries are "
                    "created but checkpoints cannot be measured")

    while True:
        cycle_start = time.monotonic()

        # phase 1: collect and store everything new this cycle
        new_items: list[tuple[RawMessage, ExtractionResult, bool, list[str]]] = []
        for channel in cfg.channels:
            for msg in collector.fetch(channel):
                processed = process_message(conn, cfg, msg, ignore)
                if processed is not None:
                    new_items.append((msg, *processed))

        # phase 2: one batched DexScreener pass over every address seen
        addresses = {a for _, result, _, _ in new_items for a in result.addresses}
        market = enrich_addresses(conn, cfg, enricher, addresses) \
            if enricher and addresses else {}

        # phase 3: open forward-test entries for first-ever-seen tokens,
        # baseline from this cycle's enrichment (M6, §10)
        calls_opened = 0
        if cfg.forward_enabled:
            for msg, _, _, first_seen in new_items:
                for addr in first_seen:
                    source_id = db.get_or_create_source(conn, msg.platform, msg.channel)
                    if forward.create_call(conn, source_id, addr, msg.ts_utc,
                                           market.get(addr),
                                           cfg.forward_checkpoints_min) is not None:
                        calls_opened += 1

        # phase 4: measure due forward-test checkpoints (same client,
        # same rate limit as enrichment)
        measured = 0
        if cfg.forward_enabled and enricher is not None:
            due = forward.due_checkpoint_addresses(conn)
            if due:
                measured = forward.record_measurements(
                    conn, enricher.fetch_tokens(due), cfg.rug_liquidity_floor_usd)

        # phase 5: alerts, carrying market data
        alerts_sent = suppressed = 0
        for msg, result, alertable, _ in new_items:
            if not alertable or bot is None:
                continue
            if alerts_sent >= cfg.alerts_max_per_cycle:
                # hit is still stored in SQLite, only the alert is dropped
                suppressed += 1
                continue
            if bot.send(format_alert(msg, result, market)):
                alerts_sent += 1

        elapsed = time.monotonic() - cycle_start
        log.info("cycle done in %.1fs: %d new messages, %d enriched, "
                 "%d calls opened, %d checkpoints measured, %d alerts%s",
                 elapsed, len(new_items), len(market), calls_opened, measured,
                 alerts_sent,
                 f", {suppressed} suppressed by cap" if suppressed else "")
        time.sleep(max(0.0, cfg.poll_interval_s - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
