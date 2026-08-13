import os
import json
import uuid
import threading
from datetime import datetime

import pandas as pd
from flask import Blueprint, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename

from app.services.minervini_trend_template_screener import screen_universe

minervini_bp = Blueprint("minervini_ind", __name__)

# --- PATH LOGIC — anchored to __file__, never os.getcwd() (unreliable
# under the Werkzeug reloader) ---
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(_PROJECT_ROOT, 'uploads', 'minervini_ind')
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_minervini_results.json')
LAST_CSV_CONFIG = os.path.join(UPLOAD_FOLDER, 'last_csv_path.json')
HISTORY_CACHE_DIR = os.path.join(UPLOAD_FOLDER, 'history_cache')
DEFAULT_NIFTY_CSV = os.path.join(_PROJECT_ROOT, 'data', 'nifty_500.csv')
BENCHMARK_SYMBOL = "^CRSLDX"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(HISTORY_CACHE_DIR, exist_ok=True)

# --- Background-scan state ---------------------------------------------
_progress_lock = threading.Lock()
_progress = {"current": 0, "total": 0, "percent": 0, "symbol": "", "running": False, "error": None}
_scan_lock = threading.Lock()  # prevents two scans running concurrently


def _reset_progress(total):
    with _progress_lock:
        _progress.update({"current": 0, "total": total, "percent": 0, "symbol": "", "running": True, "error": None})


def _update_progress(index, total, symbol):
    with _progress_lock:
        _progress.update({
            "current": index,
            "total": total,
            "percent": int((index / total) * 100) if total else 0,
            "symbol": symbol,
            "running": index < total,
        })


@minervini_bp.route("/minervini-ind/progress")
def minervini_progress():
    with _progress_lock:
        return jsonify(dict(_progress))


# --- Source resolution (three-mode: default / uploaded / pinned) -------

def _get_active_source():
    """Single source of truth for the POST handler, GET route, and
    template: (filepath, display_name, is_default)."""
    if os.path.exists(LAST_CSV_CONFIG):
        with open(LAST_CSV_CONFIG, 'r') as f:
            cfg = json.load(f)
        filepath = cfg.get('path')
        if filepath and os.path.exists(filepath):
            return filepath, cfg.get('name', os.path.basename(filepath)), False
    return DEFAULT_NIFTY_CSV, "Nifty 500 Default", True


def _load_symbols(filepath):
    df_input = pd.read_csv(filepath) if filepath.lower().endswith('.csv') else pd.read_excel(filepath)
    col_name = 'Symbol' if 'Symbol' in df_input.columns else 'symbol'
    return [str(s).strip().upper() + ".NS" for s in df_input[col_name].dropna().unique()]


# --- Scan execution (runs in a background thread) -----------------------

