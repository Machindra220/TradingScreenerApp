"""
stage2_screener_us.py  —  US Minervini Stage 2 Screener

Formula fixes vs original:
  - RS vs ^GSPC: ratio-of-relatives (1+stock_ret)/(1+bench_ret)-1
    NOT price/MA200 which was mislabelled "Relative Strength"
  - RS trend: % change in real RS ratio over 20 sessions (same logic, correct denominator)
  - MA200_20d guard: uses iloc[-22] which is safe given the 200-bar minimum check
  - Minervini conditions 1-6: all correct, preserved unchanged

Architecture fixes vs original:
  - Removed SQLAlchemy/Stage2Stock DB dependency (crashed if model missing)
  - __file__-anchored paths (not os.getcwd())
  - us_cache bulk fetch instead of 500 individual yf.Ticker() calls
  - Background thread + progress polling
  - Last 5 snapshot history with restore
  - rs_h (last 5 RS values) for sparkline bars in table
"""

import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file
from werkzeug.utils import secure_filename

from app.services.market_data_cache import us_cache, latest_bar_date

screener_us_bp = Blueprint("stage2_screener_us", __name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'stage2_us')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_stage2_us_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_stage2_us.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_stage2_us.json')
DEFAULT_US_CSV  = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
DEFAULT_US_LABEL = "S&P 500 Default (sp500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT    = 5
US_BENCHMARK     = "^GSPC"

# ── Progress ──────────────────────────────────────────────────────────────────
_lock      = threading.Lock()
_PROGRESS  = {"active": False, "processed": 0, "total": 0,
               "current_symbol": "", "stage": "idle", "error": None}

def _set_progress(**kw):
    with _lock: _PROGRESS.update(kw)

def _get_progress():
    with _lock: return dict(_PROGRESS)


# ── Source helpers ────────────────────────────────────────────────────────────

def _get_active_source():
    if os.path.exists(LAST_CSV_CONFIG):
        try:
            with open(LAST_CSV_CONFIG) as f:
                cfg = json.load(f)
            fp   = cfg.get('path', '')
            name = cfg.get('name', os.path.basename(fp))
            if fp and os.path.exists(fp):
                return fp, name, False
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_US_CSV, DEFAULT_US_LABEL, True


def _read_ticker_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Cannot read file: {e}")
    cols = {c.lower().strip(): c for c in df.columns}
    col  = next((cols[k] for k in ('symbol','ticker','symbols','tickers') if k in cols), None)
    if col is None:
        raise ValueError("File needs a Symbol/Ticker column.")
    sec_col = next((cols[k] for k in ('gics sector','sector','industry') if k in cols), None)
    results = []
    for _, row in df.iterrows():
        sym = str(row[col]).strip().upper().replace('.', '-')
        sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
        if sym and not sym.startswith('$'):
            results.append({'symbol': sym, 'sector': sec})
    return results


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _prune_snapshots(keep):
    keep = set(keep)
    for f in os.listdir(SNAPSHOT_DIR):
        if f not in keep:
            try: os.remove(os.path.join(SNAPSHOT_DIR, f))
            except OSError: pass


def _normalize_stock(s):
    s.setdefault('symbol',      '')
    s.setdefault('sector',      'Unknown')
    s.setdefault('price',       0.0)
    s.setdefault('retracement', 0.0)
    s.setdefault('volume',      0)
    s.setdefault('vol_avg',     0)
    s.setdefault('vol_status',  'Normal')
    s.setdefault('rs',          0.0)
    s.setdefault('rs_raw',      0.0)
    s.setdefault('rs_trend',    '—')
    s.setdefault('rs_change',   0.0)
    s.setdefault('rs_h',        [])
    s.setdefault('rs_up',       False)
    s.setdefault('ma200_ext',   1.0)
    s.setdefault('rs_at_52wh',   False)
    s.setdefault('ma50',        0.0)
    s.setdefault('ma200',       0.0)
    return s


# ── Core screening function ───────────────────────────────────────────────────

