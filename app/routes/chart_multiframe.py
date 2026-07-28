import os
import io
import json
import numpy as np
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, jsonify, request

chart_multiframe_bp = Blueprint("chart_multiframe", __name__)

# Self-contained on purpose — same convention as chart_carousel.py / chart_weinstein.py.
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
MARKET_BENCHMARK = {"US": "^GSPC", "NSE": "^CRSLDX"}

# ----------------------------------------------------------------------------
# Timeframe config — everything the Weinstein page hardcoded to "30-week" is
# generalized here into per-timeframe settings, so the exact same analysis
# functions run against hourly, daily, or weekly bars.
#
# IMPORTANT DATA-SOURCING NOTE: hourly bars are NOT derived from daily data —
# that's not how resampling works (you can't create finer granularity from
# coarser data). Daily and Weekly both come from ONE daily yf.download() call
# (weekly via resampling that daily data). Hourly requires its OWN separate
# intraday download (interval='60m'), which Yahoo/yfinance caps at roughly
# the last 730 days regardless of how much daily history exists — so the
# Hourly panel will always show a shorter history window than Daily/Weekly.
# This is a real data-availability constraint, not a design choice.
# ----------------------------------------------------------------------------
TIMEFRAME_CONFIG = {
    "H": {
        "label": "Hourly", "source": "intraday", "resample_rule": None,
        "ma_period": 30, "avg_vol_window": 10, "zone_lookback": 52, "breakout_lookback": 12,
        "rs_new_high_lookback": 52, "rs_new_high_recent": 8, "rs_sma": 10,
        "ma_slope_lookback": 12, "download_period": "730d", "display_bars": 400,
        "min_bars": 34,
    },
    "D": {
        "label": "Daily", "source": "daily", "resample_rule": None,
        "ma_period": 30, "avg_vol_window": 10, "zone_lookback": 52, "breakout_lookback": 12,
        "rs_new_high_lookback": 52, "rs_new_high_recent": 8, "rs_sma": 10,
        "ma_slope_lookback": 8, "download_period": "5y", "display_bars": 400,
        "min_bars": 34,
    },
    "W": {
        "label": "Weekly", "source": "daily", "resample_rule": "W-FRI",
        "ma_period": 30, "avg_vol_window": 10, "zone_lookback": 52, "breakout_lookback": 12,
        "rs_new_high_lookback": 52, "rs_new_high_recent": 8, "rs_sma": 10,
        "ma_slope_lookback": 4, "download_period": "8y", "display_bars": 400,
        "min_bars": 34,
    },
}

MA_SLOPE_FLAT_PCT = 0.5
PIVOT_LEFT = 3
PIVOT_RIGHT = 3
ZONE_CLUSTER_PCT = 3.0
VOLUME_SURGE_MULTIPLE = 1.5  # "volume exceeding 1.5x the 10-period average" per spec

STAGE_LABELS = {
    1: "Stage 1 — Basing", 2: "Stage 2 — Advancing", 3: "Stage 3 — Topping",
    4: "Stage 4 — Declining", None: "Unclassified",
}


# ----------------------------------------------------------------------------
# Screener cache loading (identical pattern to chart_carousel.py / chart_weinstein.py)
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
# Generalized pivot/zone/stage/RS math — same logic proven on the Weinstein
# page, parameterized instead of hardcoded to "30-week" so it works for any
# timeframe's bar count.
# ----------------------------------------------------------------------------

def calculate_ema(series, span):
    """Exponential MA — same convention as chart_carousel.py's calculate_ema,
    reused here for the 10/100-period overlay lines so both charts read
    the same way across the app."""
    return series.ewm(span=span, adjust=False).mean()


def find_pivots(series, left, right, mode):
    """Swing high/low detector. Cast to float explicitly: thinly-traded
    tickers occasionally leave an object-dtype column with a stray Python
    None in it, and comparing None to a number with '>' raises TypeError
    instead of being treated as missing data."""
    vals = np.asarray(series, dtype=float)
    n = len(vals)
    idxs = []
    for i in range(left, n - right):
        window = vals[i - left:i + right + 1]
        if np.isnan(window).any():
            continue
        if mode == 'high' and vals[i] >= window.max():
            idxs.append(i)
        elif mode == 'low' and vals[i] <= window.min():
            idxs.append(i)
    return idxs


