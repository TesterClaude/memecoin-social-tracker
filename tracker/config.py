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
    chain: str
    ticker_min_len: int
    ticker_max_len: int
    alerts_enabled: bool
    alerts_max_per_cycle: int
    alerts_send_delay_s: float
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

    cfg = Config(
        db_path=raw["database"]["path"],
        channels=load_channels(tg["channels_file"]),
        poll_interval_s=int(tg["poll_interval_s"]),
        request_timeout_s=int(tg["request_timeout_s"]),
        user_agent=tg["user_agent"],
        backoff_on_429_s=int(tg.get("backoff_on_429_s", 300)),
        chain=ex["chain"],
        ticker_min_len=int(ex["ticker_min_len"]),
        ticker_max_len=int(ex["ticker_max_len"]),
        alerts_enabled=bool(al["enabled"]),
        alerts_max_per_cycle=int(al["max_per_cycle"]),
        alerts_send_delay_s=float(al.get("send_delay_s", 1.1)),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", ""),
    )
    if cfg.alerts_enabled and not (cfg.bot_token and cfg.alert_chat_id):
        raise SystemExit(
            "alerts.enabled is true but TELEGRAM_BOT_TOKEN / TELEGRAM_ALERT_CHAT_ID "
            "are missing — fill .env (see .env.example) or disable alerts."
        )
    return cfg
