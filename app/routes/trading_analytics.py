"""
app/routes/trading_analytics.py
─────────────────────────────────
Phase 1 — Config status + ping (GET).
Phase 2 — POST test-connection UI action.
Phase 3 — Historical trade sync routes.

Routes:
  GET  /trading-analytics/status            → JSON config validation
  GET  /trading-analytics/ping              → JSON live connection check
  POST /trading-analytics/test-connection   → UI test action
  POST /trading-analytics/sync              → trigger background trade sync
  GET  /trading-analytics/sync/progress     → poll sync status
  GET  /trading-analytics/sync/status       → last sync metadata
"""

import logging
import threading
import traceback
from datetime import datetime, date

from flask import Blueprint, jsonify, request
from flask_login import login_required
from sqlalchemy import desc

from app.services.dhan_client import (
    DhanClient,
    DhanConfigError,
    DhanAuthError,
    DhanRateLimitError,
    DhanAPIError,
    validate_dhan_config,
)
from app.services.dhan_trade_sync import DhanTradeSync, persist_trades

log = logging.getLogger(__name__)

trading_analytics_bp = Blueprint("trading_analytics", __name__)

# ── Sync progress (same pattern as every screener) ────────────────────────────
_lock     = threading.Lock()
_SYNC_PROG = {
    "active":    False,
    "stage":     "idle",
    "pages":     0,
    "stored":    0,
    "fetched":   0,
    "error":     None,
}

def _set(**kw):
    with _lock: _SYNC_PROG.update(kw)

def _get_prog():
    with _lock: return dict(_SYNC_PROG)


# ── Routes ────────────────────────────────────────────────────────────────────

@trading_analytics_bp.route("/trading-analytics/status")
@login_required
def config_status():
    """
    Returns Dhan configuration status as JSON.
    Safe to call from the UI — contains no credential values.

    Response:
        200 { "configured": true,  "has_client_id": true,
              "has_access_token": true, "error": null }
        200 { "configured": false, "has_client_id": false,
              "has_access_token": false,
              "error": "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are not set..." }
    """
    status = validate_dhan_config()
    return jsonify(status), 200


@trading_analytics_bp.route("/trading-analytics/ping")
@login_required
def connection_ping():
    """GET — live Dhan API health check."""
    try:
        client = DhanClient.from_env()
        result = client.ping()
        log.info("[TradingAnalytics] ping OK — client=%s", result.get("client_id_masked"))
        return jsonify({"ok": True, **result}), 200
    except DhanConfigError   as e: return jsonify({"ok": False, "error": str(e), "error_type": "config"}),     503
    except DhanAuthError     as e: return jsonify({"ok": False, "error": str(e), "error_type": "auth"}),       401
    except DhanRateLimitError as e:return jsonify({"ok": False, "error": str(e), "error_type": "rate_limit"}), 429
    except DhanAPIError      as e: return jsonify({"ok": False, "error": str(e), "error_type": "api"}),        502


@trading_analytics_bp.route("/trading-analytics/test-connection", methods=["POST"])
@login_required
def test_connection():
    """POST — UI Test Connection action (Phase 2). CSRF protected."""
    try:
        client = DhanClient.from_env()
        result = client.ping()
        log.info("[TradingAnalytics] test-connection OK — client=%s", result.get("client_id_masked"))
        return jsonify({"ok": True, **result}), 200
    except DhanConfigError   as e: return jsonify({"ok": False, "error": str(e), "error_type": "config"}),     503
    except DhanAuthError     as e: return jsonify({"ok": False, "error": str(e), "error_type": "auth"}),       401
    except DhanRateLimitError as e:return jsonify({"ok": False, "error": str(e), "error_type": "rate_limit"}), 429
    except DhanAPIError      as e: return jsonify({"ok": False, "error": str(e), "error_type": "api"}),        502


# ── Phase 3: Background sync worker ──────────────────────────────────────────

