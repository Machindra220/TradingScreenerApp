"""
app/routes/trading_analytics.py
─────────────────────────────────
Phase 1 — Config status + ping (GET).
Phase 2 — POST test-connection UI action.
Phase 3 — Historical trade sync routes.
Phase 7 — Dashboard: /trading-analytics (reads from local DB only).

Routes:
  GET  /trading-analytics             → dashboard (reads local DB, never calls Dhan)
  GET  /trading-analytics/status      → JSON config validation
  GET  /trading-analytics/ping        → JSON live connection check
  POST /trading-analytics/test-connection  → UI test action
  POST /trading-analytics/sync        → trigger background trade sync
  GET  /trading-analytics/sync/progress   → poll sync status
  GET  /trading-analytics/sync/status     → last sync metadata
  POST /trading-analytics/run-fifo    → re-run FIFO matching on stored trades
"""

import logging
import threading
import traceback
from datetime import datetime, date

from flask import Blueprint, jsonify, request, render_template
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

# ── Dashboard helper: load all data from local DB ─────────────────────────────

def _load_dashboard_data() -> dict:
    """
    Read all analytics data from local DB.
    Never calls Dhan API — only reads stored data.
    Called on every dashboard GET.
    """
    try:
        from app.models_analytics import (
            DhanSyncLog, TradeExecution, MatchedTrade, OpenLot
        )
        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from sqlalchemy import desc

        store  = FifoStore()
        engine = TradeAnalyticsEngine()

        # Sync metadata
        last_sync = DhanSyncLog.query.order_by(
            desc(DhanSyncLog.started_at)).first()

        # Counts
        n_executions    = TradeExecution.query.count()
        n_matched       = store.count_matched()
        n_open          = store.count_open()

        # Analytics (from stored MatchedTrade records)
        matched_trades  = store.get_all_matched()
        report          = engine.compute(matched_trades) if matched_trades else None

        # Recent completed trades (last 20 for table)
        recent          = store.get_all_matched(limit=20)

        # Top winners / losers (by gross_pnl)
        all_matched     = matched_trades
        top_winners     = sorted(all_matched, key=lambda m: m.gross_pnl, reverse=True)[:5]
        top_losers      = sorted(all_matched, key=lambda m: m.gross_pnl)[:5]

        # Open lots
        open_lots       = store.get_open_lots()

        return {
            "last_sync":      last_sync.to_dict() if last_sync else None,
            "n_executions":   n_executions,
            "n_matched":      n_matched,
            "n_open":         n_open,
            "report":         report.to_dict(include_trades=False) if report else None,
            "recent_trades":  [m.to_dict() for m in recent],
            "top_winners":    [m.to_dict() for m in top_winners],
            "top_losers":     [m.to_dict() for m in top_losers],
            "open_lots":      [o.to_dict() for o in open_lots],
            "chart_data":     _build_chart_data(matched_trades),
        }
    except Exception as e:
        log.error("[TradingAnalytics] load_dashboard_data error: %s", e)
        return {
            "last_sync": None, "n_executions": 0, "n_matched": 0, "n_open": 0,
            "report": None, "recent_trades": [], "top_winners": [],
            "top_losers": [], "open_lots": [], "chart_data": {},
        }


