import os
import io
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_carousel_bp = Blueprint("chart_carousel", __name__)

# Complete system absolute path configuration schema mapped cleanly to your screeners
UPLOAD_ROOT = os.path.abspath(os.path.join(os.getcwd(), 'uploads'))

SCREENER_CONFIG = {
    "NSE": {
        "HH_HL_Screener": os.path.join(UPLOAD_ROOT, 'india_hhhl', 'last_india_hhhl_results.json'),
        "Adaptive_RS": os.path.join(UPLOAD_ROOT, 'volar_ind_adaptive', 'volar_results_ind_adaptive.json'),
        "RS_ROC_Momentum": os.path.join(UPLOAD_ROOT, 'rs_roc', 'last_rs_roc_results.json'),
        "Gap_Volume": os.path.join(UPLOAD_ROOT, 'gap_volume_india', 'last_gap_vol_india_results.json')
    },
    "US": {
        "Stage2_Screener": os.path.join(UPLOAD_ROOT, 'volar_us', 'last_volar_us_results.json'),
        "Adaptive_RS": os.path.join(UPLOAD_ROOT, 'volar_us_adaptive', 'volar_results_adaptive.json'),
        "Gap_Volume": os.path.join(UPLOAD_ROOT, 'gap_volume', 'last_gap_vol_results.json')
    }
}

MARKET_BENCHMARKS = {
    "US":  {"benchmark": "^GSPC", "suffix": ""},
    "NSE": {"benchmark": "^CRSLDX", "suffix": ".NS"}
}

RANGE_OPTIONS = {
    "3M": {"months": 3,  "download_period": "2y"},
    "6M": {"months": 6,  "download_period": "2y"},
    "1Y": {"months": 12, "download_period": "2y"},
    "2Y": {"months": 24, "download_period": "3y"},
}

# ----------------------------------------------------------------------------
# Shared math helpers
# ----------------------------------------------------------------------------

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_sma(series, window):
    return series.rolling(window=window).mean()

def calculate_slope(series, window=5):
    y = series.tail(window).values
    x = np.arange(len(y))
    if len(y) < window:
        return 0.0
    slope, _ = np.polyfit(x, y, 1)
    return slope


# ----------------------------------------------------------------------------
# Screener cache loading — shared by the ticker-list endpoint, the RS
# percentile lookup, and the early-risers scanner, so all three always agree
# on what "the current screener's list" actually contains.
# ----------------------------------------------------------------------------

def _load_screener_stock_entries(market, screener_key):
    """Returns the raw stock dict entries (whatever keys the screener produced,
    e.g. symbol / rs_percentile / score) for a given market+screener cache."""
    if market not in SCREENER_CONFIG or screener_key not in SCREENER_CONFIG[market]:
        return []
    target_path = SCREENER_CONFIG[market][screener_key]
    entries = []
    if os.path.exists(target_path):
        try:
            with open(target_path, 'r') as f:
                cached_data = json.load(f)
            if isinstance(cached_data, dict):
                sections = cached_data.get('sections', {})
                if sections:  # Structural layout handling for Gap & Volume layouts
                    for sec_list in sections.values():
                        entries.extend([s for s in sec_list if 'symbol' in s])
                else:
                    entries = [s for s in cached_data.get('stocks', []) if 'symbol' in s]
            elif isinstance(cached_data, list):
                entries = [s for s in cached_data if 'symbol' in s]
        except Exception as e:
            print(f"Failed to load screener entries for {market}/{screener_key}: {e}")
    return entries


def _dedupe_tickers(entries):
    seen = set()
    out = []
    for e in entries:
        sym = e['symbol'].strip().upper()
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def _resolve_screener_key(market, screener_key):
    if market not in SCREENER_CONFIG:
        market = "NSE"
    if not screener_key or screener_key not in SCREENER_CONFIG[market]:
        screener_key = list(SCREENER_CONFIG[market].keys())[0]
    return market, screener_key


def _lookup_rs_percentile(market, screener_key, symbol):
    entries = _load_screener_stock_entries(market, screener_key)
    for e in entries:
        if e.get('symbol', '').strip().upper() == symbol:
            for key in ('rs_percentile', 'rs_pct', 'percentile'):
                if key in e:
                    try:
                        return int(e[key])
                    except (TypeError, ValueError):
                        pass
    return None


# ----------------------------------------------------------------------------
# Divergence phase — 4-state version (brought in line with the combined chart)
# instead of the old 2-state True-Alpha/Flushing-only version, so "outperform"
# and "relative underperformance" phases are distinguishable too.
# ----------------------------------------------------------------------------