def _run_sync(from_date: date, to_date: date):
    """
    Background thread: fetch trades from Dhan API and persist to analytics DB.
    Uses DhanTradeSync service — no business logic here.
    """
    _set(active=True, stage="connecting", pages=0, stored=0, fetched=0, error=None)

    try:
        from app.models_analytics import DhanSyncLog
        from app.extensions import db

        # Create sync log record
        sync_log = DhanSyncLog(
            from_date  = from_date,
            to_date    = to_date,
            status     = "running",
            started_at = datetime.utcnow(),
        )
        db.session.add(sync_log)
        db.session.commit()

        # Connect
        _set(stage="connecting")
        client = DhanClient.from_env()
        client.ping()   # fast auth check before fetching

        # Fetch
        _set(stage="fetching")

        def _progress(page, stored_so_far):
            _set(pages=page + 1, stored=stored_so_far)

        syncer = DhanTradeSync(client)

        # Use chunked fetch if range > 90 days
        span = (to_date - from_date).days
        if span > 90:
            trades, results = syncer.fetch_range_chunked(
                from_date, to_date, progress_callback=_progress
            )
            total_fetched = sum(r.records_fetched for r in results)
            total_stored  = sum(r.records_stored  for r in results)
            total_skipped = sum(r.records_skipped for r in results)
            total_invalid = sum(r.records_invalid for r in results)
            total_pages   = sum(r.pages_fetched   for r in results)
            any_error     = next((r.error for r in results if r.error), None)
        else:
            trades, result = syncer.fetch_range(
                from_date, to_date, progress_callback=_progress
            )
            total_fetched = result.records_fetched
            total_stored  = result.records_stored
            total_skipped = result.records_skipped
            total_invalid = result.records_invalid
            total_pages   = result.pages_fetched
            any_error     = result.error

        _set(stage="storing", fetched=total_fetched)

        # Persist
        stored = persist_trades(trades, sync_log_id=sync_log.id)

        # Update sync log
        sync_log.finished_at      = datetime.utcnow()
        sync_log.status           = "error" if any_error else "success"
        sync_log.pages_fetched    = total_pages
        sync_log.records_fetched  = total_fetched
        sync_log.records_stored   = stored
        sync_log.records_skipped  = total_skipped
        sync_log.records_invalid  = total_invalid
        sync_log.error_message    = any_error
        db.session.commit()

        _set(active=False, stage="done", stored=stored, fetched=total_fetched)
        log.info(
            "[TradingAnalytics] Sync complete: fetched=%d stored=%d skipped=%d",
            total_fetched, stored, total_skipped
        )

    except (DhanAuthError, DhanConfigError) as e:
        _set(active=False, stage="error", error=str(e))
        log.error("[TradingAnalytics] Sync auth/config error: %s", e)
    except Exception as e:
        traceback.print_exc()
        _set(active=False, stage="error", error=str(e)[:200])
        log.error("[TradingAnalytics] Sync unexpected error: %s", e)


# ── Phase 3: Sync routes ──────────────────────────────────────────────────────

@trading_analytics_bp.route("/trading-analytics/sync", methods=["POST"])
@login_required
def trigger_sync():
    """
    POST — trigger background historical trade sync.
    CSRF protected. Expects form fields: from_date, to_date (YYYY-MM-DD).

    Returns JSON immediately — client polls /sync/progress.
    """
    if _get_prog()["active"]:
        return jsonify({"ok": False, "error": "Sync already in progress."}), 409

    from_str = request.form.get("from_date", "").strip()
    to_str   = request.form.get("to_date",   "").strip()

    try:
        from_date = date.fromisoformat(from_str)
        to_date   = date.fromisoformat(to_str)
    except ValueError:
        return jsonify({
            "ok":    False,
            "error": f"Invalid date format. Use YYYY-MM-DD. Got: from={from_str!r} to={to_str!r}",
        }), 400

    try:
        DhanTradeSync.validate_date_range(from_date, to_date)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    t = threading.Thread(target=_run_sync, args=(from_date, to_date), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Sync started.", "from_date": from_str, "to_date": to_str}), 202


@trading_analytics_bp.route("/trading-analytics/sync/progress")
@login_required
def sync_progress():
    """GET — poll current sync progress. Safe to call frequently."""
    return jsonify(_get_prog()), 200


@trading_analytics_bp.route("/trading-analytics/sync/status")
@login_required
def sync_status():
    """GET — last completed sync metadata from DB."""
    try:
        from app.models_analytics import DhanSyncLog
        last = DhanSyncLog.query.order_by(desc(DhanSyncLog.started_at)).first()
        return jsonify({
            "ok":      True,
            "last_sync": last.to_dict() if last else None,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500