def _build_chart_data(matched_trades: list) -> dict:
    """
    Build chart-ready data from MatchedTrade DB records.
    Returns JSON-serializable dicts consumed by Lightweight Charts / inline JS.
    """
    if not matched_trades:
        return {}

    from collections import defaultdict
    import json

    # Cumulative P&L over time (sorted by sell_date)
    dated = sorted(
        [m for m in matched_trades if m.sell_date],
        key=lambda m: m.sell_date
    )
    cumulative, running = [], 0.0
    for m in dated:
        running = round(running + m.gross_pnl, 2)
        cumulative.append({
            "time":  m.sell_date.isoformat(),
            "value": running,
        })

    # Deduplicate same-date points — keep last
    seen = {}
    for pt in cumulative:
        seen[pt["time"]] = pt["value"]
    pnl_series = [{"time": k, "value": v} for k, v in sorted(seen.items())]

    # Winners vs Losers bar (by month)
    monthly: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    for m in dated:
        key = m.sell_date.strftime("%Y-%m")
        if m.gross_pnl > 0:
            monthly[key]["wins"]  += 1
        elif m.gross_pnl < 0:
            monthly[key]["losses"]+= 1
        monthly[key]["pnl"] = round(monthly[key]["pnl"] + m.gross_pnl, 2)

    monthly_bars = [
        {"month": k, "wins": v["wins"], "losses": v["losses"], "pnl": v["pnl"]}
        for k, v in sorted(monthly.items())
    ]

    # Holding period buckets
    buckets = {"0": 0, "1-5": 0, "6-15": 0, "16-30": 0, "31-90": 0, "90+": 0}
    for m in matched_trades:
        d = m.holding_days
        if d is None: continue
        if d == 0:       buckets["0"]     += 1
        elif d <= 5:     buckets["1-5"]   += 1
        elif d <= 15:    buckets["6-15"]  += 1
        elif d <= 30:    buckets["16-30"] += 1
        elif d <= 90:    buckets["31-90"] += 1
        else:            buckets["90+"]   += 1

    return {
        "pnl_series":    pnl_series,
        "monthly_bars":  monthly_bars,
        "holding_buckets": [{"label": k, "count": v} for k, v in buckets.items()],
    }

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


# ── Phase 7: Dashboard route ──────────────────────────────────────────────────

@trading_analytics_bp.route("/trading-analytics")
@trading_analytics_bp.route("/trading-analytics/")
@login_required
def dashboard():
    """
    Trading Analytics dashboard.
    Reads exclusively from local DB — never calls Dhan API.
    Only Sync Now (POST /sync) triggers an API call.
    """
    cfg  = validate_dhan_config()
    data = _load_dashboard_data()
    prog = _get_prog()

    import json
    return render_template(
        "trading_analytics/dashboard.html",
        creds_configured  = cfg["configured"],
        has_client_id     = cfg["has_client_id"],
        has_access_token  = cfg["has_access_token"],
        last_sync         = data["last_sync"],
        n_executions      = data["n_executions"],
        n_matched         = data["n_matched"],
        n_open            = data["n_open"],
        report            = data["report"],
        recent_trades     = data["recent_trades"],
        top_winners       = data["top_winners"],
        top_losers        = data["top_losers"],
        open_lots         = data["open_lots"],
        chart_data_json   = json.dumps(data["chart_data"]),
        is_syncing        = prog["active"],
        sync_stage        = prog["stage"],
        sync_error        = prog.get("error") if not prog["active"] else None,
    )