def _screen_symbol(symbol: str, df: pd.DataFrame,
                   bench_close: pd.Series, sector: str) -> dict | None:
    """
    Apply all Minervini Stage 2 conditions plus real RS vs ^GSPC.

    Conditions (all six must pass):
      1. price > MA150 AND price > MA200
      2. MA150 > MA200
      3. MA200 today > MA200 20 sessions ago  (upward slope)
      4. MA50 > MA150 AND MA50 > MA200
      5. price >= 52W_low * 1.30              (at least 30% above 52W low)
      6. price >= 52W_high * 0.75             (within 25% of 52W high)

    RS: ratio-of-relatives vs ^GSPC (corrected from original price/MA200)
      rs_raw = (1 + stock_ret_3m) / (1 + bench_ret_3m) - 1
      Percentile ranking applied across the full scanned universe.

    MA200 Extension (kept separately for sorting / reference):
      ma200_ext = price / MA200
    """
    try:
        if df is None or df.empty or len(df) < 200:
            return None
        if not {'Close', 'High', 'Low', 'Volume'}.issubset(df.columns):
            return None

        close  = df['Close'].dropna()
        high_s = df['High'].dropna()
        low_s  = df['Low'].dropna()
        vol    = df['Volume'].dropna()

        if len(close) < 200:
            return None

        # Normalise tz
        if getattr(close.index, 'tz', None) is not None:
            close  = close.copy();  close.index  = close.index.tz_localize(None)
            high_s = high_s.copy(); high_s.index = high_s.index.tz_localize(None)
            low_s  = low_s.copy();  low_s.index  = low_s.index.tz_localize(None)
            vol    = vol.copy();    vol.index    = vol.index.tz_localize(None)

        curr_price = float(close.iloc[-1])

        ma50  = close.rolling(50).mean()
        ma150 = close.rolling(150).mean()
        ma200 = close.rolling(200).mean()

        curr_ma50  = float(ma50.iloc[-1])
        curr_ma150 = float(ma150.iloc[-1])
        curr_ma200 = float(ma200.iloc[-1])

        if any(np.isnan(v) for v in [curr_ma50, curr_ma150, curr_ma200]):
            return None

        ma200_20d_ago = float(ma200.iloc[-22]) if len(ma200) >= 22 else float(ma200.iloc[0])

        low_52w  = float(low_s.tail(252).min())
        high_52w = float(high_s.tail(252).max())

        # ── Six Minervini conditions ──────────────────────────────────────
        if not (curr_price > curr_ma150 and curr_price > curr_ma200): return None
        if not (curr_ma150 > curr_ma200):                              return None
        if not (curr_ma200 > ma200_20d_ago):                           return None
        if not (curr_ma50 > curr_ma150 and curr_ma50 > curr_ma200):   return None
        if not (curr_price >= low_52w * 1.30):                         return None
        if not (curr_price >= high_52w * 0.75):                        return None

        # ── Real RS vs ^GSPC (ratio-of-relatives, Memory #1) ─────────────
        bench_aligned = bench_close.reindex(close.index).ffill().bfill()
        rs_raw = 0.0
        if bench_aligned.notna().sum() >= 63:
            # Use 63-bar (3M) window aligned to available data
            s_ret = (float(close.iloc[-1]) / float(close.iloc[-63])) - 1
            b_ret = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[-63])) - 1
            if (1 + b_ret) != 0:
                rs_raw = round(float(((1 + s_ret) / (1 + b_ret)) - 1), 4)

        # ── RS Ratio line at 52-week high ──────────────────────────────────
        # Build the full daily RS ratio series (price / bench each day),
        # then check whether today's value equals or exceeds the 52-week high
        # of that series. This is the most powerful RS signal — the RS line
        # making a new annual high often PRECEDES the price breakout.
        rs_at_52wh = False
        if bench_aligned.notna().sum() >= 63:
            bench_clean  = bench_aligned.dropna()
            rs_series    = close.reindex(bench_clean.index) / bench_clean
            rs_series    = rs_series.dropna()
            if len(rs_series) >= 252:
                rs_52w_high  = float(rs_series.tail(252).max())
                rs_today     = float(rs_series.iloc[-1])
                # Allow a 0.5% tolerance so a stock at 99.6% of the 52WH still qualifies
                rs_at_52wh   = rs_today >= rs_52w_high * 0.995

        # ── RS Trend: simple difference of RS ratios over 20 sessions ────────
        # BUG FIX: the previous formula computed ((rs_now - rs_20d) / rs_20d) * 100
        # When rs_20d_ago is near zero (stock/bench returned similarly 20 days ago),
        # dividing by a near-zero denominator produces ±thousands% (e.g. +3127%).
        #
        # Correct approach: measure how many percentage POINTS the RS ratio
        # moved over 20 sessions. This is always bounded and intuitive.
        #   rs_change = (rs_now - rs_20d_ago) * 100   [in pp]
        #   > +3pp  = Accelerating (RS expanding meaningfully)
        #   >= 0pp  = Steady
        #   < 0pp   = Fading
        rs_change = 0.0
        rs_trend  = "—"
        if bench_aligned.notna().sum() >= 83:  # need 63+20 bars
            if len(close) >= 84:
                s_ret_20d = (float(close.iloc[-21]) / float(close.iloc[-84])) - 1
            else:
                s_ret_20d = 0.0
            bench_vals = bench_aligned.dropna()
            if len(bench_vals) >= 84:
                b_ret_20d = (float(bench_vals.iloc[-21]) / float(bench_vals.iloc[-84])) - 1
            else:
                b_ret_20d = 0.0
            rs_20d_ago = ((1 + s_ret_20d) / (1 + b_ret_20d) - 1) if (1 + b_ret_20d) != 0 else 0.0

            # Simple difference in percentage points — never divides by near-zero
            rs_change = round((rs_raw - rs_20d_ago) * 100, 2)

            if rs_change > 3.0:   rs_trend = "🚀 Accelerating"
            elif rs_change >= 0:  rs_trend = "🟢 Steady"
            else:                 rs_trend = "⚠️ Fading"

        # ── Volume ────────────────────────────────────────────────────────
        vol_avg = int(vol.rolling(50).mean().iloc[-1]) if len(vol) >= 50 else 0
        curr_vol = int(vol.iloc[-1])

        return {
            "symbol":      symbol,
            "sector":      sector,
            "price":       round(curr_price, 2),
            "retracement": round(((high_52w - curr_price) / high_52w) * 100, 2),
            "volume":      curr_vol,
            "vol_avg":     vol_avg,
            "vol_status":  "🔥" if curr_vol > vol_avg else "Normal",
            "rs_raw":      rs_raw,       # used for percentile ranking
            "rs":          0.0,          # percentile rank (1–99), filled after
            "rs_trend":    rs_trend,
            "rs_change":   rs_change,
            "rs_h":        [],           # last 5 RS percentiles across scans (filled after)
            "rs_up":       False,
            "rs_at_52wh":  rs_at_52wh,  # True when RS ratio line at a 52-week high
            "ma200_ext":   round(curr_price / curr_ma200, 3),
            "ma50":        round(curr_ma50, 2),
            "ma200":       round(curr_ma200, 2),
        }
    except Exception as e:
        print(f"  [Stage2US] Error {symbol}: {e}")
        return None


