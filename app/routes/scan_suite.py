"""
scan_suite.py — Daily Scan Suite

Provides three automation modes:

MODE 1: One-click sequential runner (browser-driven)
  - User clicks "Run All" on the suite page
  - JavaScript POSTs each job in order, polls until done, waits 30s, starts next
  - Existing screener background threads do all the actual work
  - Zero changes to any existing screener

MODE 2: APScheduler auto-run (server-driven)
  - Runs the same sequence automatically at a configured time (e.g. 16:30 weekdays)
  - Only active if Flask process is running at the scheduled time
  - Enabled/disabled and configured from the suite page UI

MODE 3: Windows Task Scheduler / cron
  - External script (scan_runner.py in project root) calls Flask routes via HTTP
  - Works regardless of what the browser is doing
  - Instructions provided on the suite page
"""

import os
import json
import time
import threading
import logging
from datetime import datetime, time as dtime
from flask import Blueprint, render_template, request, jsonify

# APScheduler (optional — graceful fallback if not installed)
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_SCHEDULER = True
except ImportError:
    _HAS_SCHEDULER = False

scan_suite_bp = Blueprint("scan_suite", __name__)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SUITE_DIR    = os.path.join(_PROJECT_ROOT, 'uploads', 'scan_suite')
_CONFIG_PATH  = os.path.join(_SUITE_DIR, 'suite_config.json')
_LOG_PATH     = os.path.join(_SUITE_DIR, 'suite_run_log.json')
os.makedirs(_SUITE_DIR, exist_ok=True)

