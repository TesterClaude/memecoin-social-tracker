"""Message-type classification — pure heuristics, no I/O.

Order of checks is priority: OUTCOME first, because retrospect posts often
also carry a CA plus market data and would otherwise look like NEW_CALL.
Nothing is discarded based on type; classification only labels.
"""

import re

NEW_CALL = "NEW_CALL"
OUTCOME = "OUTCOME"
LIST = "LIST"
COMMENTARY = "COMMENTARY"

# -- OUTCOME patterns ---------------------------------------------------------
# "is up 2.0X", "up 5x", "did 3.2X"
_UP_X_RE = re.compile(r"\b(?:is\s+)?(?:up|did)\s+\d+(?:[.,]\d+)?\s*x\b", re.IGNORECASE)
# "is up 86%" (real-world variant without a multiplier)
_UP_PCT_RE = re.compile(r"\b(?:is\s+)?up\s+\d+(?:[.,]\d+)?\s*%", re.IGNORECASE)
# "3.2X from ...", "2x since ..."
_X_FROM_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*x\s+(?:from|since)\b", re.IGNORECASE)
# "from Entry Signal", "from Early Signal", "from (our) call"
_FROM_SIGNAL_RE = re.compile(
    r"\bfrom\s+(?:our\s+)?(?:entry|early|first)?\s*(?:signal|call)\b", re.IGNORECASE)
# "$28K → $56K" (also ->, ➜, ⇒)
_PROGRESSION_RE = re.compile(
    r"\$\s?\d[\d.,]*\s*[KMB]?\s*(?:→|➜|⇒|->)\s*\$?\s?\d[\d.,]*\s*[KMB]?",
    re.IGNORECASE)

_OUTCOME_RES = (_UP_X_RE, _UP_PCT_RE, _X_FROM_RE, _FROM_SIGNAL_RE, _PROGRESSION_RE)


def classify(text: str, addresses: list[str], tickers: list[str]) -> str:
    """Classify a message given its (ignore-filtered) extraction results."""
    if any(rx.search(text) for rx in _OUTCOME_RES):
        return OUTCOME
    if len(addresses) == 1:
        return NEW_CALL
    if len(addresses) >= 2 or len(tickers) >= 2:
        return LIST
    return COMMENTARY


def extract_outcome_claim(text: str) -> str | None:
    """The literal claim snippets from an OUTCOME post ("up 2.0X",
    "$28K → $56K") for side-by-side display with current numbers.
    Overlapping matches ("is up 2.0X" vs "2.0X from") are deduplicated.
    Returns None if no pattern matched (shouldn't happen for OUTCOME)."""
    spans: list[tuple[int, int]] = []
    snippets = []
    for rx in (_UP_X_RE, _UP_PCT_RE, _X_FROM_RE, _PROGRESSION_RE):
        m = rx.search(text)
        if m and not any(m.start() < e and m.end() > s for s, e in spans):
            spans.append((m.start(), m.end()))
            snippets.append(" ".join(m.group(0).split()))
    return " · ".join(snippets) if snippets else None
