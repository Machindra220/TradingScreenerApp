"""
momentum_scanner.py

Nifty 500 Momentum Entry Scanner
Finds stocks passing ALL five entry conditions simultaneously:

  1. Stage 2 Uptrend   : price > EMA200, within 20% of 52-week high
  2. RS Strength       : RS percentile ≥ 70 vs Nifty 500 (^CRSLDX)
  3. Delivery Surge    : vol_ratio ≥ 2× 20-day avg on at least one of last 3 days
  4. ROC Momentum      : 21-day Rate of Change > 0 (price is advancing)
  5. Strong Close      : yesterday's close in top 40% of day range (closing_pct ≥ 0.60)

Only stocks passing ALL five are listed. The page is intentionally strict —
you want a short, high-conviction list (typically 5–20 stocks), not 200 candidates.
"""

import os
import json
import uuid
import threading
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file

from app.services.market_data_cache import ind_cache, latest_bar_date

momentum_scan_bp = Blueprint("momentum_scan", __name__)

_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'momentum_scanner')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_momentum_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_momentum.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_momentum.json')
DEFAULT_IND_CSV = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
DEFAULT_IND_LABEL = "Nifty 500 Default (nifty_500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT = 5
PRIMARY_BENCHMARK  = ("^CRSLDX", "Nifty 500")
FALLBACK_BENCHMARK = ("^NSEI",   "Nifty 50")

# Entry condition thresholds
RS_MIN_PERCENTILE = 70     # RS must be in top 30% vs Nifty 500
VOL_SURGE_MIN     = 2.0    # volume ≥ 2× 20-day avg on recent day
CLOSING_PCT_MIN   = 0.60   # close in top 40% of day range
PULLBACK_MAX      = 0.20   # within 20% of 52-week high
ROC_WINDOW        = 21     # sessions for ROC

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
# Source-selection
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
        raise ValueError(f"File must have a Symbol or Ticker column.")
    sec_col = next((col_map[k] for k in ('industry', 'sector', 'gics sector') if k in col_map), None)
    results = []
    for _, row in df.iterrows():
        sym = str(row[found]).strip().upper()
        sec = str(row[sec_col]).strip() if sec_col else 'Unknown'
        if sym:
            yf_sym = sym if sym.endswith('.NS') else f"{sym}.NS"
            results.append({'yf_sym': yf_sym, 'clean': sym, 'sector': sec})
    return results


# ---------------------------------------------------------------------------
# Schema normalisation
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('symbol',          '')
    s.setdefault('sector',          '')
    s.setdefault('price',           0.0)
    s.setdefault('pullback_pct',    0.0)
    s.setdefault('rs_percentile',   0)
    s.setdefault('vol_ratio',       1.0)
    s.setdefault('roc_21d',         0.0)
    s.setdefault('closing_pct',     0.0)
    s.setdefault('ema200',          0.0)
    s.setdefault('above_ema200',    True)
    s.setdefault('high_vol_alert',  False)
    s.setdefault('conditions_met',  5)
    s.setdefault('rank',            0)
    s.setdefault('rs_h',            [])
    s.setdefault('rs_up',           False)
    s.setdefault('rank_diff',       0)
    s.setdefault('rank_status',     'stable')
    s.setdefault('signal_strength', 'Strong')
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
# Entry condition checks
# ---------------------------------------------------------------------------

