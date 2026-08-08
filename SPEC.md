# MASTER PROMPT — Memecoin Social Signal Tracker (X / Reddit / Telegram + On-Chain)

> **How to use this file.**
> Section 0 is meta-instruction for *you* (the human). Sections 1–16 are the actual prompt — paste
> everything from `=== PROMPT START ===` onward into Claude Code / Cowork / an LLM of your choice.
> Section 17 contains shorter prompt variants for narrower jobs.
> Delete the sections you don't need — a shorter, sharper prompt beats a bloated one.

---

## 0. Before you paste anything — decide three things

| Decision | Options | Consequence |
|---|---|---|
| **Build level** | A = no-code / existing bots · B = hybrid (existing APIs + own glue) · C = full custom pipeline | Determines 90% of the cost and 100% of the maintenance burden |
| **Budget/month** | €0 · €20–50 · €200+ | X data is the hard constraint. See §4.1 |
| **Latency target** | minutes (research) · seconds (sniping) | Seconds means WebSockets + own RPC nodes. Minutes means cron + REST. Do not pretend you need seconds |

Fill these into the `[[ ]]` placeholders in §2 before pasting.

---

```
=== PROMPT START ===
```

## 1. Role and context

You are a senior data engineer and quantitative researcher specialising in social-signal
extraction for crypto markets. You are building a **memecoin social-media tracking system**
for a solo developer with an intermediate Python background and a small budget.

Your output must be **executable, cost-aware and honest about failure modes**. When a component
is legally grey, rate-limited, ban-prone or economically unviable, say so explicitly and give the
alternative — do not produce a beautiful architecture that silently assumes free unlimited X data.

Assume today's constraints (verify each before implementing, they change every few months):

- **X/Twitter API**: no free tier for new developers since Feb 2026. Pay-per-use is the default —
  roughly $0.005 per post read, $0.015 per plain post created, $0.20 per post containing a URL,
  hard-capped around 2M reads/month. Legacy Basic ($200/mo) and Pro ($5,000/mo) are closed to new
  signups and existing Basic subscribers were auto-migrated from June 2026. Full-archive search is
  effectively Enterprise-only (~$42k/mo). Third-party X data resellers exist at $0.05–$0.30 per
  1,000 posts — 90–99% cheaper — but carry ToS and continuity risk.
- **Reddit API**: free tier still real — ~100 queries/minute per OAuth client, ~10 QPM
  unauthenticated, averaged over a rolling 10-minute window, **non-commercial use only**. Since the
  Nov 2025 Responsible Builder Policy, even hobby apps need approval; expect a 2–4 week review.
  Commercial access is ~$0.24 per 1,000 calls with a five-figure minimum. Pushshift is gone.
  Structural gaps: no date-range search, no comment search, ~1,000-item pagination ceiling.
- **Telegram**: cheapest and richest source. Three access paths with very different risk profiles —
  Bot API (HTTP, safe, but the bot must be *in* the group), MTProto userbot via Telethon/Pyrogram
  (full read access, **real ban risk**, needs a burner phone number), and `t.me/s/<channel>` public
  web preview (no auth, no ban risk, public channels only, ~500–1,000 recent messages).
- **On-chain**: DexScreener REST is free and unauthenticated for core routes — ~60 req/min on
  token-profile and boost endpoints, ~300 req/min on pairs/search. It also exposes a WebSocket for
  boosts and profiles, but it does **not** stream every new pool the moment it lands on-chain.

## 2. Objective

Build a system that detects **abnormal social attention on memecoins earlier than price does**,
and that can distinguish organic attention from manufactured attention.

Parameters for this build:

- Build level: B
- Monthly budget ceiling: €30
- Latency target: minutes
- Chains in scope: Solana
- Primary use: research & journaling + alerting
- Language of code comments and UI: English

**Explicitly out of scope unless I say otherwise:** automated order execution, wallet key handling,
token deployment, any form of coordinated posting or engagement farming.

## 3. Deliver an architecture decision first — then code

Before writing a single line, produce a **comparison of three build levels** with real numbers:

