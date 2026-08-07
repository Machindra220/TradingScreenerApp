import os
import json
import glob
from io import StringIO
import requests
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, send_file
from werkzeug.utils import secure_filename
from datetime import datetime

hh_hl_us_bp = Blueprint("hh_hl_us", __name__)

# Absolute Path Ingestion & Variable Sync Configuration Matrix
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'us_hhhl'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_us_hhhl_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')

os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

def get_latest_csv(directory):
    list_of_files = glob.glob(os.path.join(directory, '*.csv'))
    return max(list_of_files, key=os.path.getmtime) if list_of_files else None

def fetch_snp500_with_sectors():
    """Fallback: Scrapes S&P 500 roster with official GICS Sector mappings."""
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        tables = pd.read_html(StringIO(response.text))
        df = tables[0]
        df['Symbol'] = df['Symbol'].str.replace('.', '-', regex=False)
        return df[['Symbol', 'GICS Sector']].rename(columns={'Symbol': 'symbol', 'GICS Sector': 'sector'}).to_dict('records')
    except Exception as e:
        print(f"Error fetching S&P 500 list: {e}")
        return []

def detect_hh_hl_stage2_us(symbol, sector="N/A", bench_df=None):
    try:
        clean_sym = str(symbol).strip().upper().replace('.', '-')
        ticker = yf.Ticker(clean_sym)
        df = ticker.history(period="1y")

        if df.empty or len(df) < 200:
            return None

        close = df['Close']
        curr_price = float(close.iloc[-1])
        ma200_series = close.rolling(window=200).mean()
        ma200 = float(ma200_series.iloc[-1])
        
        # Stage 2 Baseline Trend Filter
        if curr_price < ma200:
            return None

        # Detect 5-Bar Pivot Lows
        df['is_low'] = (df['Low'] < df['Low'].shift(1)) & (df['Low'] < df['Low'].shift(2)) & \
                       (df['Low'] < df['Low'].shift(-1)) & (df['Low'] < df['Low'].shift(-2))
        
        lows_df = df[df['is_low']]
        if len(lows_df) < 2:
            return None

        last_swing_low = float(lows_df['Low'].iloc[-1])
        prev_swing_low = float(lows_df['Low'].iloc[-2])
        last_low_date_idx = int(df.index.get_loc(lows_df.index[-1]))
        total_bars = len(df)
        bars_since_pivot = int((total_bars - 1) - last_low_date_idx)

        # HH-HL Criterion: Recent Pivot Low must be higher than previous Pivot Low
        if last_swing_low > prev_swing_low and curr_price > last_swing_low:
            high_52 = float(df['High'].max())
            retracement = float(round(((high_52 - curr_price) / high_52) * 100, 2))
            sl_percent = float(round(((curr_price - last_swing_low) / curr_price) * 100, 2))

            # Benchmark RS Ratio vs S&P 500 (^GSPC)
            if bench_df is not None and not bench_df.empty:
                aligned_bench = bench_df['Close'].reindex(df.index, method='ffill')
                rs_series = df['Close'] / aligned_bench
                rs_score = float(round(float(rs_series.iloc[-1] * 1000), 2))
                rs_20d_ago = float(rs_series.iloc[-20] * 1000) if len(rs_series) >= 20 else rs_score
                rs_change = float(round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1))
            else:
                rs_score = float(round(curr_price / ma200, 2))
                rs_20d_ago = float(round(float(close.iloc[-20]) / float(ma200_series.iloc[-20]), 2))
                rs_change = float(round(((rs_score - rs_20d_ago) / rs_20d_ago) * 100, 1))

            if rs_change > 3.0:
                rs_trend = "🚀 Accelerating"
            elif rs_change >= 0:
                rs_trend = "🟢 Steady"
            else:
                rs_trend = "⚠️ Fading"

            # Cast explicitly to native bool to avoid NumPy JSON serialization issues
            is_fresh_reversal = bool(bars_since_pivot <= 10 and sl_percent <= 8.0)

            return {
                "symbol": clean_sym,
                "sector": str(sector),
                "price": round(curr_price, 2),
                "last_swing_low": round(last_swing_low, 2),
                "sl_percent": sl_percent,
                "retracement": retracement,
                "rs": rs_score,
                "rs_trend": rs_trend,
                "rs_change": rs_change,
                "bars_since_pivot": bars_since_pivot,
                "is_fresh": is_fresh_reversal
            }
    except Exception as e:
        print(f"Error processing US symbol {symbol}: {e}")
        return None

