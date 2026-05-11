import json
from datetime import date
from pathlib import Path
import yfinance as yf

PORTFOLIO_PATH = Path(__file__).parent.parent / "docs" / "data" / "portfolio.json"
SPY = "SPY"
CONVICTION_ENTRY = 7
CONVICTION_EXIT  = 5
MAX_DAYS_HELD    = 30
MAX_TICKERS_PER_THEME = 5


def _load() -> dict:
    if PORTFOLIO_PATH.exists():
        try:
            return json.loads(PORTFOLIO_PATH.read_text())
        except Exception:
            pass
    return {"positions": [], "daily_returns": [], "summary": {}, "updated_at": None}


def _save(data: dict):
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _fetch_close(ticker: str) -> float | None:
    try:
        hist = yf.download(ticker, period="5d", auto_adjust=True, progress=False)
        s = hist["Close"].dropna()
        return round(float(s.iloc[-1]), 4) if len(s) > 0 else None
    except Exception as e:
        print(f"  [portfolio] {ticker} fetch failed: {e}")
        return None


def _fetch_closes(tickers: list[str]) -> dict[str, float]:
    result = {}
    for t in tickers:
        price = _fetch_close(t)
        if price is not None:
            result[t] = price
    return result


def _position_return(position: dict, current_prices: dict) -> float | None:
    entry = position["entry_prices"]
    tickers = [t for t in position["tickers"] if t in entry and t in current_prices and entry[t]]
    if not tickers:
        return None
    rets = []
    for t in tickers:
        r = (current_prices[t] - entry[t]) / entry[t]
        if position["direction"] == "Bearish":
            r = -r  # short: profit when price falls
        rets.append(r)
    return sum(rets) / len(rets)


def _spy_daily_return() -> float | None:
    try:
        hist = yf.download(SPY, period="5d", auto_adjust=True, progress=False)["Close"].dropna()
        if len(hist) >= 2:
            return float(hist.iloc[-1]) / float(hist.iloc[-2]) - 1
    except Exception:
        pass
    return None


def update_portfolio(snapshots: list[dict], today: date):
    print("=== Phase 5: Portfolio ===")
    data = _load()
    positions: list[dict] = data["positions"]
    daily_returns: list[dict] = data["daily_returns"]

    snap_by_id = {s["id"]: s for s in snapshots}

    # ── 1. Collect all tickers needed for prices ──────────────────────────────
    all_tickers: set[str] = {SPY}
    for p in positions:
        if p["status"] == "open":
            all_tickers.update(p["tickers"])
    # Also pre-fetch tickers for potential new positions
    for s in snapshots:
        if s["conviction_score"] >= CONVICTION_ENTRY and s["direction"] in ("Bullish", "Bearish"):
            all_tickers.update(s.get("representative_tickers", [])[:MAX_TICKERS_PER_THEME])

    current_prices = _fetch_closes(list(all_tickers))

    # ── 2. Close positions that no longer qualify ─────────────────────────────
    for p in positions:
        if p["status"] != "open":
            continue
        snap = snap_by_id.get(p["theme_id"])
        days_held = (today - date.fromisoformat(p["entry_date"])).days

        exit_reason = None
        if snap is None:
            exit_reason = "theme_gone"
        elif snap["conviction_score"] < CONVICTION_EXIT:
            exit_reason = "conviction_drop"
        elif snap["direction"] != p["direction"]:
            exit_reason = "direction_reversal"
        elif days_held >= MAX_DAYS_HELD:
            exit_reason = "max_holding"

        if exit_reason:
            exit_prices = {t: current_prices[t] for t in p["tickers"] if t in current_prices}
            p.update(status="closed", exit_date=str(today),
                     exit_prices=exit_prices, exit_reason=exit_reason)
            ret = _position_return(p, exit_prices)
            p["realized_return"] = round(ret, 6) if ret is not None else None
            sign = "+" if (p["realized_return"] or 0) >= 0 else ""
            print(f"  CLOSE {p['theme_name']} ({p['direction']})  "
                  f"reason={exit_reason}  "
                  f"return={sign}{(p['realized_return'] or 0):.2%}")

    # ── 3. Open new positions ─────────────────────────────────────────────────
    open_ids = {p["theme_id"] for p in positions if p["status"] == "open"}

    for snap in snapshots:
        if snap["conviction_score"] < CONVICTION_ENTRY:
            continue
        if snap["direction"] not in ("Bullish", "Bearish"):
            continue
        if snap["id"] in open_ids:
            continue

        raw_tickers = snap.get("representative_tickers", [])[:MAX_TICKERS_PER_THEME]
        entry_prices = {t: current_prices[t] for t in raw_tickers if t in current_prices}
        if not entry_prices:
            print(f"  SKIP  {snap['name']} — no price data for {raw_tickers}")
            continue

        positions.append({
            "theme_id":           snap["id"],
            "theme_name":         snap["name"],
            "direction":          snap["direction"],
            "entry_date":         str(today),
            "tickers":            list(entry_prices),
            "entry_prices":       entry_prices,
            "conviction_at_entry": snap["conviction_score"],
            "status":             "open",
            "exit_date":          None,
            "exit_prices":        None,
            "exit_reason":        None,
            "realized_return":    None,
        })
        open_ids.add(snap["id"])
        print(f"  OPEN  {snap['name']} ({snap['direction']})  "
              f"conv={snap['conviction_score']}  tickers={list(entry_prices)}")

    # ── 4. Daily return record ────────────────────────────────────────────────
    today_str = str(today)
    already_recorded = any(r["date"] == today_str for r in daily_returns)

    open_positions = [p for p in positions if p["status"] == "open"]
    if open_positions and not already_recorded:
        pos_returns = [r for p in open_positions
                       if (r := _position_return(p, current_prices)) is not None]
        spy_ret = _spy_daily_return()
        if pos_returns:
            daily_returns.append({
                "date":             today_str,
                "portfolio_return": round(sum(pos_returns) / len(pos_returns), 6),
                "spy_return":       round(spy_ret, 6) if spy_ret is not None else 0.0,
                "open_count":       len(open_positions),
            })

    # ── 5. Cumulative summary ─────────────────────────────────────────────────
    port_cum = spy_cum = 1.0
    for r in daily_returns:
        port_cum *= 1 + r["portfolio_return"]
        spy_cum  *= 1 + r["spy_return"]

    closed = [p for p in positions if p["status"] == "closed" and p["realized_return"] is not None]
    wins   = [p for p in closed if p["realized_return"] > 0]

    data.update(
        positions=positions,
        daily_returns=daily_returns,
        updated_at=today_str,
        summary={
            "portfolio_cumulative": round(port_cum - 1, 6),
            "spy_cumulative":       round(spy_cum - 1, 6),
            "alpha":                round(port_cum - spy_cum, 6),
            "open_count":           len(open_positions),
            "closed_count":         len(closed),
            "win_rate":             round(len(wins) / len(closed), 3) if closed else None,
            "tracking_days":        len(daily_returns),
        },
    )
    _save(data)

    s = data["summary"]
    sign = lambda v: "+" if v >= 0 else ""
    print(f"  Portfolio {sign(s['portfolio_cumulative'])}{s['portfolio_cumulative']:.2%}  "
          f"SPY {sign(s['spy_cumulative'])}{s['spy_cumulative']:.2%}  "
          f"Alpha {sign(s['alpha'])}{s['alpha']:.2%}  "
          f"Open={s['open_count']}  Closed={s['closed_count']}")