**Level A — Assemble from existing tools (no code).**
Cover: LunarCrush (Galaxy Score, AltRank, social volume across X/Reddit/YouTube; also ships an MCP
server), Santiment (social volume, social dominance, on-chain correlation), Kaito (mindshare and
narrative rotation; note its Jan 2026 pivot away from Yaps after X banned pay-to-post apps),
Cookie.fun (attention/creator reputation, X + multichain API), DexScreener/DEXTools/Birdeye
(on-chain trending), plus off-the-shelf Telegram alert bots and call-tracker bots that score the
historical hit rate of alpha callers. For each: what it gives, what it costs, what it *cannot* do.

**Level B — Hybrid.** Own orchestration layer + paid aggregator APIs for X, free APIs for
Reddit/Telegram/on-chain. This is almost always the right answer under €100/month.

**Level C — Full custom.** Own collectors on every platform, own storage, own scoring, own bot.
Maximum control, maximum maintenance, maximum ToS exposure.

State a **recommendation** for my stated budget and be blunt if my budget doesn't support my goal.

## 4. Data source layer — cover every ingestion path

For each platform below, produce: available methods, auth requirements, cost, rate limits, legal
status, ban risk, data completeness, and a working Python code skeleton.

### 4.1 X / Twitter — the expensive one

Cover **all** of these paths, with a cost model per path at 10k / 100k / 1M posts read per month:

1. **Official API v2 pay-per-use** — recent search, filtered stream with rules, user timelines,
   list timelines. Note which endpoints bill as "owned reads" (cheapest) vs. search.
2. **Third-party X data resellers** — flat per-1k-post pricing, no monthly minimum, no 2M cap.
   Name the trade-offs: no SLA from X, ToS grey zone, provider can die overnight.
3. **Free-ish workarounds** — public syndication endpoints, RSS bridges, Nitter-style mirrors.
   State honestly which of these still function and which have been killed.
4. **Browser automation** on a logged-in account (Playwright / Claude in Chrome). Full fidelity,
   but account-ban risk and it does not scale.
5. **Curated-list strategy** — instead of firehose search, follow 50–300 hand-picked accounts via
   list timelines. Explain why this cuts cost by 10–100x and usually *raises* signal quality.

Then design the actual collector:
- Track: cashtags (`$TICKER`), contract addresses (regex per chain), keyword sets, quote-tweet
  cascades, reply-tree depth, account-creation date, follower count, follower **quality**.
- Capture for every post: `post_id, author_id, author_created_at, author_followers, text,
  created_at, likes, reposts, replies, quotes, views, is_reply, is_quote, lang, urls, media_hash`.
- Handle: deleted posts, edited posts, rate-limit 429 with exponential backoff, pagination cursors,
  duplicate detection across overlapping queries.

### 4.2 Reddit — the cheap, slow, high-quality one

- PRAW / asyncpraw with OAuth script app. Practical ceiling ~60–100 req/min.
- Streams: `subreddit.stream.submissions()` and `.comments()`. Poll strategy for the ~1,000-item cap.
- Subreddits to seed: r/CryptoMoonShots, r/SatoshiStreetBets, r/CryptoCurrency, r/solana,
  r/memecoins, r/pumpfun, r/altstreetbets — plus a discovery routine that finds new ones.
- Metrics unique to Reddit: upvote **ratio** (not just score), comment-to-upvote ratio, account age
  of commenters, crosspost spread, whether a ticker appears in multiple unrelated subs without
  obvious promotion (the strongest organic signal on this platform).
- Handle: deleted/removed content, shadowbanned accounts, mod-removed spam, the Responsible
  Builder approval process, and what to do while the 2–4 week review is pending.

### 4.3 Telegram — the fastest and richest one

Design **all three** access paths and let me pick:

1. **Bot API** (`python-telegram-bot`, `aiogram`) — bot must be added to the group; privacy mode
   must be disabled to read all messages; cannot read channels it isn't in. Zero ban risk.
2. **MTProto userbot** (Telethon or Pyrogram) — `api_id`/`api_hash` from my.telegram.org, session
   file, reads every channel the account has joined. Must handle `FloodWaitError`, session
   persistence, and the real possibility of the account being banned. Use a burner number, never a
   primary account. Never store the session file in a repo.