@hh_hl_us_bp.route("/hh-hl-us", methods=["GET", "POST"])
def hh_hl_us_view():
    stocks = []
    summary_message = None
    last_run_time = None
    
    last_file_name = "None"
    if os.path.exists(LAST_CSV_CONFIG):
        try:
            with open(LAST_CSV_CONFIG, 'r') as cf:
                last_file_name = json.load(cf).get("last_used_csv", "None")
        except Exception:
            last_file_name = "None"

    if request.method == "POST":
        file = request.files.get('file')
        filepath = get_latest_csv(UPLOAD_FOLDER)

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            last_file_name = filename
            try:
                with open(LAST_CSV_CONFIG, 'w') as cf:
                    json.dump({"last_used_csv": last_file_name}, cf)
            except Exception as e:
                print(f"Error saving config: {e}")

        # Download S&P 500 Benchmark for RS Calculations
        bench_df = yf.Ticker("^GSPC").history(period="1y")

        if filepath and os.path.exists(filepath):
            try:
                df_input = pd.read_csv(filepath)
                df_input.columns = df_input.columns.str.strip().str.lower()
                
                stock_items = []
                sym_col = 'symbol' if 'symbol' in df_input.columns else df_input.columns[0]
                sec_col = 'sector' if 'sector' in df_input.columns else None

                for idx, row in df_input.iterrows():
                    sym = str(row[sym_col]).strip().upper()
                    sec = str(row[sec_col]).strip() if sec_col and pd.notna(row[sec_col]) else "N/A"
                    stock_items.append({"symbol": sym, "sector": sec})
            except Exception as e:
                summary_message = f"❌ Error reading CSV: {str(e)}"
                stock_items = []
        else:
            print("🌐 Processing default S&P 500 roster...")
            stock_items = fetch_snp500_with_sectors()
            last_file_name = "S&P 500 Index (Default)"

        if stock_items:
            for item in stock_items[:200]:
                res = detect_hh_hl_stage2_us(item['symbol'], sector=item.get('sector', 'N/A'), bench_df=bench_df)
                if res:
                    stocks.append(res)

            stocks.sort(key=lambda x: x['rs'], reverse=True)
            last_run_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

            cache_payload = {
                "last_run": last_run_time,
                "stocks": stocks
            }

            try:
                with open(RESULTS_JSON, 'w') as f:
                    json.dump(cache_payload, f)
            except Exception as e:
                print(f"Error caching results: {e}")

            summary_message = f"✅ US Scan Complete using {last_file_name}. Found {len(stocks)} stocks."

    else:
        # Safe JSON decoding on GET requests
        if os.path.exists(RESULTS_JSON):
            try:
                with open(RESULTS_JSON, 'r') as f:
                    cached_data = json.load(f)
                    if isinstance(cached_data, dict):
                        stocks = cached_data.get("stocks", [])
                        last_run_time = cached_data.get("last_run", "Unknown")
                    else:
                        stocks = cached_data
                        last_run_time = "N/A"
            except (json.JSONDecodeError, Exception) as e:
                print(f"Warning: Could not decode cached JSON file ({e}). Starting fresh.")
                stocks = []
                last_run_time = "N/A"

    # Sector Breakdown Analysis
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'N/A')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sorted_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top_5 = [s[0] for s in sorted_sectors[:5] if s[0] != 'N/A']

    color_palette = [
        {"bg": "rgba(16, 185, 129, 0.15)", "text": "#34d399", "badge": "#10b981", "border": "rgba(16, 185, 129, 0.3)"},
        {"bg": "rgba(59, 130, 246, 0.15)", "text": "#60a5fa", "badge": "#3b82f6", "border": "rgba(59, 130, 246, 0.3)"},
        {"bg": "rgba(168, 85, 247, 0.15)", "text": "#c084fc", "badge": "#a855f7", "border": "rgba(168, 85, 247, 0.3)"},
        {"bg": "rgba(245, 158, 11, 0.15)", "text": "#fbbf24", "badge": "#f59e0b", "border": "rgba(245, 158, 11, 0.3)"},
        {"bg": "rgba(244, 63, 94, 0.15)", "text": "#f472b6", "badge": "#f43f5e", "border": "rgba(244, 63, 94, 0.3)"}
    ]

    top_sectors_meta = []
    sector_color_map = {}

    for idx, sec in enumerate(top_5):
        theme = color_palette[idx]
        count = sector_counts[sec]
        sector_color_map[sec] = theme
        top_sectors_meta.append({
            "name": sec,
            "count": count,
            "theme": theme
        })

    return render_template("hh_hl_us.html", 
                           stocks=stocks, 
                           summary_message=summary_message, 
                           last_file=last_file_name,
                           last_run_time=last_run_time,
                           top_sectors=top_sectors_meta,
                           sector_color_map=sector_color_map)

@hh_hl_us_bp.route("/export-hhhl-us")
def export_hhhl_us():
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON, 'r') as f:
                cached_data = json.load(f)
                data = cached_data.get("stocks", []) if isinstance(cached_data, dict) else cached_data
            df = pd.DataFrame(data)
            export_path = os.path.join(UPLOAD_FOLDER, 'us_hhhl_export.csv')
            df.to_csv(export_path, index=False)
            return send_file(export_path, as_attachment=True)
        except Exception as e:
            print(f"Error during export: {e}")
            return "Error exporting cache", 500
    return "No data to export", 404