def cluster_zone(prices, cluster_pct):
    """Tightest cluster of pivot prices -> single (low, high, touches) band."""
    if not prices:
        return None
    prices_sorted = sorted(prices)
    best_band = None
    for p in prices_sorted:
        band = [q for q in prices_sorted if p > 0 and abs(q - p) / p * 100 <= cluster_pct]
        if best_band is None or len(band) > len(best_band):
            best_band = band
    if best_band and len(best_band) >= 2:
        return (round(float(min(best_band)), 4), round(float(max(best_band)), 4), len(best_band))
    latest = prices[-1]
    half_band = latest * (cluster_pct / 200.0)
    return (round(float(latest - half_band), 4), round(float(latest + half_band), 4), 1)


def slope_pct(arr, i, lookback):
    """Linear-regression slope over the last `lookback` points, expressed as
    total % change across the window relative to its starting level.

    A naive two-point delta (just comparing arr[i] to arr[i-lookback]) is
    fine on weekly bars where a single point already represents a full
    week's smoothing, but it's noisy on daily/hourly data — a genuine
    uptrend can randomly read as "flat" if the specific bars at each end of
    a short lookback happen to be below-average momentum. Fitting a line
    through every point in the window (least squares) uses all the
    intermediate data instead of just two samples, which is what makes this
    robust enough to generalize across timeframes.
    """
    j = i - lookback + 1
    if j < 0:
        return 0.0
    window = arr[j:i + 1]
    if len(window) < 2 or np.isnan(window).any() or window[0] == 0:
        return 0.0
    x = np.arange(len(window))
    try:
        slope, _ = np.polyfit(x, window, 1)
    except Exception:
        return 0.0
    return float(slope * (len(window) - 1) / window[0] * 100)


