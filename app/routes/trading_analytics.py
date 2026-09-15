"""
app/routes/trading_analytics.py
─────────────────────────────────
Phase 1 — Secure Dhan API configuration foundation.

SCOPE (Phase 1 only):
  ✅ /trading-analytics/status  — JSON config validation status
  ✅ /trading-analytics/ping    — JSON live connection health check

NOT in this file (future phases):
  ❌ Dashboard / UI pages
  ❌ Trade sync / background threads
  ❌ Progress polling
  ❌ Trade storage
  ❌ Any data fetching beyond connection ping

SECURITY:
  - All routes are @login_required.
  - All responses are JSON only in Phase 1 — no templates that could
    accidentally expose config values.
  - The ping endpoint never returns the access token or full client_id.
  - Errors are human-readable but contain no secret values.
"""

import logging

from flask import Blueprint, jsonify
from flask_login import login_required

from app.services.dhan_client import (
    DhanClient,
    DhanConfigError,
    DhanAuthError,
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
    Makes a live call to Dhan API /fundlimit to verify credentials are valid.
    Returns safe masked summary — no token, no full client_id.

    Response 200:  { "ok": true,  "status": "connected",
                     "client_id_masked": "1105****00",
                     "available_balance": 21529.9, "currency": "INR" }
    Response 401:  { "ok": false, "error": "...", "error_type": "auth" }
    Response 502:  { "ok": false, "error": "...", "error_type": "api" }
    Response 503:  { "ok": false, "error": "...", "error_type": "config" }
    """
    try:
        client = DhanClient.from_env()
        result = client.ping()
        log.info(
            "[TradingAnalytics] ping OK — client=%s",
            result.get("client_id_masked", "?")
        )
        return jsonify({"ok": True, **result}), 200

    except DhanConfigError as e:
        # Credentials not configured — safe message, no secrets
        return jsonify({
            "ok":         False,
            "error":      str(e),
            "error_type": "config",
        }), 503

    except DhanAuthError as e:
        # Token rejected — safe message, no token in str(e)
        return jsonify({
            "ok":         False,
            "error":      str(e),
            "error_type": "auth",
        }), 401

    except DhanAPIError as e:
        # Network / server error — safe message
        return jsonify({
            "ok":         False,
            "error":      str(e),
            "error_type": "api",
        }), 502