@trading_analytics_bp.route("/trading-analytics/run-fifo", methods=["POST"])
@login_required
def run_fifo():
    """
    POST — re-run FIFO matching on all stored TradeExecution records.
    CSRF protected. Used after a sync to update MatchedTrade + OpenLot tables.
    Returns JSON so the dashboard can poll or reload.
    """
    try:
        from app.models_analytics import TradeExecution
        from app.services.fifo_store import run_and_save

        executions = TradeExecution.query.order_by(
            TradeExecution.trade_date.asc(),
            TradeExecution.traded_at.asc(),
        ).all()

        summary = run_and_save(executions)
        log.info("[TradingAnalytics] FIFO run complete: %s", summary)
        return jsonify({"ok": True, **summary}), 200
    except Exception as e:
        log.error("[TradingAnalytics] FIFO run error: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@trading_analytics_bp.route("/trading-analytics/export")
@login_required
def export_trades():
    """GET — export all MatchedTrade records as CSV."""
    try:
        import io
        import csv
        from flask import make_response
        from app.services.fifo_store import FifoStore

        store   = FifoStore()
        matched = store.get_all_matched()

        si  = io.StringIO()
        cw  = csv.writer(si)
        cw.writerow([
            "Symbol","Exchange","Instrument","Qty",
            "Buy Date","Buy Price","Buy Value",
            "Sell Date","Sell Price","Sell Value",
            "Gross P&L","Holding Days",
            "Buy Trade ID","Sell Trade ID","Product",
        ])
        for m in matched:
            cw.writerow([
                m.symbol, m.exchange, m.instrument_type, m.quantity,
                m.buy_date, m.buy_price, m.buy_value,
                m.sell_date, m.sell_price, m.sell_value,
                m.gross_pnl, m.holding_days,
                m.buy_trade_id, m.sell_trade_id, m.product_type,
            ])

        from datetime import datetime
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        resp = make_response(si.getvalue())
        resp.headers["Content-Disposition"] = f"attachment; filename=TradingAnalytics_{ts}.csv"
        resp.headers["Content-type"] = "text/csv"
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Phase 8: Monthly and yearly analytics routes ──────────────────────────────

@trading_analytics_bp.route("/trading-analytics/monthly")
@login_required
def monthly_analytics():
    """
    GET — monthly trading performance analytics.
    Reads from local DB, uses cache when run_id unchanged.
    Never calls Dhan API.
    """
    try:
        from app.services.fifo_store import FifoStore
        from app.services.period_analytics import PeriodAnalytics
        from app.services.analytics_cache import AnalyticsCache

        cache = AnalyticsCache()
        run_id = cache.current_run_id()

        cached = cache.get(run_id)
        if cached and "monthly" in cached:
            return jsonify({"ok": True, "monthly": cached["monthly"],
                            "cached": True, "run_id": run_id}), 200

        # Cache miss — compute
        matched = FifoStore().get_all_matched()
        periods = PeriodAnalytics().compute_monthly(matched)
        data    = [p.to_dict() for p in periods]

        # Store in cache alongside yearly (get_or_compute pattern)
        existing = cache.get(run_id) or {}
        existing["monthly"] = data
        if run_id:
            cache.set(run_id, {k: v for k, v in existing.items()
                               if k not in ("run_id", "cached_at")})

        return jsonify({"ok": True, "monthly": data,
                        "cached": False, "run_id": run_id}), 200
    except Exception as e:
        log.error("[TradingAnalytics] monthly_analytics error: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@trading_analytics_bp.route("/trading-analytics/yearly")
@login_required
def yearly_analytics():
    """
    GET — yearly trading performance analytics.
    Reads from local DB, uses cache when run_id unchanged.
    Never calls Dhan API.
    """
    try:
        from app.services.fifo_store import FifoStore
        from app.services.period_analytics import PeriodAnalytics
        from app.services.analytics_cache import AnalyticsCache

        cache  = AnalyticsCache()
        run_id = cache.current_run_id()

        cached = cache.get(run_id)
        if cached and "yearly" in cached:
            return jsonify({"ok": True, "yearly": cached["yearly"],
                            "cached": True, "run_id": run_id}), 200

        # Cache miss — compute both so one cache write covers both endpoints
        matched  = FifoStore().get_all_matched()
        engine   = PeriodAnalytics()
        monthly  = engine.compute_monthly(matched)
        yearly   = engine.compute_yearly(matched, monthly_periods=monthly)

        m_data = [p.to_dict() for p in monthly]
        y_data = [p.to_dict() for p in yearly]

        if run_id:
            cache.set(run_id, {"monthly": m_data, "yearly": y_data})

        return jsonify({"ok": True, "yearly": y_data,
                        "cached": False, "run_id": run_id}), 200
    except Exception as e:
        log.error("[TradingAnalytics] yearly_analytics error: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


# ── Phase 9: Insights route ───────────────────────────────────────────────────

@trading_analytics_bp.route("/trading-analytics/insights")
@login_required
def trading_insights():
    """
    GET — deterministic trading insights from historical trade data.
    Reads exclusively from local DB. Uses analytics cache when fresh.
    Never calls Dhan API, never calls an AI API.
    """
    try:
        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from app.services.period_analytics import PeriodAnalytics
        from app.services.insights_engine import InsightsEngine
        from app.services.analytics_cache import AnalyticsCache

        cache  = AnalyticsCache()
        run_id = cache.current_run_id()

        # Check cache for insights
        cached = cache.get(run_id)
        if cached and "insights" in cached:
            return jsonify({
                "ok":      True,
                "insights": cached["insights"],
                "cached":   True,
                "run_id":   run_id,
            }), 200

        # Compute
        matched      = FifoStore().get_all_matched()
        analytics_rpt= TradeAnalyticsEngine().compute(matched)
        monthly      = PeriodAnalytics().compute_monthly(matched)
        insights_rpt = InsightsEngine().compute(
            summaries = analytics_rpt.trades,
            report    = analytics_rpt,
            monthly   = monthly,
        )
        data = insights_rpt.to_dict()

        # Cache alongside monthly/yearly
        if run_id:
            existing = cache.get(run_id) or {}
            existing["insights"] = data
            cache.set(run_id, {k: v for k, v in existing.items()
                               if k not in ("run_id", "cached_at")})

        return jsonify({"ok": True, "insights": data,
                        "cached": False, "run_id": run_id}), 200

    except Exception as e:
        log.error("[TradingAnalytics] insights error: %s", e)
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


# ── Phase 11: Export routes ───────────────────────────────────────────────────

@trading_analytics_bp.route("/trading-analytics/export/trades/xlsx")
@login_required
def export_trades_xlsx():
    """
    GET — export MatchedTrade records as XLSX (pandas + openpyxl).
    Includes all trade detail columns with auto-sized column widths.
    """
    try:
        import io
        import pandas as pd
        from flask import make_response
        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from datetime import datetime

        matched = FifoStore().get_all_matched()
        report  = TradeAnalyticsEngine().compute(matched)

        rows = []
        for t in report.trades:
            bv = t.buy_value
            rows.append({
                "Symbol":          t.symbol,
                "Exchange":        t.exchange,
                "Instrument":      t.instrument_type,
                "Buy Date":        t.buy_date.isoformat()  if t.buy_date  else "",
                "Buy Price (₹)":   t.buy_price,
                "Sell Date":       t.sell_date.isoformat() if t.sell_date else "",
                "Sell Price (₹)":  t.sell_price,
                "Quantity":        t.quantity,
                "Buy Value (₹)":   t.buy_value,
                "Sell Value (₹)":  t.sell_value,
                "Gross P&L (₹)":   t.gross_pnl,
                "Return %":        t.return_pct,
                "Holding Days":    t.holding_period_calendar_days,
                "Outcome":         t.outcome,
                "Product":         t.product_type,
            })

        df = pd.DataFrame(rows)
        buf = io.BytesIO()

        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Completed Trades")
            ws = writer.sheets["Completed Trades"]
            # Auto-size columns
            for col in ws.columns:
                max_len = max(
                    len(str(col[0].value or "")),
                    max((len(str(c.value or "")) for c in col[1:]), default=0)
                )
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 30)

        buf.seek(0)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        resp = make_response(buf.read())
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=TradingAnalytics_Trades_{ts}.xlsx"
        )
        resp.headers["Content-type"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        return resp

    except Exception as e:
        log.error("[TradingAnalytics] export_trades_xlsx error: %s", e)
        return jsonify({"error": str(e)}), 500


@trading_analytics_bp.route("/trading-analytics/export/analytics/json")
@login_required
def export_analytics_json():
    """
    GET — export full analytics report as JSON.
    Includes aggregate stats, monthly periods, and yearly periods.
    Never includes Dhan credentials or access tokens.
    """
    try:
        import json
        from flask import make_response
        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from app.services.period_analytics import PeriodAnalytics
        from datetime import datetime

        matched  = FifoStore().get_all_matched()
        engine   = PeriodAnalytics()
        report   = TradeAnalyticsEngine().compute(matched)
        monthly  = engine.compute_monthly(matched)
        yearly   = engine.compute_yearly(matched, monthly_periods=monthly)

        payload = {
            "exported_at":   datetime.utcnow().isoformat() + "Z",
            "total_trades":  report.total_trades,
            "analytics":     report.to_dict(include_trades=False),
            "monthly":       [m.to_dict() for m in monthly],
            "yearly":        [y.to_dict() for y in yearly],
        }

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        resp = make_response(json.dumps(payload, indent=2, default=str))
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=TradingAnalytics_Report_{ts}.json"
        )
        resp.headers["Content-type"] = "application/json"
        return resp

    except Exception as e:
        log.error("[TradingAnalytics] export_analytics_json error: %s", e)
        return jsonify({"error": str(e)}), 500


@trading_analytics_bp.route("/trading-analytics/export/analytics/csv")
@login_required
def export_analytics_csv():
    """
    GET — export analytics summary as CSV (two sheets: summary + monthly).
    """
    try:
        import io, csv
        from flask import make_response
        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from app.services.period_analytics import PeriodAnalytics
        from datetime import datetime

        matched = FifoStore().get_all_matched()
        report  = TradeAnalyticsEngine().compute(matched)
        monthly = PeriodAnalytics().compute_monthly(matched)

        si  = io.StringIO()
        cw  = csv.writer(si)

        # Section 1: Overall summary
        cw.writerow(["=== OVERALL ANALYTICS SUMMARY ==="])
        cw.writerow(["Metric", "Value"])
        for k, v in [
            ("Total Trades",              report.total_trades),
            ("Winning Trades",            report.winning_trades),
            ("Losing Trades",             report.losing_trades),
            ("Breakeven Trades",          report.breakeven_trades),
            ("Win Rate %",                report.win_rate_pct),
            ("Total Gross P&L (₹)",       report.total_gross_pnl),
            ("Total Buy Value (₹)",       report.total_buy_value),
            ("Total Sell Value (₹)",      report.total_sell_value),
            ("Avg Winner (₹)",            report.avg_winner),
            ("Avg Loser (₹)",             report.avg_loser),
            ("Largest Winner (₹)",        report.largest_winner),
            ("Largest Loser (₹)",         report.largest_loser),
            ("Avg Winner Return %",       report.avg_winner_return_pct),
            ("Avg Loser Return %",        report.avg_loser_return_pct),
            ("Avg Holding (calendar days)",report.avg_holding_calendar_days),
            ("Median Holding (days)",     report.median_holding_calendar_days),
            ("Min Holding (days)",        report.min_holding_calendar_days),
            ("Max Holding (days)",        report.max_holding_calendar_days),
        ]:
            cw.writerow([k, v])

        cw.writerow([])
        cw.writerow(["=== MONTHLY BREAKDOWN ==="])
        cw.writerow([
            "Period", "Is Complete", "Trades", "Winners", "Losers",
            "Win Rate %", "Gross P&L", "Avg Winner", "Avg Loser",
            "Avg Hold (days)", "Best Stock", "Worst Stock",
            "P&L Delta", "P&L Delta %", "vs",
        ])
        for m in monthly:
            cw.writerow([
                m.period_key, m.is_complete, m.total_trades,
                m.winning_trades, m.losing_trades, m.win_rate_pct,
                m.gross_pnl, m.avg_winner, m.avg_loser,
                m.avg_holding_days, m.best_stock, m.worst_stock,
                m.pnl_delta, m.pnl_delta_pct, m.comparison_note,
            ])

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        resp = make_response(si.getvalue())
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=TradingAnalytics_Summary_{ts}.csv"
        )
        resp.headers["Content-type"] = "text/csv; charset=utf-8"
        return resp

    except Exception as e:
        log.error("[TradingAnalytics] export_analytics_csv error: %s", e)
        return jsonify({"error": str(e)}), 500