def compute_stage_analysis(df, ma_col, cfg):
    """Stage classification + S/R zones + breakout/breakdown/pullback events,
    generalized to any timeframe via cfg (ma_period, zone_lookback,
    breakout_lookback, ma_slope_lookback all come from TIMEFRAME_CONFIG)."""
    n = len(df)
    if n < cfg["min_bars"]:
        return None

    close = np.asarray(df['Close'].values, dtype=float)
    ma = np.asarray(df[ma_col].values, dtype=float)
    last = n - 1
    lookback = cfg["ma_slope_lookback"]

    cur_slope = slope_pct(ma, last, lookback)
    prior_slope = slope_pct(ma, max(0, last - lookback), lookback)

    stage = None
    if not np.isnan(ma[last]):
        above_ma = close[last] > ma[last]
        if cur_slope > MA_SLOPE_FLAT_PCT and above_ma:
            stage = 2
        elif cur_slope < -MA_SLOPE_FLAT_PCT and not above_ma:
            stage = 4
        elif abs(cur_slope) <= MA_SLOPE_FLAT_PCT:
            stage = 3 if prior_slope > 0 else 1
        else:
            stage = 2 if above_ma else 4

    lb_zone = min(cfg["zone_lookback"], n)
    recent = df.iloc[-lb_zone:]
    high_pivot_idx = find_pivots(recent['High'], PIVOT_LEFT, PIVOT_RIGHT, 'high')
    low_pivot_idx = find_pivots(recent['Low'], PIVOT_LEFT, PIVOT_RIGHT, 'low')
    resistance_prices = [float(recent['High'].iloc[i]) for i in high_pivot_idx]
    support_prices = [float(recent['Low'].iloc[i]) for i in low_pivot_idx]

    resistance_zone = cluster_zone(resistance_prices, ZONE_CLUSTER_PCT)
    support_zone = cluster_zone(support_prices, ZONE_CLUSTER_PCT)

    events = {
        "breakout": False, "breakout_bars_ago": None, "breakout_volume_confirmed": False,
        "breakdown": False, "breakdown_bars_ago": None, "breakdown_volume_confirmed": False,
        "pullback_holding": False, "pullback_failed": False,
        "extended_above_ma_pct": None,
    }

    lookback_n = min(cfg["breakout_lookback"], n - 1)
    avg_vol_col = f"avg_vol{cfg['avg_vol_window']}"

    if resistance_zone:
        r_hi = resistance_zone[1]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] > r_hi and close[i - 1] <= r_hi:
                events["breakout"] = True
                events["breakout_bars_ago"] = w - 1
                vol_raw = df['Volume'].iloc[i]
                avgvol_raw = df[avg_vol_col].iloc[i - 1]
                vol_i = float(vol_raw) if pd.notna(vol_raw) else float('nan')
                avgvol_i = float(avgvol_raw) if pd.notna(avgvol_raw) else float('nan')
                events["breakout_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break
        if events["breakout"]:
            if close[last] >= r_hi:
                pass
            elif close[last] >= r_hi * (1 - ZONE_CLUSTER_PCT / 100):
                events["pullback_holding"] = True
            else:
                events["pullback_failed"] = True

    if support_zone:
        s_lo = support_zone[0]
        for w in range(1, lookback_n + 1):
            i = n - w
            if i - 1 < 0:
                break
            if close[i] < s_lo and close[i - 1] >= s_lo:
                events["breakdown"] = True
                events["breakdown_bars_ago"] = w - 1
                vol_raw = df['Volume'].iloc[i]
                avgvol_raw = df[avg_vol_col].iloc[i - 1]
                vol_i = float(vol_raw) if pd.notna(vol_raw) else float('nan')
                avgvol_i = float(avgvol_raw) if pd.notna(avgvol_raw) else float('nan')
                events["breakdown_volume_confirmed"] = bool(
                    not np.isnan(avgvol_i) and vol_i > avgvol_i * VOLUME_SURGE_MULTIPLE
                )
                break

    if not np.isnan(ma[last]) and ma[last] > 0:
        events["extended_above_ma_pct"] = round(float((close[last] / ma[last] - 1) * 100), 2)

    return {
        "stage": stage, "ma_slope_pct": round(cur_slope, 3),
        "resistance_zone": resistance_zone, "support_zone": support_zone, "events": events,
    }


def compute_rs_events(rs_ratio, lookback, recent_window, trend_lookback=4):
    result = {"rs_new_high": False, "rs_new_high_bars_ago": None, "rs_trend_up": False}
    vals = np.asarray(rs_ratio.values, dtype=float)
    n = len(vals)
    if n < 2:
        return result

    lb = min(lookback, n)
    rw = min(recent_window, n - 1)

    for w in range(0, rw + 1):
        i = n - 1 - w
        if i < 0:
            break
        window = vals[max(0, i - lb + 1):i + 1]
        if len(window) == 0 or np.all(np.isnan(window)) or np.isnan(vals[i]):
            continue
        if vals[i] >= np.nanmax(window):
            result["rs_new_high"] = True
            result["rs_new_high_bars_ago"] = w
            break

    j = n - 1
    if j - trend_lookback >= 0 and not np.isnan(vals[j]) and not np.isnan(vals[j - trend_lookback]) and vals[j - trend_lookback] != 0:
        result["rs_trend_up"] = bool(vals[j] > vals[j - trend_lookback])

    return result


# ----------------------------------------------------------------------------
# Data fetching — daily/weekly share one download; hourly is separate (see
# the DATA-SOURCING NOTE on TIMEFRAME_CONFIG above).
# ----------------------------------------------------------------------------

def _sanitize_ohlcv(df):
    """Coerce to clean numeric dtype BEFORE any resampling/aggregation —
    thinly-traded tickers can hand yfinance a stray Python None on gappy
    bars, and resample('...').agg({'High':'max',...}) does pairwise '>'
    comparisons internally that raise TypeError on a bare None. Coercing
    here (not after resampling) fixes it at the actual source."""
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Close", "Open", "High", "Low"])


def fetch_bars(fetch_symbol, benchmark_symbol, timeframe):
    cfg = TIMEFRAME_CONFIG[timeframe]

    if cfg["source"] == "intraday":
        raw = yf.download(
            [fetch_symbol, benchmark_symbol], period=cfg["download_period"], interval="60m",
            auto_adjust=True, progress=False
        )
    else:
        raw = yf.download(
            [fetch_symbol, benchmark_symbol], period=cfg["download_period"], interval="1d",
            auto_adjust=True, progress=False
        )

    if raw.empty:
        return None, None, "No data returned from yfinance."

    if isinstance(raw.columns, pd.MultiIndex):
        if raw.columns.names[0] != 'Price':
            try: raw.columns = raw.columns.swaplevel(0, 1)
            except Exception: pass
        raw.columns.names = ['Price', 'Ticker']

    if 'Close' not in raw or fetch_symbol not in raw['Close'].columns:
        return None, None, f"Invalid ticker '{fetch_symbol}'."

    bars = pd.DataFrame({
        "Open": raw['Open'][fetch_symbol], "High": raw['High'][fetch_symbol],
        "Low": raw['Low'][fetch_symbol], "Close": raw['Close'][fetch_symbol],
        "Volume": raw['Volume'][fetch_symbol] if 'Volume' in raw else pd.Series(dtype=float)
    })
    bench_close = raw['Close'][benchmark_symbol] if benchmark_symbol in raw['Close'].columns else pd.Series(dtype=float)

    bars = _sanitize_ohlcv(bars)
    bench_close = pd.to_numeric(bench_close, errors="coerce")

    if cfg["resample_rule"]:
        bars = bars.resample(cfg["resample_rule"]).agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        })
        bars = _sanitize_ohlcv(bars)
        bench_resampled = bench_close.resample(cfg["resample_rule"]).last().ffill().bfill()
        bench_aligned = bench_resampled.reindex(bars.index, method='ffill').bfill()
    else:
        bench_aligned = bench_close.reindex(bars.index, method='ffill').bfill()

    if len(bars) < cfg["min_bars"]:
        return None, None, (
            f"Only {len(bars)} clean {cfg['label'].lower()} bars available "
            f"(need {cfg['min_bars']}+) — "
            + ("hourly data is capped at ~730 days by Yahoo Finance itself." if cfg["source"] == "intraday"
               else "this ticker may be too newly listed for this timeframe yet.")
        )

    return bars, bench_aligned, None


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------

