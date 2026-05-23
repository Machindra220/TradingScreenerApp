import os
import json
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, current_app

volar_ind_adaptive_bp = Blueprint('volar_ind_adaptive_bp', __name__)

UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads', 'volar_ind_adaptive')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'volar_results_ind_adaptive.json')


def compute_volar(prices):
    if len(prices) < 2:
        return 0
    returns = prices.pct_change().dropna()
    std = returns.std()
    total_ret = (prices.iloc[-1] / prices.iloc[0]) - 1
    return round(total_ret / std, 2) if std != 0 else 0


def is_volar_adaptive_ind(symbol, lookback):
    try:
        ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
        print(f"Processing symbol: {symbol} → {ticker_symbol}")

        ticker = yf.Ticker(ticker_symbol)
        index_ticker = yf.Ticker("^NSEI")

        # FIX: yfinance does NOT support arbitrary "Nd" period strings like "110d".
        # Use start= date instead to reliably fetch enough history.
        fetch_days = lookback + 100  # extra buffer for weekends/holidays
        start_date = (datetime.today() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")

        df = ticker.history(start=start_date)
        idx_df = index_ticker.history(start=start_date)

        print(f"Data length for {ticker_symbol}: {len(df)}")
        print(f"Index data length: {len(idx_df)}")

        if len(df) < lookback or len(idx_df) < lookback:
            print(f"Not enough data for {ticker_symbol}: got {len(df)} rows, need {lookback}")
            return None

        close = df['Close']
        curr = close.iloc[-1]
        start_price = close.iloc[-lookback]
        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]

        perf = (curr / start_price) - 1
        idx_perf = (idx_df['Close'].iloc[-1] / idx_df['Close'].iloc[-lookback]) - 1

        rs_ratio = perf / idx_perf if idx_perf != 0 else 0
        volar_val = compute_volar(close.iloc[-lookback:])

        if curr > ema200:
            return {
                "symbol": symbol.replace(".NS", ""),
                "price": round(curr, 2),
                "volar": volar_val,
                "relative_strength": round(rs_ratio, 4),
                "performance": round(perf * 100, 2)
            }
        else:
            print(f"{ticker_symbol} below EMA200 (curr={curr:.2f}, ema200={ema200:.2f}), skipping.")
            return None

    except Exception as e:
        # FIX: Log the actual error instead of silently swallowing it
        print(f"ERROR processing {symbol}: {e}")
        return None


@volar_ind_adaptive_bp.route("/volar-ind-adaptive", methods=["GET", "POST"])
def volar_ind_process():
    stocks = []
    last_processed_time = None
    source_name = "None"
    last_lookback = session.get('last_lookback_ind', 55)

    if 'scan_history_ind' not in session:
        session['scan_history_ind'] = []

    if request.method == "POST":
        lookback = int(request.form.get('lookback', 55))
        session['last_lookback_ind'] = lookback
        last_lookback = lookback
        file = request.files.get('file')

        if file and file.filename != '':
            filepath = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(filepath)
            session['last_uploaded_csv_ind'] = filepath
            session['last_filename_ind'] = file.filename

        saved_path = session.get('last_uploaded_csv_ind')

        if saved_path and os.path.exists(saved_path):
            df_input = pd.read_csv(saved_path)
            symbols = df_input['symbol'].dropna().unique().tolist()
            print(f"Symbols loaded from CSV: {symbols}")

            raw_results = []
            for sym in symbols:
                res = is_volar_adaptive_ind(sym, lookback)
                print(f"Result for {sym}: {res}")
                if res:
                    raw_results.append(res)

            if raw_results:
                df = pd.DataFrame(raw_results)
                df['rs_percentile'] = df['relative_strength'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

                # --- TREND TRACKING LOGIC ---
                existing_history = {}
                if os.path.exists(RESULTS_JSON):
                    with open(RESULTS_JSON, 'r') as f:
                        old_cache = json.load(f).get('stocks', [])
                        existing_history = {
                            s['symbol']: {
                                'rs_h': s.get('rs_h', []),
                                'vol_h': s.get('vol_h', []),
                                'perf_h': s.get('perf_h', [])
                            } for s in old_cache
                        }

                def inject_history(row):
                    sym = row['symbol']
                    h = existing_history.get(sym, {'rs_h': [], 'vol_h': [], 'perf_h': []})
                    row['rs_h'] = (h['rs_h'] + [row['rs_percentile']])[-5:]
                    row['vol_h'] = (h['vol_h'] + [row['volar']])[-5:]
                    row['perf_h'] = (h['perf_h'] + [row['performance']])[-5:]
                    row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
                    return row

                df = df.apply(inject_history, axis=1)
                # ----------------------------

                df = df.sort_values('relative_strength', ascending=False).reset_index(drop=True)
                df['rank'] = df.index + 1
                stocks = df.to_dict(orient='records')
                print(f"Final stocks count: {len(stocks)}")

            last_processed_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            source_name = f"{session.get('last_filename_ind')} ({lookback}d)"

            with open(RESULTS_JSON, 'w') as f:
                json.dump({'stocks': stocks, 'time': last_processed_time, 'source': source_name}, f)

            history = session['scan_history_ind']
            history.insert(0, {
                "time": last_processed_time,
                "source": session.get('last_filename_ind'),
                "lookback": f"{lookback}d",
                "count": len(stocks)
            })
            session['scan_history_ind'] = history[:5]
            session.modified = True

    elif os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source')

    return render_template(
        "stage2_adaptive_volar_ind_scr.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
        source_name=source_name,
        history=session.get('scan_history_ind', []),
        active_file=session.get('last_filename_ind'),
        last_lookback=last_lookback
    )