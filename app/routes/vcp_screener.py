# app/routes/vcp_screener.py
import os
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template
from datetime import datetime
from scipy.signal import find_peaks

# Create Blueprint
vcp_bp = Blueprint("vcp", __name__, url_prefix="/vcp")

# -------------------------------
# VCP Logic
# -------------------------------

def compute_roc(series, days=7):
    """Rate of Change over N days."""
    if len(series) < days:
        return None
    return ((series.iloc[-1] - series.iloc[-days]) / series.iloc[-days]) * 100

def get_contractions(hist, lookback=90):
    """
    Detect contractions in the last N days.
    Contractions are % drops from swing highs to swing lows.
    """
    closes = hist["Close"].iloc[-lookback:]
    if closes.empty:
        return []

    # Find swing highs and lows
    peaks, _ = find_peaks(closes)
    troughs, _ = find_peaks(-closes)

    contractions = []
    for i in range(min(len(peaks), len(troughs))):
        high = closes.iloc[peaks[i]]
        low = closes.iloc[troughs[i]]
        drop_pct = ((low - high) / high) * 100
        contractions.append(round(drop_pct, 2))

    return contractions

def analyze_vcp(ticker):
    """Analyze a single stock for VCP characteristics."""
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="6mo")
        if hist.empty or len(hist) < 40:
            return None

        # Compute contractions
        contractions = get_contractions(hist, lookback=90)
        is_contracting = all(contractions[i] > contractions[i+1] 
                             for i in range(len(contractions)-1)) if contractions else False

        # Volume dry-up
        vol_avg = hist["Volume"].rolling(50).mean().iloc[-1]
        vol_recent = hist["Volume"].iloc[-5:].mean()
        vol_dryup = vol_recent < 0.7 * vol_avg if vol_avg > 0 else False

        # Breakout check
        resistance = hist["Close"].rolling(20).max().iloc[-2]
        breakout = hist["Close"].iloc[-1] > resistance and hist["Volume"].iloc[-1] > 1.5 * vol_avg

        latest = hist.iloc[-1]
        roc7 = compute_roc(hist["Close"], 7)
        roc21 = compute_roc(hist["Close"], 21)

        return {
            "date": latest.name.strftime("%Y-%m-%d"),
            "symbol": ticker,
            "price": round(latest["Close"], 2),
            "volume": int(latest["Volume"]),
            "roc7": round(roc7, 2) if roc7 else None,
            "roc21": round(roc21, 2) if roc21 else None,
            "contractions": contractions,
            "is_contracting": is_contracting,
            "vol_dryup": vol_dryup,
            "breakout": breakout,
        }
    except Exception as e:
        print(f"Error analyzing {ticker}: {e}")
        return None

def scan_universe(tickers):
    """Scan a list of tickers for VCP candidates."""
    results = []
    for t in tickers:
        res = analyze_vcp(t)
        if res and res["is_contracting"] and res["vol_dryup"]:
            results.append(res)
    return results

# -------------------------------
# Flask Routes
# -------------------------------

@vcp_bp.route("/", methods=["GET"])
def vcp_view():
    """Render the VCP screener page with a process button."""
    return render_template("vcp.html")

@vcp_bp.route("/process", methods=["POST"])
def vcp_process():
    """Run the VCP screener when the button is clicked."""
    path = "data/MCAPge250cr.csv"
    if not os.path.exists(path):
        return render_template("vcp.html", error=f"⚠️ Source file not found: {path}")

    df = pd.read_csv(path)
    symbols = [s + ".NS" for s in df["symbol"].dropna().unique()]
    source_name = "MCAPge250cr"

    # Run VCP scan
    results = scan_universe(symbols)

    # Enrich results with tags
    enriched = []
    for stock in results:
        stock["symbol_clean"] = stock["symbol"].replace(".NS", "")
        stock["persistence"] = "new"  # placeholder for persistence tracking
        stock["tag"] = "✅ VCP"
        enriched.append(stock)

    last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
    summary_message = f"✅ Processed {len(enriched)} stocks from {source_name} at {last_processed_time}"

    return render_template("vcp.html", results=enriched, summary=summary_message)