@chart_multiframe_bp.route("/chart-multiframe-analysis")
def multiframe_dashboard():
    default_market = request.args.get("market", "NSE").strip().upper()
    if default_market not in MARKET_SUFFIX: default_market = "NSE"
    default_stock = request.args.get("symbol", "PRAJIND" if default_market == "NSE" else "NVDA")
    return render_template(
        "chart_multiframe.html",
        default_stock=default_stock,
        default_market=default_market,
        timeframe_config={k: {"label": v["label"]} for k, v in TIMEFRAME_CONFIG.items()}
    )


@chart_multiframe_bp.route("/api/v1/mtf-ticker-payload")
def get_mtf_ticker_payload():
    market = request.args.get("market", "NSE").strip().upper()
    screener_key = request.args.get("screener", "").strip()
    market, screener_key = _resolve_screener_key(market, screener_key)
    entries = _load_screener_stock_entries(market, screener_key)
    tickers = _dedupe_tickers(entries)
    return jsonify({"status": "success", "market": market, "screener": screener_key, "tickers": tickers})


@chart_multiframe_bp.route("/api/v1/mtf-telemetry-data/<symbol>")
def get_mtf_telemetry_data(symbol):
    try:
        market = request.args.get("market", "NSE").strip().upper()
        if market not in MARKET_SUFFIX: market = "NSE"
        suffix = MARKET_SUFFIX[market]

        timeframe = request.args.get("timeframe", "D").strip().upper()
        if timeframe not in TIMEFRAME_CONFIG: timeframe = "D"
        cfg = TIMEFRAME_CONFIG[timeframe]

        symbol_clean = symbol.strip().upper().replace(".NS", "").replace(".", "-")
        fetch_symbol = f"{symbol_clean}{suffix}"
        benchmark_symbol = MARKET_BENCHMARK[market]

        bars, bench_aligned, err = fetch_bars(fetch_symbol, benchmark_symbol, timeframe)
        if err:
            return jsonify({"status": "error", "message": f"{symbol_clean} ({cfg['label']}): {err}"}), 400

        ma_col = f"ma{cfg['ma_period']}"
        avg_vol_col = f"avg_vol{cfg['avg_vol_window']}"
        bars[ma_col] = bars['Close'].rolling(cfg['ma_period']).mean()
        bars[avg_vol_col] = bars['Volume'].rolling(cfg['avg_vol_window']).mean()
        bars['ema10'] = calculate_ema(bars['Close'], 10)
        bars['ema100'] = calculate_ema(bars['Close'], 100)
        bars['bench'] = bench_aligned
        bars['rs_ratio'] = bars['Close'] / bars['bench']
        bars['rs_sma'] = bars['rs_ratio'].rolling(cfg['rs_sma']).mean()

        analysis = compute_stage_analysis(bars, ma_col, cfg)
        if analysis is None:
            return jsonify({
                "status": "error",
                "message": f"{symbol_clean} ({cfg['label']}): not enough bars yet for a {cfg['ma_period']}-period MA read."
            }), 400

        rs_events = compute_rs_events(bars['rs_ratio'], cfg['rs_new_high_lookback'], cfg['rs_new_high_recent'], cfg['ma_slope_lookback'])

        n_bars = len(bars)
        disp_n = min(cfg['display_bars'], n_bars)
        display = bars.iloc[-disp_n:]
        display_start_pos = n_bars - len(display)

        use_intraday_time = (cfg["source"] == "intraday")

        def fmt_time(ts):
            if use_intraday_time:
                return int(ts.timestamp())
            return ts.strftime("%Y-%m-%d")

        candles, ma_series, ema10_series, ema100_series, volume_series, rs_ratio_series, rs_sma_series = [], [], [], [], [], [], []
        for idx, row in display.iterrows():
            t = fmt_time(idx)
            candles.append({
                "time": t, "open": round(float(row['Open']), 4), "high": round(float(row['High']), 4),
                "low": round(float(row['Low']), 4), "close": round(float(row['Close']), 4),
                "volume": int(row['Volume']) if pd.notna(row['Volume']) else 0
            })
            if pd.notna(row[ma_col]):
                ma_series.append({"time": t, "value": round(float(row[ma_col]), 4)})
            if pd.notna(row['ema10']):
                ema10_series.append({"time": t, "value": round(float(row['ema10']), 4)})
            if pd.notna(row['ema100']):
                ema100_series.append({"time": t, "value": round(float(row['ema100']), 4)})

            surge = bool(pd.notna(row[avg_vol_col]) and row['Volume'] > row[avg_vol_col] * VOLUME_SURGE_MULTIPLE)
            up_bar = row['Close'] >= row['Open']
            color = 'rgba(245,158,11,0.85)' if surge else ('rgba(34,197,94,0.5)' if up_bar else 'rgba(239,68,68,0.5)')
            volume_series.append({"time": t, "value": int(row['Volume']) if pd.notna(row['Volume']) else 0, "color": color, "surge": surge})

            if pd.notna(row['rs_ratio']):
                rs_ratio_series.append({"time": t, "value": round(float(row['rs_ratio']), 6)})
            if pd.notna(row['rs_sma']):
                rs_sma_series.append({"time": t, "value": round(float(row['rs_sma']), 6)})

        markers, rs_markers = [], []
        ev = analysis['events']
        if ev['breakout'] and ev['breakout_bars_ago'] is not None:
            i = n_bars - 1 - ev['breakout_bars_ago']
            if display_start_pos <= i < n_bars:
                markers.append({
                    "time": fmt_time(bars.index[i]), "position": "belowBar", "color": "#22c55e", "shape": "arrowUp",
                    "text": "Breakout ✓Vol" if ev['breakout_volume_confirmed'] else "Breakout"
                })
        if ev['breakdown'] and ev['breakdown_bars_ago'] is not None:
            i = n_bars - 1 - ev['breakdown_bars_ago']
            if display_start_pos <= i < n_bars:
                markers.append({
                    "time": fmt_time(bars.index[i]), "position": "aboveBar", "color": "#ef4444", "shape": "arrowDown",
                    "text": "Breakdown ✓Vol" if ev['breakdown_volume_confirmed'] else "Breakdown"
                })
        if rs_events['rs_new_high'] and rs_events['rs_new_high_bars_ago'] is not None:
            i = n_bars - 1 - rs_events['rs_new_high_bars_ago']
            if display_start_pos <= i < n_bars:
                rs_markers.append({
                    "time": fmt_time(bars.index[i]), "position": "aboveBar", "color": "#facc15", "shape": "circle",
                    "text": f"{cfg['rs_new_high_lookback']}-Bar RS High"
                })

        return jsonify({
            "status": "success", "symbol": symbol_clean, "market": market,
            "timeframe": timeframe, "timeframe_label": cfg['label'],
            "intraday": use_intraday_time,
            "stage": analysis['stage'], "stage_label": STAGE_LABELS.get(analysis['stage']),
            "ma_period": cfg['ma_period'], "ma_slope_pct": analysis['ma_slope_pct'],
            "resistance_zone": analysis['resistance_zone'], "support_zone": analysis['support_zone'],
            "events": analysis['events'], "rs_events": rs_events,
            "series": {
                "candles": candles, "ma": ma_series, "ema10": ema10_series, "ema100": ema100_series,
                "volume": volume_series, "markers": markers,
                "rs_ratio": rs_ratio_series, "rs_sma": rs_sma_series, "rs_markers": rs_markers
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ----------------------------------------------------------------------------
# Custom symbol-list upload — same pattern as carousel/weinstein, kept local.
# ----------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTENSIONS = {'.csv', '.xlsx', '.xls'}


@chart_multiframe_bp.route("/api/v1/mtf-upload-symbols", methods=["POST"])
def upload_mtf_symbols():
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