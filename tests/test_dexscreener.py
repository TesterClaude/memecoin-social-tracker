import pytest

from tracker.enrich.dexscreener import (
    DexScreenerClient,
    SchemaError,
    _chunks,
    estimate_mcap_at_creation,
    parse_tokens_response,
)

CA = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
CA2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WSOL = "So11111111111111111111111111111111111111112"


def _pair(base=CA, liq=10_000.0, chain="solana", **overrides):
    p = {
        "chainId": chain,
        "dexId": "raydium",
        "pairAddress": "PAIRADDR111",
        "baseToken": {"address": base, "name": "Test Coin", "symbol": "TEST"},
        "quoteToken": {"address": WSOL, "symbol": "SOL"},
        "priceUsd": "0.0015",
        "txns": {"h1": {"buys": 120, "sells": 80}},
        "volume": {"m5": 1500.5, "h1": 42000.0},
        "priceChange": {"h24": 210.0},
        "liquidity": {"usd": liq},
        "fdv": 150000,
        "marketCap": 140000,
        "pairCreatedAt": 1754700000000,
        "info": {"socials": [{"type": "twitter", "url": "https://x.com/test"}]},
    }
    p.update(overrides)
    return p


class TestParse:
    def test_happy_path(self):
        out = parse_tokens_response({"pairs": [_pair()]}, [CA])
        e = out[CA]
        assert e is not None
        assert e.price_usd == 0.0015
        assert e.mcap == 150000          # fdv preferred
        assert e.liquidity_usd == 10000
        assert e.vol_5m == 1500.5 and e.vol_1h == 42000.0
        assert e.txns_buy == 120 and e.txns_sell == 80
        assert e.price_change_h24 == 210.0
        assert e.pool_created_at == "2025-08-09T00:40:00+00:00"
        assert "twitter" in e.socials_json

    def test_no_pairs_means_none(self):
        out = parse_tokens_response({"pairs": None}, [CA])
        assert out == {CA: None}

    def test_best_pair_by_liquidity(self):
        low = _pair(liq=500.0, pairAddress="LOW")
        high = _pair(liq=90_000.0, pairAddress="HIGH")
        out = parse_tokens_response({"pairs": [low, high]}, [CA])
        assert out[CA].pair_address == "HIGH"

    def test_queried_as_quote_token_does_not_match(self):
        # WSOL is the quote in the pair; querying WSOL must not return it
        out = parse_tokens_response({"pairs": [_pair(base=CA)]}, [WSOL])
        assert out == {WSOL: None}

    def test_wrong_chain_filtered(self):
        out = parse_tokens_response({"pairs": [_pair(chain="ethereum")]}, [CA])
        assert out == {CA: None}

    def test_broken_pair_skipped_good_pair_kept(self):
        broken = {"chainId": "solana"}  # missing identity fields
        out = parse_tokens_response({"pairs": [broken, _pair(base=CA2)]}, [CA, CA2])
        assert out[CA] is None and out[CA2] is not None

    def test_garbage_numbers_become_none_not_garbage(self):
        p = _pair(priceUsd="not-a-price", fdv=None, marketCap="?")
        out = parse_tokens_response({"pairs": [p]}, [CA])
        assert out[CA].price_usd is None and out[CA].mcap is None

    def test_schema_change_raises_not_stores(self):
        with pytest.raises(SchemaError):
            parse_tokens_response({"data": []}, [CA])
        with pytest.raises(SchemaError):
            parse_tokens_response({"pairs": "wat"}, [CA])
        with pytest.raises(SchemaError):
            parse_tokens_response([1, 2, 3], [CA])


class TestMcapEstimate:
    def test_young_pool(self):
        # +200% since creation, mcap now 300k -> 100k at creation
        assert estimate_mcap_at_creation(300_000, 200.0, 3.0) == pytest.approx(100_000)

    def test_old_pool_none(self):
        assert estimate_mcap_at_creation(300_000, 200.0, 30.0) is None

    def test_minus_100_percent_guarded(self):
        assert estimate_mcap_at_creation(300_000, -100.0, 3.0) is None

    def test_missing_inputs(self):
        assert estimate_mcap_at_creation(None, 200.0, 3.0) is None
        assert estimate_mcap_at_creation(300_000, None, 3.0) is None
        assert estimate_mcap_at_creation(300_000, 200.0, None) is None


class TestChunks:
    def test_chunking(self):
        items = [str(i) for i in range(65)]
        chunks = _chunks(items, 30)
        assert [len(c) for c in chunks] == [30, 30, 5]
        assert sum(chunks, []) == items


def _fake_universe_client(universe: dict[str, list[dict]]) -> tuple[DexScreenerClient, list]:
    """Client whose _get simulates the real API's 30-pair response cap."""
    client = DexScreenerClient("http://test", 1, 0.0, 0)
    calls: list[list[str]] = []

    def fake_get(url: str):
        addrs = url.rsplit("/", 1)[1].split(",")
        calls.append(addrs)
        pairs = []
        for a in addrs:
            pairs.extend(universe.get(a, []))
        return {"pairs": pairs[:30]}  # the cap

    client._get = fake_get
    return client, calls


class TestTruncationBisect:
    def test_cutoff_address_is_recovered_not_marked_no_pairs(self):
        # CA has 30 pairs -> a joint query returns only CA's pairs and CA2
        # is cut off; the client must re-query CA2 instead of reporting None
        universe = {
            CA: [_pair(base=CA, liq=float(i), pairAddress=f"P{i}") for i in range(30)],
            CA2: [_pair(base=CA2, liq=777.0, pairAddress="C2PAIR")],
        }
        client, calls = _fake_universe_client(universe)
        out = client.fetch_tokens([CA, CA2])
        assert out[CA] is not None
        assert out[CA2] is not None and out[CA2].pair_address == "C2PAIR"
        assert calls[0] == [CA, CA2] and [CA2] in calls[1:]

    def test_uncapped_response_missing_address_is_genuinely_none(self):
        universe = {CA: [_pair(base=CA)]}
        client, calls = _fake_universe_client(universe)
        out = client.fetch_tokens([CA, CA2])
        # 1 pair total, far below the cap -> CA2 really has no pairs
        assert out[CA] is not None
        assert out[CA2] is None
        assert len(calls) == 1  # no needless re-queries

    def test_single_address_with_capped_pairs_terminates(self):
        universe = {CA: [_pair(base=CA, liq=float(i)) for i in range(35)]}
        client, calls = _fake_universe_client(universe)
        out = client.fetch_tokens([CA])
        assert out[CA] is not None
        assert len(calls) == 1
