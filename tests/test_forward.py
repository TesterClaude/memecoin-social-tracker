"""Forward-testing log: time-injected tests, in-memory DB, no network."""

from datetime import datetime, timedelta, timezone

import pytest

from tracker import db, forward
from tracker.models import EnrichedToken

CA = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"

T0 = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _conn():
    conn = db.connect(":memory:")
    return conn, db.get_or_create_source(conn, "telegram", "chan_a")


def _market(price=100.0, liq=5000.0, mcap=100_000.0) -> EnrichedToken:
    return EnrichedToken(address=CA, chain_id="solana", dex_id="pumpswap",
                         pair_address="P1", price_usd=price,
                         liquidity_usd=liq, mcap=mcap)


def _measure(conn, e, at: datetime, floor=1000.0) -> int:
    return forward.record_measurements(conn, {CA: e}, floor, now=at)


class TestCreateCall:
    def test_first_mention_creates_call_and_checkpoints(self):
        conn, src = _conn()
        cid = forward.create_call(conn, src, CA, T0.isoformat(), _market(), now=T0)
        assert cid is not None
        dues = [r[0] for r in conn.execute(
            "SELECT due_ts FROM call_checkpoints ORDER BY checkpoint_min")]
        assert dues == [(T0 + timedelta(minutes=m)).isoformat(timespec="seconds")
                        for m in (15, 60, 240, 1440)]
        row = conn.execute("SELECT price_at_call, status FROM calls").fetchone()
        assert row == (100.0, "open")

    def test_second_mention_no_new_call(self):
        conn, src = _conn()
        assert forward.create_call(conn, src, CA, T0.isoformat(), _market(), now=T0)
        assert forward.create_call(conn, src, CA, T0.isoformat(), _market(),
                                   now=T0 + timedelta(minutes=5)) is None

    def test_late_ingested_mention_skipped(self):
        # mention older than the first checkpoint interval: its "+15m" is
        # already in the past — no entry, no fake forward data
        conn, src = _conn()
        assert forward.create_call(conn, src, CA,
                                   (T0 - timedelta(minutes=20)).isoformat(),
                                   _market(), now=T0) is None

    def test_pre_pool_call_without_baseline(self):
        conn, src = _conn()
        assert forward.create_call(conn, src, CA, T0.isoformat(), None, now=T0)
        row = conn.execute("SELECT price_at_call, baseline_ts FROM calls").fetchone()
        assert row == (None, None)


