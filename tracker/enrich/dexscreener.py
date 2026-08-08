"""DexScreener enrichment via /latest/dex/tokens/{addresses}.

Split into a dumb HTTP client (rate limit, backoff, chunking) and pure
parsing/validation functions so the parsing is unit-testable offline.

Three outcomes per queried address, and they mean different things:
- EnrichedToken   -> pairs found, validated market data
- None            -> API answered, no pair for this address ("invalid",
                     caller marks it and never queries again)
- absent from the returned dict -> the REQUEST failed (network, 5xx,
                     unexpected schema); NOT invalid, retry next cycle
"""

import json
import logging
import time
from datetime import datetime, timezone

import requests

from tracker.models import EnrichedToken

log = logging.getLogger(__name__)

TOKENS_PATH = "/latest/dex/tokens/"

# The tokens endpoint caps the response at ~30 pairs TOTAL, regardless of
# how many addresses were queried. An address missing from a capped
# response may simply have been cut off — treating that as "no pairs"
# falsely marks live tokens invalid (observed live: 25 addresses -> 30
# pairs -> only 19 addresses covered).
RESPONSE_PAIR_CAP = 30


class SchemaError(ValueError):
    """The API response does not look like the documented schema at all."""


def _f(value) -> float | None:
    """Lenient numeric coercion: API sends numbers, numeric strings, or null."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value) -> int | None:
    f = _f(value)
    return int(f) if f is not None else None


def _ms_to_iso(ms) -> str | None:
    f = _f(ms)
    if f is None or f <= 0:
        return None
    return datetime.fromtimestamp(f / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_pair(pair: dict) -> dict | None:
    """Validate one pair object; None if it lacks the required identity
    fields (then it is skipped — a schema change must not store garbage)."""
    if not isinstance(pair, dict):
        return None
    chain_id = pair.get("chainId")
    dex_id = pair.get("dexId")
    pair_address = pair.get("pairAddress")
    base = pair.get("baseToken")
    if not (isinstance(chain_id, str) and isinstance(dex_id, str)
            and isinstance(pair_address, str) and isinstance(base, dict)
            and isinstance(base.get("address"), str)):
        return None
    txns_h1 = (pair.get("txns") or {}).get("h1") or {}
    volume = pair.get("volume") or {}
    socials = (pair.get("info") or {}).get("socials")
    return {
        "base_address": base["address"],
        "token": EnrichedToken(
            address=base["address"],
            chain_id=chain_id,
            dex_id=dex_id,
            pair_address=pair_address,
            name=base.get("name") if isinstance(base.get("name"), str) else None,
            symbol=base.get("symbol") if isinstance(base.get("symbol"), str) else None,
            price_usd=_f(pair.get("priceUsd")),
            mcap=_f(pair.get("fdv")) if _f(pair.get("fdv")) is not None
                 else _f(pair.get("marketCap")),
            liquidity_usd=_f((pair.get("liquidity") or {}).get("usd")),
            vol_5m=_f(volume.get("m5")),
            vol_1h=_f(volume.get("h1")),
            txns_buy=_i(txns_h1.get("buys")),
            txns_sell=_i(txns_h1.get("sells")),
            price_change_h24=_f((pair.get("priceChange") or {}).get("h24")),
            pool_created_at=_ms_to_iso(pair.get("pairCreatedAt")),
            socials_json=json.dumps(socials) if isinstance(socials, list) else None,
        ),
        "liquidity": _f((pair.get("liquidity") or {}).get("usd")) or 0.0,
    }


def parse_tokens_response(data, queried: list[str],
                          chain_id: str = "solana") -> dict[str, EnrichedToken | None]:
    """Map every queried address to its best pair (highest liquidity on the
    target chain) or None if the API knows no pair for it.

    Raises SchemaError if the response shape is fundamentally wrong —
    callers must treat that as a failed request, never as "invalid token".
    """
    if not isinstance(data, dict) or "pairs" not in data:
        raise SchemaError(f"unexpected response shape: {type(data).__name__}, "
                          f"keys={list(data)[:5] if isinstance(data, dict) else '-'}")
    pairs = data["pairs"]
    if pairs is None:
        pairs = []
    if not isinstance(pairs, list):
        raise SchemaError(f"'pairs' is {type(pairs).__name__}, expected list or null")

    best: dict[str, dict] = {}
    skipped = 0
    for raw in pairs:
        parsed = _parse_pair(raw)
        if parsed is None:
            skipped += 1
            continue
        if parsed["token"].chain_id != chain_id:
            continue
        addr = parsed["base_address"]
        # only pairs whose BASE token was queried count — a queried address
        # appearing as quote token (e.g. WSOL) must not match
        if addr not in queried:
            continue
        if addr not in best or parsed["liquidity"] > best[addr]["liquidity"]:
            best[addr] = parsed
    if skipped:
        log.warning("skipped %d pair objects failing schema validation", skipped)
    return {addr: best[addr]["token"] if addr in best else None for addr in queried}


def estimate_mcap_at_creation(mcap: float | None,
                              price_change_h24: float | None,
                              pool_age_hours: float | None) -> float | None:
    """Estimate the market cap at pool creation.

    Only defined for pools younger than 24h: there the h24 price change
    covers the pool's entire life, so mcap_then = mcap_now / (1 + pc/100).
    Older pools -> None (DexScreener has no historical prices)."""
    if mcap is None or price_change_h24 is None or pool_age_hours is None:
        return None
    if pool_age_hours >= 24 or price_change_h24 <= -100:
        return None
    return mcap / (1 + price_change_h24 / 100)


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class DexScreenerClient:
    def __init__(self, api_base: str, timeout_s: int, min_interval_s: float,
                 max_retries_429: int, chunk_size: int = 30,
                 chain_id: str = "solana"):
        self._base = api_base.rstrip("/")
        self._timeout = timeout_s
        self._min_interval = min_interval_s
        self._max_retries = max_retries_429
        self._chunk_size = chunk_size
        self._chain_id = chain_id
        self._session = requests.Session()
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> dict | None:
        """GET with rate limiting and exponential backoff on 429.
        None = request failed (caller retries the addresses next cycle)."""
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except requests.RequestException as e:
                log.warning("dexscreener request failed: %s", e)
                return None
            if resp.status_code == 429:
                backoff = 2 ** (attempt + 1)
                log.warning("dexscreener 429, backing off %ds (attempt %d/%d)",
                            backoff, attempt + 1, self._max_retries)
                time.sleep(backoff)
                continue
            if resp.status_code != 200:
                log.warning("dexscreener HTTP %s for %s", resp.status_code, url)
                return None
            try:
                return resp.json()
            except ValueError as e:
                log.warning("dexscreener non-JSON response: %s", e)
                return None
        log.warning("dexscreener still 429 after %d retries, giving up this cycle",
                    self._max_retries)
        return None

    def fetch_tokens(self, addresses: list[str]) -> dict[str, EnrichedToken | None]:
        """Fetch market data for up to N addresses, chunked by 30.
        See module docstring for the three-way result semantics."""
        results: dict[str, EnrichedToken | None] = {}
        for chunk in _chunks(list(addresses), self._chunk_size):
            self._fetch_chunk(chunk, results)
        return results

    def _fetch_chunk(self, chunk: list[str],
                     results: dict[str, EnrichedToken | None]) -> None:
        """Fetch one chunk; on a response that hit the pair cap, bisect the
        uncovered addresses instead of trusting their absence. Only a
        response BELOW the cap proves an address really has no pairs.
        (Within a capped response the chosen best-by-liquidity pair can
        still come from a truncated pair list — acceptable inaccuracy.)"""
        data = self._get(f"{self._base}{TOKENS_PATH}{','.join(chunk)}")
        if data is None:
            return  # request failed -> chunk retried next cycle
        try:
            parsed = parse_tokens_response(data, chunk, self._chain_id)
        except SchemaError as e:
            log.error("dexscreener schema changed? %s — chunk treated as "
                      "failed request, nothing stored", e)
            return
        raw_pair_count = len(data.get("pairs") or [])
        unresolved = [a for a, v in parsed.items() if v is None]
        if raw_pair_count >= RESPONSE_PAIR_CAP and unresolved and len(chunk) > 1:
            log.info("response hit the %d-pair cap, re-checking %d uncovered "
                     "addresses in smaller batches",
                     RESPONSE_PAIR_CAP, len(unresolved))
            results.update({a: v for a, v in parsed.items() if v is not None})
            mid = (len(unresolved) + 1) // 2
            self._fetch_chunk(unresolved[:mid], results)
            self._fetch_chunk(unresolved[mid:], results)
        else:
            results.update(parsed)
