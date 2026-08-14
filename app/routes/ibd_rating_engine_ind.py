import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, send_file, redirect, url_for, jsonify

from app.services.market_data_cache import ind_cache, latest_bar_date  # shared IND SQLite cache

ibd_engine_ind_bp = Blueprint("ibd_engine_ind", __name__)

# Anchor all paths to __file__ — os.getcwd() at module level breaks after
# Werkzeug hot-reload (Memory #12).
_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'ibd_india')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_ibd_india_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_ibd_india.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_ibd_india.json')
SECTOR_CACHE    = os.path.join(_PROJECT_ROOT, 'instance', 'sector_cache.json')
DEFAULT_IND_CSV = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER,                            exist_ok=True)
os.makedirs(SNAPSHOT_DIR,                             exist_ok=True)
os.makedirs(os.path.join(_PROJECT_ROOT, 'instance'), exist_ok=True)

HISTORY_LIMIT = 5

# Benchmark: Nifty 500 is the correct broad-market index for RS vs the Nifty 500
# universe this screener uses. Fall back to Nifty 50 (^NSEI) if ^CRSLDX unavailable.
PRIMARY_BENCHMARK  = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI",   "Nifty 50")

# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_PROG = {"active": False, "processed": 0, "total": 0,
         "current_symbol": "", "stage": "idle", "error": None}

def _set_progress(**kw):
    with _lock: _PROG.update(kw)

def _get_progress():
    with _lock: return dict(_PROG)


# ---------------------------------------------------------------------------
# Source-selection (Memory #9 / #10)
# ---------------------------------------------------------------------------

