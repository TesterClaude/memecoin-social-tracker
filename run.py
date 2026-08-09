"""M1 entrypoint: poll t.me/s/ channels -> extract -> store -> alert raw hits.

Ctrl+C stops cleanly. One unreachable channel never kills the loop.
"""

import logging
import time

from datetime import datetime, timedelta, timezone

from tracker import alert as alert_fmt
from tracker import classify, db, extract, forward
from tracker.alert import AlertBot
from tracker.collector.telegram_preview import TelegramPreviewCollector
from tracker.config import load_config
from tracker.enrich.dexscreener import DexScreenerClient
from tracker.models import (AlertFacts, EnrichedToken, ExtractionResult,
                            ProcessedMessage, RawMessage)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("run")


def process_message(conn, cfg, msg: RawMessage,
                    ignore_mints: frozenset[str] = frozenset(),
                    ) -> ProcessedMessage | None:
    """Store and classify a new message; None if already ingested.
    Nothing is discarded based on type — classification only labels."""
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
    message_type = classify.classify(msg.text, result.addresses, result.tickers)
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
        message_type=message_type,
        links=msg.links,
    )
    first_seen = []
    for addr in result.addresses:
        if db.upsert_token_first_seen(
                conn, addr, cfg.chain,
                result.tickers[0] if result.tickers else None, msg.ts_utc):
            first_seen.append(addr)

    return ProcessedMessage(result=result, message_type=message_type,
                            is_duplicate=is_dup, first_seen=first_seen)


def build_facts(conn, msg: RawMessage, result: ExtractionResult,
                e: EnrichedToken | None) -> AlertFacts:
    """Assemble the fact lines for an alert — pure facts from our own DB."""
    facts = AlertFacts()
    if result.tickers and result.addresses:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)) \
            .isoformat(timespec="seconds")
        facts.ticker_collisions_24h = db.ticker_collision_count(
            conn, result.tickers[0], since)
    if result.addresses and msg.ts_utc:
        facts.chain_position, facts.first_channel, facts.minutes_after_first = \
            db.mention_chain_position(conn, result.addresses[0], msg.ts_utc)
    if result.addresses:
        if e is None:
            facts.pool_missing = True
        elif e.pool_created_at and msg.ts_utc and msg.ts_utc < e.pool_created_at:
            facts.prepool_lead_min = (
                datetime.fromisoformat(e.pool_created_at)
                - datetime.fromisoformat(msg.ts_utc)).total_seconds() / 60
    return facts


def send_alert(conn, bot: AlertBot, msg: RawMessage, p: ProcessedMessage,
               market: dict[str, EnrichedToken]) -> bool:
    """Format per type, handle threading, persist the message id."""
    addr = p.result.addresses[0] if p.result.addresses else None
    e = market.get(addr) if addr else None

    if p.message_type == classify.NEW_CALL:
        facts = build_facts(conn, msg, p.result, e)
        message_id = bot.send(alert_fmt.format_new_call(msg, p.result, e, facts))
        if message_id is not None and addr:
            db.set_alert_message_id(conn, addr, message_id)
        return message_id is not None

    if p.message_type == classify.OUTCOME:
        facts = build_facts(conn, msg, p.result, e)
        origin_id = db.get_alert_message_id(conn, addr) if addr else None
        # OUTCOME posts often name the token without $-prefix: fall back
        # to the ticker stored with the token
        fallback = db.get_token_ticker(conn, addr) \
            if addr and not p.result.tickers else None
        text = alert_fmt.format_outcome(msg, p.result, e,
                                        has_origin=origin_id is not None,
                                        facts=facts, fallback_ticker=fallback)
        return bot.send(text, reply_to_message_id=origin_id) is not None

    return bot.send(alert_fmt.format_compact(msg, p.result, p.message_type)) is not None


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

        # phase 1: collect, store and classify everything new this cycle
        new_items: list[tuple[RawMessage, ProcessedMessage]] = []
        for channel in cfg.channels:
            for msg in collector.fetch(channel):
                processed = process_message(conn, cfg, msg, ignore)
                if processed is not None:
                    new_items.append((msg, processed))

        # phase 2: one batched DexScreener pass over every address seen
        addresses = {a for _, p in new_items for a in p.result.addresses}
        market = enrich_addresses(conn, cfg, enricher, addresses) \
            if enricher and addresses else {}

        # phase 3: open forward-test entries for first-ever-seen tokens,
        # baseline from this cycle's enrichment (M6, §10). If the first
        # mention is an OUTCOME retrospect, the call is late-discovered
        # and flagged so it never poisons channel statistics.
        calls_opened = 0
        if cfg.forward_enabled:
            for msg, p in new_items:
                for addr in p.first_seen:
                    source_id = db.get_or_create_source(conn, msg.platform, msg.channel)
                    if forward.create_call(
                            conn, source_id, addr, msg.ts_utc, market.get(addr),
                            cfg.forward_checkpoints_min,
                            late_discovery=p.message_type == classify.OUTCOME,
                    ) is not None:
                        calls_opened += 1

        # phase 4: measure due forward-test checkpoints (same client,
        # same rate limit as enrichment)
        measured = 0
        if cfg.forward_enabled and enricher is not None:
            due = forward.due_checkpoint_addresses(conn)
            if due:
                measured = forward.record_measurements(
                    conn, enricher.fetch_tokens(due), cfg.rug_liquidity_floor_usd)

        # phase 4b: post finalised +24h outcomes as replies under the
        # origin call (config-switchable, restart-safe via outcome_posted)
        if bot is not None and cfg.post_24h_outcome_reply:
            for final in forward.unposted_final_calls(conn):
                if final["alert_message_id"] is not None:
                    bot.send(alert_fmt.format_forward_outcome(
                                 final["status"], final["mfe"], final["mae"],
                                 final["rugged"], final["mcap_final"]),
                             reply_to_message_id=final["alert_message_id"])
                forward.mark_outcome_posted(conn, final["call_id"])

        # phase 5: alerts, gated per message type
        alerts_sent = suppressed = 0
        for msg, p in new_items:
            if bot is None or p.is_duplicate \
                    or not cfg.alert_types.get(p.message_type, False):
                continue
            if alerts_sent >= cfg.alerts_max_per_cycle:
                # hit is still stored in SQLite, only the alert is dropped
                suppressed += 1
                continue
            if send_alert(conn, bot, msg, p, market):
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
