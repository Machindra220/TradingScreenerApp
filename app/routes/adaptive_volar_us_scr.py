import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session

volar_us_adaptive_bp = Blueprint('volar_us_adaptive_bp', __name__)

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads', 'volar_us_adaptive')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_adaptive.json')

# Fixed lookback periods (trading days)
LB_3M = 55    # ~3 months
LB_6M = 122   # ~6 months

# S&P 500 is the correct broad-market benchmark for US RS comparison
INDEX_TICKER = "^GSPC"


def compute_volar(prices):
    if len(prices) < 2:
        return 0
    returns = prices.pct_change().dropna()
    std = returns.std()
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2) if std != 0 else 0


def is_volar_adaptive(symbol):
    """
    Computes RS and VOLAR for both 3M (55-day) and 6M (122-day) lookback periods
    in a single data fetch. Returns None if stock fails EMA200 filter or lacks data.
    """
    try:
        print(f"Processing: {symbol}")

        ticker = yf.Ticker(symbol)
        index_ticker = yf.Ticker(INDEX_TICKER)

        # Fetch enough for 6M lookback + EMA200 warmup + weekend/holiday buffer
        fetch_days = LB_6M + 220
        start_date = (datetime.today() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")

        df = ticker.history(start=start_date)
        idx_df = index_ticker.history(start=start_date)

        print(f"  Data rows → stock: {len(df)}, index: {len(idx_df)}")

        # Need at least 6M data to compute both periods
        if len(df) < LB_6M or len(idx_df) < LB_6M:
            print(f"  Skipping {symbol}: insufficient data (need {LB_6M}, got {len(df)})")
            return None

        close = df['Close']
        idx_close = idx_df['Close']
        curr = close.iloc[-1]

        # EMA200 filter — must be above for uptrend confirmation
        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        if curr <= ema200:
            print(f"  Skipping {symbol}: below EMA200 ({curr:.2f} <= {ema200:.2f})")
            return None

        # --- 3M (55-day) calculations ---
        start_3m = close.iloc[-LB_3M]
        idx_start_3m = idx_close.iloc[-LB_3M]
        perf_3m = (curr / start_3m) - 1
        idx_perf_3m = (idx_close.iloc[-1] / idx_start_3m) - 1
        rs_3m = round(perf_3m / idx_perf_3m, 4) if idx_perf_3m != 0 else 0
        volar_3m = compute_volar(close.iloc[-LB_3M:])

        # --- 6M (122-day) calculations ---
        start_6m = close.iloc[-LB_6M]
        idx_start_6m = idx_close.iloc[-LB_6M]
        perf_6m = (curr / start_6m) - 1
        idx_perf_6m = (idx_close.iloc[-1] / idx_start_6m) - 1
        rs_6m = round(perf_6m / idx_perf_6m, 4) if idx_perf_6m != 0 else 0
        volar_6m = compute_volar(close.iloc[-LB_6M:])

        return {
            "symbol": symbol,
            "price": round(curr, 2),
            # 3M fields
            "rs_3m": rs_3m,
            "volar_3m": volar_3m,
            "perf_3m": round(perf_3m * 100, 2),
            # 6M fields
            "rs_6m": rs_6m,
            "volar_6m": volar_6m,
            "perf_6m": round(perf_6m * 100, 2),
        }

    except Exception as e:
        print(f"  ERROR processing {symbol}: {e}")
        return None


@volar_us_adaptive_bp.route("/volar-us-adaptive", methods=["GET", "POST"])
def volar_us_process():
    stocks, last_processed_time, source_name = [], None, "None"

    if 'scan_history_us' not in session:
        session['scan_history_us'] = []

    if request.method == "POST":
        file = request.files.get('file')

        # Save new CSV if uploaded; otherwise keep the one already in session
        if file and file.filename != '':
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            session['last_uploaded_csv_us'] = filepath
            session['last_filename_us'] = file.filename
            session.modified = True

        saved_path = session.get('last_uploaded_csv_us')

        if saved_path and os.path.exists(saved_path):
            symbols = pd.read_csv(saved_path)['symbol'].dropna().unique().tolist()
            print(f"Symbols loaded: {symbols}")

            raw_results = [r for r in (is_volar_adaptive(s) for s in symbols) if r]
            print(f"Passed EMA200 filter: {len(raw_results)}/{len(symbols)}")

            if raw_results:
                df = pd.DataFrame(raw_results)

                # Percentile rank both RS periods within this universe
                df['rs3_pct'] = df['rs_3m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
                df['rs6_pct'] = df['rs_6m'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

                # --- TREND TRACKING LOGIC ---
                existing_history = {}
                if os.path.exists(RESULTS_JSON):
                    with open(RESULTS_JSON, 'r') as f:
                        old_cache = json.load(f).get('stocks', [])
                        existing_history = {
                            s['symbol']: {
                                'rs3_h':  s.get('rs3_h', []),
                                'rs6_h':  s.get('rs6_h', []),
                                'vol_h':  s.get('vol_h', []),
                                'perf_h': s.get('perf_h', [])
                            } for s in old_cache
                        }

                def inject_history(row):
                    h = existing_history.get(row['symbol'], {'rs3_h': [], 'rs6_h': [], 'vol_h': [], 'perf_h': []})
                    row['rs3_h']  = (h['rs3_h']  + [row['rs3_pct']])[-5:]
                    row['rs6_h']  = (h['rs6_h']  + [row['rs6_pct']])[-5:]
                    row['vol_h']  = (h['vol_h']  + [row['volar_3m']])[-5:]
                    row['perf_h'] = (h['perf_h'] + [row['perf_3m']])[-5:]
                    # True if 3M RS percentile strictly rising across all stored scans
                    row['rs3_up'] = len(row['rs3_h']) > 1 and all(x < y for x, y in zip(row['rs3_h'], row['rs3_h'][1:]))
                    # True if 6M RS percentile strictly rising across all stored scans
                    row['rs6_up'] = len(row['rs6_h']) > 1 and all(x < y for x, y in zip(row['rs6_h'], row['rs6_h'][1:]))
                    return row

                df = df.apply(inject_history, axis=1)
                # ----------------------------

                # Sort by 3M RS ratio (strongest relative performers first)
                df = df.sort_values('rs_3m', ascending=False).reset_index(drop=True)
                df['rank'] = df.index + 1
                stocks = df.to_dict(orient='records')
                print(f"Final stocks count: {len(stocks)}")

            last_processed_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            source_name = session.get('last_filename_us', 'Unknown')

            with open(RESULTS_JSON, 'w') as f:
                json.dump({'stocks': stocks, 'time': last_processed_time, 'source': source_name}, f)

            history = session['scan_history_us']
            history.insert(0, {
                "time": last_processed_time,
                "source": source_name,
                "count": len(stocks)
            })
            session['scan_history_us'] = history[:5]
            session.modified = True

    elif os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source')

    return render_template(
        "stage2_adaptive_volar_us_scr.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
        source_name=source_name,
        history=session.get('scan_history_us', []),
        active_file=session.get('last_filename_us')
    )