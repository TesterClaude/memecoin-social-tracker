"""M1 entrypoint: poll t.me/s/ channels -> extract -> store -> alert raw hits.

Ctrl+C stops cleanly. One unreachable channel never kills the loop.
"""

import logging
import time

from tracker import db, extract
from tracker.alert import AlertBot, format_alert
from tracker.collector.telegram_preview import TelegramPreviewCollector
from tracker.config import load_config
from tracker.models import ExtractionResult, RawMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("run")


def process_message(conn, cfg, msg: RawMessage) -> ExtractionResult | None:
    """Store a new message; return its extraction result if it is a fresh
    (non-duplicate) hit that deserves an alert, else None."""
    if db.mention_exists(conn, msg.platform, msg.external_id):
        return None

    result = ExtractionResult(
        addresses=extract.extract_addresses(msg.text, msg.links),
        tickers=extract.extract_tickers(msg.text, cfg.ticker_min_len, cfg.ticker_max_len),
        dedupe_hash=extract.dedupe_hash(msg.text),
    )
    # Copy-paste forward wave: same normalised text already seen in another
    # channel. Stored (N forwards, one idea) but not re-alerted.
    is_dup = bool(msg.text) and db.is_known_hash(conn, result.dedupe_hash)

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
    for addr in result.addresses:
        db.upsert_token_first_seen(
            conn, addr, cfg.chain,
            result.tickers[0] if result.tickers else None, msg.ts_utc,
        )

    if result.has_hit and not is_dup:
        return result
    return None


def main() -> None:
    cfg = load_config()
    conn = db.connect(cfg.db_path)
    collector = TelegramPreviewCollector(
        cfg.user_agent, cfg.request_timeout_s, cfg.backoff_on_429_s
    )
    bot = AlertBot(cfg.bot_token, cfg.alert_chat_id, cfg.alerts_send_delay_s) \
        if cfg.alerts_enabled else None

    log.info("M1 collector started: %d channels, poll every %ds, alerts %s",
             len(cfg.channels), cfg.poll_interval_s,
             "on" if bot else "off")

    while True:
        cycle_start = time.monotonic()
        alerts_sent = suppressed = 0
        for channel in cfg.channels:
            for msg in collector.fetch(channel):
                hit = process_message(conn, cfg, msg)
                if hit is None or bot is None:
                    continue
                if alerts_sent >= cfg.alerts_max_per_cycle:
                    # hit is still stored in SQLite, only the alert is dropped
                    suppressed += 1
                    continue
                if bot.send(format_alert(msg, hit)):
                    alerts_sent += 1
        elapsed = time.monotonic() - cycle_start
        log.info("cycle done in %.1fs, %d alerts sent%s", elapsed, alerts_sent,
                 f", {suppressed} suppressed by cap" if suppressed else "")
        time.sleep(max(0.0, cfg.poll_interval_s - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
