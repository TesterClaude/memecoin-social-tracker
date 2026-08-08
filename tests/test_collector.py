from datetime import datetime, timedelta, timezone

from tracker.collector.telegram_preview import newest_message_age_days, parse_views
from tracker.models import RawMessage


def _msg(ts: str | None) -> RawMessage:
    return RawMessage(platform="telegram", channel="c", external_id="c/1",
                      url="https://t.me/c/1", text="x", ts_utc=ts)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestStaleness:
    def test_fresh_channel(self):
        age = newest_message_age_days([_msg(_iso(30)), _msg(_iso(0.5))])
        assert age is not None and age < 1

    def test_stale_channel(self):
        age = newest_message_age_days([_msg(_iso(400)), _msg(_iso(390))])
        assert age is not None and age > 14

    def test_no_timestamps(self):
        assert newest_message_age_days([_msg(None), _msg(None)]) is None

    def test_empty_list(self):
        assert newest_message_age_days([]) is None


class TestParseViews:
    def test_plain(self):
        assert parse_views("512") == 512

    def test_suffixed(self):
        assert parse_views("70.3K") == 70300
        assert parse_views("1.2M") == 1_200_000

    def test_garbage(self):
        assert parse_views(None) is None
        assert parse_views("n/a") is None