def assign_divergence_strength(row):
    rs_m, sp_m = row['rs_slope'], row['bench_slope']
    if rs_m > 0 and sp_m < 0:
        return 2.0    # True Alpha Divergence
    elif rs_m > 0 and sp_m >= 0 and rs_m > sp_m:
        return 1.0    # Outperformance
    elif rs_m <= 0 and sp_m > 0:
        return -1.0   # Relative Underperformance
    elif rs_m < 0 and sp_m <= 0:
        return -2.0   # Flushing Phase
    return 0.0


DIV_COLOR_MAP = {
    2.0:  'rgba(59,130,246,0.16)',
    1.0:  'rgba(96,165,250,0.10)',
    -1.0: 'rgba(248,113,113,0.10)',
    -2.0: 'rgba(185,28,28,0.16)',
    0.0:  'transparent'
}


@chart_carousel_bp.route("/chart-fast-carousel")
def carousel_dashboard():
    default_market = request.args.get("market", "NSE").strip().upper()
    if default_market not in MARKET_BENCHMARKS: default_market = "NSE"
    default_stock = request.args.get("symbol", "PRAJIND" if default_market == "NSE" else "NVDA")
    return render_template(
        "chart_carousel.html",
        default_stock=default_stock,
        default_market=default_market
    )


@chart_carousel_bp.route("/api/v1/carousel-ticker-payload")
def get_carousel_ticker_payload():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)

    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)

    return jsonify({"status": "success", "market": market, "screener": screener_key, "tickers": tickers})


