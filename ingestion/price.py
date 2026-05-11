import json
from datetime import date
from pathlib import Path
import yfinance as yf
from db.models import RawSignal
from db.session import get_session

DEFAULT_WATCHLIST = [
    "NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "META", "AMZN", "TSLA",
    "AAPL", "TSM", "ASML", "INTC", "QCOM", "ARM",  # Semiconductors
    "NEE", "CEG", "VST", "NRG", "ETR",              # Nuclear / Energy
    "IONQ", "RGTI", "QUBT",                          # Quantum
    "RKLB", "LUNR", "ASTS",                          # Space
    "ISRG", "DXCM", "ABMD",                          # Health tech
]

TREASURY_TICKERS = {
    "^IRX": "2Y",
    "^FVX": "5Y",
    "^TNX": "10Y",
}

MACRO_PATH = Path(__file__).parent.parent / "data" / "macro_context.json"


def fetch_prices(tickers: list[str] | None = None) -> list[RawSignal]:
    watchlist = tickers or DEFAULT_WATCHLIST
    signals: list[RawSignal] = []
    today = date.today()

    try:
        data = yf.download(watchlist, period="21d", auto_adjust=True, progress=False)
        close = data["Close"]
        volume = data["Volume"]

        for ticker in watchlist:
            if ticker not in close.columns:
                continue
            try:
                prices = close[ticker].dropna()
                vols = volume[ticker].dropna()
                if len(prices) < 2:
                    continue

                latest_close = float(prices.iloc[-1])
                prev_close = float(prices.iloc[-2])
                pct_change = (latest_close - prev_close) / prev_close * 100
                vol_today = float(vols.iloc[-1])
                vol_avg20 = float(vols.iloc[-21:-1].mean()) if len(vols) >= 21 else vol_today
                vol_ratio = vol_today / vol_avg20 if vol_avg20 > 0 else 1.0

                signals.append(RawSignal(
                    source_type="price",
                    source_name="Yahoo Finance",
                    ticker=ticker,
                    headline=f"{ticker} closed at ${latest_close:.2f} ({pct_change:+.1f}%), volume ratio {vol_ratio:.1f}x",
                    content=f"Close: {latest_close:.2f}, Prev: {prev_close:.2f}, Change: {pct_change:+.1f}%, Volume ratio vs 20d avg: {vol_ratio:.2f}x",
                    signal_date=today,
                    url=f"https://finance.yahoo.com/quote/{ticker}",
                ))
            except Exception as e:
                print(f"[price] {ticker} failed: {e}")
    except Exception as e:
        print(f"[price] download failed: {e}")

    return signals


def fetch_treasury_yields() -> dict:
    yields: dict[str, float] = {}
    changes: dict[str, float] = {}

    try:
        tickers = list(TREASURY_TICKERS.keys())
        data = yf.download(tickers, period="5d", auto_adjust=True, progress=False)
        close = data["Close"]

        for ticker, label in TREASURY_TICKERS.items():
            if ticker not in close.columns:
                continue
            series = close[ticker].dropna()
            if len(series) < 2:
                continue
            yields[label] = round(float(series.iloc[-1]) / 100, 4)
            changes[label] = round((float(series.iloc[-1]) - float(series.iloc[-2])) / 100, 4)
    except Exception as e:
        print(f"[price] treasury yield fetch failed: {e}")

    if not yields:
        return {}

    # Yield curve slope and risk label
    y10 = yields.get("10Y", 0)
    y2 = yields.get("2Y", 0)
    spread_2_10 = round(y10 - y2, 4)

    risk_label = _rate_risk_label(y10, changes.get("10Y", 0), spread_2_10)

    return {
        "yields": yields,
        "changes": changes,
        "spread_2_10": spread_2_10,
        "risk_label": risk_label,
        "date": str(date.today()),
    }


def _rate_risk_label(y10: float, change_10: float, spread: float) -> str:
    if spread < 0:
        return "yield_curve_inverted"
    if change_10 > 0.001 and y10 > 0.045:
        return "rising_rates_pressure"
    if change_10 < -0.001:
        return "rates_easing"
    return "rates_stable"


def ingest_prices(extra_tickers: list[str] | None = None):
    signals = fetch_prices(extra_tickers)
    with get_session() as session:
        for s in signals:
            session.add(s)
        session.commit()
    print(f"[price] ingested {len(signals)} signals")

    # Always refresh macro context (no DB, just a JSON file)
    macro = fetch_treasury_yields()
    if macro:
        MACRO_PATH.parent.mkdir(exist_ok=True)
        MACRO_PATH.write_text(json.dumps(macro, ensure_ascii=False, indent=2))
        print(f"[price] treasury yields: 2Y={macro['yields'].get('2Y', 'n/a'):.2%} "
              f"5Y={macro['yields'].get('5Y', 'n/a'):.2%} "
              f"10Y={macro['yields'].get('10Y', 'n/a'):.2%}  "
              f"risk={macro['risk_label']}")