@trading_analytics_bp.route("/trading-analytics/export/report/pdf")
@login_required
def export_report_pdf():
    """
    GET — export analytics summary as PDF using reportlab (pure Python).
    reportlab is already installed in the project venv.
    Includes: summary KPIs, monthly table, top winners/losers.
    Does NOT include Dhan credentials or access tokens.
    """
    try:
        import io
        from datetime import datetime
        from flask import make_response
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph,
            Spacer, HRFlowable,
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT

        from app.services.fifo_store import FifoStore
        from app.services.trade_analytics import TradeAnalyticsEngine
        from app.services.period_analytics import PeriodAnalytics

        matched = FifoStore().get_all_matched()
        report  = TradeAnalyticsEngine().compute(matched)
        monthly = PeriodAnalytics().compute_monthly(matched)

        buf      = io.BytesIO()
        doc      = SimpleDocTemplate(
            buf, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm,
        )
        styles   = getSampleStyleSheet()
        h1       = ParagraphStyle("h1", parent=styles["Heading1"],
                                  fontSize=16, textColor=colors.HexColor("#4f46e5"))
        h2       = ParagraphStyle("h2", parent=styles["Heading2"],
                                  fontSize=12, textColor=colors.HexColor("#374151"))
        small    = ParagraphStyle("small", parent=styles["Normal"], fontSize=8)
        right_s  = ParagraphStyle("right", parent=styles["Normal"],
                                  fontSize=9, alignment=TA_RIGHT)

        story = []

        # Title
        story.append(Paragraph("Trading Analytics Report", h1))
        story.append(Paragraph(
            f"Generated: {datetime.now().strftime('%d %b %Y %H:%M')} | "
            f"Calendar days holding period | Gross P&L only (no charges)",
            small
        ))
        story.append(Spacer(1, 0.4*cm))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#e5e7eb")))
        story.append(Spacer(1, 0.4*cm))

        # KPI summary table
        story.append(Paragraph("Performance Summary", h2))
        story.append(Spacer(1, 0.2*cm))

        def _fmt(v, prefix="", suffix=""):
            if v is None: return "—"
            if isinstance(v, float): return f"{prefix}{v:,.2f}{suffix}"
            return f"{prefix}{v}{suffix}"

        kpi_data = [
            ["Metric", "Value", "Metric", "Value"],
            ["Total Trades",     str(report.total_trades),
             "Win Rate",         _fmt(report.win_rate_pct, suffix="%")],
            ["Winning Trades",   str(report.winning_trades),
             "Losing Trades",    str(report.losing_trades)],
            ["Gross P&L (₹)",   _fmt(report.total_gross_pnl),
             "Total Buy (₹)",   _fmt(report.total_buy_value)],
            ["Avg Winner (₹)",  _fmt(report.avg_winner),
             "Avg Loser (₹)",   _fmt(report.avg_loser)],
            ["Largest Winner",  _fmt(report.largest_winner, "₹"),
             "Largest Loser",   _fmt(report.largest_loser, "₹")],
            ["Avg Hold (days)", _fmt(report.avg_holding_calendar_days),
             "Median Hold",     _fmt(report.median_holding_calendar_days)],
        ]

        kpi_tbl = Table(kpi_data, colWidths=[4.5*cm, 3.5*cm, 4.5*cm, 3.5*cm])
        kpi_tbl.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#4f46e5")),
            ("TEXTCOLOR",   (0,0), (-1,0), colors.white),
            ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",    (0,0), (-1,-1), 9),
            ("ROWBACKGROUNDS", (0,1), (-1,-1),
             [colors.HexColor("#f9fafb"), colors.white]),
            ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#e5e7eb")),
            ("PADDING",     (0,0), (-1,-1), 5),
            ("FONTNAME",    (0,1), (0,-1), "Helvetica-Bold"),
            ("FONTNAME",    (2,1), (2,-1), "Helvetica-Bold"),
        ]))
        story.append(kpi_tbl)
        story.append(Spacer(1, 0.5*cm))

        # Monthly table
        if monthly:
            story.append(Paragraph("Monthly Performance", h2))
            story.append(Spacer(1, 0.2*cm))

            m_header = ["Month","Complete","Trades","Win%","Gross P&L (₹)",
                        "Avg Hold","Best Stock","vs Prior"]
            m_rows   = [m_header]
            for m in monthly:
                pnl_str  = _fmt(m.gross_pnl)
                delta_str= (f"{'+' if (m.pnl_delta or 0)>=0 else ''}"
                            f"{_fmt(m.pnl_delta)}") if m.pnl_delta is not None else "—"
                m_rows.append([
                    m.period_key,
                    "✓" if m.is_complete else "⚠ partial",
                    str(m.total_trades),
                    _fmt(m.win_rate_pct, suffix="%"),
                    pnl_str,
                    _fmt(m.avg_holding_days),
                    m.best_stock or "—",
                    delta_str,
                ])

            m_tbl = Table(m_rows, colWidths=[2.2*cm,1.8*cm,1.5*cm,1.6*cm,
                                              2.8*cm,1.8*cm,2.5*cm,2.2*cm])
            m_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#374151")),
                ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
                ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE",   (0,0), (-1,-1), 8),
                ("ROWBACKGROUNDS", (0,1), (-1,-1),
                 [colors.HexColor("#f9fafb"), colors.white]),
                ("GRID",       (0,0), (-1,-1), 0.4, colors.HexColor("#e5e7eb")),
                ("PADDING",    (0,0), (-1,-1), 4),
            ]))
            story.append(m_tbl)
            story.append(Spacer(1, 0.5*cm))

        # Top 5 winners and losers
        winners = sorted(matched, key=lambda m: m.gross_pnl, reverse=True)[:5]
        losers  = sorted(matched, key=lambda m: m.gross_pnl)[:5]

        for title, trades, col in [
            ("Top 5 Winners", winners, colors.HexColor("#059669")),
            ("Top 5 Losers",  losers,  colors.HexColor("#dc2626")),
        ]:
            story.append(Paragraph(title, h2))
            story.append(Spacer(1, 0.2*cm))
            tbl_data = [["Symbol","Buy Date","Sell Date","Qty",
                         "Buy (₹)","Sell (₹)","Hold","P&L (₹)"]]
            for t in trades:
                tbl_data.append([
                    t.symbol,
                    str(t.buy_date)  if t.buy_date  else "—",
                    str(t.sell_date) if t.sell_date else "—",
                    str(t.quantity),
                    _fmt(t.buy_price),
                    _fmt(t.sell_price),
                    str(t.holding_days) if t.holding_days is not None else "—",
                    _fmt(t.gross_pnl),
                ])
            tl = Table(tbl_data, colWidths=[2.5*cm,2.2*cm,2.2*cm,1.2*cm,
                                             2.2*cm,2.2*cm,1.5*cm,2.2*cm])
            tl.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), col),
                ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
                ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTSIZE",   (0,0), (-1,-1), 8),
                ("ROWBACKGROUNDS", (0,1), (-1,-1),
                 [colors.HexColor("#f9fafb"), colors.white]),
                ("GRID",       (0,0), (-1,-1), 0.4, colors.HexColor("#e5e7eb")),
                ("PADDING",    (0,0), (-1,-1), 4),
            ]))
            story.append(tl)
            story.append(Spacer(1, 0.5*cm))

        # Footer note
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#e5e7eb")))
        story.append(Spacer(1, 0.2*cm))
        story.append(Paragraph(
            "All P&L figures are gross (before brokerage, STT, and taxes). "
            "Holding period in calendar days. This report contains no account "
            "credentials or Dhan API tokens.",
            small
        ))

        doc.build(story)
        buf.seek(0)

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        resp = make_response(buf.read())
        resp.headers["Content-Disposition"] = (
            f"attachment; filename=TradingAnalytics_Report_{ts}.pdf"
        )
        resp.headers["Content-type"] = "application/pdf"
        return resp

    except Exception as e:
        log.error("[TradingAnalytics] export_report_pdf error: %s", e)
        return jsonify({"error": str(e)[:200]}), 500