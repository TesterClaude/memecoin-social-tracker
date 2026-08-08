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

## Notes

- `t.me/s/` serves only the ~20 most recent messages per fetch; at a 45 s
  poll interval that is more than enough for call channels.
- Duplicate forwards across channels are stored (`is_duplicate=1`) but only
  the first occurrence triggers an alert.
- Extracted base58 addresses can be token mints **or** pair addresses —
  M1 stores both, M2 resolves them via DexScreener.
- This is a research/monitoring tool, not investment advice.
