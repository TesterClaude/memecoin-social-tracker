"""Alert formatting (one formatter per message type) and Bot API sending.

Facts only — no scoring, no recommendation. German labels per the agreed
alert mockup; code and logs stay English.

Note: holders / bundled % / sniper count are NOT available from
DexScreener REST — that line arrives with M5 (on-chain). Until then the
block shows the facts we do have (volume + transactions).
"""

import html
import logging
import time
from datetime import datetime, timezone

import requests

from tracker.classify import extract_outcome_claim
from tracker.models import AlertFacts, EnrichedToken, ExtractionResult, RawMessage

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
_SEPARATOR = "────────────"


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
    """Percent since pool creation — only derivable while the pool is
    younger than 24h (h24 window covers its whole life)."""
    if e.pool_created_at and e.price_change_h24 is not None:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(e.pool_created_at)).total_seconds() / 3600
        except ValueError:
            return "n/a"
        if age_h < 24:
            return f"{e.price_change_h24:+.0f}%"
    return "n/a"


def _market_lines(e: EnrichedToken) -> list[str]:
    lines = [f"MC {fmt_usd(e.mcap)} · Liq {fmt_usd(e.liquidity_usd)}"
             f" · Pool {fmt_pool_age(e.pool_created_at)}"
             f" · seit Pool {fmt_run_up(e)}"]
    if e.vol_1h is not None or e.txns_buy is not None:
        tx = (f"{e.txns_buy}/{e.txns_sell}"
              if e.txns_buy is not None and e.txns_sell is not None else "?")
        lines.append(f"Vol 1h {fmt_usd(e.vol_1h)} · Tx 1h {tx}")
    return lines


def _links_line(msg: RawMessage, addr: str | None) -> str:
    parts = []
    if addr:
        parts.append(f'<a href="https://dexscreener.com/solana/{addr}">DexScreener</a>')
    parts.append(f'<a href="{msg.url}">Original</a>')
    return " · ".join(parts)


def _fact_lines(facts: AlertFacts | None, result: ExtractionResult) -> list[str]:
    if facts is None:
        return []
    lines = []
    if facts.ticker_collisions_24h > 1 and result.tickers:
        lines.append(f"⚠️ Ticker-Kollision: {facts.ticker_collisions_24h} Contracts"
                     f" für ${html.escape(result.tickers[0])} in 24h")
    if facts.chain_position == 1:
        lines.append("🥇 Erstmeldung")
    elif facts.first_channel:
        lines.append(f"#{facts.chain_position} · {facts.minutes_after_first:.0f} min"
                     f" nach @{html.escape(facts.first_channel)}")
    if facts.prepool_lead_min is not None:
        lines.append(f"⏱️ Call {facts.prepool_lead_min:.0f} min vor Pool-Erstellung")
    elif facts.pool_missing:
        lines.append("⏱️ Call vor Pool-Erstellung — Pool existiert noch nicht")
    return lines


def format_new_call(msg: RawMessage, result: ExtractionResult,
                    e: EnrichedToken | None,
                    facts: AlertFacts | None = None) -> str:
    addr = result.addresses[0] if result.addresses else None
    lines = [f"🆕 NEUER CALL · {html.escape(msg.channel)}"]
    if result.tickers:
        lines.append("Ticker: " + " ".join(f"${html.escape(t)}" for t in result.tickers))
    if addr:
        lines.append(f"CA: <code>{html.escape(addr)}</code>")
    lines.append(_SEPARATOR)
    if e is not None:
        lines.extend(_market_lines(e))
    lines.extend(_fact_lines(facts, result))
    lines.append(_links_line(msg, addr))
    return "\n".join(lines)


def format_outcome(msg: RawMessage, result: ExtractionResult,
                   e: EnrichedToken | None, has_origin: bool,
                   facts: AlertFacts | None = None,
                   fallback_ticker: str | None = None) -> str:
    """fallback_ticker: from the tokens row of the CA — OUTCOME posts often
    name the token without a $-prefix, so message extraction finds none."""
    addr = result.addresses[0] if result.addresses else None
    lines = [f"📈 OUTCOME · {html.escape(msg.channel)}"]
    tickers = result.tickers or ([fallback_ticker] if fallback_ticker else [])
    if tickers:
        lines.append("Ticker: " + " ".join(f"${html.escape(t)}" for t in tickers))
    if addr:
        lines.append(f"CA: <code>{html.escape(addr)}</code>")
    claim = extract_outcome_claim(msg.text)
    lines.append(_SEPARATOR)
    if claim:
        lines.append(f"Kanal behauptet: {html.escape(claim)}")
    if e is not None:
        lines.append("Aktuell: " + _market_lines(e)[0])
    if not has_origin:
        lines.append("(kein Ursprungs-Call im Kanal)")
    lines.extend(_fact_lines(facts, result))
    lines.append(_links_line(msg, addr))
    return "\n".join(lines)


def format_compact(msg: RawMessage, result: ExtractionResult,
                   message_type: str) -> str:
    """Single-line format for LIST and COMMENTARY."""
    icon = "📋" if message_type == "LIST" else "💬"
    if result.tickers:
        body = " ".join(f"${html.escape(t)}" for t in result.tickers[:6])
        if len(result.tickers) > 6:
            body += f" (+{len(result.tickers) - 6})"
    else:
        snippet = " ".join(msg.text.split())[:90]
        body = f"„{html.escape(snippet)}…“" if len(msg.text) > 90 \
            else f"„{html.escape(snippet)}“"
    return (f"{icon} {html.escape(msg.channel)} · {body}"
            f' — <a href="{msg.url}">Original</a>')


def format_forward_outcome(status: str, mfe: float | None, mae: float | None,
                           rugged: bool, mcap_24h: float | None) -> str:
    """+24h forward-log measurement, posted as reply under the origin call."""
    if status == "no_pool":
        return "⏱️ +24h: kein Pool entstanden"
    parts = []
    if mcap_24h is not None:
        parts.append(f"MC {fmt_usd(mcap_24h)}")
    if mfe is not None:
        parts.append(f"MFE {mfe:+.0f}%")
    if mae is not None:
        parts.append(f"MAE {mae:+.0f}%")
    if rugged:
        parts.append("💀 Liquidität weg (Rug)")
    return "⏱️ +24h: " + (" · ".join(parts) if parts else "keine Messung")


class AlertBot:
    def __init__(self, token: str, chat_id: str, send_delay_s: float = 1.1):
        self._url = _API.format(token=token)
        self._chat_id = chat_id
        self._delay = send_delay_s

    def send(self, text: str,
             reply_to_message_id: int | None = None) -> int | None:
        """Send one alert; returns the Telegram message_id, None on failure."""
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            # if the origin alert was deleted, send normally instead of failing
            payload["allow_sending_without_reply"] = True
        try:
            resp = requests.post(self._url, json=payload, timeout=15)
        except requests.RequestException as e:
            log.warning("alert send failed: %s", e)
            return None
        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
            log.warning("Bot API 429, waiting %ss", retry_after)
            time.sleep(retry_after)
            return self.send(text, reply_to_message_id)
        if resp.status_code != 200:
            log.warning("Bot API %s: %s", resp.status_code, resp.text[:200])
            return None
        time.sleep(self._delay)
        try:
            return resp.json()["result"]["message_id"]
        except (ValueError, KeyError, TypeError):
            return None
