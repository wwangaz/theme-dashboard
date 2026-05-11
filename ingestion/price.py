from datetime import date
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


def ingest_prices(extra_tickers: list[str] | None = None):
    signals = fetch_prices(extra_tickers)
    with get_session() as session:
        for s in signals:
            session.add(s)
        session.commit()
    print(f"[price] ingested {len(signals)} signals")
