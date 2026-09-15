"""
app/routes/trading_analytics.py
─────────────────────────────────
Phase 1 — Config status + ping (GET).
Phase 2 — Reusable client, POST test-connection UI action.

Routes:
  GET  /trading-analytics/status           → JSON config validation
  GET  /trading-analytics/ping             → JSON live connection check (Phase 1)
  POST /trading-analytics/test-connection  → UI "Test Connection" action (Phase 2)
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required

from app.services.dhan_client import (
    DhanClient,
    DhanConfigError,
    DhanAuthError,
    DhanRateLimitError,
    DhanAPIError,
    validate_dhan_config,
)

log = logging.getLogger(__name__)

trading_analytics_bp = Blueprint("trading_analytics", __name__)


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
    """
    GET — live Dhan API health check. Returns safe masked summary.
    """
    try:
        client = DhanClient.from_env()
        result = client.ping()
        log.info("[TradingAnalytics] ping OK — client=%s",
                 result.get("client_id_masked", "?"))
        return jsonify({"ok": True, **result}), 200
    except DhanConfigError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "config"}), 503
    except DhanAuthError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "auth"}), 401
    except DhanRateLimitError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "rate_limit"}), 429
    except DhanAPIError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "api"}), 502


@trading_analytics_bp.route("/trading-analytics/test-connection", methods=["POST"])
@login_required
def test_connection():
    """
    POST — UI "Test Dhan Connection" action (Phase 2).
    CSRF protected (token injected globally via context_processor).

    Identical result shape to /ping so the frontend can reuse the same
    handler. POST is used (not GET) because this is a user-triggered
    action, not a passive read — consistent with the rest of the app.

    Response 200: { "ok": true,  "status": "connected",
                    "client_id_masked": "...", "available_balance": ...,
                    "used_margin": ..., "currency": "INR" }
    Response 4xx/5xx: { "ok": false, "error": "...", "error_type": "..." }
    """
    try:
        client = DhanClient.from_env()
        result = client.ping()
        log.info("[TradingAnalytics] test-connection OK — client=%s",
                 result.get("client_id_masked", "?"))
        return jsonify({"ok": True, **result}), 200
    except DhanConfigError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "config"}), 503
    except DhanAuthError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "auth"}), 401
    except DhanRateLimitError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "rate_limit"}), 429
    except DhanAPIError as e:
        return jsonify({"ok": False, "error": str(e), "error_type": "api"}), 502