3. **`t.me/s/<channel>` public preview scraping** — no auth, no ban risk, public channels only,
   no reactions/poll data, ~500–1,000 recent messages. Best default for OSINT-style monitoring.

Collector design:
- Channel inventory: call channels, launch-announcement channels, sniper-bot output channels,
  project official channels, whale-alert channels. Maintain them in config, not in code.
- Per message extract: `channel_id, message_id, sender_id, text, timestamp, views, forwards,
  reply_to, entities (URLs, mentions), extracted_contract_addresses, extracted_tickers`.
- **Caller attribution**: track which channel/person called which token at which market cap, then
  forward-score their hit rate. This is the single highest-value feature in the whole system.
- Detect: mass-forward waves (same message hitting N channels within M minutes), member-count
  spikes, sudden channel creation, admin changes.

### 4.4 Discord (optional but worth it)

Bot with `MESSAGE_CONTENT` privileged intent, only in servers I've been invited to. Note that
self-botting is a straight ToS violation and gets accounts terminated — do not propose it.

### 4.5 On-chain layer — the ground truth social data must be joined against

- **DexScreener REST**: `/token-profiles/latest/v1`, `/token-boosts/latest/v1`,
  `/token-boosts/top/v1`, `/latest/dex/search?q=`, `/latest/dex/tokens/{addresses}` (max 30 per
  call). Respect ~60 req/min on profile/boost, ~300 req/min on pairs. The `info.socials` field
  gives you the token's declared X/Telegram handles — that's your join key.
- **Launchpad feeds**: pump.fun and equivalents, new-pool events.
- **RPC/indexer webhooks** (Helius, QuickNode, Bitquery, Birdeye) for sub-second new-pair detection
  if latency target is "seconds".
- **Safety checks**: liquidity locked/burned, mint & freeze authority revoked, top-10 holder
  concentration, dev wallet behaviour, bundled-buy detection, honeypot simulation.

### 4.6 Aggregator APIs — buy instead of build where it makes sense

LunarCrush, Santiment, Kaito, Cookie.fun. For each: what metric they expose, whether there's an
MCP server, price band, and — critically — **the lag**. An aggregator that updates hourly is
useless for a token that lives four hours.

## 5. Canonical data model

Define one normalised schema all collectors write into. At minimum:

```
sources(source_id, platform, handle, tier, credibility_score, first_seen, notes)
mentions(mention_id, platform, source_id, external_id, ts_utc, raw_text,
         ticker, contract_address, chain, sentiment, confidence,
         author_age_days, author_followers, engagement_json, is_duplicate, dedupe_hash)
tokens(contract_address, chain, ticker, name, first_mention_ts, first_seen_price,
       first_seen_mcap, socials_json, launch_ts)
token_snapshots(contract_address, ts_utc, price_usd, mcap, liquidity_usd,
                vol_5m, vol_1h, txns_buy, txns_sell, holders)
signals(signal_id, contract_address, ts_utc, signal_type, score, components_json,
        triggered_alert, outcome_json)
calls(call_id, source_id, contract_address, ts_utc, mcap_at_call, outcome_mfe, outcome_mae)
```

Rules: everything in UTC; every row carries its raw source; `dedupe_hash` over normalised text so
the same copy-pasted shill across 40 channels collapses to one *unique idea* but N *forwards*.

## 6. Signal engine — the actual intelligence

Do **not** just count mentions. Implement and document each of these, with formulas:

1. **Mention velocity** — mentions per 5m/15m/1h.
2. **Acceleration** — second derivative. A token going 2→4→16 matters; 40→41→42 does not.
3. **Z-score vs. own baseline** — each token is compared against its own 24h/7d history, not a
   global threshold.
4. **Unique-author count** — 100 mentions from 4 accounts is noise; 40 mentions from 38 accounts
   is a signal. Weight authors by inverse posting frequency.
5. **Follower-weighted reach** with logarithmic dampening so one big account can't dominate.
6. **Cross-platform confluence** — score highest when a ticker appears independently on X *and*
   Reddit *and* Telegram within a window. Independent arrival is the hardest thing to fake.
