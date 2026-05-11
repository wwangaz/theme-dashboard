#!/usr/bin/env python3
"""
Backtest the TOP-8 Bullish portfolio strategy against collected git history.

Reads every historical commit of docs/data/latest.json, simulates the same
open/close rules as analysis/portfolio.py, and fetches actual historical
closing prices via yfinance.

Usage:
    python backtest.py
Output:
    docs/data/backtest.json   (read by portfolio.html)
"""

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent
OUTPUT_PATH = ROOT / "docs" / "data" / "backtest.json"

SPY               = "SPY"
CONVICTION_ENTRY  = 7
MAX_POSITIONS     = 8
MAX_TICKERS       = 5
INITIAL_NAV       = 100.0   # normalised starting value


# ── Git helpers ───────────────────────────────────────────────────────────────

def git_history() -> list[tuple[str, date]]:
    """Return (hash, commit_date) for every commit that touched latest.json, oldest first."""
    result = subprocess.run(
        ["git", "log", "--format=%H %cd", "--date=short", "--",
         "docs/data/latest.json"],
        capture_output=True, text=True, cwd=ROOT,
    )
    rows = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        h, d = line.split(" ", 1)
        rows.append((h, date.fromisoformat(d)))
    return list(reversed(rows))  # oldest first


def git_show_json(hash_: str) -> dict | None:
    result = subprocess.run(
        ["git", "show", f"{hash_}:docs/data/latest.json"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except Exception:
        return None


# ── Price helpers ─────────────────────────────────────────────────────────────

def download_all_prices(tickers: set[str], start: date, end: date) -> pd.DataFrame:
    """Batch-download adjusted closes for all tickers across the whole period."""
    t_list = sorted(tickers)
    raw = yf.download(t_list, start=start, end=end + timedelta(days=7),
                      auto_adjust=True, progress=False)
    # yfinance returns a MultiIndex DataFrame for multiple tickers
    try:
        closes = raw["Close"]
    except KeyError:
        closes = raw  # single ticker fallback
    # Flatten single-ticker edge case (Series → DataFrame)
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(name=t_list[0])
    return closes


def price_on(closes: pd.DataFrame, ticker: str, target: date) -> float | None:
    if ticker not in closes.columns:
        return None
    series = closes[ticker].dropna()
    available = series[series.index <= pd.Timestamp(target)]
    return float(available.iloc[-1]) if not available.empty else None


def basket_return(entry_prices: dict, exit_prices: dict) -> float | None:
    pairs = [(entry_prices[t], exit_prices[t])
             for t in entry_prices if t in exit_prices and entry_prices[t]]
    if not pairs:
        return None
    return sum((ex - en) / en for en, ex in pairs) / len(pairs)


# ── Simulation ────────────────────────────────────────────────────────────────

def run_backtest():
    history = git_history()
    if not history:
        print("No git history found for docs/data/latest.json — has the pipeline run yet?")
        sys.exit(1)

    print(f"Found {len(history)} snapshots: {history[0][1]} → {history[-1][1]}")

    # ── Collect all tickers we'll ever need ───────────────────────────────────
    print("Scanning snapshots for tickers...")
    all_tickers: set[str] = {SPY}
    daily_snaps: list[tuple[date, dict]] = []

    for hash_, snap_date in history:
        data = git_show_json(hash_)
        if not data:
            continue
        daily_snaps.append((snap_date, data))
        for theme in data.get("themes", []):
            if (theme.get("direction") == "Bullish"
                    and theme.get("conviction_score", 0) >= CONVICTION_ENTRY):
                all_tickers.update(
                    theme.get("representative_tickers", [])[:MAX_TICKERS])

    if not daily_snaps:
        print("No readable snapshots found.")
        sys.exit(1)

    # ── Fetch all prices in one shot ──────────────────────────────────────────
    start_d, end_d = daily_snaps[0][0], daily_snaps[-1][0]
    print(f"Downloading prices for {len(all_tickers)} tickers ({start_d} → {end_d})...")
    closes = download_all_prices(all_tickers, start_d - timedelta(days=7), end_d)

    # ── Portfolio state ───────────────────────────────────────────────────────
    # Each slot carries a dollar allocation; NAV = sum of slot values + cash.
    nav        = INITIAL_NAV
    cash       = INITIAL_NAV
    slots: list[dict] = []   # each slot: theme_id, theme_name, tickers,
                              #   entry_prices, entry_alloc, conviction_at_entry

    trade_log: list[dict] = []   # closed trades for the table
    daily_nav: list[dict] = []   # [{date, portfolio_nav, spy_nav}]
    spy_baseline: float | None = None

    for sim_date, snap_data in daily_snaps:
        themes = snap_data.get("themes", [])
        candidates = sorted(
            [t for t in themes
             if t.get("direction") == "Bullish"
             and t.get("conviction_score", 0) >= CONVICTION_ENTRY],
            key=lambda t: t["conviction_score"], reverse=True,
        )
        cand_ids = {c["id"] for c in candidates}

        # ── Close slots no longer in candidate set ────────────────────────────
        still_open = []
        for slot in slots:
            if slot["theme_id"] in cand_ids:
                still_open.append(slot)
                continue
            # Determine exit reason
            original = next((t for t in themes if t["id"] == slot["theme_id"]), None)
            if original is None:
                reason = "theme_gone"
            elif original.get("conviction_score", 0) < CONVICTION_ENTRY:
                reason = "conviction_drop"
            else:
                reason = "direction_changed"

            exit_prices = {t: price_on(closes, t, sim_date) for t in slot["tickers"]}
            exit_prices = {t: v for t, v in exit_prices.items() if v}
            ret = basket_return(slot["entry_prices"], exit_prices)
            realized_value = slot["entry_alloc"] * (1 + (ret or 0))
            cash += realized_value

            trade_log.append({
                "theme_id":   slot["theme_id"],
                "theme_name": slot["theme_name"],
                "entry_date": slot["entry_date"],
                "exit_date":  str(sim_date),
                "exit_reason": reason,
                "realized_return": round(ret, 6) if ret is not None else None,
            })
        slots = still_open

        # ── Rebalance: open new / displace weakest ────────────────────────────
        for cand in candidates:
            if any(s["theme_id"] == cand["id"] for s in slots):
                continue  # already held

            if len(slots) >= MAX_POSITIONS:
                weakest = min(slots, key=lambda s: s["conviction_at_entry"])
                if cand["conviction_score"] <= weakest["conviction_at_entry"]:
                    continue
                # Close weakest to make room
                exit_prices = {t: price_on(closes, t, sim_date) for t in weakest["tickers"]}
                exit_prices = {t: v for t, v in exit_prices.items() if v}
                ret = basket_return(weakest["entry_prices"], exit_prices)
                cash += weakest["entry_alloc"] * (1 + (ret or 0))
                trade_log.append({
                    "theme_id":   weakest["theme_id"],
                    "theme_name": weakest["theme_name"],
                    "entry_date": weakest["entry_date"],
                    "exit_date":  str(sim_date),
                    "exit_reason": "displaced_by_stronger",
                    "realized_return": round(ret, 6) if ret is not None else None,
                })
                slots = [s for s in slots if s["theme_id"] != weakest["theme_id"]]

            raw_tickers = cand.get("representative_tickers", [])[:MAX_TICKERS]
            entry_prices = {t: price_on(closes, t, sim_date) for t in raw_tickers}
            entry_prices = {t: v for t, v in entry_prices.items() if v}
            if not entry_prices:
                continue

            # Allocate equal slot = nav / MAX_POSITIONS
            alloc = nav / MAX_POSITIONS
            if cash < alloc:
                alloc = cash  # use what's available
            if alloc <= 0:
                continue
            cash -= alloc
            slots.append({
                "theme_id":   cand["id"],
                "theme_name": cand["name"],
                "entry_date": str(sim_date),
                "tickers":    list(entry_prices),
                "entry_prices": entry_prices,
                "entry_alloc":  alloc,
                "conviction_at_entry": cand["conviction_score"],
            })

        # ── Compute today's NAV ───────────────────────────────────────────────
        slot_values = []
        for slot in slots:
            curr = {t: price_on(closes, t, sim_date) for t in slot["tickers"]}
            curr = {t: v for t, v in curr.items() if v}
            ret = basket_return(slot["entry_prices"], curr)
            slot_values.append(slot["entry_alloc"] * (1 + (ret or 0)))
        nav = cash + sum(slot_values)

        spy_price = price_on(closes, SPY, sim_date)
        if spy_baseline is None:
            spy_baseline = spy_price
        spy_nav = INITIAL_NAV * (spy_price / spy_baseline) if spy_price and spy_baseline else INITIAL_NAV

        daily_nav.append({
            "date":          str(sim_date),
            "portfolio_nav": round(nav, 4),
            "spy_nav":       round(spy_nav, 4),
            "open_count":    len(slots),
        })
        port_ret = (nav / INITIAL_NAV - 1)
        spy_ret  = (spy_nav / INITIAL_NAV - 1)
        print(f"  {sim_date}: {len(slots)} open | "
              f"NAV={nav:.2f} ({port_ret:+.2%}) | "
              f"SPY={spy_nav:.2f} ({spy_ret:+.2%})")

    # ── Final summary ─────────────────────────────────────────────────────────
    final_nav     = daily_nav[-1]["portfolio_nav"] if daily_nav else INITIAL_NAV
    final_spy_nav = daily_nav[-1]["spy_nav"]       if daily_nav else INITIAL_NAV
    port_cum = final_nav     / INITIAL_NAV - 1
    spy_cum  = final_spy_nav / INITIAL_NAV - 1

    wins = [t for t in trade_log if (t["realized_return"] or 0) > 0]

    output = {
        "generated_at": str(date.today()),
        "period": {"start": str(daily_snaps[0][0]), "end": str(daily_snaps[-1][0])},
        "initial_nav": INITIAL_NAV,
        "daily_nav": daily_nav,
        "trades": trade_log,
        "summary": {
            "portfolio_cumulative": round(port_cum, 6),
            "spy_cumulative":       round(spy_cum, 6),
            "alpha":                round(port_cum - spy_cum, 6),
            "total_trades":         len(trade_log),
            "win_rate":             round(len(wins) / len(trade_log), 3) if trade_log else None,
            "tracking_days":        len(daily_nav),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    s = output["summary"]
    sign = lambda v: "+" if v >= 0 else ""
    print(f"\n{'='*50}")
    print(f"  Period:    {output['period']['start']} → {output['period']['end']}")
    print(f"  Portfolio: {sign(s['portfolio_cumulative'])}{s['portfolio_cumulative']:.2%}")
    print(f"  SPY:       {sign(s['spy_cumulative'])}{s['spy_cumulative']:.2%}")
    print(f"  Alpha:     {sign(s['alpha'])}{s['alpha']:.2%}")
    print(f"  Trades:    {s['total_trades']}  Win rate: {(s['win_rate'] or 0):.0%}")
    print(f"  Days:      {s['tracking_days']}")
    print(f"  Output:    {OUTPUT_PATH}")


if __name__ == "__main__":
    run_backtest()
