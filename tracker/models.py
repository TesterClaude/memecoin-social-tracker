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