7. **First-mention latency** — how long between first social mention and pool creation. Mentions
   *before* liquidity are either genuine alpha or an insider group. Flag, don't assume.
8. **Sentiment** — start with a crypto-tuned lexicon plus emoji handling, upgrade to a small
   transformer or LLM batch-classification only if the lexicon underperforms. Measure both.
9. **Caller credibility weight** — from §4.3, weight each mention by the historical forward-tested
   hit rate of its source.
10. **Composite score 0–100** with every component and its weight printed in the alert, so the
    score is auditable and tunable rather than a black box.

## 7. Anti-manipulation layer — treat this as a first-class feature, not a filter

Memecoin social data is adversarial by construction. Research on delisted memecoins has found the
large majority showed signs of artificial community growth. Build detection for:

- **Bot/sybil accounts**: account age, follower/following ratio, posting cadence regularity,
  default avatar, username entropy (`crypto_bull_84719`), near-duplicate bios.
- **Copy-paste raids**: normalised-text clustering; N accounts posting the same text within a
  short window.
- **Engagement pods**: reciprocal interaction graphs.
- **Paid promotion**: DexScreener boost purchases, "trending" placements, undisclosed shill posts.
- **Reply-spam under unrelated viral posts** — the classic memecoin distribution vector.
- **Ticker hijacking**: multiple contracts sharing one ticker. **Always key on contract address,
  never on ticker.** Ticker collision handling is mandatory, not optional.
- **Ratio checks**: social volume vs. actual unique holders vs. actual buy transactions. Attention
  without wallets is manufactured.

Output an **"organic score"** separate from the attention score. High attention + low organic score
is the single most useful red flag the system can produce.

## 8. Alerting layer — my own Telegram bot

- BotFather bot, private channel or DM target.
- Alert message: ticker, contract (copyable, monospace), chain, composite score with component
  breakdown, organic score, mcap, liquidity, age, top 3 source quotes, deep links to DexScreener /
  Rugcheck / Birdeye / the originating post.
- **Tiered alerts**: watch / warm / hot. Different channels per tier so hot alerts stay rare.
- Deduplication and cooldown per contract, so one token can't spam 40 alerts.
- Inline buttons: mute token, mark as followed, mark as false positive → **feedback loop into the
  labelled dataset**.
- Quiet hours, daily digest, weekly caller-leaderboard post.

## 9. Infrastructure

- Python 3.11+, async throughout. Poetry or uv. Docker Compose.
- Storage: SQLite → Postgres + TimescaleDB when it hurts; Redis for dedupe sets and rate-limit
  buckets. Do not start with a graph database.
- Scheduler: APScheduler or Celery beat. Message bus only if genuinely needed.
- Secrets in `.env`, never committed. Session files gitignored.
- Structured logging, per-source health metrics, a `/status` command in the bot.
- Deployment: a €5/month VPS is enough for Level B. Say if it isn't.

## 10. Validation — no signal is real until it's forward-tested

This is non-negotiable and mirrors how I validate trading indicators:

- **Forward-recording log**: every signal writes a row *at trigger time*, then a job fills in
  outcomes at +15m, +1h, +4h, +24h — max favourable excursion, max adverse excursion, and whether
  liquidity was pulled.
- Report, per signal type and per source: hit rate, median MFE, median MAE, expectancy, and the
  **base rate** of tokens that fail regardless of signal.
- Ground the whole thing: on the largest launchpad, only ~1% of tokens ever graduate their bonding
  curve. Any evaluation that ignores this base rate is lying to me.
- **Survivorship-bias warning**: backtesting on tokens that exist today systematically excludes
  every rug. Historical validation must be built from a live-recorded universe, not a retrospective
  token list. State this explicitly in the report.
- Look-ahead-bias audit: confirm no feature uses data unavailable at signal time.

## 11. Scenario catalogue — handle each one explicitly

For every scenario below, specify detection, system behaviour, and alert wording.

