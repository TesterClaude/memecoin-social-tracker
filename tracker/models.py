"""Data contracts between collector, extraction and storage.

Collectors for other platforms (M3 Reddit, M4 X) must produce the same
RawMessage shape so extract/db/alert stay platform-agnostic.
"""

from dataclasses import dataclass, field


@dataclass
class RawMessage:
    platform: str                 # "telegram"
    channel: str                  # handle without @, e.g. "degenonesol"
    external_id: str              # unique per platform, e.g. "degenonesol/1234"
    url: str                      # canonical link to the post
    text: str                     # visible text, "" for media-only posts
    links: list[str] = field(default_factory=list)  # every href in the message block
    ts_utc: str | None = None     # ISO-8601 UTC timestamp of the post
    views: int | None = None


@dataclass
class ExtractionResult:
    addresses: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    dedupe_hash: str = ""

    @property
    def has_hit(self) -> bool:
        return bool(self.addresses or self.tickers)


@dataclass
class EnrichedToken:
    """Market state of one token at fetch time (best pair by liquidity)."""
    address: str
    chain_id: str
    dex_id: str
    pair_address: str
    name: str | None = None
    symbol: str | None = None
    price_usd: float | None = None
    mcap: float | None = None            # fdv, falling back to marketCap
    liquidity_usd: float | None = None
    vol_5m: float | None = None
    vol_1h: float | None = None
    txns_buy: int | None = None          # h1 window
    txns_sell: int | None = None
    price_change_h24: float | None = None
    pool_created_at: str | None = None   # ISO-8601 UTC from pairCreatedAt
    socials_json: str | None = None      # raw info.socials as JSON
