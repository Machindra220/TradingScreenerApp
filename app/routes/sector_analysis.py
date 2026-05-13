import yfinance as yf
import pandas as pd

def fetch_weekly_data(symbol, weeks=60):
    return yf.Ticker(symbol).history(period=f"{weeks}wk", interval="1wk")

def compute_relative_strength(sector_df, index_df):
    rs = sector_df["Close"] / index_df["Close"]
    return rs.rolling(window=10).mean()

def analyze_sector(index_symbol, benchmark="^NSEI"):
    sector_df = fetch_weekly_data(index_symbol)
    index_df = fetch_weekly_data(benchmark)

    if sector_df.empty or index_df.empty or len(sector_df) < 35:
        return None

    sector_df["30w_ma"] = sector_df["Close"].rolling(window=30).mean()
    sector_df["rs"] = compute_relative_strength(sector_df, index_df)

    latest = sector_df.iloc[-1]
    prev = sector_df.iloc[-2]

    tag = (
        "🔥 Strong" if latest["Close"] > latest["30w_ma"] and latest["30w_ma"] > prev["30w_ma"] and latest["rs"] > prev["rs"]
        else "🌱 Emerging" if latest["Close"] > latest["30w_ma"]
        else "⚠️ Weak" if latest["Close"] < latest["30w_ma"] and latest["rs"] < prev["rs"]
        else "⏸ Neutral"
    )

    summary_message = (
        f"{index_symbol} is currently tagged as {tag}. "
        f"Price: ₹{round(latest['Close'], 2)}, "
        f"30W MA: ₹{round(latest['30w_ma'], 2)}, "
        f"RS: {round(latest['rs'], 2)}"
    )

    return {
        "index": index_symbol,
        "price": round(latest["Close"], 2),
        "ma_30w": round(latest["30w_ma"], 2),
        "rs": round(latest["rs"], 2),
        "tag": tag,
        "summary_message": summary_message
    }
