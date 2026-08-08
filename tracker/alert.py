"""Raw-match alert sender via the Telegram Bot API.

Deliberately dumb: takes a finished hit, formats it, sends it. Scoring and
tiering slot in front of this module in M2 without touching it.
"""

import html
import logging
import time
from datetime import datetime, timezone

import requests

from tracker.models import EnrichedToken, ExtractionResult, RawMessage

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "?"
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= threshold:
            return f"${value / threshold:.1f}{suffix}"
    return f"${value:.0f}"


def fmt_pool_age(pool_created_at: str | None) -> str:
    if not pool_created_at:
        return "?"
    try:
        created = datetime.fromisoformat(pool_created_at)
    except ValueError:
        return "?"
    minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60
    if minutes < 120:
        return f"{minutes:.0f}m"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def fmt_run_up(e: EnrichedToken) -> str:
    """Percent the token has run since pool creation — only derivable while
    the pool is younger than 24h (h24 window covers its whole life)."""
    if e.pool_created_at and e.price_change_h24 is not None:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(e.pool_created_at)).total_seconds() / 3600
        except ValueError:
            return "n/a"
        if age_h < 24:
            return f"{e.price_change_h24:+.0f}%"
    return "n/a"


def market_line(e: EnrichedToken) -> str:
    return (f"MC {fmt_usd(e.mcap)} · Liq {fmt_usd(e.liquidity_usd)}"
            f" · pool {fmt_pool_age(e.pool_created_at)}"
            f" · since pool {fmt_run_up(e)}")


def format_alert(msg: RawMessage, result: ExtractionResult,
                 market: dict[str, EnrichedToken] | None = None) -> str:
    lines = [f"🚨 <b>@{html.escape(msg.channel)}</b>"]
    if result.tickers:
        lines.append("Ticker: " + " ".join(f"${html.escape(t)}" for t in result.tickers))
    for addr in result.addresses:
        lines.append(f"CA: <code>{html.escape(addr)}</code>")
        e = (market or {}).get(addr)
        if e is not None:
            lines.append(market_line(e))
        lines.append(f'<a href="https://dexscreener.com/solana/{addr}">DexScreener</a>')
    if msg.text:
        snippet = msg.text[:280] + ("…" if len(msg.text) > 280 else "")
        lines.append(f"<i>{html.escape(snippet)}</i>")
    meta = []
    if msg.ts_utc:
        meta.append(msg.ts_utc)
    if msg.views is not None:
        meta.append(f"{msg.views:,} views")
    if meta:
        lines.append(" · ".join(meta))
    lines.append(f'<a href="{msg.url}">Original post</a>')
    return "\n".join(lines)


class AlertBot:
    def __init__(self, token: str, chat_id: str, send_delay_s: float = 1.1):
        self._url = _API.format(token=token)
        self._chat_id = chat_id
        self._delay = send_delay_s

    def send(self, text: str) -> bool:
        try:
            resp = requests.post(self._url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=15)
        except requests.RequestException as e:
            log.warning("alert send failed: %s", e)
            return False
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning("Bot API 429, waiting %ss", retry_after)
            time.sleep(retry_after)
            return self.send(text)
        if resp.status_code != 200:
            log.warning("Bot API %s: %s", resp.status_code, resp.text[:200])
            return False
        time.sleep(self._delay)
        return True
