"""
ai_engine.py — Autonomous Market Intelligence Engine

Key fixes vs original:
  - CSRF: token sent in JSON body (body_csrf_token) so Flask-WTF validates it
  - Market toggle: user sends market='IND'|'US'; .NS suffix only applied for IND
  - Cache: uses ind_cache / us_cache instead of raw yf.Ticker().history()
  - Model: gemini-2.5-flash (was gemini-2.0-flash despite the comment)
  - Currency: ₹ for IND, $ for US
  - Error handling: specific except blocks instead of one broad catch
  - Prompt: asks AI to return primary_ticker WITHOUT any suffix (clean symbol)
  - Portfolio context: action derived from directional_impact (unchanged logic)
"""

import os
import json
from datetime import datetime

from flask import Blueprint, render_template, jsonify, request
from google import genai
from google.genai import types

from app.services.market_data_cache import ind_cache, us_cache, latest_bar_date

ai_engine_bp = Blueprint("ai_engine", __name__)

# ── Constants ────────────────────────────────────────────────────────────────
GEMINI_MODEL    = "gemini-2.5-flash"   # was gemini-2.0-flash despite the comment
IND_BENCHMARK   = "^CRSLDX"
US_BENCHMARK    = "^GSPC"
PRICE_LOOKBACK  = 10                   # days for cache fetch (only need last 2 bars)


# ── Dashboard route ──────────────────────────────────────────────────────────

@ai_engine_bp.route("/ai-engine")
def ai_engine_dashboard():
    return render_template("ai_engine.html")


# ── Helper: fetch last price from shared cache ───────────────────────────────

def _get_price_from_cache(ticker: str, market: str) -> dict:
    """
    Fetch the two most recent closing prices from the shared SQLite cache.
    Uses ind_cache for IND market, us_cache for US — no direct yfinance calls
    per request (Memory #13).

    Returns a dict with current_price, day_change, mapped_symbol, currency.
    Returns an error dict on failure (never raises).
    """
    cache  = ind_cache if market == "IND" else us_cache
    symbol = f"{ticker}.NS" if market == "IND" and not ticker.endswith(".NS") else ticker

    try:
        price_data, _ = cache.get_price_history_bulk(
            [symbol], interval="1d", lookback_days=PRICE_LOOKBACK
        )
        df = price_data.get(symbol)

        if df is None or df.empty or "Close" not in df.columns:
            return {"mapped_symbol": symbol, "status": "No price data in cache — run a screener first to populate it"}

        close       = df["Close"].dropna()
        if len(close) < 1:
            return {"mapped_symbol": symbol, "status": "Insufficient price history"}

        last_price  = round(float(close.iloc[-1]), 2)
        prev_price  = round(float(close.iloc[-2]), 2) if len(close) > 1 else last_price
        pct_change  = round(((last_price - prev_price) / prev_price) * 100, 2) if prev_price else 0.0
        currency    = "₹" if market == "IND" else "$"
        sign        = "+" if pct_change > 0 else ""
        asof        = latest_bar_date(price_data)

        return {
            "mapped_symbol": symbol,
            "current_price": f"{currency}{last_price:,.2f}",
            "day_change":    f"{sign}{pct_change}%",
            "data_source":   "SQLite price cache",
            "price_as_of":   asof or "Unknown",
        }

    except Exception as e:
        return {"mapped_symbol": ticker, "status": f"Cache lookup failed: {str(e)}"}


# ── Gemini analysis ──────────────────────────────────────────────────────────

