"""Launch-baseline collector: admission, coverage, comparison, deployers."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tracker import baseline, db, forward
from tracker.models import EnrichedToken

CA1 = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
CA2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CA3 = "BfjTeD53ovaDmFTjaDTLtzdsaDdZxUGrYoNWKnL7iDnP"
WSOL = "So11111111111111111111111111111111111111112"

CFG = SimpleNamespace(baseline_max_new_per_cycle=10, baseline_max_pool_age_min=60,
                      forward_checkpoints_min=[15, 60, 240, 1440])


def _e(addr, pool_age_min=5.0, socials=None) -> EnrichedToken:
    created = (datetime.now(timezone.utc)
               - timedelta(minutes=pool_age_min)).isoformat(timespec="seconds")
    return EnrichedToken(address=addr, chain_id="solana", dex_id="pumpswap",
                         pair_address="P" + addr[:6], price_usd=0.001,
                         mcap=30_000.0, liquidity_usd=8_000.0,
                         pool_created_at=created,
                         socials_json=json.dumps(socials) if socials else None)


class FakeClient:
    def __init__(self, profiles, tokens):
        self._profiles = profiles
        self._tokens = tokens
        self.profile_calls = 0

    def fetch_token_profiles(self):
        self.profile_calls += 1
        return self._profiles

    def fetch_tokens(self, addresses):
        return {a: self._tokens.get(a) for a in addresses}


def _profile(addr):
    return {"address": addr, "links_json": None}


class TestDiscovery:
    def test_admission_creates_token_and_baseline_call(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1)], {CA1: _e(CA1)})
        assert baseline.run_discovery(conn, CFG, client, frozenset()) == 1
        row = conn.execute("SELECT chain, ticker, enrich_status, mentioned_ts"
                           " FROM baseline_tokens").fetchone()
        assert row[0] == "solana" and row[2] == "ok" and row[3] is None
        call = conn.execute("SELECT is_baseline, status FROM calls").fetchone()
        assert call == (1, "open")
        n_cp = conn.execute("SELECT COUNT(*) FROM call_checkpoints").fetchone()[0]
        assert n_cp == 4

    def test_known_and_ignored_skipped(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(WSOL), _profile(CA1)], {CA1: _e(CA1)})
        assert baseline.run_discovery(conn, CFG, client, frozenset([WSOL])) == 1
        # second pass: CA1 already known, nothing new
        assert baseline.run_discovery(conn, CFG, client, frozenset([WSOL])) == 0
        assert conn.execute("SELECT COUNT(*) FROM baseline_tokens").fetchone()[0] == 1

    def test_admission_cap(self):
        conn = db.connect(":memory:")
        cas = [CA1, CA2, CA3]
        cfg = SimpleNamespace(**{**CFG.__dict__, "baseline_max_new_per_cycle": 2})
        client = FakeClient([_profile(c) for c in cas], {c: _e(c) for c in cas})
        assert baseline.run_discovery(conn, cfg, client, frozenset()) == 2

    def test_old_pool_rejected(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1)], {CA1: _e(CA1, pool_age_min=600)})
        assert baseline.run_discovery(conn, CFG, client, frozenset()) == 0
        assert conn.execute("SELECT COUNT(*) FROM baseline_tokens").fetchone()[0] == 0

    def test_pre_pool_profile_admitted_as_no_pool_candidate(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1)], {CA1: None})
        assert baseline.run_discovery(conn, CFG, client, frozenset()) == 1
        status = conn.execute("SELECT enrich_status FROM baseline_tokens").fetchone()[0]
        assert status is None  # unresolved, forward no_pool machinery takes over

    def test_already_mentioned_token_still_admitted_and_flagged(self):
        conn = db.connect(":memory:")
        src = db.get_or_create_source(conn, "telegram", "chan_a")
        db.insert_mention(conn, platform="telegram", source_id=src,
                          external_id="chan_a/1", ts_utc="2026-08-09T10:00:00+00:00",
                          raw_text=f"call {CA1}", ticker=None, contract_address=CA1,
                          chain="solana", views=None, is_duplicate=False,
                          dedupe_hash="h")
        client = FakeClient([_profile(CA1)], {CA1: _e(CA1)})
        assert baseline.run_discovery(conn, CFG, client, frozenset()) == 1
        mentioned = conn.execute("SELECT mentioned_ts FROM baseline_tokens").fetchone()[0]
        assert mentioned == "2026-08-09T10:00:00+00:00"

    def test_mentioned_flag_synced_later(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1)], {CA1: _e(CA1)})
        baseline.run_discovery(conn, CFG, client, frozenset())
        src = db.get_or_create_source(conn, "telegram", "chan_a")
        db.insert_mention(conn, platform="telegram", source_id=src,
                          external_id="chan_a/2", ts_utc="2026-08-09T11:00:00+00:00",
                          raw_text=f"late call {CA1}", ticker=None,
                          contract_address=CA1, chain="solana", views=None,
                          is_duplicate=False, dedupe_hash="h2")
        assert baseline.sync_mentioned_flags(conn) == 1
        mentioned = conn.execute("SELECT mentioned_ts FROM baseline_tokens").fetchone()[0]
        assert mentioned == "2026-08-09T11:00:00+00:00"


class TestStatsSeparation:
    def test_channel_stats_exclude_baseline_calls(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1)], {CA1: _e(CA1)})
        baseline.run_discovery(conn, CFG, client, frozenset())
        stats = forward.channel_stats(conn)
        assert [s["channel"] for s in stats] == ["(all)"]
        assert stats[0]["calls_open"] == 0

    def test_group_comparison_and_coverage(self):
        conn = db.connect(":memory:")
        client = FakeClient([_profile(CA1), _profile(CA2)],
                            {CA1: _e(CA1), CA2: _e(CA2)})
        baseline.run_discovery(conn, CFG, client, frozenset())
        src = db.get_or_create_source(conn, "telegram", "chan_a")
        db.insert_mention(conn, platform="telegram", source_id=src,
                          external_id="chan_a/1", ts_utc="2026-08-09T10:00:00+00:00",
                          raw_text=f"x {CA1}", ticker=None, contract_address=CA1,
                          chain="solana", views=None, is_duplicate=False,
                          dedupe_hash="h")
        baseline.sync_mentioned_flags(conn)
        comp = baseline.group_comparison(conn)
        assert comp["baseline_total"] == 2
        assert comp["baseline_mentioned"] == 1
        assert comp["coverage"] == 0.5


class TestTwitterHandles:
    def test_handle_extraction(self):
        socials = json.dumps([
            {"type": "twitter", "url": "https://x.com/jeetassassin/status/208622"},
            {"type": "twitter", "url": "https://twitter.com/@SomeDev"},
            {"type": "telegram", "url": "https://t.me/somechannel"},
            {"type": "twitter", "url": "https://x.com/intent/tweet?text=hi"},
        ])
        assert baseline.twitter_handles(socials) == ["jeetassassin", "somedev"]

    def test_garbage_json(self):
        assert baseline.twitter_handles(None) == []
        assert baseline.twitter_handles("not json") == []
        assert baseline.twitter_handles('{"a": 1}') == []

    def test_serial_deployers(self):
        conn = db.connect(":memory:")
        socials = json.dumps([{"type": "twitter", "url": "https://x.com/serialdev"}])
        client = FakeClient(
            [_profile(CA1), _profile(CA2)],
            {CA1: _e(CA1, socials=[{"type": "twitter",
                                    "url": "https://x.com/serialdev"}]),
             CA2: _e(CA2, socials=[{"type": "twitter",
                                    "url": "https://x.com/serialdev"}])})
        baseline.run_discovery(conn, CFG, client, frozenset())
        # a third token via the tokens table with the same handle
        conn.execute("INSERT INTO tokens (contract_address, chain, socials_json)"
                     " VALUES (?, 'solana', ?)", (CA3, socials))
        conn.commit()
        deployers = baseline.serial_deployers(conn)
        assert len(deployers) == 1
        assert deployers[0]["handle"] == "serialdev"
        assert deployers[0]["n_tokens"] == 3
