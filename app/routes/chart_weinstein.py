import os
import io
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_weinstein_bp = Blueprint("chart_weinstein", __name__)

# Self-contained on purpose (same convention as chart_carousel.py / chart_combined.py
# each keeping their own copy of this config rather than importing from one another).
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

MARKET_SUFFIX = {"US": "", "NSE": ".NS"}

# Weinstein's method is explicitly a WEEKLY chart method — daily data is
# downloaded and resampled to weekly bars below, with generous extra history
# so the 30-week MA (needs 30 bars) has proper warm-up before the display window.
RANGE_OPTIONS = {
    "2Y":  {"weeks": 104, "download_period": "5y"},
    "3Y":  {"weeks": 156, "download_period": "6y"},
    "5Y":  {"weeks": 260, "download_period": "8y"},
    "10Y": {"weeks": 520, "download_period": "max"},
}

# ----------------------------------------------------------------------------
# Tunable thresholds for the stage/zone/event read below. Kept as named
# constants (not magic numbers) so the logic stays auditable/adjustable.
# ----------------------------------------------------------------------------
VOLUME_SURGE_MULTIPLE = 1.5     # Weinstein's "well above average" volume-confirmation bar
MA_SLOPE_LOOKBACK = 4            # weeks, short-term slope read on the 30-week MA
MA_SLOPE_FLAT_PCT = 0.5          # slope magnitude under this % over the lookback = "flat"
PIVOT_LEFT = 3                   # bars on each side required to confirm a swing high/low
PIVOT_RIGHT = 3
ZONE_CLUSTER_PCT = 3.0           # merge pivots within this % of each other into one zone
ZONE_LOOKBACK_WEEKS = 52         # only look at pivots from the last N weeks for "the current range"
BREAKOUT_LOOKBACK_WEEKS = 12     # how far back a breakout/breakdown still counts as "recent"
EXTENDED_ABOVE_MA_PCT = 30.0     # Weinstein's chasing-caution zone: %+ above the 30WMA


# ----------------------------------------------------------------------------
# Screener cache loading (identical pattern to chart_carousel.py)
# ----------------------------------------------------------------------------

def _load_screener_stock_entries(market, screener_key):
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
                if sections:
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


# ----------------------------------------------------------------------------
# Weekly resampling + pivot/zone/stage math
# ----------------------------------------------------------------------------

def resample_weekly(daily_df):
    """Standard Friday-ending weekly bars from daily OHLCV."""
    weekly = daily_df.resample('W-FRI').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    })
    return weekly.dropna(subset=['Close', 'Open', 'High', 'Low'])


def find_pivots(series, left, right, mode):
    """Simple swing high/low detector: a bar qualifies if it's the max/min
    within a window of `left` bars before and `right` bars after it."""
    vals = series.values
    n = len(vals)
    idxs = []
    for i in range(left, n - right):
        window = vals[i - left:i + right + 1]
        if mode == 'high' and vals[i] >= window.max():
            idxs.append(i)
        elif mode == 'low' and vals[i] <= window.min():
            idxs.append(i)
    return idxs


def cluster_zone(prices, cluster_pct):
    """Given pivot price levels (chronological order), find the tightest
    cluster with the most touches and return (low, high, touch_count) — this
    is "the" support or resistance zone, mirroring the single horizontal band
    drawn in Weinstein's own charts rather than every individual swing point."""
    if not prices:
        return None
    prices_sorted = sorted(prices)
    best_band = None
    for p in prices_sorted:
        band = [q for q in prices_sorted if p > 0 and abs(q - p) / p * 100 <= cluster_pct]
        if best_band is None or len(band) > len(best_band):
            best_band = band
    if best_band and len(best_band) >= 2:
        return (round(float(min(best_band)), 2), round(float(max(best_band)), 2), len(best_band))
    latest = prices[-1]
    half_band = latest * (cluster_pct / 200.0)
    return (round(float(latest - half_band), 2), round(float(latest + half_band), 2), 1)


