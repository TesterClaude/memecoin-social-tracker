"""Single load point for config.yaml, .env and channels.txt.

No other module reads environment variables or files on its own.
"""

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


@dataclass
class Config:
    db_path: str
    channels: list[str]
    poll_interval_s: int
    request_timeout_s: int
    user_agent: str
    backoff_on_429_s: int
    stale_after_days: int
    chain: str
    ticker_min_len: int
    ticker_max_len: int
    ignore_mints: list[str]
    enrich_enabled: bool
    enrich_api_base: str
    enrich_timeout_s: int
    enrich_max_addresses_per_call: int
    enrich_min_request_interval_s: float
    enrich_max_retries_429: int
    first_mention_proxy_window_min: int
    no_pairs_retry_window_h: int
    forward_enabled: bool
    forward_checkpoints_min: list[int]
    rug_liquidity_floor_usd: float
    alerts_enabled: bool
    alerts_max_per_cycle: int
    alerts_send_delay_s: float
    alert_types: dict
    post_24h_outcome_reply: bool
    bot_token: str = field(repr=False, default="")
    alert_chat_id: str = ""


def load_channels(path: str) -> list[str]:
    channels = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("@")
            if line and not line.startswith("#"):
                channels.append(line)
    return channels


def load_config(config_path: str = "config.yaml") -> Config:
    load_dotenv()
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    tg = raw["sources"]["telegram"]
    ex = raw["extraction"]
    al = raw["alerts"]
    en = raw.get("enrichment", {})
    ft = raw.get("forward_testing", {})

    cfg = Config(
        db_path=raw["database"]["path"],
        channels=load_channels(tg["channels_file"]),
        poll_interval_s=int(tg["poll_interval_s"]),
        request_timeout_s=int(tg["request_timeout_s"]),
        user_agent=tg["user_agent"],
        backoff_on_429_s=int(tg.get("backoff_on_429_s", 300)),
        stale_after_days=int(tg.get("stale_after_days", 14)),
        chain=ex["chain"],
        ticker_min_len=int(ex["ticker_min_len"]),
        ticker_max_len=int(ex["ticker_max_len"]),
        ignore_mints=list(raw.get("ignore_mints", [])),
        enrich_enabled=bool(en.get("enabled", False)),
        enrich_api_base=en.get("api_base", "https://api.dexscreener.com"),
        enrich_timeout_s=int(en.get("request_timeout_s", 15)),
        enrich_max_addresses_per_call=int(en.get("max_addresses_per_call", 30)),
        enrich_min_request_interval_s=float(en.get("min_request_interval_s", 0.2)),
        enrich_max_retries_429=int(en.get("max_retries_429", 4)),
        first_mention_proxy_window_min=int(en.get("first_mention_proxy_window_min", 30)),
        no_pairs_retry_window_h=int(en.get("no_pairs_retry_window_h", 24)),
        forward_enabled=bool(ft.get("enabled", False)),
        forward_checkpoints_min=[int(m) for m in
                                 ft.get("checkpoints_min", [15, 60, 240, 1440])],
        rug_liquidity_floor_usd=float(ft.get("rug_liquidity_floor_usd", 1000)),
        alerts_enabled=bool(al["enabled"]),
        alerts_max_per_cycle=int(al["max_per_cycle"]),
        alerts_send_delay_s=float(al.get("send_delay_s", 1.1)),
        alert_types={str(k): bool(v) for k, v in
                     al.get("by_type", {"NEW_CALL": True, "OUTCOME": True,
                                        "LIST": False, "COMMENTARY": False}).items()},
        post_24h_outcome_reply=bool(al.get("post_24h_outcome_reply", True)),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", ""),
    )
    if cfg.alerts_enabled and not (cfg.bot_token and cfg.alert_chat_id):
        raise SystemExit(
            "alerts.enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID "
            "are missing — fill .env (see .env.example) or disable alerts."
        )
    return cfg
