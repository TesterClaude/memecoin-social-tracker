"""Pipeline-level dedupe, ignore-list and retry semantics (in-memory DB, no network)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from run import enrich_addresses, process_message
from tracker import db
from tracker.models import EnrichedToken, RawMessage

CA1 = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
CA2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WSOL = "So11111111111111111111111111111111111111112"

CFG = SimpleNamespace(chain="solana", ticker_min_len=2, ticker_max_len=10,
                      first_mention_proxy_window_min=30,
                      no_pairs_retry_window_h=24)


def _iso(minutes_ago: float = 0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _msg(channel: str, msg_id: int, text: str, links: list[str] | None = None,
         ts: str | None = None) -> RawMessage:
    return RawMessage(platform="telegram", channel=channel,
                      external_id=f"{channel}/{msg_id}",
                      url=f"https://t.me/{channel}/{msg_id}",
                      text=text, links=links or [],
                      ts_utc=ts or "2026-08-09T10:00:00+00:00")


def _conn():
    return db.connect(":memory:")


class FakeClient:
    """Stands in for DexScreenerClient; records what got queried."""

    def __init__(self, responses: dict):
        self._responses = responses
        self.queried: list[str] = []

    def fetch_tokens(self, addresses):
        self.queried.extend(addresses)
        return {a: self._responses.get(a) for a in addresses}


def _enriched(addr: str) -> EnrichedToken:
    return EnrichedToken(address=addr, chain_id="solana", dex_id="pumpswap",
                         pair_address="PAIR1", symbol="TEST", mcap=50_000.0,
                         price_usd=0.001, liquidity_usd=9_000.0)


class TestDedupe:
    def test_identical_forward_is_duplicate(self):
        conn = _conn()
        p1 = process_message(conn, CFG, _msg("chan_a", 1, f"APE NOW {CA1}"))
        assert not p1.is_duplicate and p1.result.addresses == [CA1] \
            and p1.first_seen == [CA1]
        # same text forwarded to another channel: stored, marked duplicate,
        # and the token is no longer first-seen
        p2 = process_message(conn, CFG, _msg("chan_b", 9, f"APE NOW {CA1}"))
        assert p2.is_duplicate and p2.first_seen == []
        dup = conn.execute(
            "SELECT is_duplicate FROM mentions WHERE external_id='chan_b/9'").fetchone()
        assert dup == (1,)

    def test_url_only_posts_do_not_suppress_each_other(self):
        # regression: two unrelated URL-only posts both normalise to "" and
        # used to collide on the empty hash, killing the second alert
        conn = _conn()
        m1 = _msg("chan_a", 1, "https://jup.ag/swap?buy=x", [f"https://jup.ag/swap?buy={CA1}"])
        m2 = _msg("chan_b", 2, "https://jup.ag/swap?buy=y", [f"https://jup.ag/swap?buy={CA2}"])
        p1 = process_message(conn, CFG, m1)
        p2 = process_message(conn, CFG, m2)
        assert not p1.is_duplicate and p1.result.addresses == [CA1]
        assert not p2.is_duplicate and p2.result.addresses == [CA2]
        dups = conn.execute("SELECT SUM(is_duplicate) FROM mentions").fetchone()[0]
        assert dups == 0

    def test_links_stored_as_json(self):
        conn = _conn()
        link = f"https://dexscreener.com/solana/{CA1}"
        process_message(conn, CFG, _msg("chan_a", 1, "CHART 📈", [link]))
        process_message(conn, CFG, _msg("chan_a", 2, "no links here"))
        rows = dict(conn.execute(
            "SELECT external_id, links_json FROM mentions").fetchall())
        assert rows["chan_a/1"] == f'["{link}"]'
        assert rows["chan_a/2"] is None

    def test_message_type_stored(self):
        conn = _conn()
        process_message(conn, CFG, _msg("chan_a", 1, f"New Trending! MC $16K {CA1}"))
        process_message(conn, CFG, _msg("chan_a", 2, "market is bleeding"))
        types = dict(conn.execute(
            "SELECT external_id, message_type FROM mentions").fetchall())
        assert types == {"chan_a/1": "NEW_CALL", "chan_a/2": "COMMENTARY"}

    def test_same_message_not_reingested(self):
        conn = _conn()
        m = _msg("chan_a", 1, f"gem {CA1}")
        assert process_message(conn, CFG, m) is not None
        assert process_message(conn, CFG, m) is None
        n = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
        assert n == 1


class TestIgnoreMints:
    def test_system_mint_not_stored_not_alertable(self):
        conn = _conn()
        m = _msg("chan_a", 1, "swap link", [f"https://jup.ag/swap?sell={WSOL}"])
        p = process_message(conn, CFG, m, frozenset([WSOL]))
        assert p.result.addresses == [] and p.first_seen == []
        assert p.message_type == "COMMENTARY"  # no CA left after filtering
        assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
        ca = conn.execute("SELECT contract_address FROM mentions").fetchone()
        assert ca == (None,)

    def test_real_token_survives_next_to_system_mint(self):
        conn = _conn()
        m = _msg("chan_a", 1, "swap",
                 [f"https://jup.ag/swap?sell={WSOL}&buy={CA1}"])
        p = process_message(conn, CFG, m, frozenset([WSOL]))
        assert p.result.addresses == [CA1] and p.first_seen == [CA1]
        assert p.message_type == "NEW_CALL"


class TestNoPairsRetry:
    def _ingest(self, conn, ca: str, minutes_ago: float) -> None:
        process_message(conn, CFG, _msg("chan_a", 1, f"pre-pool call {ca}",
                                        ts=_iso(minutes_ago)))

    def test_pre_pool_token_recovers_when_pool_appears(self):
        conn = _conn()
        self._ingest(conn, CA1, minutes_ago=5)
        # cycle 1: pool does not exist yet -> marked invalid_no_pairs
        assert enrich_addresses(conn, CFG, FakeClient({CA1: None}), {CA1}) == {}
        assert db.get_enrich_status(conn, CA1) == "invalid_no_pairs"
        # cycle 2: no new mention of CA1, but the retry rule re-checks it,
        # the pool exists now -> recovered
        client = FakeClient({CA1: _enriched(CA1)})
        market = enrich_addresses(conn, CFG, client, set())
        assert client.queried == [CA1]
        assert market[CA1].mcap == 50_000.0
        assert db.get_enrich_status(conn, CA1) == "ok"
        snaps = conn.execute("SELECT COUNT(*) FROM token_snapshots").fetchone()[0]
        assert snaps == 1

    def test_old_no_pairs_token_is_final(self):
        conn = _conn()
        self._ingest(conn, CA1, minutes_ago=30 * 60)  # first mention 30h ago
        db.set_enrich_status(conn, CA1, "invalid_no_pairs")
        client = FakeClient({CA1: _enriched(CA1)})
        market = enrich_addresses(conn, CFG, client, set())
        # outside the 24h window: not queried, stays invalid
        assert client.queried == []
        assert market == {}
        assert db.get_enrich_status(conn, CA1) == "invalid_no_pairs"

    def test_failed_retry_keeps_status_and_window_semantics(self):
        conn = _conn()
        self._ingest(conn, CA1, minutes_ago=60)
        db.set_enrich_status(conn, CA1, "invalid_no_pairs")
        client = FakeClient({CA1: None})  # still no pool
        assert enrich_addresses(conn, CFG, client, set()) == {}
        assert client.queried == [CA1]
        assert db.get_enrich_status(conn, CA1) == "invalid_no_pairs"

    def test_remention_of_young_no_pairs_not_queried_twice(self):
        conn = _conn()
        self._ingest(conn, CA1, minutes_ago=5)
        db.set_enrich_status(conn, CA1, "invalid_no_pairs")
        client = FakeClient({CA1: None})
        # CA1 is both re-mentioned this cycle AND in the retry set — must
        # appear in the query list exactly once
        enrich_addresses(conn, CFG, client, {CA1})
        assert client.queried == [CA1]
