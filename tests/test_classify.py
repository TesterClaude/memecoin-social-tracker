"""Classification heuristics — patterns taken from real channel messages."""

from tracker.classify import (
    COMMENTARY,
    LIST,
    NEW_CALL,
    OUTCOME,
    classify,
    extract_outcome_claim,
)

CA = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
CA2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


class TestNewCall:
    def test_early_signal_with_ca(self):
        text = "🔥 RUNTIME New Trending 🕒 Age : 1m 🔗 CHART 💰 MC: $16.2K"
        assert classify(text, [CA], ["RUNTIME"]) == NEW_CALL

    def test_bare_ca_apeing(self):
        # real message from the DB: no market data, still a call
        assert classify(f"Just apeing into CAT at {CA}", [CA], []) == NEW_CALL

    def test_ca_only_in_link(self):
        # CHART-button post: visible text has no CA, links do
        assert classify("CHART 📈 | Buy now!", [CA], []) == NEW_CALL


class TestOutcome:
    def test_up_x(self):
        text = f"🚀 TOLY is up 2.0X from Entry Signal 💰 {CA}"
        assert classify(text, [CA], ["TOLY"]) == OUTCOME

    def test_progression_arrow(self):
        assert classify("MC: $28K → $56K since call", [CA], []) == OUTCOME

    def test_did_x(self):
        assert classify("$WIF did 5x for us", [], ["WIF"]) == OUTCOME

    def test_up_percent(self):
        # real message shape: "tod is up 86% from Entry Signal"
        text = "📈 tod is up 86% 📈 from ⚡️ Entry Signal"
        assert classify(text, [], []) == OUTCOME
        assert "is up 86%" in extract_outcome_claim(text)

    def test_outcome_beats_new_call(self):
        # outcome posts often contain a CA + market data — must not be NEW_CALL
        text = f"up 3.4X from Early Signal! MC $120K {CA}"
        assert classify(text, [CA], []) == OUTCOME

    def test_claim_extraction(self):
        claim = extract_outcome_claim("TOLY is up 2.0X 💰 $28K → $56K")
        assert "up 2.0X" in claim and "$28K → $56K" in claim


class TestListAndCommentary:
    def test_trending_list(self):
        text = "Top trending: $WIF $BONK $POPCAT $MEW"
        assert classify(text, [], ["WIF", "BONK", "POPCAT", "MEW"]) == LIST

    def test_multi_ca_is_list(self):
        assert classify("compare these", [CA, CA2], []) == LIST

    def test_commentary_no_hits(self):
        assert classify("Market is bleeding today, stay safe", [], []) == COMMENTARY

    def test_single_ticker_no_ca_is_commentary(self):
        assert classify("watching $WIF closely", [], ["WIF"]) == COMMENTARY