**Data-source failures**
1. X API 429 / credit exhaustion mid-month → degrade gracefully, alert me, don't crash.
2. X repricing again → cost model must be a config value, not hard-coded.
3. Third-party reseller shuts down → adapter pattern, swap provider in one file.
4. Reddit OAuth revoked or app rejected → fall back to `.json` endpoints at 10 QPM, warn about limits.
5. Telegram `FloodWaitError` → respect the wait, never retry-hammer.
6. Telegram userbot account banned → detect, alert, fail over to `t.me/s/` scraping, tell me how
   to re-provision.
7. DexScreener rate-limit or schema change → cache last-good, back off, validate schema on ingest.
8. RPC node outage → secondary provider.
9. Network partition / VPS reboot → resume from last cursor, no duplicate alerts.

**Data-quality traps**
10. Same ticker, multiple contracts → resolve by liquidity and pool age; alert on ambiguity.
11. Contract address in an image only (no text) → OCR path or explicit "not covered" statement.
12. Obfuscated tickers (`$P U M P`, unicode lookalikes) → normalisation with confusable mapping.
13. Non-English shill waves (CN/KR/RU/TR) → language detection; don't discard, they're often early.
14. Sarcasm and post-rug mourning read as positive sentiment → negation and past-tense handling.
15. A memecoin named after a real event → separate "narrative" mentions from "token" mentions.
16. Old post resurfacing / edited post → use `created_at`, not ingestion time.
17. Deleted evidence → snapshot text at ingest; never rely on refetch.

**Market/adversarial scenarios**
18. Coordinated launch: 30 channels post the same contract within 60 seconds → this is a *negative*
    organic signal, not a positive one. Make sure the scoring reflects that.
19. Slow organic build: one small account, then three unrelated ones over 6 hours → highest-value
    pattern. Make sure the scoring catches it.
20. Celebrity/large-account mention → separate alert class; measure how often it's already too late.
21. Insider pre-launch chatter → mentions before pool creation.
22. Rug in progress: liquidity removal, dev wallet sells, holder count collapsing → **exit alert**
    is as important as entry alert.
23. Honeypot: buys succeed, sells fail → simulate before alerting.
24. Exchange-listing rumour vs. confirmed listing.
25. Broad market crash → suppress all long-side alerts when the whole sector is bleeding.
26. Weekend/low-liquidity distortions.
27. A caller with a great record suddenly turning into a paid shill → rolling-window credibility,
    not lifetime.

**Operational**
28. Alert storm (>N alerts/hour) → global circuit breaker + summary digest instead.
29. Storage growth → retention policy, cold archive.
30. Cost overrun → hard monthly spend cap that stops collectors and notifies me.

## 12. Legal, compliance and personal risk — a short, honest section

- Platform ToS: what each method technically violates and what the realistic enforcement is.
- Scraping public data vs. authenticated scraping — different legal footing.
- GDPR: I'm in Germany. Storing usernames and post content is personal-data processing. Address
  lawful basis, retention limits, and why you should not build profiles of private individuals.
- MiCA / BaFin context for EU retail crypto.
- German tax: crypto disposals are private sales (§23 EStG) with a one-year holding rule, taxed at
  the personal income rate — **not** the 25% KESt that applies to my securities account. Note that
  I need transaction-level records from day one, and that this differs from how my stock account is
  taxed.
- State clearly: this system is a **research and monitoring tool**. It is not investment advice and
  must not be described as such.
- Do **not** propose anything that manufactures engagement, buys promotion, or coordinates posting.

## 13. Reality check — include this section verbatim in your output

Before the code, tell me plainly:
- What fraction of memecoin pumps are realistically detectable from social data *before* the move.
- How much of the edge is latency (which I will lose to funded sniper infrastructure) vs.
  interpretation (where a careful solo dev can actually compete).
- Why a monitoring system that produces *good journaling data* is worth more to me than one that
  produces fast alerts I can't act on.
- The honest failure mode: most tokens go to zero, and a tracker that finds them earlier mostly
  finds losers earlier.

## 14. Build order — ship something working in week one

1. **M1:** Telegram collector (public preview mode) + SQLite + contract-address extraction + a bot
   that forwards raw matches. Working end-to-end in days.
