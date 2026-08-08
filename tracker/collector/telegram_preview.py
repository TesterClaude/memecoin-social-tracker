"""t.me/s/<channel> public-preview collector.

Knows only the Telegram web-preview DOM; emits RawMessage objects and nothing
else. No auth, no ban risk, public channels only, ~last 20 messages per fetch.

DOM notes (verified against live pages):
- each message: div.tgme_widget_message with data-post="channel/12345"
- visible text: div.tgme_widget_message_text (absent for media-only posts)
- timestamp:    time[datetime] inside the message footer
- views:        span.tgme_widget_message_views, human-formatted ("70.3K")
- hrefs: contract addresses often live ONLY in <a href> attributes (CHART /
  Buy buttons), so every href in the message block is collected.
"""

import logging
import re
import time as _time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from tracker.models import RawMessage

log = logging.getLogger(__name__)

_VIEWS_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def newest_message_age_days(messages: list[RawMessage]) -> float | None:
    """Age in days of the newest message timestamp, None if no timestamps."""
    stamps = [m.ts_utc for m in messages if m.ts_utc]
    if not stamps:
        return None
    newest = max(datetime.fromisoformat(t) for t in stamps)
    return (datetime.now(timezone.utc) - newest).total_seconds() / 86400


def parse_views(raw: str | None) -> int | None:
    """'70.3K' -> 70300, '512' -> 512."""
    if not raw:
        return None
    raw = raw.strip().replace(",", ".")
    m = re.match(r"^([\d.]+)\s*([KMB]?)$", raw, re.IGNORECASE)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return int(value * _VIEWS_SUFFIX.get(m.group(2).upper(), 1))


def parse_preview_html(html: str, channel: str) -> list[RawMessage]:
    soup = BeautifulSoup(html, "html.parser")
    messages: list[RawMessage] = []
    for block in soup.select("div.tgme_widget_message"):
        post = block.get("data-post")  # "channel/12345"
        if not post:
            continue
        text_div = block.select_one("div.tgme_widget_message_text")
        text = text_div.get_text(" ", strip=True) if text_div else ""

        links = [a["href"] for a in block.select("a[href]")]

        time_el = block.select_one("time[datetime]")
        ts_utc = time_el["datetime"] if time_el else None

        views_el = block.select_one("span.tgme_widget_message_views")
        views = parse_views(views_el.get_text(strip=True) if views_el else None)

        messages.append(RawMessage(
            platform="telegram",
            channel=channel,
            external_id=post,
            url=f"https://t.me/{post}",
            text=text,
            links=links,
            ts_utc=ts_utc,
            views=views,
        ))
    return messages


class TelegramPreviewCollector:
    def __init__(self, user_agent: str, timeout_s: int, backoff_on_429_s: int,
                 stale_after_days: int = 14):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._timeout = timeout_s
        self._backoff_429 = backoff_on_429_s
        self._stale_days = stale_after_days
        self._blocked_until: dict[str, float] = {}
        self._stale_warned: set[str] = set()

    def fetch(self, channel: str) -> list[RawMessage]:
        """Fetch and parse one channel. Returns [] on any failure — the poll
        loop must never die because one channel is unreachable."""
        if _time.monotonic() < self._blocked_until.get(channel, 0):
            log.info("skipping %s (429 backoff active)", channel)
            return []
        try:
            resp = self._session.get(f"https://t.me/s/{channel}", timeout=self._timeout)
        except requests.RequestException as e:
            log.warning("fetch failed for %s: %s", channel, e)
            return []
        if resp.status_code == 429:
            self._blocked_until[channel] = _time.monotonic() + self._backoff_429
            log.warning("429 for %s, backing off %ss", channel, self._backoff_429)
            return []
        if resp.status_code != 200:
            log.warning("HTTP %s for %s", resp.status_code, channel)
            return []
        # Channels without a public preview redirect to t.me/<channel>
        if "tgme_widget_message" not in resp.text:
            log.warning("no preview content for %s (private or no preview?)", channel)
            return []
        messages = parse_preview_html(resp.text, channel)

        # Dead-channel guard: if even the newest post is old, the channel is
        # dormant — skip it (warn once, not every cycle; re-check each fetch
        # so it comes back automatically when the channel posts again).
        age = newest_message_age_days(messages)
        if age is not None and age > self._stale_days:
            if channel not in self._stale_warned:
                log.warning(
                    "channel %s is stale (newest post %.0f days old, threshold %dd)"
                    " — skipping until it posts again", channel, age, self._stale_days)
                self._stale_warned.add(channel)
            return []
        self._stale_warned.discard(channel)
        return messages