@chart_carousel_bp.route("/api/v1/carousel-telemetry-data/<symbol>")
def get_carousel_telemetry_data(symbol):
    try:
        market = request.args.get("market", "NSE").strip().upper()
        if market not in MARKET_BENCHMARKS: market = "NSE"
        cfg = MARKET_BENCHMARKS[market]

        range_key = request.args.get("range", "1Y").strip().upper()
        if range_key not in RANGE_OPTIONS: range_key = "1Y"
        range_cfg = RANGE_OPTIONS[range_key]

        # Optional — used only to look up the RS percentile from whichever
        # screener cache is currently active in the UI, exactly like the
        # combined chart does. Doesn't affect the price data fetched below.
        screener_key = request.args.get("screener", "").strip()
        _, resolved_screener = _resolve_screener_key(market, screener_key)

        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}{cfg['suffix']}" if market == "NSE" else symbol_clean

        data = yf.download(
            [fetch_symbol, cfg["benchmark"]],
            period=range_cfg["download_period"],
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": "No data returned from yfinance."}), 400

        if isinstance(data.columns, pd.MultiIndex):
            if data.columns.names[0] != 'Price':
                try: data.columns = data.columns.swaplevel(0, 1)
                except: pass
            data.columns.names = ['Price', 'Ticker']

        if 'Close' not in data or fetch_symbol not in data['Close'].columns:
            return jsonify({"status": "error", "message": f"Invalid {market} ticker '{symbol_clean}'."}), 400

        combined = pd.DataFrame({
            "open":   data['Open'][fetch_symbol],
            "high":   data['High'][fetch_symbol],
            "low":    data['Low'][fetch_symbol],
            "stock":  data['Close'][fetch_symbol],
            "bench":  data['Close'][cfg["benchmark"]],
            "volume": data['Volume'][fetch_symbol] if 'Volume' in data else pd.Series(dtype=float)
        }).dropna(subset=["stock", "open", "high", "low"])

        if len(combined) < 60:
            return jsonify({"status": "error", "message": "Insufficient data to compile indicators."}), 400

        combined["bench"] = combined["bench"].ffill().bfill()
        combined["volume"] = combined["volume"].fillna(0)
        combined = combined.sort_index()
        combined = combined[~combined.index.duplicated(keep='first')]

        combined['ema10']  = calculate_ema(combined['stock'], 10)
        combined['ema20']  = calculate_ema(combined['stock'], 20)
        combined['ema50']  = calculate_ema(combined['stock'], 50)
        combined['ema100'] = calculate_ema(combined['stock'], 100)
        combined['ema200'] = calculate_ema(combined['stock'], 200)

        combined['rs_ratio'] = combined['stock'] / combined['bench']
        combined['rs_sma10'] = calculate_sma(combined['rs_ratio'], 10)
        combined['rs_ema21'] = calculate_ema(combined['rs_ratio'], 21)
        combined['rs_sma50'] = calculate_sma(combined['rs_ratio'], 50)

        # Parse On-Balance Volume Style Accumulation/Distribution Data Stream Vector
        combined['price_chg'] = combined['stock'].diff()
        combined['acc_vol'] = combined.apply(
            lambda r: r['volume'] if r['price_chg'] > 0
                      else (-r['volume'] if r['price_chg'] < 0 else 0),
            axis=1
        )
        combined['acc_line'] = combined['acc_vol'].cumsum()

        combined['rs_slope']    = combined['rs_ratio'].rolling(window=5).apply(calculate_slope)
        combined['bench_slope'] = combined['bench'].rolling(window=5).apply(calculate_slope)
        combined['div_strength'] = combined.apply(assign_divergence_strength, axis=1)

        # RS uptrend marker (4 consecutive rising RS days)
        combined['rs_inc'] = combined['rs_ratio'].gt(combined['rs_ratio'].shift(1))
        combined['rs_up_flag'] = (
            combined['rs_inc'].rolling(window=4).sum()
            .apply(lambda x: 1 if x == 4 else 0)
            .fillna(0)
        )

        rs_percentile = _lookup_rs_percentile(market, resolved_screener, symbol_clean)
        if rs_percentile is None:
            rs_percentile = 50

        range_start = combined.index[-1] - pd.DateOffset(months=range_cfg["months"])
        display = combined[combined.index >= range_start]

        series_data = {
            "candles": [], "ema10": [], "ema20": [], "ema50": [], "ema100": [], "ema200": [],
            "rs_ratio": [], "rs_sma10": [], "rs_ema21": [], "rs_sma50": [],
            "div_hist": [], "bench_line": [], "acc_line": [], "rs_up_markers": []
        }

        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            series_data["candles"].append({
                "time": date_str, "open": round(float(row['open']), 2), "high": round(float(row['high']), 2),
                "low": round(float(row['low']), 2), "close": round(float(row['stock']), 2),
                "volume": int(row['volume']), "rs_pct": int(rs_percentile)
            })
            for key in ["ema10", "ema20", "ema50", "ema100", "ema200"]:
                series_data[key].append({"time": date_str, "value": round(float(row[key]), 2)})

            series_data["rs_ratio"].append({"time": date_str, "value": round(float(row['rs_ratio']), 6)})
            series_data["rs_sma10"].append({"time": date_str, "value": round(float(row['rs_sma10']), 6)})
            series_data["rs_ema21"].append({"time": date_str, "value": round(float(row['rs_ema21']), 6)})
            series_data["rs_sma50"].append({"time": date_str, "value": round(float(row['rs_sma50']), 6)})
            series_data["bench_line"].append({"time": date_str, "value": round(float(row['bench']), 2)})
            series_data["acc_line"].append({"time": date_str, "value": round(float(row['acc_line']), 0)})

            v_strength = row['div_strength']
            series_data["div_hist"].append({
                "time": date_str, "value": float(v_strength),
                "color": DIV_COLOR_MAP.get(v_strength, 'transparent')
            })

            if int(row['rs_up_flag']) == 1:
                series_data["rs_up_markers"].append({"time": date_str, "price": round(float(row['low']), 2)})

        return jsonify({
            "status": "success", "symbol": symbol_clean, "market": market, "range": range_key,
            "screener": resolved_screener, "rs_percentile": rs_percentile, "series": series_data
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ----------------------------------------------------------------------------
# Early-risers scanner — "stocks that just started rising".
#
# One batch yfinance call across the entire active screener list (cheap: a
# single request, not N), then a purely mechanical, transparent test per
# ticker:
#   1. fresh_cross   — price closed above EMA20 today, but was below EMA20
#                       at some point 4-10 sessions ago (a *recent* cross,
#                       not something that happened months back)
#   2. short_turn_up  — EMA10 > EMA20 (short-term trend has actually turned)
#   3. not_extended   — price is still within 10% of EMA50 (hasn't already
#                       run away — this is what separates "just starting"
#                       from "already extended")
#   4. volume_confirm — today's volume is above its 20-day average
#
# A ticker qualifies if fresh_cross is true AND at least 2 of the other 3
# also hold. Results are sorted by score (desc) then by how close to EMA20
# they are (tightest/freshest first).
# ----------------------------------------------------------------------------

@chart_carousel_bp.route("/api/v1/carousel-early-risers")
def get_carousel_early_risers():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)

    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)

    if not tickers:
        return jsonify({"status": "success", "market": market, "screener": screener_key, "count": 0, "risers": []})

    cfg = MARKET_BENCHMARKS[market]
    suffix = cfg["suffix"]
    fetch_list = [f"{t}{suffix}" for t in tickers] if market == "NSE" else list(tickers)

    try:
        raw = yf.download(
            fetch_list, period="6mo", interval="1d",
            auto_adjust=True, progress=False, group_by='ticker', threads=True
        )
    except Exception as e:
        return jsonify({"status": "error", "message": f"Batch download failed: {e}"}), 500

    risers = []
    single_ticker_mode = len(fetch_list) == 1

    for orig_sym, fsym in zip(tickers, fetch_list):
        try:
            if single_ticker_mode:
                df = raw
            elif isinstance(raw.columns, pd.MultiIndex):
                if fsym not in raw.columns.get_level_values(0):
                    continue
                df = raw[fsym]
            else:
                continue

            df = df.dropna(subset=["Close"])
            if len(df) < 55:
                continue

            close = df["Close"]
            vol = df["Volume"] if "Volume" in df.columns else pd.Series(dtype=float)

            ema10 = calculate_ema(close, 10)
            ema20 = calculate_ema(close, 20)
            ema50 = calculate_ema(close, 50)

            last_close = float(close.iloc[-1])
            last_ema10 = float(ema10.iloc[-1])
            last_ema20 = float(ema20.iloc[-1])
            last_ema50 = float(ema50.iloc[-1])

            if last_ema20 <= 0:
                continue

            cross_up_now = last_close > last_ema20
            lookback_close = close.iloc[-10:-3]
            lookback_ema20 = ema20.iloc[-10:-3]
            was_below_recently = bool((lookback_close < lookback_ema20).any()) if len(lookback_close) else False
            fresh_cross = bool(cross_up_now and was_below_recently)

            if not fresh_cross:
                continue

            short_turn_up = bool(last_ema10 > last_ema20)
            not_extended = bool(last_close < last_ema50 * 1.10) if last_ema50 > 0 else False

            vol_confirm = False
            if len(vol) >= 20 and vol.iloc[-1] > 0:
                avg_vol20 = float(vol.iloc[-20:].mean())
                vol_confirm = bool(float(vol.iloc[-1]) > avg_vol20)

            score = sum([fresh_cross, short_turn_up, not_extended, vol_confirm])
            if score < 3:
                continue

            pct_above_ema20 = round(((last_close / last_ema20) - 1) * 100, 2)

            reasons = ["Fresh cross above EMA20 (within the last ~week)"]
            if short_turn_up: reasons.append("EMA10 has turned back above EMA20")
            if not_extended: reasons.append("Still within 10% of EMA50 — not extended yet")
            if vol_confirm: reasons.append("Today's volume is above its 20-day average")

            risers.append({
                "symbol": orig_sym,
                "score": score,
                "last_close": round(last_close, 2),
                "pct_above_ema20": pct_above_ema20,
                "fresh_cross_above_ema20": fresh_cross,
                "ema10_over_ema20": short_turn_up,
                "not_extended": not_extended,
                "volume_confirmed": vol_confirm,
                "reasons": reasons
            })
        except Exception:
            continue

    risers.sort(key=lambda r: (-r["score"], r["pct_above_ema20"]))

    return jsonify({
        "status": "success", "market": market, "screener": screener_key,
        "count": len(risers), "scanned": len(tickers), "risers": risers
    })


# ----------------------------------------------------------------------------
# Custom symbol-list upload — lets the user browse an arbitrary CSV/Excel
# file of tickers (must contain a "Symbol" column, case-insensitive) through
# the same carousel prev/next controls, independent of the built-in
# screener caches. Parsed entirely in memory — nothing is written to disk.
# ----------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


@chart_carousel_bp.route("/api/v1/carousel-upload-symbols", methods=["POST"])
def upload_carousel_symbols():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "No file was included in the upload request."}), 400

    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "No file selected."}), 400

    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return jsonify({
            "status": "error",
            "message": f"Unsupported file type '{ext or 'unknown'}'. Please upload a .csv, .xlsx, or .xls file."
        }), 400

    try:
        raw_bytes = file.read()
        buffer = io.BytesIO(raw_bytes)
        if ext == '.csv':
            try:
                df = pd.read_csv(buffer, encoding='utf-8-sig')
            except UnicodeDecodeError:
                buffer.seek(0)
                df = pd.read_csv(buffer)
        elif ext == '.xlsx':
            df = pd.read_excel(buffer, engine='openpyxl')
        else:  # .xls
            df = pd.read_excel(buffer)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Couldn't parse '{filename}': {e}"}), 400

    # Defensive: strip BOM char / stray whitespace some spreadsheet exports
    # leave on the header row so the 'Symbol' match below doesn't silently miss.
    df.columns = [str(c).replace('\ufeff', '').strip() for c in df.columns]

    symbol_col = None
    for col in df.columns:
        if str(col).strip().lower() == 'symbol':
            symbol_col = col
            break

    if symbol_col is None:
        return jsonify({
            "status": "error",
            "message": "No 'Symbol' column found in the uploaded file. Make sure the header row has a column named exactly 'Symbol'."
        }), 400

    raw_symbols = df[symbol_col].dropna().astype(str).tolist()
    tickers = []
    seen = set()
    for s in raw_symbols:
        sym = s.strip().upper().replace('.NS', '')
        if sym and sym.upper() not in ('NAN', 'NONE', '') and sym not in seen:
            seen.add(sym)
            tickers.append(sym)

    if not tickers:
        return jsonify({"status": "error", "message": "The 'Symbol' column didn't contain any usable ticker values."}), 400

    return jsonify({
        "status": "success", "filename": filename, "count": len(tickers), "tickers": tickers
    })