2. **M2:** DexScreener enrichment + safety checks + first composite score.
3. **M3:** Reddit collector + cross-platform confluence.
4. **M4:** X collector at the chosen budget tier (curated lists first, search later).
5. **M5:** Anti-manipulation layer + organic score.
6. **M6:** Forward-testing log + weekly performance report + caller leaderboard.
7. **M7:** Tuning from real labelled outcomes.

Do not build M4 before M1–M3 work. X is where the money goes and the last place you should spend it.

## 15. Output format I want from you

1. Architecture comparison table (Level A/B/C) with real costs.
2. Recommendation + reasoning.
3. Full repo tree.
4. Complete, runnable code — no `# TODO: implement`, no pseudocode.
5. `config.yaml` with every tunable in one place (channels, subreddits, X lists, thresholds,
   weights, cooldowns, spend caps).
6. `.env.example` with every required credential and where to obtain it.
7. `README.md` with setup, cost table, and the legal section.
8. A `RISKS.md` restating §11 and §13.
9. Test suite covering the scenario catalogue.

Ask me clarifying questions **only** where a wrong assumption would waste significant work.
Otherwise choose sensible defaults and state them.

```
=== PROMPT END ===
```

---

## 16. Config skeleton to hand over with the prompt

```yaml
budget:
  monthly_cap_eur: 30
  x_provider: "third_party"      # official | third_party | none
  hard_stop_on_cap: true

sources:
  telegram:
    mode: "public_preview"        # bot_api | mtproto | public_preview
    channels: []                  # fill with t.me handles
    poll_interval_s: 45
  reddit:
    enabled: true
    subreddits: [CryptoMoonShots, SatoshiStreetBets, solana, memecoins]
    poll_interval_s: 60
  x:
    enabled: false
    mode: "lists"                 # lists | search | stream
    list_ids: []
    max_reads_per_day: 2000

scoring:
  weights:
    velocity: 0.20
    acceleration: 0.25
    unique_authors: 0.20
    cross_platform: 0.20
    caller_credibility: 0.15
  organic_score_floor: 40         # below this, suppress alert regardless of attention
  alert_tiers: { watch: 55, warm: 70, hot: 85 }
  cooldown_minutes_per_contract: 60

safety:
  require_liquidity_usd_min: 8000
  require_mint_authority_revoked: true
  max_top10_holder_pct: 35
  honeypot_check: true
```

---

## 17. Shorter prompt variants

**17a — "Just tell me which existing tools to use" (no build)**
> Compare the current landscape of memecoin social-tracking tools across X, Reddit and Telegram as
> of today. Cover LunarCrush, Santiment, Kaito, Cookie.fun, DexScreener/DEXTools/Birdeye, and the
> main Telegram alert and call-tracker bots. For each: exact metrics exposed, update latency,
> price, API availability, and what it cannot do. Then recommend a stack for a €30/month budget
> whose goal is research and journaling, not sniping. Be explicit about which tools are marketing
> wrappers around a public API. Verify pricing by searching — do not answer from memory.

**17b — "Build only the Telegram half"**
> Build a production Telegram memecoin monitor in Python. Compare Bot API vs. Telethon MTProto vs.
> `t.me/s/` public-preview scraping on access, ban risk, setup cost and data completeness, then
> implement the safest one that meets the requirement of reading public call channels I have not
> joined. Extract contract addresses and tickers, enrich via DexScreener, deduplicate forward waves,
> attribute every call to its channel, and forward-score each channel's hit rate at +1h/+24h.
> Handle FloodWaitError, session persistence and account-ban failover. Output a runnable repo.

**17c — "Design only the scoring model"**
> Design a composite attention-scoring model for memecoins from multi-platform social data.
> Specify every feature, its formula, its normalisation, its weight, and its failure mode. Include
> a separate organic-vs-manufactured score. Explain how to forward-test it without survivorship
> bias and what statistical power I need before trusting the weights. Assume the data is
> adversarial and that most tokens go to zero.

**17d — "Audit what I built"**
> Here is my memecoin social tracker. Act as a hostile reviewer. Find: look-ahead bias, survivorship
> bias, unhandled rate limits, silent data loss, ticker-collision bugs, sentiment errors on sarcasm
> and post-rug text, and any place where manufactured engagement would score as organic. Rank
> findings by how much money they'd cost me. Do not praise anything.