class TestMeasurement:
    def test_due_selection(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), _market(), now=T0)
        assert forward.due_checkpoint_addresses(conn, now=T0 + timedelta(minutes=10)) == []
        assert forward.due_checkpoint_addresses(conn, now=T0 + timedelta(minutes=16)) == [CA]

    def test_mfe_mae_over_checkpoints(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), _market(price=100), now=T0)
        _measure(conn, _market(price=180), T0 + timedelta(minutes=16))
        _measure(conn, _market(price=60), T0 + timedelta(minutes=61))
        mfe, mae = conn.execute("SELECT outcome_mfe, outcome_mae FROM calls").fetchone()
        assert round(mfe) == 80 and round(mae) == -40

    def test_rug_vs_plain_price_drop(self):
        # plain drop: price -80% but liquidity intact -> NOT a rug
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(),
                            _market(price=100, liq=5000), now=T0)
        _measure(conn, _market(price=20, liq=4000), T0 + timedelta(minutes=16))
        assert conn.execute("SELECT liq_gone FROM call_checkpoints"
                            " WHERE checkpoint_min=15").fetchone() == (0,)
        # then liquidity collapses below the floor -> rug flag
        _measure(conn, _market(price=15, liq=80.0), T0 + timedelta(minutes=61))
        assert conn.execute("SELECT liq_gone FROM call_checkpoints"
                            " WHERE checkpoint_min=60").fetchone() == (1,)

    def test_vanished_pair_is_rug_and_minus_100(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(),
                            _market(price=100, liq=5000), now=T0)
        _measure(conn, None, T0 + timedelta(minutes=16))
        cp = conn.execute("SELECT pair_missing, liq_gone FROM call_checkpoints"
                          " WHERE checkpoint_min=15").fetchone()
        assert cp == (1, 1)
        mae = conn.execute("SELECT outcome_mae FROM calls").fetchone()[0]
        assert mae == -100.0

    def test_no_pool_outcome_survives_in_stats(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), None, now=T0)
        for minutes in (16, 61, 241, 1441):
            _measure(conn, None, T0 + timedelta(minutes=minutes))
        status, mfe = conn.execute("SELECT status, outcome_mfe FROM calls").fetchone()
        assert status == "no_pool" and mfe is None
        stats = forward.channel_stats(conn)
        assert stats[0]["no_pool_share"] == 1.0
        assert stats[0]["calls_completed"] == 1

    def test_late_baseline_from_first_priced_checkpoint(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), None, now=T0)
        _measure(conn, None, T0 + timedelta(minutes=16))          # still no pool
        _measure(conn, _market(price=50), T0 + timedelta(minutes=61))  # pool now
        price0, baseline_ts = conn.execute(
            "SELECT price_at_call, baseline_ts FROM calls").fetchone()
        assert price0 == 50 and baseline_ts is not None
        _measure(conn, _market(price=100), T0 + timedelta(minutes=241))
        mfe = conn.execute("SELECT outcome_mfe FROM calls").fetchone()[0]
        assert round(mfe) == 100  # +100% vs the LATE baseline

    def test_failed_request_leaves_checkpoint_unmeasured(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), _market(), now=T0)
        # address absent from results = failed request, not "no pair"
        n = forward.record_measurements(conn, {}, 1000.0,
                                        now=T0 + timedelta(minutes=16))
        assert n == 0
        assert forward.due_checkpoint_addresses(
            conn, now=T0 + timedelta(minutes=17)) == [CA]

    def test_catchup_marks_earlier_slots_missed(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), _market(price=100), now=T0)
        # +15m and +1h both overdue: only the +1h slot gets the observation
        _measure(conn, _market(price=140), T0 + timedelta(minutes=70))
        cp15 = conn.execute("SELECT measured_ts IS NOT NULL, price_usd"
                            " FROM call_checkpoints WHERE checkpoint_min=15").fetchone()
        cp60 = conn.execute("SELECT price_usd FROM call_checkpoints"
                            " WHERE checkpoint_min=60").fetchone()
        assert cp15 == (1, None)   # closed as missed, no fabricated value
        assert cp60 == (140.0,)

    def test_completion_sets_done(self):
        conn, src = _conn()
        forward.create_call(conn, src, CA, T0.isoformat(), _market(price=100), now=T0)
        for minutes, price in ((16, 120), (61, 90), (241, 200), (1441, 10)):
            _measure(conn, _market(price=price), T0 + timedelta(minutes=minutes))
        status, mfe, mae = conn.execute(
            "SELECT status, outcome_mfe, outcome_mae FROM calls").fetchone()
        assert status == "done"
        assert round(mfe) == 100 and round(mae) == -90


class TestChannelStats:
    def test_over_50_share_and_medians(self):
        conn, src = _conn()
        cas = [CA[:-1] + c for c in "abc"]
        for i, ca in enumerate(cas):
            forward.create_call(conn, src, ca, T0.isoformat(),
                                _market(price=100), now=T0)
        outcomes = {cas[0]: 200.0, cas[1]: 40.0, cas[2]: 90.0}  # +100%, -60%, -10%
        for minutes in (16, 61, 241, 1441):
            forward.record_measurements(
                conn, {ca: _market(price=p) for ca, p in outcomes.items()},
                1000.0, now=T0 + timedelta(minutes=minutes))
        (s,) = [x for x in forward.channel_stats(conn) if x["channel"] == "chan_a"]
        assert s["calls_completed"] == 3
        assert s["over_50_share"] == 1 / 3
        assert s["median_mfe"] == 0.0                        # per-call MFEs: [100, 0, 0]
        assert s["median_mae"] == pytest.approx(-10.0)       # per-call MAEs: [0, -60, -10]
