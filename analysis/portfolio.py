import json
from datetime import date
from pathlib import Path
import yfinance as yf

PORTFOLIO_PATH = Path(__file__).parent.parent / "docs" / "data" / "portfolio.json"
SPY = "SPY"
CONVICTION_ENTRY   = 7    # minimum conviction to open a position
CONVICTION_EXIT    = 5    # hard exit threshold (avoids churn at the 7 boundary)
MIN_HOLDING_DAYS   = 5    # calendar days before any voluntary close (grace period)
MAX_POSITIONS      = 8    # always hold top-N by conviction; bump weakest out if full
MAX_TICKERS_PER_THEME = 5


def _load() -> dict:
    if PORTFOLIO_PATH.exists():
        try:
            return json.loads(PORTFOLIO_PATH.read_text())
        except Exception:
            pass
    return {"positions": [], "daily_returns": [], "theme_history": {}, "summary": {}, "updated_at": None}


def _save(data: dict):
    PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _fetch_close(ticker: str) -> float | None:
    try:
        hist = yf.download(ticker, period="5d", auto_adjust=True, progress=False)
        s = hist["Close"].squeeze().dropna()
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
    """Cumulative since-entry return — used for floating_return display and exit P&L."""
    entry = position["entry_prices"]
    tickers = [t for t in position["tickers"] if t in entry and t in current_prices and entry[t]]
    if not tickers:
        return None
    rets = [(current_prices[t] - entry[t]) / entry[t] for t in tickers]
    return sum(rets) / len(rets)


def _position_daily_return(position: dict, current_prices: dict) -> float | None:
    """Single-day delta — uses the most recent snapshot (or entry_prices if none yet)
    as the prior reference. This is what feeds the portfolio NAV time series.
    """
    snaps = position.get("daily_snapshots") or []
    prev_prices = snaps[-1]["prices"] if snaps else position["entry_prices"]
    tickers = [t for t in position["tickers"]
               if t in prev_prices and t in current_prices and prev_prices[t]]
    if not tickers:
        return None
    rets = [(current_prices[t] - prev_prices[t]) / prev_prices[t] for t in tickers]
    return sum(rets) / len(rets)


def _spy_daily_return() -> float | None:
    try:
        hist = yf.download(SPY, period="5d", auto_adjust=True, progress=False)["Close"].squeeze().dropna()
        if len(hist) >= 2:
            return float(hist.iloc[-1]) / float(hist.iloc[-2]) - 1
    except Exception:
        pass
    return None


def _close_position(p: dict, today: date, exit_reason: str, current_prices: dict):
    exit_prices = {t: current_prices[t] for t in p["tickers"] if t in current_prices}
    p.update(status="closed", exit_date=str(today),
             exit_prices=exit_prices, exit_reason=exit_reason)
    ret = _position_return(p, exit_prices)
    p["realized_return"] = round(ret, 6) if ret is not None else None

    spy_entry = p.get("spy_price_at_entry")
    spy_exit = current_prices.get(SPY)
    if spy_entry and spy_exit:
        spy_ret = (spy_exit - spy_entry) / spy_entry
        p["spy_return_during_holding"] = round(spy_ret, 6)
        p["excess_return"] = round((p["realized_return"] or 0) - spy_ret, 6)
    else:
        p["spy_return_during_holding"] = None
        p["excess_return"] = None

    sign = "+" if (p["realized_return"] or 0) >= 0 else ""
    exc  = p["excess_return"]
    exc_str = f"  excess={'+' if (exc or 0) >= 0 else ''}{(exc or 0):.2%}" if exc is not None else ""
    print(f"  CLOSE {p['theme_name']}  reason={exit_reason}  "
          f"return={sign}{(p['realized_return'] or 0):.2%}{exc_str}")