# ── Background scan ───────────────────────────────────────────────────────────

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)
    try:
        tickers = _read_ticker_file(source_path)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return

    symbols  = [t['symbol'] for t in tickers]
    sym_meta = {t['symbol']: t for t in tickers}
    _set_progress(total=len(symbols))

    # Benchmark once
    _set_progress(stage="fetching_benchmark")
    bench_result, _ = us_cache.get_price_history_bulk(
        [US_BENCHMARK], interval='1d', lookback_days=500
    )
    bench_df = bench_result.get(US_BENCHMARK)
    if bench_df is None or bench_df.empty:
        _set_progress(active=False, stage="error", error="Could not fetch ^GSPC benchmark.")
        return

    bench_close = bench_df['Close'].dropna()
    if getattr(bench_close.index, 'tz', None) is not None:
        bench_close.index = bench_close.index.tz_localize(None)

    # Bulk fetch
    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set_progress(
            stage="fetching_prices", processed=i, total=t, current_symbol=s)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf, _fl = (fetch_report['from_cache'], fetch_report['fetched'],
                     fetch_report['failed'])

    print(f"\n[Stage2 US] {source_name}: {len(symbols)} symbols | "
          f"Cache: {_ch} | Fetched: {_yf} | Failed: {len(_fl)}")

    # Load previous RS history for sparkline bars
    old_rs_h = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                prev = json.load(f)
            for s in prev.get('stocks', []):
                old_rs_h[s.get('symbol', '')] = s.get('rs_h', [])
        except (json.JSONDecodeError, OSError):
            pass

    # Per-symbol screening
    _set_progress(stage="screening", processed=0, total=len(symbols))
    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        df   = price_data.get(sym)
        meta = sym_meta[sym]
        res  = _screen_symbol(sym, df, bench_close, meta['sector'])
        if res:
            raw_results.append(res)
    _set_progress(processed=len(symbols), current_symbol="")

    # RS percentile ranking within qualifying universe
    if raw_results:
        df_r = pd.DataFrame(raw_results)
        df_r['rs_raw'] = pd.to_numeric(df_r['rs_raw'], errors='coerce').fillna(0)
        df_r['rs'] = (df_r['rs_raw'].rank(pct=True).mul(98).add(1)
                      .round(0).clip(1, 99).astype(int))
        df_r.sort_values('rs', ascending=False, inplace=True)
        df_r.reset_index(drop=True, inplace=True)

        def enrich(row):
            sym  = row['symbol']
            hist = (old_rs_h.get(sym, []) + [int(row['rs'])])[-5:]
            row['rs_h']  = hist
            row['rs_up'] = (len(hist) > 1 and
                            all(x < y for x, y in zip(hist, hist[1:])))
            return row

        df_r = df_r.apply(enrich, axis=1)
        stocks = df_r.to_dict(orient='records')
    else:
        stocks = []

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    # Sector breakdown for top sectors
    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'Unknown')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    accel_count   = sum(1 for s in stocks if 'Accelerating' in s.get('rs_trend', ''))
    rs_52wh_count = sum(1 for s in stocks if s.get('rs_at_52wh', False))
    top_rs_count = sum(1 for s in stocks if s.get('rs', 0) >= 80)

    payload = {
        'stocks':          stocks,
        'time':            last_time,
        'source':          source_name,
        'scanned_count':   len(symbols),
        'passed_count':    len(stocks),
        'excluded_count':  len(symbols) - len(stocks),
        'price_data_asof': price_data_asof,
        'cache_hits':      _ch,
        'yf_fetches':      _yf,
        'sector_counts':   sector_counts,
    }

    snap_file = f"snapshot_stage2_us_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snap_file), 'w') as f:
        json.dump(payload, f)
    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "source":          source_name,
        "count":           len(stocks),
        "accel_count":     accel_count,
        "rs_52wh_count":   rs_52wh_count,
        "top_rs_count":    top_rs_count,
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done")


