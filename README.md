# Memecoin Tracker — M1 + M2 + M6

Telegram collector (public preview via `t.me/s/`, no auth, no ban risk) →
SQLite (§5 schema) → raw-match alerts into a private Telegram channel.

No scoring, no enrichment, no safety checks yet — that's M2+.

## Setup

1. `python -m venv .venv && .venv\Scripts\activate`
2. `pip install -r requirements.txt`
3. Create a bot via **@BotFather**, add it as admin to your private alert
   channel, copy `.env.example` → `.env` and fill in token + chat id.
4. Copy `channels.example.txt` to `channels.txt` (gitignored) and put in
   your channels, one public channel handle per line. Adjust `config.yaml`
   if needed.
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

## Message classification & alert threading

Every ingested message is classified (`mentions.message_type`) as
NEW_CALL (exactly one CA), OUTCOME (retrospect: "up 2.0X", "up 86%",
"$28K → $56K", "from Entry Signal"), LIST (multiple tickers/CAs, no
single CA) or COMMENTARY (rest). Nothing is discarded — classification
only labels. Per-type alert switches live in `alerts.by_type`
(default: NEW_CALL + OUTCOME on).

NEW_CALL alerts store their Telegram message id
(`tokens.alert_message_id`); OUTCOME alerts for the same CA are sent as
replies under the origin call, and (switchable via
`alerts.post_24h_outcome_reply`) the +24h forward-log measurement is
posted there too. Alerts show facts only: market data, volume/tx (holder
count, bundled %, sniper count need an on-chain source — M5), ticker
collisions in 24h, position in the mention chain, pre-pool lead time.

If a token's FIRST sighting is an OUTCOME post, its forward-test call is
flagged `late_discovery=1` and excluded from main channel statistics.

## M6: Forward-testing log

Every FIRST mention of a token opens a call entry at mention time (no
look-ahead, no retroactive entries) with checkpoints at +15m/+1h/+4h/+24h.
Each checkpoint records price/mcap/liquidity plus a rug flag (`liq_gone`:
liquidity fell below `rug_liquidity_floor_usd` or the pair vanished after
having had liquidity — distinct from a plain price drop). MFE/MAE are
derived per call; tokens that never get a pool end as `no_pool` and stay
in every statistic (no survivorship bias). Report per channel:

```
python report_forward.py
```

## Launch baseline

An independent collector samples new Solana tokens from DexScreener's
token-profiles feed (`baseline` in config.yaml), regardless of channel
mentions, into `baseline_tokens` — each with forward-log checkpoints via
the shared calls machinery (`calls.is_baseline=1`, synthetic source,
excluded from channel stats). Admission is capped (`max_new_per_cycle`,
`max_pool_age_min`) so the baseline cannot eat the shared 300 req/min
budget (~0.4 req/min at defaults). `report_forward.py` compares called
vs. baseline outcomes, shows the coverage of the baseline by tracked
channels, and lists serial deployers (same X handle on >= 2 tokens).
Honesty: the profiles feed is a biased sample (marketed tokens), not the
universe of all new pools — a true firehose needs an on-chain indexer.

## Notes

- `t.me/s/` serves only the ~20 most recent messages per fetch; at a 45 s
  poll interval that is more than enough for call channels.
- Duplicate forwards across channels are stored (`is_duplicate=1`) but only
  the first occurrence triggers an alert.
- Extracted base58 addresses can be token mints **or** pair addresses —
  M1 stores both, M2 resolves them via DexScreener.
- This is a research/monitoring tool, not investment advice.
