"""M1 data-quality check — read-only report over the tracker database.

Usage:  python check_m1.py [path/to/tracker.db]      (default: data/tracker.db)

Opens the DB in read-only mode (URI mode=ro) — this script can never modify it.
"""

import sqlite3
import sys

# Console on Windows may fall back to cp1252 when piped; message text
# contains emojis, so force UTF-8 with replacement.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_decoded_len(s: str) -> int:
    """Byte length of the base58-decoded string, -1 if not valid base58.
    A real Solana pubkey decodes to exactly 32 bytes — the strongest cheap
    test for regex false positives."""
    n = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            return -1
        n = n * 58 + idx
    return (n.bit_length() + 7) // 8 + (len(s) - len(s.lstrip("1")))


def snippet(text: str, width: int = 70) -> str:
    t = " ".join((text or "").split())
    return t[: width - 1] + "…" if len(t) > width else t


def print_table(title: str, headers: list[str], rows: list[tuple]) -> None:
    print(f"\n== {title} ==")
    if not rows:
        print("(no rows)")
        return
    str_rows = [[str(c) if c is not None else "-" for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in str_rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in str_rows:
        print(fmt.format(*r))


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/tracker.db"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = conn.execute

    # -- 1. message counts, total and per channel ------------------------------
    total = q("SELECT COUNT(*) FROM mentions").fetchone()[0]
    per_channel = q("""
        SELECT s.handle, COUNT(*) AS n,
               SUM(CASE WHEN m.contract_address IS NOT NULL THEN 1 ELSE 0 END) AS with_ca,
               SUM(m.is_duplicate) AS dups
        FROM mentions m JOIN sources s ON s.source_id = m.source_id
        GROUP BY s.handle ORDER BY n DESC
    """).fetchall()
    print(f"\nDatabase: {db_path}   |   total messages: {total}")
    print_table("1. Messages per channel", ["channel", "messages", "with CA", "duplicates"],
                per_channel)

    # -- 2. timestamp range and UTC conformity ---------------------------------
    ts_min, ts_max, ts_null = q(
        "SELECT MIN(ts_utc), MAX(ts_utc), SUM(CASE WHEN ts_utc IS NULL THEN 1 ELSE 0 END)"
        " FROM mentions").fetchone()
    non_utc = q("""
        SELECT COUNT(*) FROM mentions
        WHERE ts_utc IS NOT NULL
          AND ts_utc NOT LIKE '%+00:00' AND ts_utc NOT LIKE '%Z'
    """).fetchone()[0]
    print_table("2. Timestamps", ["metric", "value"], [
        ("oldest", ts_min),
        ("newest", ts_max),
        ("NULL timestamps", ts_null),
        ("non-UTC offsets", f"{non_utc}  ({'OK, all UTC' if non_utc == 0 else 'PROBLEM'})"),
    ])

    # -- 3. hit composition ----------------------------------------------------
    comp = q("""
        SELECT
          SUM(CASE WHEN contract_address IS NOT NULL AND ticker IS NOT NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN contract_address IS NOT NULL AND ticker IS NULL     THEN 1 ELSE 0 END),
          SUM(CASE WHEN contract_address IS NULL     AND ticker IS NOT NULL THEN 1 ELSE 0 END),
          SUM(CASE WHEN contract_address IS NULL     AND ticker IS NULL     THEN 1 ELSE 0 END)
        FROM mentions
    """).fetchone()
    both, ca_only, ticker_only, none = comp
    print_table("3. Hit composition", ["category", "count", "share"], [
        ("CA + ticker", both, f"{both / total:.1%}" if total else "-"),
        ("CA only", ca_only, f"{ca_only / total:.1%}" if total else "-"),
        ("ticker only", ticker_only, f"{ticker_only / total:.1%}" if total else "-"),
        ("no hit (7.)", none, f"{none / total:.1%}" if total else "-"),
    ])

    # -- 4. distinct contract addresses ----------------------------------------
    distinct_ca = q("SELECT COUNT(DISTINCT contract_address) FROM mentions"
                    " WHERE contract_address IS NOT NULL").fetchone()[0]

    # full-scan base58 validity: every distinct address, not just the sample
    all_addrs = [r[0] for r in q("SELECT DISTINCT contract_address FROM mentions"
                                 " WHERE contract_address IS NOT NULL")]
    invalid = [a for a in all_addrs if b58_decoded_len(a) != 32]
    print_table("4. Distinct contract addresses", ["metric", "value"], [
        ("distinct addresses", distinct_ca),
        ("decode to 32 bytes (valid pubkey shape)", distinct_ca - len(invalid)),
        ("do NOT decode to 32 bytes (suspect)", len(invalid)),
    ])
    if invalid:
        print_table("4b. Suspect addresses", ["address", "decoded bytes"],
                    [(a, b58_decoded_len(a)) for a in invalid[:10]])

    # -- 5. forward waves (dedupe_hash seen more than once) --------------------
    # Media-only posts have raw_text='' and share the hash of the empty string;
    # they are excluded so they don't fake a giant wave.
    wave_count = q("""
        SELECT COUNT(*) FROM (
          SELECT dedupe_hash FROM mentions WHERE raw_text != ''
          GROUP BY dedupe_hash HAVING COUNT(*) > 1)
    """).fetchone()[0]
    top_waves = q("""
        SELECT COUNT(*) AS occurrences,
               COUNT(DISTINCT s.handle) AS channels,
               MIN(m.raw_text) AS sample_text
        FROM mentions m JOIN sources s ON s.source_id = m.source_id
        WHERE m.raw_text != ''
        GROUP BY m.dedupe_hash HAVING COUNT(*) > 1
        ORDER BY occurrences DESC, channels DESC LIMIT 5
    """).fetchall()
    print(f"\n== 5. Forward waves ==\nhashes seen more than once: {wave_count}")
    print_table("Top 5 waves", ["occurrences", "channels", "text sample"],
                [(o, c, snippet(t, 60)) for o, c, t in top_waves])

    # -- 6. sample of extracted addresses with context -------------------------
    sample = q("""
        SELECT m.contract_address, s.handle, m.raw_text
        FROM mentions m JOIN sources s ON s.source_id = m.source_id
        WHERE m.contract_address IS NOT NULL
        GROUP BY m.contract_address
        ORDER BY RANDOM() LIMIT 10
    """).fetchall()
    print_table("6. Address sample (fresh random sample each run)",
                ["contract_address", "b58", "channel", "text excerpt"],
                [(a, "32B" if b58_decoded_len(a) == 32 else f"{b58_decoded_len(a)}B!",
                  ch, snippet(t, 60)) for a, ch, t in sample])

    # -- 7. no-hit messages (also shown in table 3) ----------------------------
    print(f"\n== 7. Messages without any hit: {none} of {total} "
          f"({none / total:.1%})" if total else "\n== 7. empty DB ==")

    conn.close()


if __name__ == "__main__":
    main()