# ── Color palette for sectors ─────────────────────────────────────────────────
_PALETTE = [
    {"bg": "#d1fae5", "text": "#065f46", "badge": "#10b981", "border": "#a7f3d0", "name": "emerald"},
    {"bg": "#dbeafe", "text": "#1e40af", "badge": "#3b82f6", "border": "#bfdbfe", "name": "blue"},
    {"bg": "#f3e8ff", "text": "#6b21a8", "badge": "#a855f7", "border": "#e9d5ff", "name": "purple"},
    {"bg": "#fef3c7", "text": "#92400e", "badge": "#f59e0b", "border": "#fde68a", "name": "amber"},
    {"bg": "#ffe4e6", "text": "#9f1239", "badge": "#f43f5e", "border": "#fecdd3", "name": "rose"},
]


def _build_sector_meta(stocks, sector_counts):
    sorted_secs = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top5        = [s[0] for s in sorted_secs[:5] if s[0] not in ('Unknown', 'N/A')]
    color_map, meta = {}, []
    for i, sec in enumerate(top5):
        theme = _PALETTE[i]
        color_map[sec] = theme
        meta.append({"name": sec, "count": sector_counts[sec], "theme": theme})
    return meta, color_map


# ── Routes ────────────────────────────────────────────────────────────────────

