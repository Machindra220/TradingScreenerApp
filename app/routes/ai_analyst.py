import os
import json
import time
#import google.generativeai as genai
from flask import Blueprint, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

ai_analyst_bp = Blueprint('ai_analyst', __name__)

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DATA_PATH = os.path.join(os.getcwd(), 'uploads', 'volar_us_adaptive', 'volar_results_adaptive.json')

#if GEMINI_API_KEY:
#    genai.configure(api_key=GEMINI_API_KEY)
 #   model = genai.GenerativeModel('gemini-2.0-flash')
#else:
 #   model = None

def retry_on_429(func):
    """Decorator to retry the API call if a rate limit is hit"""
    def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    print(f"Rate limit hit. Retrying in {5 * (attempt + 1)}s...")
                    time.sleep(5 * (attempt + 1))
                    continue
                raise e
        return None
    return wrapper

@retry_on_429
def call_gemini(prompt):
    """Wrapped API call with retry logic"""
 #   response = model.generate_content(prompt)
  #  return response.text

def generate_analysis(query, stocks):
    """Summarizes stock data to minimize token usage"""
    # TOKEN OPTIMIZATION: Only send the top 10 stocks and high-level market stats
    top_stocks = stocks[:10]
    avg_volar = round(sum(s['volar'] for s in stocks) / len(stocks), 2) if stocks else 0
    
    # Create a compact string context
    context_list = [f"{s['symbol']}(V:{s['volar']},RS:{s['rs_percentile']})" for s in top_stocks]
    stock_context = ", ".join(context_list)

    prompt = (
        f"Role: Financial SRE. Strategy: VOLAR. \n"
        f"Market Stats: Avg Volar {avg_volar}, Total Tickers {len(stocks)}.\n"
        f"Top Performers: {stock_context}.\n"
        f"User Query: {query}\n"
        f"Instruction: Analyze technicals briefly."
    )
    return call_gemini(prompt)

@ai_analyst_bp.route("/ai-analyst", methods=["GET", "POST"])
def ai_chat():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True)
        user_query = data.get('message')

        if not os.path.exists(DATA_PATH):
            return jsonify({"response": "No scan data found. Run a scan first."})

        with open(DATA_PATH, 'r') as f:
            stocks = json.load(f).get('stocks', [])
        
        if not stocks:
            return jsonify({"response": "Scan results are empty."})

        try:
            answer = generate_analysis(user_query, stocks)
            return jsonify({"response": answer})
        except Exception as e:
            return jsonify({"response": f"System error: {str(e)}"}), 500

    return render_template("ai_analyst_chat.html")