# ── Default suite config ──────────────────────────────────────────────────────
# Each job: {id, label, post_url, progress_url, form, enabled}
# 'form' is the extra POST body fields (besides csrf_token which is added at runtime)
DEFAULT_JOBS = [
    {
        "id":           "stage2_us",
        "label":        "Stage 2 US",
        "market":       "US",
        "post_url":     "/stage2-us",
        "progress_url": "/stage2-us/progress",
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "stage2_ind",
        "label":        "Stage 2 India",
        "market":       "IND",
        "post_url":     "/stage2-india",
        "progress_url": "/stage2-india/progress",
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "vcp_us",
        "label":        "VCP US",
        "market":       "US",
        "post_url":     "/vcp/process",
        "progress_url": "/vcp/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {"market": "US"},
        "enabled":      True,
    },
    {
        "id":           "vcp_ind",
        "label":        "VCP India",
        "market":       "IND",
        "post_url":     "/vcp/process",
        "progress_url": "/vcp/progress",
        "form":         {"market": "IND"},
        "enabled":      True,
    },
    {
        "id":           "trendline_us",
        "label":        "Trendline US",
        "market":       "US",
        "post_url":     "/trendline-scan",
        "progress_url": "/trendline-scan/progress",
        "form":         {"market": "US"},
        "enabled":      True,
    },
    {
        "id":           "trendline_ind",
        "label":        "Trendline India",
        "market":       "IND",
        "post_url":     "/trendline-scan",
        "progress_url": "/trendline-scan/progress",
        "form":         {"market": "INDIA"},
        "enabled":      True,
    },
    {
        "id":           "universal_us",
        "label":        "Universal US",
        "market":       "US",
        "post_url":     "/universal-screener",
        "progress_url": "/universal-screener/progress",
        "form":         {"market": "US"},
        "enabled":      True,
    },
    {
        "id":           "universal_ind",
        "label":        "Universal India",
        "market":       "IND",
        "post_url":     "/universal-screener",
        "progress_url": "/universal-screener/progress",
        "form":         {"market": "IND"},
        "enabled":      True,
    },
    {
        "id":           "ma_us",
        "label":        "MA Screener US",
        "market":       "US",
        "post_url":     "/ma-screener",
        "progress_url": "/ma-screener/progress",
        "form":         {"market": "US", "rules_json": "[]",
                         "scan_name": "Daily MA Suite",
                         "preset_key": "bullish_ema_alignment"},
        "enabled":      False,   # off by default — user picks a preset
    },
    {
        "id":           "ma_ind",
        "label":        "MA Screener India",
        "market":       "IND",
        "post_url":     "/ma-screener",
        "progress_url": "/ma-screener/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {"market": "IND", "rules_json": "[]",
                         "scan_name": "Daily MA Suite",
                         "preset_key": "bullish_ema_alignment"},
        "enabled":      False,
    },
    {
        "id":           "hh_hl_us",
        "label":        "HH-HL US",
        "market":       "US",
        "post_url":     "/hh-hl-us",
        "progress_url": "/hh-hl-us/progress",
        "running_key":  "running",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "hh_hl_ind",
        "label":        "HH-HL India",
        "market":       "IND",
        "post_url":     "/hh-hl-india",
        "progress_url": "/hh-hl-india/progress",
        "running_key":  "running",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "quant_us",
        "label":        "Quant US",
        "market":       "US",
        "post_url":     "/quant-screeners-us",
        "progress_url": "/quant-screeners-us/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "quant_ind",
        "label":        "Quant India",
        "market":       "IND",
        "post_url":     "/quant-screeners",
        "progress_url": "/quant-screeners/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "avolar_us",
        "label":        "Adaptive VOLAR US",
        "market":       "US",
        "post_url":     "/volar-us-adaptive",
        "progress_url": "/volar-us-adaptive/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "avolar_ind",
        "label":        "Adaptive VOLAR India",
        "market":       "IND",
        "post_url":     "/volar-ind-adaptive",
        "progress_url": "/volar-ind-adaptive/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "gap_us",
        "label":        "Gap Volume US",
        "market":       "US",
        "post_url":     "/gap-volume-scan",
        "progress_url": None,
        "running_key":  None,
        "async_scan":   False,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "gap_ind",
        "label":        "Gap Volume India",
        "market":       "IND",
        "post_url":     "/gap-volume-india-scan",
        "progress_url": None,
        "running_key":  None,
        "async_scan":   False,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "rs_roc_us",
        "label":        "RS-ROC US",
        "market":       "US",
        "post_url":     "/rs-roc-us-momentum",
        "progress_url": None,
        "running_key":  None,
        "async_scan":   False,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "rs_roc_ind",
        "label":        "RS-ROC India",
        "market":       "IND",
        "post_url":     "/rs-roc-momentum",
        "progress_url": None,
        "running_key":  None,
        "async_scan":   False,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "delivery_ind",
        "label":        "Delivery Surge India",
        "market":       "IND",
        "post_url":     "/delivery-surge-screener",
        "progress_url": "/delivery-surge-screener/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "volar_stage2_us",
        "label":        "VOLAR Stage 2 US",
        "market":       "US",
        "post_url":     "/volar-us",
        "progress_url": "/volar-us/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "volar_stage2_ind",
        "label":        "VOLAR Stage 2 India",
        "market":       "IND",
        "post_url":     "/volar-ind",
        "progress_url": "/volar-ind/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {},
        "enabled":      True,
    },
    {
        "id":           "staircase_us",
        "label":        "Staircase US",
        "market":       "US",
        "post_url":     "/staircase-screener",
        "progress_url": "/staircase-screener/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {"market": "US"},
        "enabled":      True,
    },
    {
        "id":           "staircase_ind",
        "label":        "Staircase India",
        "market":       "IND",
        "post_url":     "/staircase-screener",
        "progress_url": "/staircase-screener/progress",
        "running_key":  "active",
        "async_scan":   True,
        "form":         {"market": "IND"},
        "enabled":      True,
    },
]

DEFAULT_CONFIG = {
    "gap_seconds":       30,       # wait between each job
    "auto_enabled":      False,    # APScheduler auto-run
    "auto_hour":         16,       # 4 PM
    "auto_minute":       30,       # :30
    "auto_days":         "mon-fri",
    "jobs":              DEFAULT_JOBS,
}


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            # Merge with defaults (handles new keys added in updates)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            # Ensure all default jobs exist
            existing_ids = {j['id'] for j in cfg.get('jobs', [])}
            for dj in DEFAULT_JOBS:
                if dj['id'] not in existing_ids:
                    cfg['jobs'].append(dj)
            return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