def _run_scan_and_save(filepath, source_name):
    symbols = _load_symbols(filepath)
    total = len(symbols)
    _reset_progress(total)

    old_stocks = []
    old_ranks = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            old_stocks = cache.get('stocks', [])
            old_ranks = {s.get('symbol_clean', s['symbol']): s['rank'] for s in old_stocks}
    old_rs_history = {s.get('symbol_clean', s['symbol']): s.get('rs_h', []) for s in old_stocks}

    results_df = screen_universe(
        symbols,
        benchmark_symbol=BENCHMARK_SYMBOL,
        progress_callback=_update_progress,
    )

    scanned_count = total
    insufficient_data_count = total - len(results_df)

    enriched = []
    if not results_df.empty:
        results_df = results_df.sort_values("rs_rating", ascending=False).reset_index(drop=True)
        results_df["rank"] = results_df.index + 1

        for _, row in results_df.iterrows():
            stock = row.to_dict()
            sym_clean = stock["symbol"].replace(".NS", "")
            stock["symbol_clean"] = sym_clean

            prev_rank = old_ranks.get(sym_clean)
            if prev_rank is None:
                stock["rank_status"], stock["rank_diff"] = "new", 0
            else:
                diff = prev_rank - stock["rank"]
                stock["rank_diff"] = diff
                stock["rank_status"] = "up" if diff > 0 else ("down" if diff < 0 else "stable")

            rs_h = (old_rs_history.get(sym_clean, []) + [int(stock["rs_rating"])])[-5:]
            stock["rs_h"] = rs_h
            stock["rs_up"] = len(rs_h) > 1 and all(x < y for x, y in zip(rs_h, rs_h[1:]))

            enriched.append(stock)

    last_processed_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    passed_count = sum(1 for s in enriched if s.get("passes_all"))
    rs80_count = sum(1 for s in enriched if s.get("rs_pass"))
    qualifiers = [s["symbol_clean"] for s in enriched if s.get("passes_all")]

    # Snapshot to disk (last 5 kept) — mirrors your other IND screeners'
    # history-cache pattern exactly.
    snapshot_id = f"snapshot_{uuid.uuid4().hex}"
    with open(os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json"), 'w') as f:
        json.dump(enriched, f)

    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(meta_history_file):
        with open(meta_history_file, 'r') as f:
            history_meta = json.load(f)
    history_meta.insert(0, {
        "snapshot_id": snapshot_id,
        "time": last_processed_time,
        "source": source_name,
        "count": scanned_count,
        "qualifiers_count": passed_count,
        "qualifiers": qualifiers,
    })
    history_meta = history_meta[:5]
    with open(meta_history_file, 'w') as f:
        json.dump(history_meta, f)

    with open(RESULTS_JSON, 'w') as f:
        json.dump({
            'stocks': enriched,
            'time': last_processed_time,
            'source': source_name,
            'scanned_count': scanned_count,
            'passed_count': passed_count,
            'rs80_count': rs80_count,
            'insufficient_data_count': insufficient_data_count,
        }, f)

    with _progress_lock:
        _progress.update({"percent": 100, "running": False})


def _threaded_scan(filepath, source_name):
    with _scan_lock:
        try:
            _run_scan_and_save(filepath, source_name)
        except Exception as e:
            with _progress_lock:
                _progress.update({"running": False, "error": str(e)})


# --- Routes ---------------------------------------------------------------

@minervini_bp.route("/minervini-ind", methods=["GET", "POST"])
def minervini_process():
    if request.method == "POST":
        # Quick synchronous action: unpin the uploaded source, no scan.
        if request.form.get('clear_source') == '1':
            if os.path.exists(LAST_CSV_CONFIG):
                os.remove(LAST_CSV_CONFIG)
            return redirect(url_for('minervini_ind.minervini_process'))

        file = request.files.get('file')
        use_default = request.form.get('use_default') == '1'

        if file and file.filename != '':
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            with open(LAST_CSV_CONFIG, 'w') as f:
                json.dump({'path': filepath, 'name': filename}, f)
            source_name = filename
        elif use_default:
            filepath, source_name = DEFAULT_NIFTY_CSV, "Nifty 500 Default"
        else:
            filepath, source_name, _ = _get_active_source()

        if not os.path.exists(filepath):
            return jsonify({"status": "error", "message": f"File not found: {filepath}"}), 404

        if _scan_lock.locked():
            return jsonify({"status": "already_running"}), 409

        thread = threading.Thread(target=_threaded_scan, args=(filepath, source_name))
        thread.daemon = True
        thread.start()

        # This is the AJAX entry point (template posts here via fetch(),
        # not a normal form submit) — the frontend then polls /progress and
        # reloads the page itself once the background thread finishes, so
        # a lightweight JSON ack is all that's needed here.
        return jsonify({"status": "started"})

    # --- GET: render whatever's currently cached ---
    stocks = []
    last_processed_time = None
    source_name = "None"
    scanned_count = passed_count = rs80_count = insufficient_data_count = 0

    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            cache = json.load(f)
            stocks = cache.get('stocks', [])
            last_processed_time = cache.get('time')
            source_name = cache.get('source', 'Cached Scan')
            scanned_count = cache.get('scanned_count', 0)
            passed_count = cache.get('passed_count', 0)
            rs80_count = cache.get('rs80_count', 0)
            insufficient_data_count = cache.get('insufficient_data_count', 0)

    compare_mode = request.args.get('compare') == 'true'
    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')
    history_meta = []
    if os.path.exists(meta_history_file):
        with open(meta_history_file, 'r') as f:
            history_meta = json.load(f)

    if compare_mode and len(history_meta) >= 3:
        qualifier_sets = [set(h.get('qualifiers', [])) for h in history_meta[:3]]
        consistent_symbols = set.intersection(*qualifier_sets) if qualifier_sets else set()
        for s in stocks:
            if s.get('symbol_clean') in consistent_symbols:
                s['is_consistent'] = True

    _, _, is_default_source = _get_active_source()

    return render_template(
        "minervini_screener_ind.html",
        stocks=stocks,
        last_processed_time=last_processed_time,
        source_name=source_name,
        is_default_source=is_default_source,
        history=history_meta,
        compare_mode=compare_mode,
        currency_symbol="₹",
        scanned_count=scanned_count,
        passed_count=passed_count,
        rs80_count=rs80_count,
        insufficient_data_count=insufficient_data_count,
    )


@minervini_bp.route("/restore-minervini-ind/<snapshot_id>")
def restore_minervini_snapshot(snapshot_id):
    snapshot_file_path = os.path.join(HISTORY_CACHE_DIR, f"{snapshot_id}.json")
    meta_history_file = os.path.join(HISTORY_CACHE_DIR, 'meta_history.json')

    if os.path.exists(snapshot_file_path):
        with open(snapshot_file_path, 'r') as f:
            restored_records = json.load(f)

        restored_time = datetime.now().strftime("%d-%b-%Y %H:%M:%S") + " (Restored Snapshot)"
        restored_source = "Snapshot"
        scanned_count = passed_count = 0

        if os.path.exists(meta_history_file):
            with open(meta_history_file, 'r') as f:
                meta_list = json.load(f)
            for m in meta_list:
                if m.get('snapshot_id') == snapshot_id:
                    restored_time = m.get('time') + " (Restored Snapshot)"
                    restored_source = m.get('source')
                    scanned_count = m.get('count', 0)
                    passed_count = m.get('qualifiers_count', 0)
                    break

        rs80_count = sum(1 for s in restored_records if s.get('rs_pass'))

        with open(RESULTS_JSON, 'w') as f:
            json.dump({
                'stocks': restored_records,
                'time': restored_time,
                'source': restored_source,
                'scanned_count': scanned_count,
                'passed_count': passed_count,
                'rs80_count': rs80_count,
                'insufficient_data_count': 0,
            }, f)

    return redirect(url_for('minervini_ind.minervini_process'))
