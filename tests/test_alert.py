"""Alert formatting — output structure per message type, no network."""

from tracker.alert import (
    format_compact,
    format_forward_outcome,
    format_new_call,
    format_outcome,
)
from tracker.models import AlertFacts, EnrichedToken, ExtractionResult, RawMessage

CA = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"


def _msg(text="buy now") -> RawMessage:
    return RawMessage(platform="telegram", channel="SolEarlySignal",
                      external_id="SolEarlySignal/1",
                      url="https://t.me/SolEarlySignal/1", text=text,
                      ts_utc="2026-08-09T12:00:00+00:00")


def _result(addresses=None, tickers=None) -> ExtractionResult:
    return ExtractionResult(
        addresses=[CA] if addresses is None else addresses,
        tickers=["TOLY"] if tickers is None else tickers,
        dedupe_hash="h")


def _e() -> EnrichedToken:
    return EnrichedToken(address=CA, chain_id="solana", dex_id="pumpswap",
                         pair_address="P", mcap=36_200.0, liquidity_usd=12_800.0,
                         vol_1h=42_000.0, txns_buy=120, txns_sell=80,
                         price_usd=0.001)


class TestNewCallFormat:
    def test_full_block(self):
        text = format_new_call(_msg(), _result(), _e(),
                               AlertFacts(chain_position=1))
        assert "🆕 NEUER CALL · SolEarlySignal" in text
        assert "$TOLY" in text and f"<code>{CA}</code>" in text
        assert "MC $36.2K · Liq $12.8K" in text
        assert "Vol 1h $42.0K · Tx 1h 120/80" in text
        assert "🥇 Erstmeldung" in text
        assert "dexscreener.com/solana/" in text and "t.me/SolEarlySignal/1" in text

    def test_chain_position_line(self):
        facts = AlertFacts(chain_position=3, first_channel="AlphaStrikeSol",
                           minutes_after_first=12.4)
        text = format_new_call(_msg(), _result(), _e(), facts)
        assert "#3 · 12 min nach @AlphaStrikeSol" in text

    def test_collision_warning(self):
        facts = AlertFacts(ticker_collisions_24h=3)
        text = format_new_call(_msg(), _result(), _e(), facts)
        assert "⚠️ Ticker-Kollision: 3 Contracts für $TOLY in 24h" in text

    def test_prepool_labels(self):
        text = format_new_call(_msg(), _result(), _e(),
                               AlertFacts(prepool_lead_min=42.0))
        assert "42 min vor Pool-Erstellung" in text
        text2 = format_new_call(_msg(), _result(), None,
                                AlertFacts(pool_missing=True))
        assert "Pool existiert noch nicht" in text2


class TestOutcomeFormat:
    def test_claim_vs_current(self):
        msg = _msg("TOLY is up 2.0X from Entry Signal $28K → $56K")
        text = format_outcome(msg, _result(), _e(), has_origin=True)
        assert "📈 OUTCOME" in text
        # the channel's claim is quoted verbatim ("is up 2.0X")
        assert "Kanal behauptet: is up 2.0X · $28K → $56K" in text
        assert "Aktuell: MC $36.2K" in text
        assert "kein Ursprungs-Call" not in text

    def test_missing_origin_hint(self):
        msg = _msg("up 2.0X from call")
        text = format_outcome(msg, _result(), None, has_origin=False)
        assert "(kein Ursprungs-Call im Kanal)" in text

    def test_fallback_ticker_from_tokens_row(self):
        # "tod is up 86%" — no $-prefix, extraction finds no ticker
        msg = _msg("tod is up 86% from Entry Signal")
        result = _result(tickers=[])
        text = format_outcome(msg, result, _e(), has_origin=True,
                              fallback_ticker="TOD")
        assert "Ticker: $TOD" in text

    def test_message_ticker_beats_fallback(self):
        msg = _msg("$PINKY is up 2.0X")
        text = format_outcome(msg, _result(tickers=["PINKY"]), _e(),
                              has_origin=True, fallback_ticker="OLD")
        assert "Ticker: $PINKY" in text and "$OLD" not in text

    def test_no_ticker_anywhere_no_line(self):
        msg = _msg("up 2.0X from call")
        text = format_outcome(msg, _result(tickers=[]), _e(), has_origin=True)
        assert "Ticker:" not in text


class TestCompactFormat:
    def test_list_line(self):
        result = _result(addresses=[], tickers=["WIF", "BONK", "MEW"])
        text = format_compact(_msg("trending"), result, "LIST")
        assert text.count("\n") == 0
        assert "📋" in text and "$WIF $BONK $MEW" in text

    def test_commentary_line(self):
        result = _result(addresses=[], tickers=[])
        text = format_compact(_msg("Market is bleeding"), result, "COMMENTARY")
        assert text.count("\n") == 0 and "💬" in text and "Market is bleeding" in text


class TestForwardOutcomeFormat:
    def test_done(self):
        text = format_forward_outcome("done", 120.0, -35.0, False, 88_000.0)
        assert text == "⏱️ +24h: MC $88.0K · MFE +120% · MAE -35%"

    def test_rug(self):
        text = format_forward_outcome("done", 40.0, -100.0, True, 500.0)
        assert "💀 Liquidität weg (Rug)" in text

    def test_no_pool(self):
        assert format_forward_outcome("no_pool", None, None, False, None) \
            == "⏱️ +24h: kein Pool entstanden"
