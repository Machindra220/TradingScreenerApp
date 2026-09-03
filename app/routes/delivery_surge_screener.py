"""
delivery_surge_screener.py

NSE Delivery Surge Screener — identifies stocks with abnormally high delivery
volume vs their 20-day average, combined with positive RS vs Nifty 500.

Key design decisions vs the original:
  - No SQLAlchemy DB — all results stored in JSON files (RESULTS_JSON, HISTORY_JSON)
  - No dependency on Stage2Stock model — ticker list from uploadable CSV / nifty_500.csv
  - Background thread + progress-on-button (Memory #3)
  - Shared ind_cache SQLite for price data (Memory #13)
  - RS = ratio-of-relatives vs Nifty 500 (Memory #1), NOT simple subtraction
  - 20-day avg volume window (was "all days except today")
  - Vol ratio badge at 2× threshold
  - Per-ticker delivery spike history (del_h) and RS history (rs_h) across last 5 scans
  - Snapshots + restore (Memory #5/#11)
  - Source selector: nifty_500.csv default + upload (Memory #9/#10)
  - History section embedded on the same page (no separate route)
"""

import os
import json
import uuid
import threading
import pandas as pd
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file

from app.services.market_data_cache import ind_cache, latest_bar_date

delivery_surge_bp = Blueprint("delivery_surge", __name__)

# Anchor all paths to __file__ (Memory #12)
_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'ind_delivery_surge')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_ind_delivery_surge_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_delivery.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_delivery.json')
DEFAULT_IND_CSV = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)


# ---------------------------------------------------------------------------
# DataFrame column normaliser — handles simple-string AND MultiIndex/tuple
# column formats returned by the shared cache bulk fetch. Without this,
# df['Close'] on a MultiIndex DataFrame crashes silently and skips symbols.
# ---------------------------------------------------------------------------

def _normalise_df(df, sym=None):
    if df is None or df.empty:
        return None
    cols = df.columns
    if isinstance(cols, pd.MultiIndex) or (len(cols) > 0 and isinstance(cols[0], tuple)):
        if sym is not None:
            for cand in [sym, sym.replace('.NS', ''), sym + '.NS']:
                try:
                    sliced = df.xs(cand, axis=1, level=1)
                    sliced.columns = [c.title() for c in sliced.columns]
                    return sliced
                except KeyError:
                    pass
        flat = {}
        for c in cols:
            field = c[0] if isinstance(c, tuple) else c
            if field.lower() in ('open', 'high', 'low', 'close', 'volume'):
                flat[c] = field.title()
        if flat:
            df = df[list(flat.keys())].copy()
            df.columns = list(flat.values())
            return df
        return None
    df = df.copy()
    df.columns = [
        c.title() if isinstance(c, str) and c.lower() in
        ('open', 'high', 'low', 'close', 'volume') else c
        for c in df.columns
    ]
    return df

HISTORY_LIMIT   = 5
DELIVERY_THRESHOLD = 2.0    # vol_ratio ≥ 2× 20-day avg to qualify as a surge
HIGH_VOL_BADGE  = 3.0       # vol_ratio ≥ 3× shown as "Surge 🔥"
ROC_WINDOW      = 21        # sessions for ROC and RS calculation

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
        raise ValueError(f"Could not read file: {e}")
    col_map = {c.lower(): c for c in df.columns}
    found   = next((col_map[k] for k in ('symbol', 'ticker', 'symbols', 'tickers') if k in col_map), None)
    if found is None:
        raise ValueError(f"File must have a Symbol or Ticker column. Found: {', '.join(df.columns.tolist())}")
    symbols = []
    for s in df[found].dropna().unique():
        sym = str(s).strip().upper()
        if sym:
            yf_sym = sym if sym.endswith('.NS') else f"{sym}.NS"
            symbols.append(yf_sym)
    return symbols


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('symbol',         '')
    s.setdefault('symbol_clean',   '')
    s.setdefault('price',          0.0)
    s.setdefault('price_change_pct', 0.0)
    s.setdefault('volume',         0)
    s.setdefault('vol_ratio',      1.0)
    s.setdefault('delivery_spike', 1.0)
    s.setdefault('roc_21d',        0.0)
    s.setdefault('rs_vs_index',    0.0)
    s.setdefault('rs_percentile',  0)
    s.setdefault('above_ema200',   False)
    s.setdefault('tag',            '')
    s.setdefault('high_vol_alert', False)
    s.setdefault('del_h',          [])
    s.setdefault('rs_h',           [])
    s.setdefault('del_up',         False)
    s.setdefault('rs_up',          False)
    s.setdefault('rank',           0)
    s.setdefault('rank_diff',      0)
    s.setdefault('rank_status',    'stable')
    return s


