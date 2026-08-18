"""
us_volume_surge_screener.py

US Volume Surge Screener — identifies S&P 500 stocks with abnormally high
institutional-quality volume vs their 20-day average, combined with positive
RS vs S&P 500.

US market context vs NSE delivery:
  NSE has a "delivery percentage" field (% of volume that was actual delivery,
  not intraday squared-off). US markets don't publish this separately — instead
  we approximate institutional-quality volume using two combined signals:

  1. Vol Ratio   : today's volume / 20-day avg volume  (same as IND)
  2. Closing Pct : (close - low) / (high - low) — must be ≥ 0.60 (top 40% of
                   the day's range). This filters out high-volume sell-offs and
                   keeps only days where the price CLOSED strong, which is the
                   institutional footprint equivalent of NSE's delivery %.

  A stock qualifying on both signals is classified as "Institutional Accumulation"
  — the closest US equivalent to NSE's delivery surge.

Additional US-specific indicators:
  - Price must be above EMA200 (Stage-2 uptrend filter)
  - RS vs ^GSPC using ratio-of-relatives (Memory #1)
  - ROC 21D (same as IND)
  - VWAP comparison (today's close vs VWAP — above = institutional buying)
  - Dollar volume (price × volume) — filters out illiquid micro-caps
"""

import os
import json
import uuid
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, send_file

from app.services.market_data_cache import us_cache, latest_bar_date  # US SQLite cache

us_vol_surge_bp = Blueprint("us_vol_surge", __name__)

# Anchor all paths to __file__ (Memory #12)
_PROJECT_ROOT   = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER   = os.path.join(_PROJECT_ROOT, 'uploads', 'us_volume_surge')
SNAPSHOT_DIR    = os.path.join(UPLOAD_FOLDER, 'snapshots')
RESULTS_JSON    = os.path.join(UPLOAD_FOLDER, 'last_us_vol_surge_results.json')
HISTORY_JSON    = os.path.join(UPLOAD_FOLDER, 'scan_history_us_vol_surge.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_us_vol_surge.json')
DEFAULT_US_CSV  = os.path.join(_PROJECT_ROOT, 'data', 'sp500.csv')
DEFAULT_US_LABEL = "S&P 500 Default (sp500.csv)"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_DIR,  exist_ok=True)

HISTORY_LIMIT      = 5
VOL_THRESHOLD      = 2.0   # vol_ratio ≥ 2× 20-day avg to qualify
HIGH_VOL_BADGE     = 3.0   # vol_ratio ≥ 3× shown as "Surge 🔥"
CLOSING_PCT_MIN    = 0.60  # close must be in top 40% of day's range (institutional close quality)
MIN_DOLLAR_VOL     = 5e6   # minimum $5M daily dollar volume (filters micro-caps)
ROC_WINDOW         = 21    # sessions for ROC and RS calculation
US_BENCHMARK       = ("^GSPC", "S&P 500")

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
    return DEFAULT_US_CSV, DEFAULT_US_LABEL, True


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
    return [str(s).strip().upper().replace('.', '-') for s in df[found].dropna().unique() if str(s).strip()]


# ---------------------------------------------------------------------------
# Schema normalisation (Memory #3)
# ---------------------------------------------------------------------------

def _normalize_stock(s):
    s.setdefault('symbol',           '')
    s.setdefault('price',            0.0)
    s.setdefault('price_change_pct', 0.0)
    s.setdefault('volume',           0)
    s.setdefault('vol_ratio',        1.0)
    s.setdefault('closing_pct',      0.0)
    s.setdefault('dollar_volume_m',  0.0)
    s.setdefault('roc_21d',          0.0)
    s.setdefault('rs_vs_index',      0.0)
    s.setdefault('rs_percentile',    0)
    s.setdefault('above_ema200',     False)
    s.setdefault('close_above_vwap', False)
    s.setdefault('tag',              '')
    s.setdefault('high_vol_alert',   False)
    s.setdefault('vol_h',            [])
    s.setdefault('rs_h',             [])
    s.setdefault('vol_up',           False)
    s.setdefault('rs_up',            False)
    s.setdefault('rank',             0)
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