def update_portfolio(snapshots: list[dict], today: date):
    print("=== Phase 5: Portfolio ===")
    data = _load()
    positions: list[dict] = data["positions"]
    daily_returns: list[dict] = data["daily_returns"]
    theme_history: dict = data.setdefault("theme_history", {})

    # Only Bullish themes with sufficient conviction are candidates
    candidates = sorted(
        [s for s in snapshots
         if s["direction"] == "Bullish" and s["conviction_score"] >= CONVICTION_ENTRY],
        key=lambda s: s["conviction_score"],
        reverse=True,
    )
    candidate_ids = {s["id"] for s in candidates}

    # ── 1. Fetch prices for all relevant tickers ──────────────────────────────
    all_tickers: set[str] = {SPY}
    for p in positions:
        if p["status"] == "open":
            all_tickers.update(p["tickers"])
    for s in candidates:
        all_tickers.update(s.get("representative_tickers", [])[:MAX_TICKERS_PER_THEME])

    current_prices = _fetch_closes(list(all_tickers))
    today_str = str(today)

    # ── 1b. Daily return record — uses prior snapshot, computed BEFORE today's
    # snapshot is appended so the delta is yesterday→today, not today→today.
    positions_held_today = [p for p in positions if p["status"] == "open"]
    if positions_held_today and not any(r["date"] == today_str for r in daily_returns):
        pos_returns = [r for p in positions_held_today
                       if (r := _position_daily_return(p, current_prices)) is not None]
        spy_ret = _spy_daily_return()
        if pos_returns:
            daily_returns.append({
                "date":             today_str,
                "portfolio_return": round(sum(pos_returns) / len(pos_returns), 6),
                "spy_return":       round(spy_ret, 6) if spy_ret is not None else 0.0,
                "open_count":       len(positions_held_today),
            })

    # ── 2. Daily snapshot for each open position (AFTER daily_return calc) ────
    for p in positions:
        if p["status"] != "open":
            continue
        pos_tickers = [t for t in p["tickers"] if t in current_prices]
        if not pos_tickers:
            continue
        p.setdefault("daily_snapshots", [])
        if not any(s["date"] == today_str for s in p["daily_snapshots"]):
            p["daily_snapshots"].append({
                "date": today_str,
                "prices": {t: current_prices[t] for t in pos_tickers},
                "floating_return": round(_position_return(p, current_prices) or 0.0, 6),
            })

    # ── 3. Close positions that are no longer in the candidate set ───────────
    for p in positions:
        if p["status"] != "open":
            continue
        if p["theme_id"] in candidate_ids:
            continue  # still a top Bullish candidate — hold

        snap = next((s for s in snapshots if s["id"] == p["theme_id"]), None)
        days_held = (today - date.fromisoformat(p["entry_date"])).days

        if snap is not None and snap["direction"] == "Bearish":
            # Direction flipped to Bearish — exit immediately, no grace period
            _close_position(p, today, "direction_changed", current_prices)
        elif snap is None:
            # Theme not generated today; wait out the grace period
            if days_held >= MIN_HOLDING_DAYS:
                _close_position(p, today, "theme_gone", current_prices)
        elif snap["conviction_score"] < CONVICTION_EXIT:
            # Conviction collapsed below hard exit threshold
            if days_held >= MIN_HOLDING_DAYS:
                _close_position(p, today, "conviction_drop", current_prices)
        # else: conviction between CONVICTION_EXIT and CONVICTION_ENTRY — hold

    # ── 4. Rebalance: keep top MAX_POSITIONS, bump weakest if needed ──────────
    open_positions = [p for p in positions if p["status"] == "open"]
    open_ids = {p["theme_id"] for p in open_positions}

    def _current_conv(p) -> int:
        """Current conviction for a held position. 0 if theme not in today's snapshots
        (grace-period position) so it's the natural candidate for displacement."""
        snap = next((s for s in snapshots if s["id"] == p["theme_id"]), None)
        return snap["conviction_score"] if snap else 0

    for cand in candidates:
        if cand["id"] in open_ids:
            continue  # already held

        open_positions = [p for p in positions if p["status"] == "open"]

        if len(open_positions) < MAX_POSITIONS:
            # Slot available — open directly
            pass
        else:
            # Check if this candidate is stronger than the weakest held position
            # by CURRENT conviction (not entry conviction — themes evolve)
            weakest = min(open_positions, key=_current_conv)
            if cand["conviction_score"] <= _current_conv(weakest):
                continue  # not strong enough to displace anyone
            _close_position(weakest, today, "displaced_by_stronger", current_prices)
            open_ids.discard(weakest["theme_id"])

        raw_tickers = cand.get("representative_tickers", [])[:MAX_TICKERS_PER_THEME]
        entry_prices = {t: current_prices[t] for t in raw_tickers if t in current_prices}
        if not entry_prices:
            print(f"  SKIP  {cand['name']} — no price data for {raw_tickers}")
            continue

        new_pos = {
            "theme_id":              cand["id"],
            "theme_name":            cand["name"],
            "direction":             "Bullish",
            "entry_date":            str(today),
            "tickers":               list(entry_prices),
            "entry_prices":          entry_prices,
            "conviction_at_entry":   cand["conviction_score"],
            "spy_price_at_entry":    current_prices.get(SPY),
            "status":                "open",
            "exit_date":             None,
            "exit_prices":           None,
            "exit_reason":           None,
            "realized_return":       None,
            "spy_return_during_holding": None,
            "excess_return":         None,
        }
        positions.append(new_pos)
        open_ids.add(cand["id"])
        print(f"  OPEN  {cand['name']}  conv={cand['conviction_score']}  "
              f"tickers={list(entry_prices)}")

    # ── 4b. Theme lifecycle history ───────────────────────────────────────────
    for snap in snapshots:
        theme_id = snap["id"]
        if theme_id not in theme_history:
            theme_history[theme_id] = []
        if not any(h["date"] == today_str for h in theme_history[theme_id]):
            theme_history[theme_id].append({
                "date":       today_str,
                "conviction": snap["conviction_score"],
                "direction":  snap["direction"],
                "stage":      snap["stage"],
                "tickers":    snap.get("representative_tickers", []),
            })

    # ── 5. Cumulative summary ─────────────────────────────────────────────────
    port_cum = spy_cum = 1.0
    for r in daily_returns:
        port_cum *= 1 + r["portfolio_return"]
        spy_cum  *= 1 + r["spy_return"]

    closed = [p for p in positions if p["status"] == "closed" and p["realized_return"] is not None]
    # Win = beat SPY (excess_return > 0). Fall back to absolute return only if
    # excess wasn't computable. Note: Python's `or` treats 0 as falsy, so use an
    # explicit None check to avoid mis-attributing a break-even-vs-SPY position.
    def _win_metric(p):
        e = p.get("excess_return")
        return e if e is not None else p["realized_return"]
    wins = [p for p in closed if _win_metric(p) > 0]

    data.update(
        positions=positions,
        daily_returns=daily_returns,
        theme_history=theme_history,
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