def _get_active_source():
    """Returns (filepath, display_name, is_default)."""
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
    """Read CSV or XLSX — accept Symbol/Ticker column (case-insensitive)."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        df = pd.read_excel(filepath) if ext in ('.xlsx', '.xls') else pd.read_csv(filepath)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found   = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol or Ticker column. Found: {', '.join(df.columns.tolist())}")
    symbols = []
    for s in df[found].dropna().unique():
        sym = str(s).strip().upper()
        if sym and sym not in ('', 'NAN'):
            # Ensure .NS suffix — avoid double-suffix
            yf_sym = sym if sym.endswith('.NS') else f"{sym}.NS"
            symbols.append(yf_sym)
    return symbols


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('symbol',           '')
    s.setdefault('price',            0.0)
    s.setdefault('composite_rating', 0)
    s.setdefault('rs_percentile',    0)
    s.setdefault('rs_raw',           0.0)
    s.setdefault('eps_rating',       50)
    s.setdefault('ad_rating',        'C')
    s.setdefault('rs_line_up',       False)
    s.setdefault('high_vol_alert',   False)
    s.setdefault('vol_ratio',        1.0)
    s.setdefault('serial_no',        0)
    s.setdefault('rs_h',             [])
    s.setdefault('comp_h',           [])
    s.setdefault('rs_up',            False)
    s.setdefault('rank_diff',        0)
    s.setdefault('rank_status',      'stable')
    return s


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _prune_snapshots(keep_filenames):
    keep = set(keep_filenames)
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname not in keep:
            try: os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError: pass


# ---------------------------------------------------------------------------
# Sector / EPS cache (15-day TTL, shared with other IND screeners)
# ---------------------------------------------------------------------------

def _load_sector_cache():
    if os.path.exists(SECTOR_CACHE):
        try:
            with open(SECTOR_CACHE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_sector_cache(cache):
    try:
        with open(SECTOR_CACHE, 'w') as f:
            json.dump(cache, f)
    except OSError:
        pass


def _get_eps_rating_cached(yf_symbol, sector_cache):
    """
    Compute a 1-99 EPS proxy from yfinance earningsGrowth.
    Uses sector_cache.json (15-day TTL) so we don't hammer yfinance
    with .info calls on every scan for 500 symbols.

    Mapping:
      earningsGrowth ≥ 50% → 90-99
      ≥ 25%  → 75-89
      ≥ 10%  → 60-74
      0-10%  → 45-59
      negative → 20-44
      no data   → 50 (neutral, avoids dragging down valid stocks)
    """
    today  = datetime.now().strftime("%Y-%m-%d")
    cached = sector_cache.get(yf_symbol, {})

    if cached.get('eps_fetched') == today:
        return cached.get('eps_rating', 50)

    try:
        import yfinance as yf
        info = yf.Ticker(yf_symbol).info
        eg   = info.get('earningsGrowth')

        if eg is None:
            eps_score = 50
        elif eg >= 0.50:
            eps_score = 90 + min(int(eg * 20), 9)
        elif eg >= 0.25:
            eps_score = 75 + int((eg - 0.25) * 60)
        elif eg >= 0.10:
            eps_score = 60 + int((eg - 0.10) * 100)
        elif eg >= 0.0:
            eps_score = 45 + int(eg * 100)
        else:
            eps_score = max(20, 44 + int(eg * 40))

        eps_score = int(min(99, max(1, eps_score)))
        sector_cache.setdefault(yf_symbol, {})
        sector_cache[yf_symbol]['eps_rating'] = eps_score
        sector_cache[yf_symbol]['eps_fetched'] = today
    except Exception:
        eps_score = 50

    return eps_score


# ---------------------------------------------------------------------------
# IBD-style indicators
# ---------------------------------------------------------------------------

def _compute_rs_ratio(stock_close, bench_close):
    """
    True RS vs Nifty 500 — ratio-of-relatives (Memory #1).
    Old code ranked by raw 1-year return (perf_1y), not outperformance vs the index.
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


def _compute_ad_rating(close, open_, volume, lookback=20):
    """A/D rating A-E: up-volume vs down-volume over last 20 sessions."""
    if len(close) < lookback:
        return 'C'
    c = close.tail(lookback).values
    o = open_.reindex(close.index).tail(lookback).values
    v = volume.reindex(close.index).tail(lookback).values
    up_vol   = sum(v[i] for i in range(len(c)) if c[i] > o[i])
    down_vol = sum(v[i] for i in range(len(c)) if c[i] <= o[i])
    if down_vol == 0: return 'A'
    ratio = up_vol / down_vol
    if   ratio >= 1.6: return 'A'
    elif ratio >= 1.3: return 'B'
    elif ratio >= 0.9: return 'C'
    elif ratio >= 0.6: return 'D'
    else:              return 'E'


def _compute_rs_line_slope(stock_close, bench_close, window=20):
    """
    RS line slope using np.polyfit regression (Memory #8 — not naive 2-point delta).
    Positive slope = stock is gaining ground vs the benchmark — early leadership signal.
    """
    if len(stock_close) < window or len(bench_close) < window:
        return False, 0.0
    sc = stock_close.tail(window).values
    bc = bench_close.reindex(stock_close.index).tail(window).values
    if np.any(bc == 0) or np.any(np.isnan(bc)):
        return False, 0.0
    rs_line = sc / bc
    slope   = float(np.polyfit(np.arange(window), rs_line, 1)[0])
    return slope > 0, round(slope, 6)


def _check_high_volume(volume, lookback=20, threshold=1.5):
    """High-volume badge: today ≥ 1.5× 20-day average (institutional interest)."""
    if len(volume) < lookback + 1:
        return False, 1.0
    avg_vol  = float(volume.iloc[-(lookback+1):-1].mean())
    curr_vol = float(volume.iloc[-1])
    vol_ratio = round(curr_vol / avg_vol, 2) if avg_vol > 0 else 1.0
    return vol_ratio >= threshold, vol_ratio


def _compute_composite(rs_pct, eps_score, ad_grade):
    """
    Composite rating 1-99: RS 50% + EPS 35% + A/D 15%.
    RS is the single most important pillar (highest weight).
    """
    ad_map   = {'A': 95, 'B': 80, 'C': 60, 'D': 40, 'E': 20}
    ad_score = ad_map.get(ad_grade, 60)
    score    = (rs_pct * 0.50) + (eps_score * 0.35) + (ad_score * 0.15)
    return int(min(99, max(1, round(score))))


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    try:
        yf_symbols = _read_ticker_file(source_path)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return
    if not yf_symbols:
        _set_progress(active=False, stage="error", error="No valid symbols found.")
        return

    _set_progress(stage="fetching_benchmark", total=len(yf_symbols))

    # Fetch benchmark ONCE via shared cache — try Nifty 500, fall back to Nifty 50
    bench_close, benchmark_label = None, None
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        data, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=500)
        df = data.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            bench_close    = df['Close'].dropna()
            benchmark_label = f"{label} ({ticker})"
            break

    if bench_close is None:
        _set_progress(active=False, stage="error", error="Could not fetch benchmark.")
        return

    # Bulk-fetch all symbols via shared IND cache (Memory #8 + #13)
    def _fp(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=500, progress_callback=_fp
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(yf_symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] IBD IND {source_name}")
    print(f"{sep}")
    print(f"  Total: {_n}  |  Cache: {_ch} ({round(_ch/_n*100) if _n else 0}%)  |  Fetched: {_yf}  |  Failed: {len(_fl)}")
    print(f"  Benchmark: {benchmark_label}  |  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    # Load previous history for trend tracking + rank delta
    old_ranks    = {}
    existing_rs  = {}
    existing_comp = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                old = json.load(f)
            for s in old.get('stocks', []):
                sym = s['symbol']
                old_ranks[sym]    = s.get('serial_no', 0)
                existing_rs[sym]  = s.get('rs_h', [])
                existing_comp[sym] = s.get('comp_h', [])
        except (json.JSONDecodeError, OSError):
            pass

    sector_cache = _load_sector_cache()

    _set_progress(stage="scoring", processed=0, total=len(yf_symbols), current_symbol="")

    raw_candidates = []
    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        df = price_data.get(yf_sym)
        if df is None or df.empty:
            continue
        try:
            if 'Close' not in df.columns or 'Volume' not in df.columns:
                continue
            close  = df['Close'].dropna()
            volume = df['Volume'].dropna()

            if len(close) < 200:
                continue

            current_price = float(close.iloc[-1])
            ema200        = close.ewm(span=200, adjust=False).mean().iloc[-1]

            if current_price <= ema200:
                continue

            bench_aligned = bench_close.reindex(close.index).ffill()
            if bench_aligned.isna().any():
                continue

            rs_val = _compute_rs_ratio(close, bench_aligned)
            if rs_val is None:
                continue

            rs_up_slope, rs_slope = _compute_rs_line_slope(close, bench_aligned, window=20)

            open_ = df['Open'].dropna() if 'Open' in df.columns else pd.Series(dtype=float)
            ad    = _compute_ad_rating(close, open_, volume) if not open_.empty else 'C'

            is_high_vol, vol_ratio = _check_high_volume(volume, lookback=20, threshold=1.5)

            eps = _get_eps_rating_cached(yf_sym, sector_cache)

            # Strip .NS for display
            sym_clean = yf_sym.replace('.NS', '')

            raw_candidates.append({
                "symbol":         sym_clean,
                "yf_symbol":      yf_sym,
                "price":          round(current_price, 2),
                "rs_raw":         rs_val,
                "eps_rating":     eps,
                "ad_rating":      ad,
                "rs_line_up":     rs_up_slope,
                "rs_slope":       rs_slope,
                "high_vol_alert": is_high_vol,
                "vol_ratio":      vol_ratio,
            })
        except Exception as e:
            print(f"  Error {yf_sym}: {e}")

    _set_progress(processed=len(yf_symbols), current_symbol="")
    _save_sector_cache(sector_cache)

    stocks = []
    if raw_candidates:
        df = pd.DataFrame(raw_candidates)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])

        # RS percentile ranked within Stage-2 shortlist (Memory #1)
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df['composite_rating'] = df.apply(
            lambda r: _compute_composite(r['rs_percentile'], r['eps_rating'], r['ad_rating']), axis=1
        )

        df.sort_values('composite_rating', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['serial_no'] = df.index + 1

        def inject(row):
            sym = row['symbol']
            h   = existing_rs.get(sym, [])
            ch  = existing_comp.get(sym, [])
            row['rs_h']   = (h  + [row['rs_percentile']])[-5:]
            row['comp_h'] = (ch + [row['composite_rating']])[-5:]
            row['rs_up']  = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
            prev = old_ranks.get(sym)
            if prev is None:
                row['rank_status'], row['rank_diff'] = 'new', 0
            else:
                diff = prev - row['serial_no']
                row['rank_diff']   = diff
                row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
            return row

        df = df.apply(inject, axis=1)
        stocks = df.to_dict(orient='records')

    last_time   = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    leaders_90  = [s['symbol'] for s in stocks if s.get('rs_percentile', 0) >= 90]
    leaders_ad  = [s['symbol'] for s in stocks if s.get('ad_rating') in ('A', 'B')][:5]

    payload = {
        'stocks':               stocks,
        'time':                 last_time,
        'source':               source_name,
        'benchmark_label':      benchmark_label,
        'scanned_count':        len(yf_symbols),
        'passed_count':         len(stocks),
        'excluded_count':       len(yf_symbols) - len(stocks),
        'stale_symbols_count':  len(_fl),
        'stale_symbols_sample': [s.replace('.NS', '') for s in _fl[:10]],
        'price_data_asof':      price_data_asof,
        'cache_hits':           _ch,
        'yf_fetches':           _yf,
    }

    snapshot_filename = f"snapshot_{uuid.uuid4().hex}.json"
    with open(os.path.join(SNAPSHOT_DIR, snapshot_filename), 'w') as f:
        json.dump(payload, f)
    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    history = _load_history()
    history.insert(0, {
        "time":            last_time,
        "source":          source_name,
        "count":           len(stocks),
        "leaders_90":      leaders_90,
        "leaders_ad":      leaders_ad,
        "benchmark_label": benchmark_label,
        "price_data_asof": price_data_asof,
        "snapshot_file":   snapshot_filename,
    })
    history = history[:HISTORY_LIMIT]
    with open(HISTORY_JSON, 'w') as f:
        json.dump(history, f)

    _prune_snapshots([h['snapshot_file'] for h in history if h.get('snapshot_file')])
    _set_progress(active=False, stage="done", current_symbol="")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ibd_engine_ind_bp.route("/ibd-smartselect-india-scan", methods=["GET", "POST"])
def ibd_india_scan_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('ibd_engine_ind.ibd_india_scan_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_ibd_india_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_IND_CSV, DEFAULT_IND_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            err = f"Default file not found: {DEFAULT_IND_CSV}. Place nifty_500.csv in data/ or upload a file."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('ibd_engine_ind.ibd_india_scan_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('ibd_engine_ind.ibd_india_scan_process', scanning=1))

    # --- GET ---
    stocks = []
    last_time = benchmark_label = source_name = price_data_asof = None
    scanned_count = passed_count = excluded_count = 0
    stale_symbols_count, stale_symbols_sample = 0, []
    cache_hits = yf_fetches = None

    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            stocks               = [_normalize_stock(s) for s in cache.get('stocks', [])]
            last_time            = cache.get('time')
            benchmark_label      = cache.get('benchmark_label')
            source_name          = cache.get('source')
            scanned_count        = cache.get('scanned_count', 0)
            passed_count         = cache.get('passed_count', 0)
            excluded_count       = cache.get('excluded_count', 0)
            stale_symbols_count  = cache.get('stale_symbols_count', 0)
            stale_symbols_sample = cache.get('stale_symbols_sample', [])
            price_data_asof      = cache.get('price_data_asof')
            cache_hits           = cache.get('cache_hits', 0)
            yf_fetches           = cache.get('yf_fetches', 0)
        except (json.JSONDecodeError, OSError):
            pass

    history    = _load_history()
    progress   = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "ibd_smartselect_ind.html",
        stocks=stocks,
        last_time=last_time,
        benchmark_label=benchmark_label,
        source_name=source_name,
        scanned_count=scanned_count,
        passed_count=passed_count,
        excluded_count=excluded_count,
        stale_symbols_count=stale_symbols_count,
        stale_symbols_sample=stale_symbols_sample,
        price_data_asof=price_data_asof,
        cache_hits=cache_hits,
        yf_fetches=yf_fetches,
        history=history,
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_IND_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored')      == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@ibd_engine_ind_bp.route("/ibd-smartselect-india-scan/progress")
def ibd_india_progress():
    return jsonify(_get_progress())


@ibd_engine_ind_bp.route("/ibd-smartselect-india-scan/clear-source", methods=["POST"])
def ibd_india_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('ibd_engine_ind.ibd_india_scan_process'))


@ibd_engine_ind_bp.route("/restore-ibd-india/<snapshot_file>", methods=["POST"])
def restore_ibd_india(snapshot_file):
    """POST-only restore (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('ibd_engine_ind.ibd_india_scan_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('ibd_engine_ind.ibd_india_scan_process', restore_error=1))
    return redirect(url_for('ibd_engine_ind.ibd_india_scan_process', restored=1))


@ibd_engine_ind_bp.route("/export-ibd-india")
def export_ibd_india():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_ibd_india_export.csv')
            pd.DataFrame(stocks).to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"IND_IBD_SmartSelect_{timestamp}.csv")
    return "No scan data available.", 404