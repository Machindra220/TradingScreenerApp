"""
stage2_india.py  —  India (NSE) Minervini Stage 2 Screener

Formula fixes vs original:
  - RS vs ^CRSLDX: ratio-of-relatives (1+stock_ret)/(1+bench_ret)-1
    (^NSEI fallback if ^CRSLDX unavailable — Memory #1)
    NOT price/MA200 which was mislabelled "Relative Strength"
  - RS Trend: simple pp difference (rs_now - rs_20d_ago)*100
    NOT % change of near-zero denominator (caused +3127% explosions)
  - Minervini conditions 1-6: all verified correct, preserved unchanged

Architecture fixes vs original:
  - Removed SQLAlchemy/Stage2Stock dependency
  - __file__-anchored paths (not os.getcwd() — Memory #5)
  - ind_cache bulk fetch (not 500 individual yf.Ticker() calls)
  - Background thread + progress polling (design system standard)
  - Last 5 snapshot history with restore
  - rs_h (last 5 RS values) for sparkline bars in table
  - rs_at_52wh: RS ratio line at 52-week high badge
"""

import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from flask import (Blueprint, render_template, request,
                   redirect, url_for, jsonify, send_file)
from werkzeug.utils import secure_filename

from app.services.market_data_cache import ind_cache, latest_bar_date

screener_india_bp = Blueprint("stage2_india", __name__)

# ── Paths (__file__-anchored — Memory #5) ────────────────────────────────────
_PROJECT_ROOT    = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER    = os.path.join(_PROJECT_ROOT, 'uploads', 'india_screener')
SNAPSHOT_DIR     = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON     = os.path.join(UPLOAD_FOLDER, 'last_stage2_india_results.json')
HISTORY_JSON     = os.path.join(UPLOAD_FOLDER, 'scan_history_stage2_india.json')
LAST_CSV_CONFIG  = os.path.join(UPLOAD_FOLDER, 'last_csv_stage2_india.json')
DEFAULT_IND_CSV  = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT = 5
# Benchmark: ^CRSLDX (Nifty 500 Total Return) with ^NSEI fallback — Memory #1
IND_BENCHMARK_PRIMARY  = ("^CRSLDX", "Nifty 500")
IND_BENCHMARK_FALLBACK = ("^NSEI",   "Nifty 50")

# ── Progress ──────────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_PROGRESS = {"active": False, "processed": 0, "total": 0,
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
    return DEFAULT_IND_CSV, DEFAULT_IND_LABEL, True


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
    sec_col = next((cols[k] for k in ('industry','sector','gics sector') if k in cols), None)
    results = []
    for _, row in df.iterrows():
        raw = str(row[col]).strip().upper().replace('.NS', '').replace('.BSE', '')
        sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
        if raw and not raw.startswith('$') and raw not in ('SYMBOL','TICKER','N/A','NONE'):
            yf_sym = f"{raw}.NS"
            results.append({'symbol': raw, 'yf_sym': yf_sym, 'sector': sec})
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
    s.setdefault('rs',          0)
    s.setdefault('rs_raw',      0.0)
    s.setdefault('rs_trend',    '—')
    s.setdefault('rs_change',   0.0)
    s.setdefault('rs_h',        [])
    s.setdefault('rs_up',       False)
    s.setdefault('rs_at_52wh',  False)
    s.setdefault('ma200_ext',   1.0)
    s.setdefault('ma50',        0.0)
    s.setdefault('ma200',       0.0)
    return s


# ── Core screening function ───────────────────────────────────────────────────