@screener_us_bp.route("/stage2-us", methods=["GET", "POST"])
def stage2_us_view():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('stage2_screener_us.stage2_us_view', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            filename  = secure_filename(file.filename)
            ext       = os.path.splitext(filename)[1].lower()
            save_name = f"uploaded_stage2_us_tickers{ext}"
            filepath  = os.path.join(UPLOAD_FOLDER, save_name)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_US_CSV, DEFAULT_US_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            _set_progress(active=False, stage="error",
                          error=f"Ticker file not found: {DEFAULT_US_CSV}")
            return redirect(url_for('stage2_screener_us.stage2_us_view'))

        t = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        t.start()
        return redirect(url_for('stage2_screener_us.stage2_us_view', scanning=1))

    # GET
    stocks = []
    last_time = source_name = price_data_asof = benchmark_label = None
    scanned_count = passed_count = excluded_count = 0
    cache_hits = yf_fetches = 0
    sector_counts = {}

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            stocks         = [_normalize_stock(s) for s in cache.get('stocks', [])]
            last_time      = cache.get('time')
            source_name    = cache.get('source')
            scanned_count  = cache.get('scanned_count', 0)
            passed_count   = cache.get('passed_count', 0)
            excluded_count = cache.get('excluded_count', 0)
            price_data_asof = cache.get('price_data_asof')
            cache_hits     = cache.get('cache_hits', 0)
            yf_fetches     = cache.get('yf_fetches', 0)
            sector_counts  = cache.get('sector_counts', {})
        except (json.JSONDecodeError, OSError):
            pass

    top_sectors, sector_color_map = _build_sector_meta(stocks, sector_counts)
    history     = _load_history()
    progress    = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "stage2_screener_us.html",
        stocks          = stocks,
        last_time       = last_time,
        source_name     = source_name,
        scanned_count   = scanned_count,
        passed_count    = passed_count,
        excluded_count  = excluded_count,
        price_data_asof = price_data_asof,
        cache_hits      = cache_hits,
        yf_fetches      = yf_fetches,
        top_sectors     = top_sectors,
        sector_color_map = sector_color_map,
        history         = history,
        active_file     = active_file,
        is_default_source = is_default_source,
        default_label   = DEFAULT_US_LABEL,
        is_scanning     = is_scanning,
        scan_error      = progress.get("error"),
        restored        = request.args.get('restored')     == '1',
        restore_error   = request.args.get('restore_error') == '1',
    )


@screener_us_bp.route("/stage2-us/progress")
def stage2_us_progress():
    return jsonify(_get_progress())


@screener_us_bp.route("/stage2-us/clear-source", methods=["POST"])
def stage2_us_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('stage2_screener_us.stage2_us_view'))


@screener_us_bp.route("/restore-stage2-us/<snapshot_file>", methods=["POST"])
def restore_stage2_us(snapshot_file):
    safe      = os.path.basename(snapshot_file)
    snap_path = os.path.join(SNAPSHOT_DIR, safe)
    if not (safe.startswith('snapshot_stage2_us_') and safe.endswith('.json')
            and os.path.exists(snap_path)):
        return redirect(url_for('stage2_screener_us.stage2_us_view', restore_error=1))
    try:
        with open(snap_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except Exception:
        return redirect(url_for('stage2_screener_us.stage2_us_view', restore_error=1))
    return redirect(url_for('stage2_screener_us.stage2_us_view', restored=1))


@screener_us_bp.route("/export-stage2-us")
def export_stage2_us():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            tmp       = os.path.join(UPLOAD_FOLDER, 'tmp_export_stage2_us.csv')
            pd.DataFrame(stocks).to_csv(tmp, index=False)
            return send_file(tmp, as_attachment=True,
                             download_name=f"Stage2_US_{ts}.csv")
    return "No scan data.", 404