def _save_config(cfg: dict):
    with open(_CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Run log ───────────────────────────────────────────────────────────────────

def _load_log() -> list:
    if os.path.exists(_LOG_PATH):
        try:
            with open(_LOG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _append_log(entry: dict):
    log_entries = _load_log()
    log_entries.insert(0, entry)
    log_entries = log_entries[:20]   # keep last 20 runs
    with open(_LOG_PATH, 'w') as f:
        json.dump(log_entries, f)


# ── Server-side sequential runner (used by APScheduler / API trigger) ─────────
# The browser JS runner does the same thing client-side via fetch().
# This server-side runner is used for scheduled / API-triggered runs.

_suite_lock = threading.Lock()
_suite_running = False


def _run_suite_server_side(base_url: str, csrf_token: str, triggered_by: str = "scheduler"):
    """
    Runs all enabled jobs sequentially, server-side.
    Used by APScheduler and the /suite/trigger-server-run API.
    base_url: the Flask app base URL e.g. http://localhost:5000
    csrf_token: a valid CSRF token obtained before the run starts
    """
    global _suite_running
    import urllib.request
    import urllib.parse

    cfg = _load_config()
    gap = int(cfg.get("gap_seconds", 30))
    jobs = [j for j in cfg.get("jobs", []) if j.get("enabled", True)]

    results = []
    started_at = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    for i, job in enumerate(jobs):
        jid    = job["id"]
        label  = job["label"]
        post_u = f"{base_url}{job['post_url']}"
        prog_u = f"{base_url}{job['progress_url']}"
        form   = dict(job.get("form", {}))
        form["csrf_token"] = csrf_token

        log.info(f"[Suite] Starting {label} ({i+1}/{len(jobs)})")

        # POST to start
        try:
            data = urllib.parse.urlencode(form).encode()
            req  = urllib.request.Request(post_u, data=data,
                                           headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                _ = resp.read()
        except Exception as e:
            log.warning(f"[Suite] POST failed for {label}: {e}")
            results.append({"job": jid, "label": label, "status": "post_failed", "error": str(e)})
            continue

        # Poll until done
        t0 = time.time()
        while time.time() - t0 < 900:   # max 15 min per job
            time.sleep(5)
            try:
                with urllib.request.urlopen(prog_u, timeout=10) as resp:
                    prog = json.loads(resp.read())
                if not prog.get("active", True):
                    stage = prog.get("stage", "done")
                    results.append({"job": jid, "label": label, "status": stage})
                    log.info(f"[Suite] {label} done — stage={stage}")
                    break
            except Exception:
                pass
        else:
            results.append({"job": jid, "label": label, "status": "timeout"})

        # Gap between jobs (except after last)
        if i < len(jobs) - 1:
            log.info(f"[Suite] Waiting {gap}s before next job…")
            time.sleep(gap)

    _append_log({
        "started_at":    started_at,
        "finished_at":   datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
        "triggered_by":  triggered_by,
        "jobs_run":      len(results),
        "results":       results,
    })
    with _suite_lock:
        global _suite_running
        _suite_running = False
    log.info("[Suite] All jobs complete.")


# ── APScheduler setup ─────────────────────────────────────────────────────────

_scheduler = None


def init_scheduler(app):
    """
    Call this from app/__init__.py after register_blueprint(scan_suite_bp).
    Only initialises if APScheduler is installed and auto_enabled=True in config.
    """
    global _scheduler
    if not _HAS_SCHEDULER:
        return

    cfg = _load_config()
    if not cfg.get("auto_enabled", False):
        return

    _scheduler = BackgroundScheduler()
    hour   = int(cfg.get("auto_hour", 16))
    minute = int(cfg.get("auto_minute", 30))
    days   = cfg.get("auto_days", "mon-fri")

    def _scheduled_run():
        global _suite_running
        with _suite_lock:
            if _suite_running:
                log.warning("[Suite] Scheduled run skipped — already running")
                return
            _suite_running = True

        # We need a CSRF token — in scheduled mode we bypass CSRF by using
        # the server-side runner which can be configured to skip CSRF if needed.
        # For simplicity, we call the run_scan functions directly.
        threading.Thread(target=_direct_run_all, daemon=True).start()

    _scheduler.add_job(
        _scheduled_run,
        trigger=CronTrigger(hour=hour, minute=minute, day_of_week=days),
        id="daily_scan_suite",
        replace_existing=True,
    )
    _scheduler.start()
    log.info(f"[Suite] APScheduler started — daily at {hour:02d}:{minute:02d} ({days})")


def _direct_run_all():
    """
    Calls each screener's run_scan() function directly (no HTTP, no CSRF).
    Used by the APScheduler so it works without a live browser session.
    """
    cfg  = _load_config()
    gap  = int(cfg.get("gap_seconds", 30))
    jobs = [j for j in cfg.get("jobs", []) if j.get("enabled", True)]

    results      = []
    started_at   = datetime.now().strftime("%d-%b-%Y %H:%M:%S")

    # Import run_scan functions lazily (avoids circular import at module load)
    from app.routes.stage2_screener_us import run_scan as rs2_us, _get_active_source as _s2us_src
    from app.routes.stage2_india       import run_scan as rs2_ind, _get_active_source as _s2ind_src
    from app.routes.vcp_screener       import run_scan as rvcp
    from app.routes.trendline_screener import run_scan as rtl, MARKET_CFG as TL_MARKET
    from app.routes.universal_screener import run_scan as runiv
    from app.routes.ma_screener        import run_scan as rma, PREDEFINED_SCANS

    _DIRECT = {
        "stage2_us":    lambda: threading.Thread(target=rs2_us, args=_s2us_src()[:2], daemon=True).start(),
        "stage2_ind":   lambda: threading.Thread(target=rs2_ind, args=_s2ind_src()[:2], daemon=True).start(),
        "vcp_us":       lambda: threading.Thread(target=rvcp, args=("US",), daemon=True).start(),
        "vcp_ind":      lambda: threading.Thread(target=rvcp, args=("IND",), daemon=True).start(),
        "trendline_us": lambda: threading.Thread(target=rtl,  args=("US",  TL_MARKET["US"]["default_label"]),  daemon=True).start(),
        "trendline_ind":lambda: threading.Thread(target=rtl,  args=("INDIA", TL_MARKET["INDIA"]["default_label"]), daemon=True).start(),
        "universal_us": lambda: threading.Thread(target=runiv, args=("US",  {"name":"Daily Suite","fixed_filters":{},"ma_rules":[]}), daemon=True).start(),
        "universal_ind":lambda: threading.Thread(target=runiv, args=("IND", {"name":"Daily Suite","fixed_filters":{},"ma_rules":[]}), daemon=True).start(),
        "ma_us":        lambda: threading.Thread(target=rma, args=("US",  PREDEFINED_SCANS.get("bullish_ema_alignment",{}).get("rules",[]), "Daily MA"), daemon=True).start(),
        "ma_ind":       lambda: threading.Thread(target=rma, args=("IND", PREDEFINED_SCANS.get("bullish_ema_alignment",{}).get("rules",[]), "Daily MA"), daemon=True).start(),
    }

    for i, job in enumerate(jobs):
        jid   = job["id"]
        label = job["label"]
        fn    = _DIRECT.get(jid)
        if fn is None:
            results.append({"job": jid, "label": label, "status": "no_handler"})
            continue

        log.info(f"[Suite] Starting {label} ({i+1}/{len(jobs)})")
        try:
            fn()  # starts background thread
        except Exception as e:
            log.warning(f"[Suite] {label} start failed: {e}")
            results.append({"job": jid, "label": label, "status": "error", "error": str(e)})
            continue

        # Poll the screener's own progress dict
        from app.routes import (stage2_screener_us, stage2_india,
                                vcp_screener, trendline_screener,
                                universal_screener, ma_screener)
        _PROG_FN = {
            "stage2_us":    stage2_screener_us._get_progress,
            "stage2_ind":   stage2_india._get_progress,
            "vcp_us":       vcp_screener._get,
            "vcp_ind":      vcp_screener._get,
            "trendline_us": trendline_screener._get,
            "trendline_ind":trendline_screener._get,
            "universal_us": universal_screener._get,
            "universal_ind":universal_screener._get,
            "ma_us":        ma_screener._get,
            "ma_ind":       ma_screener._get,
        }
        get_prog = _PROG_FN.get(jid)
        t0 = time.time()
        time.sleep(3)  # give thread a moment to start
        while get_prog and time.time() - t0 < 900:
            prog = get_prog()
            if not prog.get("active", True):
                results.append({"job": jid, "label": label, "status": prog.get("stage","done")})
                break
            time.sleep(5)
        else:
            results.append({"job": jid, "label": label, "status": "timeout"})

        log.info(f"[Suite] {label} done. Waiting {gap}s…")
        if i < len(jobs) - 1:
            time.sleep(gap)

    _append_log({
        "started_at":   started_at,
        "finished_at":  datetime.now().strftime("%d-%b-%Y %H:%M:%S"),
        "triggered_by": "scheduler",
        "jobs_run":     len(results),
        "results":      results,
    })
    with _suite_lock:
        global _suite_running
        _suite_running = False
    log.info("[Suite] Scheduled run complete.")


# ── Routes ────────────────────────────────────────────────────────────────────

@scan_suite_bp.route("/scan-suite")
def suite_view():
    cfg     = _load_config()
    run_log = _load_log()
    return render_template(
        "scan_suite.html",
        jobs          = cfg["jobs"],
        gap_seconds   = cfg["gap_seconds"],
        auto_enabled  = cfg.get("auto_enabled", False),
        auto_hour     = cfg.get("auto_hour", 16),
        auto_minute   = cfg.get("auto_minute", 30),
        auto_days     = cfg.get("auto_days", "mon-fri"),
        has_scheduler = _HAS_SCHEDULER,
        run_log       = run_log,
        scheduler_active = bool(_scheduler and _scheduler.running if _HAS_SCHEDULER else False),
    )


@scan_suite_bp.route("/scan-suite/save-config", methods=["POST"])
def suite_save_config():
    cfg = _load_config()
    cfg["gap_seconds"]  = int(request.form.get("gap_seconds", 30))
    cfg["auto_enabled"] = request.form.get("auto_enabled") == "1"
    cfg["auto_hour"]    = int(request.form.get("auto_hour", 16))
    cfg["auto_minute"]  = int(request.form.get("auto_minute", 30))
    cfg["auto_days"]    = request.form.get("auto_days", "mon-fri")

    # Job enable/disable
    for job in cfg["jobs"]:
        job["enabled"] = request.form.get(f"job_{job['id']}") == "1"

    _save_config(cfg)

    # Restart scheduler with new config if running
    global _scheduler
    if _HAS_SCHEDULER and _scheduler:
        try:
            _scheduler.remove_job("daily_scan_suite")
        except Exception:
            pass
        if cfg["auto_enabled"]:
            _scheduler.add_job(
                lambda: threading.Thread(target=_direct_run_all, daemon=True).start(),
                trigger=CronTrigger(
                    hour=cfg["auto_hour"],
                    minute=cfg["auto_minute"],
                    day_of_week=cfg["auto_days"],
                ),
                id="daily_scan_suite",
                replace_existing=True,
            )
            if not _scheduler.running:
                _scheduler.start()
        log.info(f"[Suite] Config saved. Auto={cfg['auto_enabled']} {cfg['auto_hour']:02d}:{cfg['auto_minute']:02d}")

    return jsonify({"status": "ok", "gap_seconds": cfg["gap_seconds"]})


@scan_suite_bp.route("/scan-suite/run-log")
def suite_run_log():
    return jsonify(_load_log())


@scan_suite_bp.route("/scan-suite/config")
def suite_config_json():
    return jsonify(_load_config())