def _load_scan_results():
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
    Ratio-of-relatives RS vs S&P 500 (Memory #1).
    (1+stock_ret)/(1+bench_ret) - 1
    """
    stock_ret = (stock_close.iloc[-1] / stock_close.iloc[0]) - 1
    bench_ret = (bench_close.iloc[-1] / bench_close.iloc[0]) - 1
    if (1 + bench_ret) == 0:
        return None
    return ((1 + stock_ret) / (1 + bench_ret)) - 1


def _compute_vwap(high, low, close, volume):
    """
    Intraday VWAP approximation using daily OHLCV.
    VWAP ≈ sum(typical_price × volume) / sum(volume)
    typical_price = (high + low + close) / 3
    """
    typical = (high + low + close) / 3
    total_vol = volume.sum()
    if total_vol == 0:
        return float(close.iloc[-1])
    return float((typical * volume).sum() / total_vol)


def _get_vol_tag(vol_ratio, closing_pct):
    """
    US-specific volume quality tags:
    - Both high vol AND strong close → Institutional Accumulation
    - High vol but weak close → Distribution (selling into strength)
    - Moderate vol with strong close → Quiet Accumulation
    """
    if vol_ratio >= 6 and closing_pct >= 0.70:
        return "🔥 Institutional"
    if vol_ratio >= 4 and closing_pct >= 0.60:
        return "🔥 Strong Accum"
    if vol_ratio >= 3 and closing_pct >= 0.60:
        return "⚡ Accumulation"
    if vol_ratio >= 2 and closing_pct >= 0.60:
        return "📈 Surge"
    return "📊 Vol Surge"


# ---------------------------------------------------------------------------
# Background scan
# ---------------------------------------------------------------------------

def run_scan(source_path, source_name):
    _set_progress(active=True, processed=0, total=0,
                  current_symbol="", stage="loading_symbols", error=None)

    try:
        symbols = _read_ticker_file(source_path)
    except ValueError as e:
        _set_progress(active=False, stage="error", error=str(e))
        return
    if not symbols:
        _set_progress(active=False, stage="error", error="No valid symbols found.")
        return

    _set_progress(stage="fetching_benchmark", total=len(symbols))

    # Fetch benchmark ONCE via shared US cache (Memory #8)
    bench_data, _ = us_cache.get_price_history_bulk(
        [US_BENCHMARK[0]], interval='1d', lookback_days=300
    )
    bench_df = bench_data.get(US_BENCHMARK[0])
    if bench_df is None or bench_df.empty or len(bench_df) < ROC_WINDOW + 2:
        _set_progress(active=False, stage="error",
                      error=f"Could not fetch benchmark {US_BENCHMARK[0]}.")
        return
    bench_close = bench_df['Close'].dropna()
    # Normalise to tz-naive DatetimeIndex so reindex() matches stock indexes
    # from the SQLite cache (which are always tz-naive date strings).
    # tz-aware vs tz-naive comparison in reindex() returns ALL NaN —
    # that single .isna().any() check then kills every symbol.
    if hasattr(bench_close.index, 'tz') and bench_close.index.tz is not None:
        bench_close.index = bench_close.index.tz_localize(None)
    benchmark_label = f"{US_BENCHMARK[1]} ({US_BENCHMARK[0]})"

    # Bulk-fetch all symbols via shared US cache (Memory #13)
    def _fp(i, total, sym):
        _set_progress(stage="fetching_prices", processed=i, total=total, current_symbol=sym)

    price_data, fetch_report = us_cache.get_price_history_bulk(
        symbols, interval='1d', lookback_days=300, progress_callback=_fp
    )
    price_data_asof = latest_bar_date(price_data)

    # Cache source log (Memory #13)
    _n, _ch, _yf, _fl = len(symbols), fetch_report['from_cache'], fetch_report['fetched'], fetch_report['failed']
    sep = "=" * 55
    print(f"\n{sep}")
    print(f"  [CACHE] US Vol Surge — {source_name}")
    print(f"{sep}")
    print(f"  Total: {_n}  |  Cache: {_ch} ({round(_ch/_n*100) if _n else 0}%)  |  Fetched: {_yf}  |  Failed: {len(_fl)}")
    print(f"  Price data as of: {price_data_asof}")
    print(f"{sep}\n")

    # Load previous data for trend tracking + rank delta
    old_ranks = {}
    existing_vol = {}
    existing_rs  = {}
    prev = _load_scan_results()
    for s in prev.get('stocks', []):
        sym = s['symbol']
        old_ranks[sym]    = s.get('rank', 0)
        existing_vol[sym] = s.get('vol_h', [])
        existing_rs[sym]  = s.get('rs_h', [])

    _set_progress(stage="screening", processed=0, total=len(symbols), current_symbol="")

    raw_results = []
    for i, sym in enumerate(symbols):
        _set_progress(processed=i, current_symbol=sym)
        df = price_data.get(sym)
        if df is None or df.empty:
            continue
        try:
            required_cols = {'Close', 'Open', 'High', 'Low', 'Volume'}
            if not required_cols.issubset(df.columns):
                continue

            close  = df['Close'].dropna()
            high   = df['High'].dropna()
            low    = df['Low'].dropna()
            volume = df['Volume'].dropna()

            if len(close) < max(ROC_WINDOW + 2, 22):
                continue

            current_price = float(close.iloc[-1])
            current_high  = float(high.iloc[-1])
            current_low   = float(low.iloc[-1])
            current_vol   = float(volume.iloc[-1])

            # Dollar volume filter — removes illiquid stocks (< $5M daily)
            dollar_vol = current_price * current_vol
            if dollar_vol < MIN_DOLLAR_VOL:
                continue

            # 20-day avg volume (last 20 sessions, excluding today)
            avg_20d_vol = float(volume.iloc[-21:-1].mean())
            vol_ratio   = round(current_vol / avg_20d_vol, 2) if avg_20d_vol > 0 else 1.0

            # Only proceed if volume qualifies
            if vol_ratio < VOL_THRESHOLD:
                continue

            # Closing percentage — must close in top 40% of day's range
            # This is the US equivalent of NSE delivery %; it filters out
            # high-volume sell-offs and retains only institutional accumulation days
            day_range    = current_high - current_low
            closing_pct  = round((current_price - current_low) / day_range, 3) if day_range > 0 else 0.5

            if closing_pct < CLOSING_PCT_MIN:
                continue

            # EMA200 Stage-2 filter
            ema200       = close.ewm(span=200, adjust=False).mean().iloc[-1]
            above_ema200 = current_price > ema200

            # Price change vs previous close
            prev_price    = float(close.iloc[-2]) if len(close) > 1 else current_price
            price_chg_pct = round(((current_price - prev_price) / prev_price) * 100, 2)

            # ROC 21D
            roc_21d = round(((current_price / float(close.iloc[-(ROC_WINDOW+1)])) - 1) * 100, 2)

            # RS vs S&P 500 (ratio-of-relatives, Memory #1)
            #
            # Robust alignment — two bugs fixed vs original:
            #
            # Bug A (critical): bench_close from yfinance/cache may have a
            # tz-aware index while close has a tz-naive index (or vice versa).
            # reindex() requires EXACT index match — tz-aware != tz-naive even
            # for the same date, so ALL rows come back NaN and .isna().any()
            # skips every symbol. Fix: strip tz from close before reindex.
            #
            # Bug B (secondary): ffill() fills forward only. If the benchmark
            # starts 1 day before the stock's first date, the first row stays
            # NaN → .isna().any() = True → symbol skipped even though 99%
            # of rows are valid. Fix: use ffill().bfill() and check overlap %.
            close_idx = close.index
            if hasattr(close_idx, 'tz') and close_idx.tz is not None:
                close_idx = close_idx.tz_localize(None)
            close_normalised = close.copy()
            close_normalised.index = close_idx

            bench_aligned = bench_close.reindex(close_normalised.index).ffill().bfill()

            # Require at least ROC_WINDOW+1 valid (non-NaN) bars to compute RS
            valid_bars = bench_aligned.notna().sum()
            if valid_bars < ROC_WINDOW + 1:
                continue

            rs_val = _compute_rs_ratio(
                close_normalised.iloc[-(ROC_WINDOW+1):],
                bench_aligned.iloc[-(ROC_WINDOW+1):]
            )
            if rs_val is None:
                continue

            # VWAP approximation (20-day lookback)
            vwap_20d = _compute_vwap(
                high.tail(20), low.tail(20),
                close.tail(20), volume.tail(20)
            )
            close_above_vwap = current_price > vwap_20d

            # Dollar volume in millions for display
            dollar_vol_m = round(dollar_vol / 1e6, 1)

            raw_results.append({
                "symbol":           sym,
                "price":            round(current_price, 2),
                "price_change_pct": price_chg_pct,
                "volume":           int(current_vol),
                "vol_ratio":        vol_ratio,
                "closing_pct":      round(closing_pct * 100, 1),  # display as %
                "dollar_volume_m":  dollar_vol_m,
                "roc_21d":          roc_21d,
                "rs_raw":           rs_val,
                "rs_vs_index":      round(rs_val * 100, 2),
                "above_ema200":     above_ema200,
                "close_above_vwap": close_above_vwap,
                "high_vol_alert":   vol_ratio >= HIGH_VOL_BADGE,
                "tag":              _get_vol_tag(vol_ratio, closing_pct),
            })
        except Exception as e:
            print(f"  Error {sym}: {e}")

    _set_progress(processed=len(symbols), current_symbol="")

    # RS percentile across all qualifying stocks + rank/history injection
    stocks = []
    if raw_results:
        df = pd.DataFrame(raw_results)
        df['rs_raw'] = pd.to_numeric(df['rs_raw'], errors='coerce')
        df = df.dropna(subset=['rs_raw'])
        df['rs_percentile'] = df['rs_raw'].rank(pct=True).mul(100).round(0).fillna(0).astype(int)
        df.sort_values('vol_ratio', ascending=False, inplace=True)
        df.reset_index(drop=True, inplace=True)
        df['rank'] = df.index + 1

        def inject(row):
            sym = row['symbol']
            vh  = existing_vol.get(sym, [])
            rh  = existing_rs.get(sym, [])
            row['vol_h'] = (vh + [row['vol_ratio']])[-5:]
            row['rs_h']  = (rh + [row['rs_percentile']])[-5:]
            row['vol_up'] = len(row['vol_h']) > 1 and all(x < y for x, y in zip(row['vol_h'], row['vol_h'][1:]))
            row['rs_up']  = len(row['rs_h'])  > 1 and all(x < y for x, y in zip(row['rs_h'],  row['rs_h'][1:]))
            prev_rank = old_ranks.get(sym)
            if prev_rank is None:
                row['rank_status'], row['rank_diff'] = 'new', 0
            else:
                diff = prev_rank - row['rank']
                row['rank_diff']   = diff
                row['rank_status'] = 'up' if diff > 0 else ('down' if diff < 0 else 'stable')
            return row

        df = df.apply(inject, axis=1)
        stocks = df.to_dict(orient='records')

    last_time    = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    leaders_rs90 = [s['symbol'] for s in stocks if s.get('rs_percentile', 0) >= 90]
    surge_strong = [s['symbol'] for s in stocks if s.get('vol_ratio', 0) >= 4]
    accum_stocks = [s['symbol'] for s in stocks if 'Accum' in s.get('tag', '') or 'Institutional' in s.get('tag', '')]

    payload = {
        'stocks':               stocks,
        'time':                 last_time,
        'date':                 datetime.now().strftime("%Y-%m-%d"),
        'source':               source_name,
        'benchmark_label':      benchmark_label,
        'scanned_count':        len(symbols),
        'passed_count':         len(stocks),
        'excluded_count':       len(symbols) - len(stocks),
        'stale_symbols_count':  len(_fl),
        'stale_symbols_sample': _fl[:10],
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
        "leaders_rs90":    leaders_rs90,
        "surge_strong":    surge_strong,
        "accum_stocks":    accum_stocks[:5],
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

@us_vol_surge_bp.route("/us-volume-surge-screener", methods=["GET", "POST"])
def us_vol_surge_process():
    if request.method == "POST":
        if _get_progress()["active"]:
            return redirect(url_for('us_vol_surge.us_vol_surge_process', scanning=1))

        file        = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            from werkzeug.utils import secure_filename
            filename      = secure_filename(file.filename)
            ext           = os.path.splitext(filename)[1].lower()
            save_filename = f"uploaded_us_vol_surge_tickers{ext}"
            filepath      = os.path.join(UPLOAD_FOLDER, save_filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_path, source_name = filepath, filename
        elif use_default:
            source_path, source_name = DEFAULT_US_CSV, DEFAULT_US_LABEL
        else:
            source_path, source_name, _ = _get_active_source()

        if not source_path or not os.path.exists(source_path):
            err = f"Default file not found: {DEFAULT_US_CSV}. Place sp500.csv in data/ or upload a file."
            _set_progress(active=False, stage="error", error=err)
            return redirect(url_for('us_vol_surge.us_vol_surge_process'))

        thread = threading.Thread(target=run_scan, args=(source_path, source_name), daemon=True)
        thread.start()
        return redirect(url_for('us_vol_surge.us_vol_surge_process', scanning=1))

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
        "us_volume_surge_screener.html",
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
        default_label=DEFAULT_US_LABEL,
        is_scanning=is_scanning,
        scan_error=progress.get("error"),
        restored=request.args.get('restored')      == '1',
        restore_error=request.args.get('restore_error') == '1',
    )


@us_vol_surge_bp.route("/us-volume-surge-screener/progress")
def us_vol_surge_progress():
    return jsonify(_get_progress())


@us_vol_surge_bp.route("/us-volume-surge-screener/clear-source", methods=["POST"])
def us_vol_surge_clear_source():
    try:
        if os.path.exists(LAST_CSV_CONFIG):
            os.remove(LAST_CSV_CONFIG)
    except OSError:
        pass
    return redirect(url_for('us_vol_surge.us_vol_surge_process'))


@us_vol_surge_bp.route("/restore-us-vol-surge/<snapshot_file>", methods=["POST"])
def restore_us_vol_surge(snapshot_file):
    """POST-only restore (Memory #11)."""
    safe_name     = os.path.basename(snapshot_file)
    snapshot_path = os.path.join(SNAPSHOT_DIR, safe_name)
    valid = safe_name.startswith('snapshot_') and safe_name.endswith('.json') and os.path.exists(snapshot_path)
    if not valid:
        return redirect(url_for('us_vol_surge.us_vol_surge_process', restore_error=1))
    try:
        with open(snapshot_path) as f:
            payload = json.load(f)
        with open(RESULTS_JSON, 'w') as f:
            json.dump(payload, f)
    except (json.JSONDecodeError, OSError):
        return redirect(url_for('us_vol_surge.us_vol_surge_process', restore_error=1))
    return redirect(url_for('us_vol_surge.us_vol_surge_process', restored=1))


@us_vol_surge_bp.route("/export-us-vol-surge")
def export_us_vol_surge():
    cache = _load_scan_results()
    stocks = cache.get('stocks', [])
    if stocks:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_path = os.path.join(UPLOAD_FOLDER, 'temp_us_vol_surge_export.csv')
        pd.DataFrame(stocks).to_csv(temp_path, index=False)
        return send_file(temp_path, as_attachment=True,
                         download_name=f"US_VolumeSurge_{timestamp}.csv")
    return "No scan data available.", 404