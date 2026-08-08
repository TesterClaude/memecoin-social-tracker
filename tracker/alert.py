"""Raw-match alert sender via the Telegram Bot API.

Deliberately dumb: takes a finished hit, formats it, sends it. Scoring and
tiering slot in front of this module in M2 without touching it.
"""

import html
import logging
import time

import requests

from tracker.models import ExtractionResult, RawMessage

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"


def format_alert(msg: RawMessage, result: ExtractionResult) -> str:
    lines = [f"🚨 <b>@{html.escape(msg.channel)}</b>"]
    if result.tickers:
        lines.append("Ticker: " + " ".join(f"${html.escape(t)}" for t in result.tickers))
    for addr in result.addresses:
        lines.append(f"CA: <code>{html.escape(addr)}</code>")
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
