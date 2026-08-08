"""Pure extraction functions — no I/O, no platform knowledge.

Two lessons from live-testing t.me/s/ pages are baked in here:
1. Contract addresses often appear only inside link hrefs (e.g. behind a
   "CHART" button), never in the visible text. Callers must pass links too.
2. A naive $-pattern matches money amounts like "$70.3K". A ticker must
   contain at least one letter, and pure numbers with a K/M/B suffix are
   rejected.
"""

import hashlib
import re
from urllib.parse import unquote

# Base58 alphabet, 32-44 chars — Solana pubkeys (token mints AND pair/pool
# addresses; M1 stores both, M2 resolves which is which via DexScreener).
SOLANA_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# Candidate after "$": alphanumeric run. Filtering happens in Python because
# "at least one letter" + "not a K/M/B amount" is clearer as code than as
# nested lookaheads.
_TICKER_CANDIDATE_RE = re.compile(r"\$([A-Za-z0-9]{1,10})\b")

# Pure number, optionally with decimals and a K/M/B suffix: "70", "70.3K", "5B"
_MONEY_AMOUNT_RE = re.compile(r"^\d+(?:[.,]\d+)?[KMB]?$", re.IGNORECASE)

_URL_RE = re.compile(r"https?://\S+")
_HAS_LETTER_RE = re.compile(r"[A-Za-z]")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_decoded_len(s: str) -> int:
    """Byte length of the base58-decoded string, -1 if not valid base58."""
    n = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            return -1
        n = n * 58 + idx
    return (n.bit_length() + 7) // 8 + (len(s) - len(s.lstrip("1")))


def is_valid_solana_address(addr: str) -> bool:
    """True if the string decodes to exactly 32 bytes — a real pubkey shape.
    Cheap local filter for regex false positives before any API call."""
    return b58_decoded_len(addr) == 32


def extract_addresses(text: str, links: list[str]) -> list[str]:
    """Solana addresses from visible text AND from every link href.

    Hrefs are URL-decoded first so addresses inside query strings
    (?token=<CA>) are not missed. Order-preserving dedupe.
    """
    seen: dict[str, None] = {}
    for source in [text, *[unquote(l) for l in links]]:
        for addr in SOLANA_ADDRESS_RE.findall(source):
            seen.setdefault(addr, None)
    return list(seen)


def extract_tickers(text: str, min_len: int = 2, max_len: int = 10) -> list[str]:
    """$TICKER extraction that rejects money amounts.

    Kept: at least one letter, length within bounds.
    Rejected: "$70", "$70.3K" (regex stops the candidate at ".", leaving the
    all-digit "70"), "$5B", "$100M".
    """
    seen: dict[str, None] = {}
    for cand in _TICKER_CANDIDATE_RE.findall(text):
        if len(cand) < min_len or len(cand) > max_len:
            continue
        if not _HAS_LETTER_RE.search(cand):
            continue
        if _MONEY_AMOUNT_RE.match(cand):
            continue
        seen.setdefault(cand.upper(), None)
    return list(seen)


def normalize_text(text: str) -> str:
    """Normalisation for the dedupe hash: strip URLs, lowercase, collapse
    whitespace. Forwarded shills differ mostly in their ref-links and
    spacing — this collapses them onto one hash."""
    t = _URL_RE.sub("", text)
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def dedupe_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