def compute_weinstein_analysis(weekly_df):
    """Core read: stage classification, support/resistance zones, and any
    recent breakout/breakdown/pullback event. Everything here traces to a
    plain, inspectable rule — no black-box scoring."""
    df = weekly_df
    n = len(df)
    if n < 40:
        return None  # not enough weekly bars yet for a meaningful 30-week MA read

    close = df['Close'].values
    sma30 = df['sma30'].values
    last = n - 1

    def slope_pct(arr, i, lookback):
        j = i - lookback
        if j < 0 or np.isnan(arr[i]) or np.isnan(arr[j]) or arr[j] == 0:
            return 0.0
        return float((arr[i] - arr[j]) / arr[j] * 100)

    cur_slope = slope_pct(sma30, last, MA_SLOPE_LOOKBACK)
    prior_slope = slope_pct(sma30, max(0, last - MA_SLOPE_LOOKBACK), MA_SLOPE_LOOKBACK)

    stage = None
    if not np.isnan(sma30[last]):
        above_ma = close[last] > sma30[last]
        if cur_slope > MA_SLOPE_FLAT_PCT and above_ma:
            stage = 2   # Advancing
        elif cur_slope < -MA_SLOPE_FLAT_PCT and not above_ma:
            stage = 4   # Declining
        elif abs(cur_slope) <= MA_SLOPE_FLAT_PCT:
            # MA has gone flat — was the trend coming into this flattening up
            # or down? That distinguishes Stage 1 (basing after a decline)
            # from Stage 3 (topping after an advance).
            stage = 3 if prior_slope > 0 else 1
        else:
            stage = 2 if above_ma else 4  # transitional bar — lean on price position

    # --- Support / resistance zones from recent swing pivots ---
    lb = min(ZONE_LOOKBACK_WEEKS, n)
    recent = df.iloc[-lb:]
    high_pivot_idx = find_pivots(recent['High'], PIVOT_LEFT, PIVOT_RIGHT, 'high')
    low_pivot_idx = find_pivots(recent['Low'], PIVOT_LEFT, PIVOT_RIGHT, 'low')
    resistance_prices = [float(recent['High'].iloc[i]) for i in high_pivot_idx]
    support_prices = [float(recent['Low'].iloc[i]) for i in low_pivot_idx]

    resistance_zone = cluster_zone(resistance_prices, ZONE_CLUSTER_PCT)
    support_zone = cluster_zone(support_prices, ZONE_CLUSTER_PCT)

    # --- Breakout / breakdown / pullback read ---
    events = {
        "breakout": False, "breakout_weeks_ago": None, "breakout_volume_confirmed": False,
        "breakdown": False, "breakdown_weeks_ago": None, "breakdown_volume_confirmed": False,
        "pullback_holding": False, "pullback_failed": False,
        "extended_above_ma_pct": None,
    }

    lookback_n = min(BREAKOUT_LOOKBACK_WEEKS, n - 1)

    if resistance_zone:
        r_hi = resistance_zone[1]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] > r_hi and close[i - 1] <= r_hi:
                events["breakout"] = True
                events["breakout_weeks_ago"] = w - 1
                vol_i = float(df['Volume'].iloc[i])
                avgvol_i = df['avg_vol10'].iloc[i - 1]
                events["breakout_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break
        if events["breakout"]:
            if close[last] >= r_hi:
                pass  # still at/above the breakout level, no pullback yet
            elif close[last] >= r_hi * (1 - ZONE_CLUSTER_PCT / 100):
                events["pullback_holding"] = True   # classic Weinstein "buy the pullback" zone
            else:
                events["pullback_failed"] = True     # gave the breakout back — treat with caution

    if support_zone:
        s_lo = support_zone[0]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] < s_lo and close[i - 1] >= s_lo:
                events["breakdown"] = True
                events["breakdown_weeks_ago"] = w - 1
                vol_i = float(df['Volume'].iloc[i])
                avgvol_i = df['avg_vol10'].iloc[i - 1]
                events["breakdown_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break

    if not np.isnan(sma30[last]) and sma30[last] > 0:
        events["extended_above_ma_pct"] = round(float((close[last] / sma30[last] - 1) * 100), 2)

    return {
        "stage": stage,
        "ma_slope_pct": round(cur_slope, 3),
        "resistance_zone": resistance_zone,
        "support_zone": support_zone,
        "events": events,
    }


STAGE_LABELS = {
    1: "Stage 1 — Basing (Accumulation)",
    2: "Stage 2 — Advancing (Markup)",
    3: "Stage 3 — Topping (Distribution)",
    4: "Stage 4 — Declining (Markdown)",
    None: "Unclassified — not enough history yet",
}


@chart_weinstein_bp.route("/chart-weinstein-stage-analysis")
def weinstein_dashboard():
    default_market = request.args.get("market", "NSE").strip().upper()
    if default_market not in MARKET_SUFFIX: default_market = "NSE"
    default_stock = request.args.get("symbol", "PRAJIND" if default_market == "NSE" else "NVDA")
    return render_template(
        "chart_weinstein.html",
        default_stock=default_stock,
        default_market=default_market
    )


@chart_weinstein_bp.route("/api/v1/weinstein-ticker-payload")
def get_weinstein_ticker_payload():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)

    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)

    return jsonify({"status": "success", "market": market, "screener": screener_key, "tickers": tickers})


