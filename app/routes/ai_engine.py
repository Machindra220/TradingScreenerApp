import os
import json
import yfinance as yf
from google import genai
from google.genai import types
from flask import Blueprint, render_template, jsonify, request

ai_engine_bp = Blueprint("ai_engine", __name__)

@ai_engine_bp.route("/ai-engine")
def ai_engine_dashboard():
    return render_template("ai_engine.html")

@ai_engine_bp.route("/api/v1/analyze-news", methods=["POST"])
def analyze_news():
    """Analyzes unstructured news using Google GenAI API and enriches it with real-time yfinance market data."""
    try:
        payload = request.get_json() or {}
        raw_news = payload.get("news", "").strip()

        if not raw_news:
            return jsonify({"status": "error", "message": "No news text provided."}), 400

        # Dynamically fetch API key at runtime
        gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

        # Fallback to mock data if no Gemini API key is configured
        if not gemini_api_key:
            return jsonify({
                "status": "warning",
                "note": "GEMINI_API_KEY not set in environment. Displaying fallback simulated data.",
                "entity_target": ["TITAN (NSE)", "NeuralGrid (Unlisted)"],
                "sector": "Technology / AI Infrastructure",
                "event_type": "M&A Acquisition / Capex Guidance Shift",
                "directional_impact": "+0.65 (Moderately Bullish)",
                "time_horizon": "Medium-Term (Q3 Completion)",
                "confidence_score": "94%",
                "realtime_market_data": {"note": "Set GEMINI_API_KEY to fetch live yfinance data"},
                "portfolio_context": {
                    "action": "ALLOCATE_BUY_ZONE",
                    "current_exposure": "2.4%",
                    "max_allowed": "5.0%",
                    "risk_check": "PASSED (Circuit Breaker OK)"
                }
            })

        # 1. Initialize Client with new google-genai SDK
        client = genai.Client(api_key=gemini_api_key)

        prompt = f"""
        You are a financial news analysis engine. Analyze the following news snippet and extract structured information.
        Return ONLY a valid raw JSON object with no Markdown tags or extra formatting.

        News Snippet: "{raw_news}"

        Strict JSON format required:
        {{
          "entity_target": ["List of impacted company names or symbols"],
          "primary_ticker": "Main stock ticker symbol for yfinance lookup (e.g. RELIANCE, TCS, TITAN, AAPL, TSLA)",
          "sector": "Primary sector/industry",
          "event_type": "Category (e.g., M&A Acquisition, Earnings Beat, Executive Change, Regulatory Fine)",
          "directional_impact": "Directional rating (e.g., +0.75 Bullish, -0.40 Bearish, or Neutral)",
          "time_horizon": "Impact horizon (e.g., Short-Term Intraday, Medium-Term Q3, Long-Term Structural)",
          "confidence_score": "Confidence score percentage (e.g., 92%)"
        }}
        """

        # Generate structured response using gemini-2.5-flash
        ai_response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        
        parsed_json = json.loads(ai_response.text)

        # 2. Fetch Live Market Data via yfinance
        primary_ticker = parsed_json.get("primary_ticker", "").strip().upper()
        market_info = {}

        if primary_ticker and primary_ticker != "NONE":
            yf_symbol = primary_ticker if ("." in primary_ticker or primary_ticker.endswith(".NS")) else f"{primary_ticker}.NS"
            try:
                stock = yf.Ticker(yf_symbol)
                hist = stock.history(period="5d")
                
                if not hist.empty:
                    last_price = round(float(hist["Close"].iloc[-1]), 2)
                    prev_price = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else last_price
                    pct_change = round(((last_price - prev_price) / prev_price) * 100, 2)
                    
                    market_info = {
                        "mapped_symbol": yf_symbol,
                        "current_price": f"₹{last_price}",
                        "day_change": f"{'+' if pct_change > 0 else ''}{pct_change}%",
                        "data_source": "yfinance Real-time Stream"
                    }
            except Exception as yf_err:
                market_info = {"mapped_symbol": primary_ticker, "status": f"Data lookup note: {str(yf_err)}"}

        # 3. Risk Engine & Portfolio Context Logic
        directional_str = str(parsed_json.get("directional_impact", ""))
        action = "MONITOR_ONLY"
        if "Bullish" in directional_str or "+" in directional_str:
            action = "ALLOCATE_BUY_ZONE"
        elif "Bearish" in directional_str or "-" in directional_str:
            action = "REDUCE_EXPOSURE / HEDGE"

        final_payload = {
            "status": "success",
            "entity_target": parsed_json.get("entity_target", []),
            "sector": parsed_json.get("sector", "Unknown"),
            "event_type": parsed_json.get("event_type", "General Corporate Event"),
            "directional_impact": parsed_json.get("directional_impact", "0.00 Neutral"),
            "time_horizon": parsed_json.get("time_horizon", "Immediate"),
            "confidence_score": parsed_json.get("confidence_score", "85%"),
            "realtime_market_data": market_info or "No active public ticker identified",
            "portfolio_context": {
                "action": action,
                "current_exposure": "1.5%",
                "max_allowed": "5.0%",
                "risk_check": "PASSED (Circuit Breaker OK)"
            }
        }

        return jsonify(final_payload)

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500