def _screen_symbol(yf_sym: str, df: pd.DataFrame,
                   bench_close: pd.Series, sector: str,
                   clean_sym: str) -> dict | None:
    """
    Minervini Stage 2 for NSE stocks.

    Conditions (all six must pass):
      1. price > MA150 AND price > MA200
      2. MA150 > MA200
      3. MA200 today > MA200 20 sessions ago  (upward slope)
      4. MA50 > MA150 AND MA50 > MA200
      5. price >= 52W_low  * 1.30
      6. price >= 52W_high * 0.75

    RS: ratio-of-relatives vs ^CRSLDX (corrected from original price/MA200)
    RS Trend: simple pp difference — no near-zero division bug
    RS 52WH: RS ratio line at 52-week high (leading indicator)
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

        # Normalise tz (Memory — cache returns tz-naive from SQLite)
        for s in [close, high_s, low_s, vol]:
            if getattr(s.index, 'tz', None) is not None:
                s.index = s.index.tz_localize(None)

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

        # ── Real RS vs Nifty 500 (ratio-of-relatives — Memory #1) ────────
        bench_aligned = bench_close.reindex(close.index).ffill().bfill()
        rs_raw = 0.0
        if bench_aligned.notna().sum() >= 63:
            s_ret = (float(close.iloc[-1]) / float(close.iloc[-63])) - 1
            b_ret = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[-63])) - 1
            if (1 + b_ret) != 0:
                rs_raw = round(float(((1 + s_ret) / (1 + b_ret)) - 1), 4)

        # ── RS ratio line at 52-week high ─────────────────────────────────
        rs_at_52wh = False
        if bench_aligned.notna().sum() >= 63:
            bench_clean = bench_aligned.dropna()
            rs_series   = close.reindex(bench_clean.index) / bench_clean
            rs_series   = rs_series.dropna()
            if len(rs_series) >= 252:
                rs_52w_high = float(rs_series.tail(252).max())
                rs_today    = float(rs_series.iloc[-1])
                rs_at_52wh  = rs_today >= rs_52w_high * 0.995  # 0.5% tolerance

        # ── RS Trend: pp difference (fixed — no near-zero division) ───────
        rs_change = 0.0
        rs_trend  = "—"
        if bench_aligned.notna().sum() >= 83:
            s_ret_20d = (float(close.iloc[-21]) / float(close.iloc[-84])) - 1 if len(close) >= 84 else 0.0
            bench_vals = bench_aligned.dropna()
            b_ret_20d = (float(bench_vals.iloc[-21]) / float(bench_vals.iloc[-84])) - 1 if len(bench_vals) >= 84 else 0.0
            rs_20d_ago = ((1 + s_ret_20d) / (1 + b_ret_20d) - 1) if (1 + b_ret_20d) != 0 else 0.0
            rs_change  = round((rs_raw - rs_20d_ago) * 100, 2)

            if rs_change > 3.0:  rs_trend = "🚀 Accelerating"
            elif rs_change >= 0: rs_trend = "🟢 Steady"
            else:                rs_trend = "⚠️ Fading"

        # ── Volume ────────────────────────────────────────────────────────
        vol_avg  = int(vol.rolling(50).mean().iloc[-1]) if len(vol) >= 50 else 0
        curr_vol = int(vol.iloc[-1])

        return {
            "symbol":      clean_sym,
            "sector":      sector,
            "price":       round(curr_price, 2),
            "retracement": round(((high_52w - curr_price) / high_52w) * 100, 2),
            "volume":      curr_vol,
            "vol_avg":     vol_avg,
            "vol_status":  "🔥" if curr_vol > vol_avg else "Normal",
            "rs_raw":      rs_raw,
            "rs":          0,          # filled after percentile ranking
            "rs_trend":    rs_trend,
            "rs_change":   rs_change,
            "rs_h":        [],         # filled after — last 5 RS percentiles
            "rs_up":       False,
            "rs_at_52wh":  rs_at_52wh,
            "ma200_ext":   round(curr_price / curr_ma200, 3),
            "ma50":        round(curr_ma50, 2),
            "ma200":       round(curr_ma200, 2),
        }
    except Exception as e:
        print(f"  [Stage2IND] Error {yf_sym}: {e}")
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

    yf_symbols  = [t['yf_sym'] for t in tickers]
    sym_meta    = {t['yf_sym']: t for t in tickers}
    _set_progress(total=len(yf_symbols))

    # Benchmark once — ^CRSLDX with ^NSEI fallback (Memory #1)
    _set_progress(stage="fetching_benchmark")
    bench_close, bench_label = None, None
    for ticker, label in (IND_BENCHMARK_PRIMARY, IND_BENCHMARK_FALLBACK):
        res, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=500)
        df = res.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            bench_close = df['Close'].dropna()
            if getattr(bench_close.index, 'tz', None) is not None:
                bench_close.index = bench_close.index.tz_localize(None)
            bench_label = f"{label} ({ticker})"
            break

    if bench_close is None:
        _set_progress(active=False, stage="error", error="Could not fetch benchmark (^CRSLDX / ^NSEI).")
        return

    # Bulk fetch via ind_cache
    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=500,
        progress_callback=lambda i, t, s: _set_progress(
            stage="fetching_prices", processed=i, total=t, current_symbol=s)
    )
    price_data_asof = latest_bar_date(price_data)
    _ch, _yf, _fl   = (fetch_report['from_cache'], fetch_report['fetched'],
                        fetch_report['failed'])
    print(f"\n[Stage2 IND] {source_name}: {len(yf_symbols)} | "
          f"Cache: {_ch} | Fetched: {_yf} | Failed: {len(_fl)}")

    # Load previous RS history for sparklines
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
    _set_progress(stage="screening", processed=0, total=len(yf_symbols))
    raw_results = []
    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        meta = sym_meta[yf_sym]
        df   = price_data.get(yf_sym)
        res  = _screen_symbol(yf_sym, df, bench_close, meta['sector'], meta['symbol'])
        if res:
            raw_results.append(res)
    _set_progress(processed=len(yf_symbols), current_symbol="")

    # RS percentile ranking within qualifying universe
    if raw_results:
        df_r = pd.DataFrame(raw_results)
        df_r['rs_raw'] = pd.to_numeric(df_r['rs_raw'], errors='coerce').fillna(0)
        df_r['rs'] = (df_r['rs_raw'].rank(pct=True)
                      .mul(98).add(1).round(0).clip(1, 99).astype(int))
        df_r.sort_values('rs', ascending=False, inplace=True)
        df_r.reset_index(drop=True, inplace=True)

        def enrich(row):
            sym  = row['symbol']
            hist = (old_rs_h.get(sym, []) + [int(row['rs'])])[-5:]
            row['rs_h']  = hist
            row['rs_up'] = len(hist) > 1 and all(x < y for x, y in zip(hist, hist[1:]))
            return row

        df_r = df_r.apply(enrich, axis=1)
        stocks = df_r.to_dict(orient='records')
    else:
        stocks = []

    last_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    sector_counts = {}
    for s in stocks:
        sec = s.get('sector', 'Unknown')
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    accel_count   = sum(1 for s in stocks if 'Accelerating' in s.get('rs_trend', ''))
    rs_52wh_count = sum(1 for s in stocks if s.get('rs_at_52wh', False))
    top_rs_count  = sum(1 for s in stocks if s.get('rs', 0) >= 80)

    payload = {
        'stocks':          stocks,
        'time':            last_time,
        'source':          source_name,
        'benchmark_label': bench_label,
        'scanned_count':   len(yf_symbols),
        'passed_count':    len(stocks),
        'excluded_count':  len(yf_symbols) - len(stocks),
        'price_data_asof': price_data_asof,
        'cache_hits':      _ch,
        'yf_fetches':      _yf,
        'sector_counts':   sector_counts,
    }

    snap_file = f"snapshot_stage2_ind_{uuid.uuid4().hex}.json"
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
        "benchmark_label": bench_label,
        "price_data_asof": price_data_asof,
        "snapshot_file":   snap_file,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done")


# ── Sector meta helpers ───────────────────────────────────────────────────────
_PALETTE = [
    {"bg": "#d1fae5", "text": "#065f46", "badge": "#10b981", "border": "#a7f3d0"},
    {"bg": "#dbeafe", "text": "#1e40af", "badge": "#3b82f6", "border": "#bfdbfe"},
    {"bg": "#f3e8ff", "text": "#6b21a8", "badge": "#a855f7", "border": "#e9d5ff"},
    {"bg": "#fef3c7", "text": "#92400e", "badge": "#f59e0b", "border": "#fde68a"},
    {"bg": "#ffe4e6", "text": "#9f1239", "badge": "#f43f5e", "border": "#fecdd3"},
]

def _build_sector_meta(sector_counts):
    sorted_s = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)
    top5     = [s[0] for s in sorted_s[:5] if s[0] not in ('Unknown', 'N/A')]
    color_map, meta = {}, []
    for i, sec in enumerate(top5):
        theme = _PALETTE[i]
        color_map[sec] = theme
        meta.append({"name": sec, "count": sector_counts[sec], "theme": theme})
    return meta, color_map


# ── Routes ────────────────────────────────────────────────────────────────────

@screener_india_bp.route("/stage2-india", methods=["GET", "POST"])
def stage2_india_view():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('stage2_india.stage2_india_view', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            filename  = secure_filename(file.filename)
            ext       = os.path.splitext(filename)[1].lower()
            save_name = f"uploaded_stage2_ind_tickers{ext}"
            filepath  = os.path.join(UPLOAD_FOLDER, save_name)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_IND_CSV, DEFAULT_IND_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            _set_progress(active=False, stage="error",
                          error=f"Ticker file not found: {DEFAULT_IND_CSV}")
            return redirect(url_for('stage2_india.stage2_india_view'))

        t = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        t.start()
        return redirect(url_for('stage2_india.stage2_india_view', scanning=1))

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
            stocks          = [_normalize_stock(s) for s in cache.get('stocks', [])]
            last_time       = cache.get('time')
            source_name     = cache.get('source')
            benchmark_label = cache.get('benchmark_label')
            scanned_count   = cache.get('scanned_count', 0)
            passed_count    = cache.get('passed_count', 0)
            excluded_count  = cache.get('excluded_count', 0)
            price_data_asof = cache.get('price_data_asof')
            cache_hits      = cache.get('cache_hits', 0)
            yf_fetches      = cache.get('yf_fetches', 0)
            sector_counts   = cache.get('sector_counts', {})
        except (json.JSONDecodeError, OSError):
            pass

    top_sectors, sector_color_map = _build_sector_meta(sector_counts)
    history      = _load_history()
    progress     = _get_progress()
    is_scanning  = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "stage2_india.html",
        stocks           = stocks,
        last_time        = last_time,
        source_name      = source_name,
        benchmark_label  = benchmark_label,
        scanned_count    = scanned_count,
        passed_count     = passed_count,
        excluded_count   = excluded_count,
        price_data_asof  = price_data_asof,
        cache_hits       = cache_hits,
        yf_fetches       = yf_fetches,
        top_sectors      = top_sectors,
        sector_color_map = sector_color_map,
        history          = history,
        active_file      = active_file,
        is_default_source= is_default_source,
        default_label    = DEFAULT_IND_LABEL,
        is_scanning      = is_scanning,
        scan_error       = progress.get("error"),
        restored         = request.args.get('restored')      == '1',
        restore_error    = request.args.get('restore_error') == '1',
    )


@screener_india_bp.route("/stage2-india/progress")
def stage2_india_progress():
    return jsonify(_get_progress())


@screener_india_bp.route("/stage2-india/clear-source", methods=["POST"])
def stage2_india_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('stage2_india.stage2_india_view'))


@screener_india_bp.route("/restore-stage2-india/<snapshot_file>", methods=["POST"])
def restore_stage2_india(snapshot_file):
    safe      = os.path.basename(snapshot_file)
    snap_path = os.path.join(SNAPSHOT_DIR, safe)
    if not (safe.startswith('snapshot_stage2_ind_') and safe.endswith('.json')
            and os.path.exists(snap_path)):
        return redirect(url_for('stage2_india.stage2_india_view', restore_error=1))
    try:
        with open(snap_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except Exception:
        return redirect(url_for('stage2_india.stage2_india_view', restore_error=1))
    return redirect(url_for('stage2_india.stage2_india_view', restored=1))


@screener_india_bp.route("/export-stage2-india")
def export_stage2_india():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            data = json.load(f)
        stocks = data.get('stocks', [])
        if stocks:
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            tmp = os.path.join(UPLOAD_FOLDER, 'tmp_export_stage2_ind.csv')
            pd.DataFrame(stocks).to_csv(tmp, index=False)
            return send_file(tmp, as_attachment=True,
                             download_name=f"Stage2_IND_{ts}.csv")
    return "No scan data.", 404