def _signal_strength(rs_pct, vol_ratio, roc_21d, closing_pct):
    """Rate the signal strength based on how far above each threshold the stock is."""
    score = 0
    if rs_pct >= 90:    score += 3
    elif rs_pct >= 80:  score += 2
    else:               score += 1
    if vol_ratio >= 4:  score += 3
    elif vol_ratio >= 3: score += 2
    else:               score += 1
    if roc_21d >= 10:   score += 2
    elif roc_21d >= 5:  score += 1
    if closing_pct >= 80: score += 2
    elif closing_pct >= 70: score += 1
    if score >= 9:      return "🔥 Excellent"
    if score >= 7:      return "⚡ Very Strong"
    if score >= 5:      return "✅ Strong"
    return "📈 Qualifying"


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    try:
        tickers = _read_ticker_file(source_path)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return
    if not tickers:
        _set_progress(active=False, stage="error", error="No valid symbols found.")
        return

    yf_symbols = [t['yf_sym'] for t in tickers]
    sym_meta   = {t['yf_sym']: t for t in tickers}

    _set_progress(stage="fetching_benchmark", total=len(yf_symbols))

    # Fetch benchmark once
    bench_close, benchmark_label = None, None
    for ticker, label in (PRIMARY_BENCHMARK, FALLBACK_BENCHMARK):
        data, _ = ind_cache.get_price_history_bulk([ticker], interval='1d', lookback_days=300)
        df = data.get(ticker)
        if df is not None and not df.empty and len(df) >= 200:
            bench_close     = df['Close'].dropna()
            benchmark_label = f"{label} ({ticker})"
            break
    if bench_close is None:
        _set_progress(active=False, stage="error", error="Could not fetch benchmark.")
        return

    # Bulk-fetch via shared IND cache
    def _fp(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = ind_cache.get_price_history_bulk(
        yf_symbols, interval='1d', lookback_days=300, progress_callback=_fp
    )
    price_data_asof = latest_bar_date(price_data)

    _n, _ch, _yf, _fl = len(yf_symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    print(f"[Momentum Scan] {source_name}: {_n} symbols | Cache: {_ch} | Fetched: {_yf} | Failed: {len(_fl)}")

    # Load previous RS history for trend tracking + rank delta
    old_ranks   = {}
    existing_rs = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                old = json.load(f)
            for s in old.get('stocks', []):
                old_ranks[s['symbol']]   = s.get('rank', 0)
                existing_rs[s['symbol']] = s.get('rs_h', [])
        except (json.JSONDecodeError, OSError):
            pass

    _set_progress(stage="screening", processed=0, total=len(yf_symbols), current_symbol="")

    raw_pass = []     # raw RS values for percentile ranking
    raw_all  = []     # all candidate metrics before percentile filter

    for i, yf_sym in enumerate(yf_symbols):
        _set_progress(processed=i, current_symbol=yf_sym)
        meta = sym_meta[yf_sym]
        df   = price_data.get(yf_sym)
        if df is None or df.empty:
            continue
        try:
            if not {'Close', 'High', 'Low', 'Volume'}.issubset(df.columns):
                continue
            close  = df['Close'].dropna()
            high   = df['High'].dropna()
            low    = df['Low'].dropna()
            volume = df['Volume'].dropna()

            if len(close) < 200 or len(volume) < 22:
                continue

            current_price = float(close.iloc[-1])
            ema200        = close.ewm(span=200, adjust=False).mean().iloc[-1]
            high_52w      = float(close.max())
            pullback      = (high_52w - current_price) / high_52w

            # Condition 1: Stage-2 — above EMA200, within PULLBACK_MAX of 52-week high
            if current_price <= ema200 or pullback > PULLBACK_MAX:
                continue

            # Condition 4: ROC 21D > 0
            if len(close) < ROC_WINDOW + 1:
                continue
            roc_21d = ((current_price / float(close.iloc[-(ROC_WINDOW+1)])) - 1) * 100
            if roc_21d <= 0:
                continue

            # Condition 3: Delivery surge — check last 3 days
            avg_20d_vol = float(volume.iloc[-21:-1].mean())
            recent_vols = [float(volume.iloc[-k]) for k in range(1, 4) if len(volume) >= k]
            vol_ratios  = [v / avg_20d_vol for v in recent_vols if avg_20d_vol > 0]
            max_vol_ratio = max(vol_ratios) if vol_ratios else 0
            if max_vol_ratio < VOL_SURGE_MIN:
                continue
            # Use the most recent day's vol_ratio for display
            today_vol_ratio = round(float(volume.iloc[-1]) / avg_20d_vol, 2) if avg_20d_vol > 0 else 1.0

            # Condition 5: Strong close — yesterday (most recent complete day)
            day_high  = float(high.iloc[-1])
            day_low   = float(low.iloc[-1])
            day_range = day_high - day_low
            closing_pct = ((current_price - day_low) / day_range) if day_range > 0 else 0.5
            if closing_pct < CLOSING_PCT_MIN:
                continue

            # RS vs benchmark (ratio-of-relatives)
            bench_aligned = bench_close.reindex(close.index).ffill()
            if bench_aligned.isna().any():
                continue
            stock_ret = (current_price / float(close.iloc[0])) - 1
            bench_ret = (float(bench_aligned.iloc[-1]) / float(bench_aligned.iloc[0])) - 1
            if (1 + bench_ret) == 0:
                continue
            rs_raw = ((1 + stock_ret) / (1 + bench_ret)) - 1

            raw_all.append({
                "symbol":       meta['clean'],
                "sector":       meta['sector'],
                "price":        round(current_price, 2),
                "ema200":       round(ema200, 2),
                "pullback_pct": round(pullback * 100, 2),
                "vol_ratio":    today_vol_ratio,
                "max_vol_ratio": round(max_vol_ratio, 2),
                "high_vol_alert": max_vol_ratio >= 3.0,
                "roc_21d":      round(roc_21d, 2),
                "closing_pct":  round(closing_pct * 100, 1),
                "rs_raw":       rs_raw,
                "above_ema200": True,
            })
        except Exception as e:
            print(f"  Error {yf_sym}: {e}")

    _set_progress(processed=len(yf_symbols), current_symbol="")

    stocks = []
    if raw_all:
        df = pd.DataFrame(raw_all)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])

        # RS percentile ranked within all stocks that passed conditions 1,3,4,5
        # (before the RS threshold). This ensures percentile reflects the full
        # qualifying pool, not the final list only.
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)

        # Condition 2: RS percentile ≥ RS_MIN_PERCENTILE (applied AFTER ranking)
        df = df[df['rs_percentile'] >= RS_MIN_PERCENTILE]

        if not df.empty:
            df['conditions_met'] = 5   # all 5 conditions passed
            df['signal_strength'] = df.apply(
                lambda r: _signal_strength(r['rs_percentile'], r['max_vol_ratio'], r['roc_21d'], r['closing_pct']),
                axis=1
            )
            df.sort_values('rs_percentile', ascending=False, inplace=True)
            df.reset_index(drop=True, inplace=True)
            df['rank'] = df.index + 1

            def inject(row):
                sym = row['symbol']
                h   = existing_rs.get(sym, [])
                row['rs_h']  = (h + [row['rs_percentile']])[-5:]
                row['rs_up'] = len(row['rs_h']) > 1 and all(x < y for x, y in zip(row['rs_h'], row['rs_h'][1:]))
                prev = old_ranks.get(sym)
                if prev is None:
                    row['rank_status'], row['rank_diff'] = 'new', 0
                else:
                    diff = prev - row['rank']
                    row['rank_diff']   = diff
                    row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
                return row

            df = df.apply(inject, axis=1)
            stocks = df.to_dict(orient='records')

    last_time   = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    leaders_90  = [s['symbol'] for s in stocks if s.get('rs_percentile', 0) >= 90]

    payload = {
        'stocks':            stocks,
        'time':              last_time,
        'source':            source_name,
        'benchmark_label':   benchmark_label,
        'scanned_count':     len(yf_symbols),
        'passed_count':      len(stocks),
        'excluded_count':    len(yf_symbols) - len(stocks),
        'pre_rs_count':      len(raw_all),
        'stale_count':       len(_fl),
        'stale_sample':      [s.replace('.NS', '') for s in _fl[:10]],
        'price_data_asof':   price_data_asof,
        'cache_hits':        _ch,
        'yf_fetches':        _yf,
        'thresholds': {
            'rs_min':       RS_MIN_PERCENTILE,
            'vol_min':      VOL_SURGE_MIN,
            'closing_min':  CLOSING_PCT_MIN * 100,
            'pullback_max': PULLBACK_MAX * 100,
            'roc_window':   ROC_WINDOW,
        }
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
        "pre_rs_count":    len(raw_all),
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

@momentum_scan_bp.route("/momentum-scanner", methods=["GET", "POST"])
def momentum_scanner_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('momentum_scan.momentum_scanner_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_momentum_tickers{ext}"
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
            _set_progress(active=False, stage="error",
                          error=f"File not found: {DEFAULT_IND_CSV}. Place nifty_500.csv in data/.")
            return redirect(url_for('momentum_scan.momentum_scanner_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('momentum_scan.momentum_scanner_process', scanning=1))

    # GET
    cache = {}
    if os.path.exists(RESULTS_JSON):
        try:
            with open(RESULTS_JSON) as f:
                cache = json.load(f)
            cache['stocks'] = [_normalize_stock(s) for s in cache.get('stocks', [])]
        except (json.JSONDecodeError, OSError):
            pass

    history    = _load_history()
    progress   = _get_progress()
    is_scanning = progress["active"] or request.args.get('scanning') == '1'
    _, active_file, is_default_source = _get_active_source()

    return render_template(
        "momentum_scanner.html",
        stocks=cache.get('stocks', []),
        last_time=cache.get('time'),
        benchmark_label=cache.get('benchmark_label'),
        source_name=cache.get('source'),
        scanned_count=cache.get('scanned_count', 0),
        passed_count=cache.get('passed_count', 0),
        excluded_count=cache.get('excluded_count', 0),
        pre_rs_count=cache.get('pre_rs_count', 0),
        stale_count=cache.get('stale_count', 0),
        stale_sample=cache.get('stale_sample', []),
        price_data_asof=cache.get('price_data_asof'),
        cache_hits=cache.get('cache_hits', 0),
        yf_fetches=cache.get('yf_fetches', 0),
        thresholds=cache.get('thresholds', {}),
        history=history,
        active_file=active_file,
        is_default_source=is_default_source,
        default_label=DEFAULT_IND_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored')      == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@momentum_scan_bp.route("/momentum-scanner/progress")
def momentum_scanner_progress():
    return jsonify(_get_progress())


@momentum_scan_bp.route("/momentum-scanner/clear-source", methods=["POST"])
def momentum_scanner_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('momentum_scan.momentum_scanner_process'))


@momentum_scan_bp.route("/restore-momentum-scanner/<snapshot_file>", methods=["POST"])
def restore_momentum_scanner(snapshot_file):
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('momentum_scan.momentum_scanner_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('momentum_scan.momentum_scanner_process', restore_error=1))
    return redirect(url_for('momentum_scan.momentum_scanner_process', restored=1))


@momentum_scan_bp.route("/export-momentum-scanner")
def export_momentum_scanner():
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON) as f:
            stocks = json.load(f).get('stocks', [])
        if stocks:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = os.path.join(UPLOAD_FOLDER, 'temp_momentum_export.csv')
            pd.DataFrame(stocks).to_csv(temp_path, index=False)
            return send_file(temp_path, as_attachment=True,
                             download_name=f"IND_Momentum_Scanner_{timestamp}.csv")
    return "No scan data available.", 404
