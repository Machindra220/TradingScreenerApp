"""
scan_runner.py — Standalone Daily Scan Runner
Place in project root: D:\\Projects\\portfolio-app-2-Dev-Env\\scan_runner.py

Calls each screener via HTTP so Flask can be running separately.
Schedule with Windows Task Scheduler or cron.

Edit FLASK_URL and GAP_SECONDS below, then test by running:
    python scan_runner.py

Output is logged to scan_runner_log.txt in the same directory.
"""

import urllib.request
import urllib.parse
import urllib.error
import json
import time
import logging
import sys
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
FLASK_URL   = "http://localhost:5000"   # change if your app runs on a different port
GAP_SECONDS = 30                        # seconds to wait between jobs

# Edit this list to enable/disable individual jobs
# IMPORTANT: Trendline India uses 'INDIA' not 'IND' for the market field
JOBS = [
    {"label": "Stage 2 US",      "post": "/stage2-us",         "progress": "/stage2-us/progress",          "form": {}},
    {"label": "Stage 2 India",   "post": "/stage2-india",       "progress": "/stage2-india/progress",       "form": {}},
    {"label": "VCP US",          "post": "/vcp/process",        "progress": "/vcp/progress",                "form": {"market": "US"}},
    {"label": "VCP India",       "post": "/vcp/process",        "progress": "/vcp/progress",                "form": {"market": "IND"}},
    {"label": "Trendline US",    "post": "/trendline-scan",     "progress": "/trendline-scan/progress",     "form": {"market": "US"}},
    {"label": "Trendline India", "post": "/trendline-scan",     "progress": "/trendline-scan/progress",     "form": {"market": "INDIA"}},
    {"label": "Universal US",    "post": "/universal-screener", "progress": "/universal-screener/progress", "form": {"market": "US"}},
    {"label": "Universal India", "post": "/universal-screener", "progress": "/universal-screener/progress", "form": {"market": "IND"}},
]

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "scan_runner_log.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scan_runner")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(url: str, form: dict) -> bool:
    data = urllib.parse.urlencode(form).encode()
    req  = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.URLError as e:
        log.error(f"POST failed: {e}")
        return False


def _get_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_csrf_token() -> str:
    """
    Fetch a CSRF token from the Flask app.
    The /api/csrf-token endpoint returns {"csrf_token": "..."}.
    If your app doesn't have this endpoint, set SKIP_CSRF = True below.
    """
    data = _get_json(f"{FLASK_URL}/api/csrf-token")
    if data and "csrf_token" in data:
        return data["csrf_token"]
    return ""   # routes with @csrf.exempt won't need it


# ── Main runner ───────────────────────────────────────────────────────────────

def run_all():
    started = datetime.now()
    log.info("=" * 60)
    log.info(f"Scan suite started — {started:%Y-%m-%d %H:%M:%S}")
    log.info(f"Flask URL: {FLASK_URL}  |  Gap: {GAP_SECONDS}s")
    log.info("=" * 60)

    # Check Flask is reachable
    ping = _get_json(f"{FLASK_URL}/scan-suite/config")
    if ping is None:
        log.error("Flask app is not reachable. Start the Flask server first.")
        sys.exit(1)

    csrf = _get_csrf_token()
    if csrf:
        log.info(f"CSRF token obtained: {csrf[:12]}…")
    else:
        log.warning("No CSRF token obtained — routes must be @csrf.exempt or app uses session CSRF")

    results = []

    for i, job in enumerate(JOBS):
        label     = job["label"]
        post_url  = FLASK_URL + job["post"]
        prog_url  = FLASK_URL + job["progress"]
        form      = dict(job.get("form", {}))
        if csrf:
            form["csrf_token"] = csrf

        log.info(f"[{i+1}/{len(JOBS)}] Starting: {label}")

        # POST to trigger scan
        ok = _post(post_url, form)
        if not ok:
            log.error(f"  Failed to POST to {post_url}")
            results.append({"label": label, "status": "post_failed"})
            continue

        # Poll until done
        t0 = time.time()
        time.sleep(3)   # give thread a moment to start
        status = "timeout"
        while time.time() - t0 < 900:
            prog = _get_json(prog_url)
            if prog is None:
                time.sleep(5)
                continue
            active = prog.get("active", True)
            stage  = prog.get("stage", "")
            total  = prog.get("total", 0)
            done   = prog.get("processed", 0)
            pct    = round(done / total * 100) if total > 0 else 0
            log.info(f"  {label}: stage={stage} {done}/{total} ({pct}%)")
            if not active:
                status = stage if stage else "done"
                break
            time.sleep(8)

        results.append({"label": label, "status": status})
        log.info(f"  {label}: {status.upper()}")

        # Gap (skip after last job)
        if i < len(JOBS) - 1:
            log.info(f"  Waiting {GAP_SECONDS}s before next job…")
            time.sleep(GAP_SECONDS)

    finished = datetime.now()
    elapsed  = (finished - started).seconds
    log.info("=" * 60)
    log.info(f"Suite complete — {finished:%H:%M:%S}  |  elapsed {elapsed//60}m {elapsed%60}s")
    for r in results:
        log.info(f"  {r['label']:25s} → {r['status']}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_all()