def _load_history():
    if os.path.exists(HISTORY_JSON):
        try:
            with open(HISTORY_JSON) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _load_scan_results():
    """Load the last scan results file and apply _normalize_stock to each row."""
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            cache['stocks'] = [_normalize_stock(s) for s in cache.get('stocks', [])]
            return cache
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _prune_snapshots(keep_filenames):
    keep = set(keep_filenames)
    for fname in os.listdir(SNAPSHOT_DIR):
        if fname not in keep:
            try: os.remove(os.path.join(SNAPSHOT_DIR, fname))
            except OSError: pass


# ---------------------------------------------------------------------------
# Formula helpers
# ---------------------------------------------------------------------------

def _compute_rs_ratio(stock_close, bench_close):
    """
    True RS vs benchmark — ratio-of-relatives (Memory #1).
    Old code: rs = roc - benchmark_roc (simple subtraction — wrong when bench is negative).
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


def _get_delivery_tag(vol_ratio):
    if vol_ratio >= 6: return "🔥 Extreme"
    if vol_ratio >= 4: return "🔥 Strong"
    if vol_ratio >= 3: return "⚡ High"
    return "📈 Surge"


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

    _set_progress(stage="fetching_benchmark", total=len(yf_symbols))

    # Fetch benchmark once via shared cache
    bench_close, benchmark_label = None, None
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        data, _ = ind_cache.get_price_history_bulk(
            [ticker], interval='1d', lookback_days=300,
            progress_callback=lambda *a: None,
        )
        df = _normalise_df(data.get(ticker), ticker)
        if df is not None and 'Close' in df.columns and len(df) >= ROC_WINDOW + 2:
            bench_close     = df['Close'].dropna()
            benchmark_label = f"{label} ({ticker})"
            break

    if bench_close is None:
        _set_progress(active=False, stage="error", error="Could not fetch benchmark.")
        return

    # Bulk-fetch all symbols via shared IND cache (Memory #8 + #13)
    def _fp(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=300, progress_callback=_fp
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(yf_symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] Delivery Surge IND — {source_name}")
    print(f"{sep}")
    print(f"  Total: {_n}  |  Cache: {_ch} ({round(_ch/_n*100) if _n else 0}%)  |  Fetched: {_yf}  |  Failed: {len(_fl)}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    # Load previous data for trend tracking + rank delta
    old_ranks  = {}
    existing_del = {}
    existing_rs  = {}
    all_rs_vals  = {}  # for percentile ranking
    prev = _load_scan_results()
    for s in prev.get('stocks', []):
        old_ranks[s['symbol_clean']]  = s.get('rank', 0)
        existing_del[s['symbol_clean']] = s.get('del_h', [])
        existing_rs[s['symbol_clean']]  = s.get('rs_h', [])

    _set_progress(stage="screening", processed=0, total=len(yf_symbols), current_symbol="")

    raw_results = []
    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        df = _normalise_df(price_data.get(yf_sym), yf_sym)
        if df is None or df.empty:
            continue
        try:
            close  = df['Close'].dropna()
            volume = df['Volume'].dropna()
            if len(close) < ROC_WINDOW + 2 or len(volume) < 22:
                continue

            current_price  = float(close.iloc[-1])
            prev_price     = float(close.iloc[-2]) if len(close) > 1 else current_price
            price_chg_pct  = round(((current_price - prev_price) / prev_price) * 100, 2)

            # 20-day avg volume (last 20 sessions, excluding today) — Memory #8
            avg_20d_vol    = float(volume.iloc[-21:-1].mean())
            current_vol    = float(volume.iloc[-1])
            vol_ratio      = round(current_vol / avg_20d_vol, 2) if avg_20d_vol > 0 else 1.0

            # Only proceed if delivery spike qualifies
            if vol_ratio < DELIVERY_THRESHOLD:
                continue

            # EMA200 (Stage-2 filter)
            ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
            above_ema200 = current_price > ema200

            # ROC 21D
            if len(close) < ROC_WINDOW + 1:
                continue
            roc_21d = round(((current_price / float(close.iloc[-(ROC_WINDOW+1)])) - 1) * 100, 2)

            # RS vs benchmark (ratio-of-relatives, Memory #1)
            bench_aligned = bench_close.reindex(close.index).ffill()
            if len(bench_aligned) < ROC_WINDOW + 1 or bench_aligned.isna().any():
                continue

            rs_val = _compute_rs_ratio(
                close.iloc[-(ROC_WINDOW+1):],
                bench_aligned.iloc[-(ROC_WINDOW+1):]
            )
            if rs_val is None:
                continue

            sym_clean = yf_sym.replace('.NS', '')

            raw_results.append({
                "symbol":          yf_sym,
                "symbol_clean":    sym_clean,
                "price":           round(current_price, 2),
                "price_change_pct": price_chg_pct,
                "volume":          int(current_vol),
                "vol_ratio":       vol_ratio,
                "delivery_spike":  vol_ratio,   # vol_ratio IS the delivery spike
                "roc_21d":         roc_21d,
                "rs_raw":          rs_val,
                "rs_vs_index":     round(rs_val * 100, 2),
                "above_ema200":    above_ema200,
                "high_vol_alert":  vol_ratio >= HIGH_VOL_BADGE,
                "tag":             _get_delivery_tag(vol_ratio),
            })
        except Exception as e:
            print(f"  Error {yf_sym}: {e}")

    _set_progress(processed=len(yf_symbols), current_symbol="")

    # RS percentile across all qualifying stocks
    stocks = []
    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df.sort_values('delivery_spike', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject(row):
            sc  = row['symbol_clean']
            dh  = existing_del.get(sc, [])
            rh  = existing_rs.get(sc, [])
            row['del_h'] = (dh + [row['delivery_spike']])[-5:]
            row['rs_h']  = (rh + [row['rs_percentile']])[-5:]
            row['del_up'] = len(row['del_h']) > 1 and all(x < y for x, y in zip(row['del_h'], row['del_h'][1:]))
            row['rs_up']  = len(row['rs_h'])  > 1 and all(x < y for x, y in zip(row['rs_h'],  row['rs_h'][1:]))
            prev_rank = old_ranks.get(sc)
            if prev_rank is None:
                row['rank_status'], row['rank_diff'] = 'new', 0
            else:
                diff = prev_rank - row['rank']
                row['rank_diff']   = diff
                row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
            return row

        df = df.apply(inject, axis=1)
        stocks = df.to_dict(orient='records')

    last_time   = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    today_str   = datetime.now().strftime("%Y-%m-%d")
    leaders_rs90 = [s['symbol_clean'] for s in stocks if s.get('rs_percentile', 0) >= 90]
    surge_strong = [s['symbol_clean'] for s in stocks if s.get('vol_ratio', 0) >= 4]

    payload = {
        'stocks':               stocks,
        'time':                 last_time,
        'date':                 today_str,
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
        "date":            today_str,
        "source":          source_name,
        "count":           len(stocks),
        "leaders_rs90":    leaders_rs90,
        "surge_strong":    surge_strong,
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

@delivery_surge_bp.route("/delivery-surge-screener", methods=["GET", "POST"])
def delivery_surge_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('delivery_surge.delivery_surge_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_delivery_tickers{ext}"
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
            return redirect(url_for('delivery_surge.delivery_surge_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('delivery_surge.delivery_surge_process', scanning=1))

    # --- GET ---
    cache = _load_scan_results()
    stocks              = cache.get('stocks', [])
    last_time           = cache.get('time')
    benchmark_label     = cache.get('benchmark_label')
    source_name         = cache.get('source')
    scanned_count       = cache.get('scanned_count', 0)
    passed_count        = cache.get('passed_count', 0)
    excluded_count      = cache.get('excluded_count', 0)
    stale_count         = cache.get('stale_symbols_count', 0)
    stale_sample        = cache.get('stale_symbols_sample', [])
    price_data_asof     = cache.get('price_data_asof')
    cache_hits          = cache.get('cache_hits', 0)
    yf_fetches          = cache.get('yf_fetches', 0)

    history    = _load_history()
    progress   = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "delivery_surge_screener.html",
        stocks=stocks,
        last_time=last_time,
        benchmark_label=benchmark_label,
        source_name=source_name,
        scanned_count=scanned_count,
        passed_count=passed_count,
        excluded_count=excluded_count,
        stale_count=stale_count,
        stale_sample=stale_sample,
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


@delivery_surge_bp.route("/delivery-surge-screener/progress")
def delivery_surge_progress():
    return jsonify(_get_progress())


@delivery_surge_bp.route("/delivery-surge-screener/clear-source", methods=["POST"])
def delivery_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('delivery_surge.delivery_surge_process'))


@delivery_surge_bp.route("/restore-delivery-surge/<snapshot_file>", methods=["POST"])
def restore_delivery_surge(snapshot_file):
    """POST-only restore (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('delivery_surge.delivery_surge_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('delivery_surge.delivery_surge_process', restore_error=1))
    return redirect(url_for('delivery_surge.delivery_surge_process', restored=1))


@delivery_surge_bp.route("/export-delivery-surge")
def export_delivery_surge():
    cache = _load_scan_results()
    stocks = cache.get('stocks', [])
    if stocks:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_path = os.path.join(UPLOAD_FOLDER, 'temp_delivery_export.csv')
        pd.DataFrame(stocks).to_csv(temp_path, index=False)
        return send_file(temp_path, as_attachment=True,
                         download_name=f"IND_DeliverySurge_{timestamp}.csv")
    return "No scan data available.", 404