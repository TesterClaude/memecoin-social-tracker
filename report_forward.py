"""Forward-testing report per channel (§10, M6). Read-only.

Usage:  python report_forward.py [path/to/tracker.db]
"""

import sqlite3
import sys
from urllib.parse import quote

from tracker.baseline import group_comparison, serial_deployers
from tracker.forward import channel_stats

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _fmt_pct(value: float | None, decimals: int = 1) -> str:
    return f"{value:+.{decimals}f}%" if value is not None else "-"


def _fmt_share(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "-"


def _print_table(headers: list[str], rows: list[list]) -> None:
    str_rows = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in str_rows)) if str_rows else len(h)
              for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in str_rows:
        print(fmt.format(*r))


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/tracker.db"
    path_uri = quote(db_path, safe=":/\\")
    conn = sqlite3.connect(f"file:{path_uri}?mode=ro", uri=True)

    stats = channel_stats(conn)
    headers = ["channel", "done", "open", "late", "med MFE", "med MAE",
               "rug", "no pool", ">+50%"]
    rows = [[s["channel"], s["calls_completed"], s["calls_open"],
             s["late_discovery"],
             _fmt_pct(s["median_mfe"]), _fmt_pct(s["median_mae"]),
             _fmt_share(s["rug_share"]), _fmt_share(s["no_pool_share"]),
             _fmt_share(s["over_50_share"])] for s in stats]

    print(f"Forward-testing report — {db_path}\n")
    _print_table(headers, rows)

    # -- called vs. launch baseline -------------------------------------------
    comp = group_comparison(conn)
    print("\n== Called vs. launch baseline (completed calls) ==")
    _print_table(
        ["group", "done", "med MFE", "rug", "no pool"],
        [[name, g["n_done"], _fmt_pct(g["median_mfe"]),
          _fmt_share(g["rug_share"]), _fmt_share(g["no_pool_share"])]
         for name, g in (("called", comp["called"]), ("baseline", comp["baseline"]))])
    if comp["baseline_total"]:
        print(f"\nCoverage: {comp['baseline_mentioned']} of "
              f"{comp['baseline_total']} baseline tokens "
              f"({comp['coverage']:.0%}) were mentioned by tracked channels.")
    else:
        print("\nCoverage: no baseline tokens collected yet.")

    # -- serial deployers -----------------------------------------------------
    deployers = serial_deployers(conn)
    print("\n== Serial deployers (same X handle on >= 2 tokens) ==")
    if deployers:
        _print_table(
            ["handle", "tokens", "done", "med MFE", "rug"],
            [[d["handle"], d["n_tokens"], d["n_done"],
              _fmt_pct(d["median_mfe"]), _fmt_share(d["rug_share"])]
             for d in deployers[:15]])
        if len(deployers) > 15:
            print(f"({len(deployers) - 15} more not shown)")
    else:
        print("(none found)")

    print("""
Notes (§10, non-negotiable context):
- Universe = live-recorded calls since M6 went live. No retroactive
  entries; pre-M6 tokens are absent by design, not by survivorship.
- 'no pool' calls stay in every denominator. Most launchpad tokens never
  graduate — a report ignoring that base rate is lying.
- MFE/MAE are sampled at the fixed checkpoints only (+15m/+1h/+4h/+24h);
  spikes between checkpoints are invisible.
- 'rug' = liquidity fell below the configured floor (or the pair
  vanished) after having been above it — distinct from a price drop
  with intact liquidity. Median MFE/MAE cover calls with a price
  baseline; 'rug'/'no pool'/'>+50%' shares cover ALL completed calls.
- 'late' = calls whose FIRST sighting was an OUTCOME retrospect
  (late discovery). They are excluded from every other column.
- The baseline comes from DexScreener's token-profiles feed: tokens
  whose deployers set up a profile. It leans toward marketed tokens and
  is NOT the universe of all new pools. Coverage measures exactly this
  sample. Baseline checkpoints anchor at discovery time, channel calls
  at mention time.
- A serial X handle is a fact, not a verdict.""")
    conn.close()


if __name__ == "__main__":
    main()
