# Memecoin Tracker — M1

Telegram collector (public preview via `t.me/s/`, no auth, no ban risk) →
SQLite (§5 schema) → raw-match alerts into a private Telegram channel.

No scoring, no enrichment, no safety checks yet — that's M2+.

## Setup

1. `python -m venv .venv && .venv\Scripts\activate`
2. `pip install -r requirements.txt`
3. Create a bot via **@BotFather**, add it as admin to your private alert
   channel, copy `.env.example` → `.env` and fill in token + chat id.
4. Adjust `channels.txt` (one public channel handle per line) and
   `config.yaml` if needed.
5. `python run.py`

## Tests

```
python -m pytest tests/ -q
```

## M2: DexScreener enrichment

Every address seen in a cycle is batch-enriched via
`/latest/dex/tokens/{addresses}` (30 per call, 300 req/min respected).
Alerts then carry market cap, liquidity, pool age and the run-up since
pool creation. Tokens without pairs or failing the base58 shape check are
marked `invalid_*` in `tokens.enrich_status` and not queried again —
except `invalid_no_pairs` tokens within `no_pairs_retry_window_h` (24h)
of their first mention: pre-pool calls are re-checked every cycle until
their pool appears or the window closes;
system mints (WSOL/USDC/USDT, `ignore_mints` in config.yaml) are never
stored, queried or alerted.

One-time backfill of everything collected before M2:

```
python backfill.py
```

Honesty notes on the M2 fields: `mcap_at_first_mention` is only set when
enrichment happens within `first_mention_proxy_window_min` of the first
mention (always true live, mostly NULL in backfill). `mcap_at_pool_creation`
is an estimate from the h24 price change, only for pools younger than 24h.

## Notes

- `t.me/s/` serves only the ~20 most recent messages per fetch; at a 45 s
  poll interval that is more than enough for call channels.
- Duplicate forwards across channels are stored (`is_duplicate=1`) but only
  the first occurrence triggers an alert.
- Extracted base58 addresses can be token mints **or** pair addresses —
  M1 stores both, M2 resolves them via DexScreener.
- This is a research/monitoring tool, not investment advice.