@chart_weinstein_bp.route("/api/v1/weinstein-telemetry-data/<symbol>")
def get_weinstein_telemetry_data(symbol):
    try:
        market = request.args.get("market", "NSE").strip().upper()
        if market not in MARKET_SUFFIX: market = "NSE"
        suffix = MARKET_SUFFIX[market]

        range_key = request.args.get("range", "5Y").strip().upper()
        if range_key not in RANGE_OPTIONS: range_key = "5Y"
        range_cfg = RANGE_OPTIONS[range_key]

        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}{suffix}"

        data = yf.download(
            fetch_symbol, period=range_cfg["download_period"], interval="1d",
            auto_adjust=True, progress=False
        )

        if data.empty:
            return jsonify({"status": "error", "message": f"No data returned for '{symbol_clean}'."}), 400

        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data = data.dropna(subset=["Close", "Open", "High", "Low"])
        if len(data) < 250:
            return jsonify({
                "status": "error",
                "message": "Insufficient daily history to build a reliable weekly Stage Analysis chart (need ~1yr+)."
            }), 400

        weekly = resample_weekly(data)
        weekly['sma30'] = weekly['Close'].rolling(30).mean()
        weekly['avg_vol10'] = weekly['Volume'].rolling(10).mean()

        analysis = compute_weinstein_analysis(weekly)
        if analysis is None:
            return jsonify({
                "status": "error",
                "message": "Not enough weekly bars yet for a 30-week MA read (need 40+ weeks of history)."
            }), 400

        weeks = range_cfg["weeks"]
        display = weekly.iloc[-weeks:] if len(weekly) > weeks else weekly
        # Index of the first displayed bar within the *full* weekly series —
        # needed to translate event "weeks ago" positions into marker dates below.
        display_start_pos = len(weekly) - len(display)

        candles, sma30_series, volume_series = [], [], []
        for idx, row in display.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            candles.append({
                "time": date_str, "open": round(float(row['Open']), 2), "high": round(float(row['High']), 2),
                "low": round(float(row['Low']), 2), "close": round(float(row['Close']), 2),
                "volume": int(row['Volume'])
            })
            if not pd.isna(row['sma30']):
                sma30_series.append({"time": date_str, "value": round(float(row['sma30']), 2)})

            surge = bool(not pd.isna(row['avg_vol10']) and row['Volume'] > row['avg_vol10'] * VOLUME_SURGE_MULTIPLE)
            up_week = row['Close'] >= row['Open']
            color = 'rgba(245,158,11,0.85)' if surge else ('rgba(34,197,94,0.5)' if up_week else 'rgba(239,68,68,0.5)')
            volume_series.append({"time": date_str, "value": int(row['Volume']), "color": color, "surge": surge})

        markers = []
        ev = analysis['events']
        full_n = len(weekly)
        if ev['breakout'] and ev['breakout_weeks_ago'] is not None:
            i = full_n - 1 - ev['breakout_weeks_ago']
            if display_start_pos <= i < full_n:
                t = weekly.index[i].strftime("%Y-%m-%d")
                markers.append({
                    "time": t, "position": "belowBar", "color": "#22c55e", "shape": "arrowUp",
                    "text": "Breakout ✓Vol" if ev['breakout_volume_confirmed'] else "Breakout"
                })
        if ev['breakdown'] and ev['breakdown_weeks_ago'] is not None:
            i = full_n - 1 - ev['breakdown_weeks_ago']
            if display_start_pos <= i < full_n:
                t = weekly.index[i].strftime("%Y-%m-%d")
                markers.append({
                    "time": t, "position": "aboveBar", "color": "#ef4444", "shape": "arrowDown",
                    "text": "Breakdown ✓Vol" if ev['breakdown_volume_confirmed'] else "Breakdown"
                })

        return jsonify({
            "status": "success", "symbol": symbol_clean, "market": market, "range": range_key,
            "stage": analysis['stage'], "stage_label": STAGE_LABELS.get(analysis['stage']),
            "ma_slope_pct": analysis['ma_slope_pct'],
            "resistance_zone": analysis['resistance_zone'],
            "support_zone": analysis['support_zone'],
            "events": analysis['events'],
            "series": {"candles": candles, "sma30": sma30_series, "volume": volume_series, "markers": markers}
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ----------------------------------------------------------------------------
# Custom symbol-list upload — identical pattern/behavior to the carousel's
# upload endpoint, kept local rather than imported so this blueprint has no
# hard dependency on chart_carousel.py.
# ----------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


@chart_weinstein_bp.route("/api/v1/weinstein-upload-symbols", methods=["POST"])
def upload_weinstein_symbols():
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
        else:
            df = pd.read_excel(buffer)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Couldn't parse '{filename}': {e}"}), 400

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

    return jsonify({"status": "success", "filename": filename, "count": len(tickers), "tickers": tickers})