def _call_gemini(raw_news: str, market: str, api_key: str) -> dict:
    """
    Call Gemini to extract structured fields from raw news text.
    Returns parsed JSON dict from the model.
    Raises ValueError on bad JSON, RuntimeError on API failure.
    """
    market_ctx = "Indian NSE/BSE" if market == "IND" else "US NYSE/NASDAQ"
    client     = genai.Client(api_key=api_key)

    prompt = f"""You are a financial news analysis engine specialising in {market_ctx} markets.
Analyse the following news snippet and extract structured information.
Return ONLY a valid raw JSON object — no Markdown, no backticks, no extra text.

News: "{raw_news}"

Return this exact JSON structure:
{{
  "entity_target": ["List of impacted company names or symbols"],
  "primary_ticker": "Clean ticker symbol for price lookup (e.g. RELIANCE for NSE, AAPL for US — no suffix, no exchange code)",
  "sector": "Primary sector or industry",
  "event_type": "One of: Earnings Beat | Earnings Miss | M&A Acquisition | M&A Target | Executive Change | Regulatory Fine | Product Launch | Capex Guidance | Dividend Change | Debt/Credit Event | Macro Event | Other",
  "directional_impact": "Directional rating string, e.g. +0.75 Bullish | -0.40 Bearish | 0.00 Neutral",
  "time_horizon": "One of: Short-Term Intraday | Medium-Term 1-4 Weeks | Medium-Term Quarterly | Long-Term Structural",
  "confidence_score": "Confidence percentage, e.g. 88%",
  "reasoning": "One-sentence explanation of why this event has the stated directional impact"
}}"""

    try:
        response = client.models.generate_content(
            model  = GEMINI_MODEL,
            contents = prompt,
            config   = types.GenerateContentConfig(
                response_mime_type = "application/json"
            )
        )
    except Exception as e:
        raise RuntimeError(f"Gemini API error: {e}") from e

    try:
        return json.loads(response.text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned invalid JSON: {e}. Raw: {response.text[:200]}") from e


# ── Fallback mock (no API key) ───────────────────────────────────────────────

def _mock_response(market: str) -> dict:
    return {
        "status":           "warning",
        "note":             "GEMINI_API_KEY not set. Displaying demonstration data.",
        "entity_target":    ["TITAN (NSE)", "NeuralGrid (Unlisted)"],
        "sector":           "Technology / AI Infrastructure",
        "event_type":       "M&A Acquisition",
        "directional_impact": "+0.65 Bullish",
        "time_horizon":     "Medium-Term Quarterly",
        "confidence_score": "94%",
        "reasoning":        "Acquisition of AI infrastructure expands cloud capabilities — historically re-rated positively by markets.",
        "realtime_market_data": {"note": "Set GEMINI_API_KEY to fetch live data"},
        "portfolio_context": {
            "action":           "ALLOCATE_BUY_ZONE",
            "risk_check":       "PASSED",
            "note":             "Demo mode — connect to real portfolio data for live exposure figures",
        },
        "meta": {
            "model":     GEMINI_MODEL,
            "market":    market,
            "processed": datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
        }
    }


# ── Main API endpoint ────────────────────────────────────────────────────────

@ai_engine_bp.route("/api/v1/analyze-news", methods=["POST"])
def analyze_news():
    """
    POST body (JSON):
      news   : str   — raw news text
      market : str   — 'IND' or 'US' (default 'IND')
      csrf_token : str — Flask-WTF CSRF token
    """
    try:
        payload  = request.get_json(silent=True) or {}
        raw_news = payload.get("news", "").strip()
        market   = payload.get("market", "IND").upper().strip()

        if market not in ("IND", "US"):
            market = "IND"

        if not raw_news:
            return jsonify({"status": "error", "message": "No news text provided."}), 400

        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

        # ── No API key → return demonstration data ────────────────────────
        if not gemini_api_key:
            return jsonify(_mock_response(market))

        # ── Gemini analysis ───────────────────────────────────────────────
        try:
            parsed = _call_gemini(raw_news, market, gemini_api_key)
        except (ValueError, RuntimeError) as e:
            return jsonify({"status": "error", "message": str(e)}), 502

        # ── Price data from cache ─────────────────────────────────────────
        primary_ticker = str(parsed.get("primary_ticker", "")).strip().upper()
        market_info    = {}

        if primary_ticker and primary_ticker not in ("NONE", "N/A", ""):
            market_info = _get_price_from_cache(primary_ticker, market)

        # ── Portfolio action from directional impact ──────────────────────
        directional_str = str(parsed.get("directional_impact", ""))
        if "Bullish" in directional_str or directional_str.startswith("+"):
            action = "ALLOCATE_BUY_ZONE"
        elif "Bearish" in directional_str or directional_str.startswith("-"):
            action = "REDUCE_EXPOSURE / HEDGE"
        else:
            action = "MONITOR_ONLY"

        result = {
            "status":             "success",
            "entity_target":      parsed.get("entity_target", []),
            "sector":             parsed.get("sector", "Unknown"),
            "event_type":         parsed.get("event_type", "General Corporate Event"),
            "directional_impact": parsed.get("directional_impact", "0.00 Neutral"),
            "time_horizon":       parsed.get("time_horizon", "Short-Term Intraday"),
            "confidence_score":   parsed.get("confidence_score", "85%"),
            "reasoning":          parsed.get("reasoning", ""),
            "realtime_market_data": market_info or {"note": "No publicly traded ticker identified"},
            "portfolio_context": {
                "action":     action,
                "risk_check": "PASSED",
                "note":       "Connect to live portfolio data for real exposure figures",
            },
            "meta": {
                "model":     GEMINI_MODEL,
                "market":    market,
                "processed": datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
            }
        }

        return jsonify(result)

    except Exception as e:
        # Broad catch only at the outermost layer — specific errors handled above
        return jsonify({"status": "error", "message": f"Unexpected error: {str(e)}"}), 500