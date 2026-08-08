"""Locks in the two failure modes found during pretest.py:
1. CAs hidden in link hrefs must be found.
2. Money amounts ($70.3K) must not become tickers.
"""

from tracker.extract import (
    dedupe_hash,
    extract_addresses,
    extract_tickers,
    normalize_text,
)

CA = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"  # TRUMP mint, valid base58


class TestAddresses:
    def test_address_in_visible_text(self):
        assert extract_addresses(f"new gem {CA} send it", []) == [CA]

    def test_address_only_in_href(self):
        # the pretest finding: CA only behind a CHART button link
        links = [f"https://dexscreener.com/solana/{CA}"]
        assert extract_addresses("CHART 📈 | Buy now!", links) == [CA]

    def test_address_in_urlencoded_query(self):
        links = [f"https://example.com/swap?outputCurrency%3D{CA}"]
        assert extract_addresses("", links) == [CA]

    def test_dedupe_text_and_link(self):
        links = [f"https://pump.fun/coin/{CA}"]
        assert extract_addresses(f"ape {CA}", links) == [CA]

    def test_no_false_positive_on_short_words(self):
        assert extract_addresses("moon soon 100x guaranteed", []) == []


class TestTickers:
    def test_plain_ticker(self):
        assert extract_tickers("buy $WIF now") == ["WIF"]

    def test_money_amounts_rejected(self):
        # the pretest finding: naive $-pattern matched money amounts
        assert extract_tickers("mcap $70.3K, vol $5M, target $1B") == []

    def test_pure_number_rejected(self):
        assert extract_tickers("made $700 today") == []

    def test_ticker_with_digits_kept(self):
        assert extract_tickers("$PEPE2 is back") == ["PEPE2"]

    def test_mixed_line(self):
        assert extract_tickers("$BONK from $12.5K to $4M !!") == ["BONK"]

    def test_lowercase_normalised(self):
        assert extract_tickers("$wif $WIF") == ["WIF"]


class TestDedupe:
    def test_forward_wave_collapses(self):
        a = "🚀 $GEM sending!  https://t.me/chan1/5 GO GO"
        b = "🚀 $GEM   sending! https://ref.link/xyz GO GO"
        assert dedupe_hash(a) == dedupe_hash(b)

    def test_different_text_differs(self):
        assert dedupe_hash("$GEM pumping") != dedupe_hash("$GEM dumping")

    def test_normalize_strips_urls_and_case(self):
        assert normalize_text("BUY https://x.com/a NOW") == "buy now"
