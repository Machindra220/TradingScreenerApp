"""
cache_admin.py  —  Cache Inspector & Force-Refresh Admin Page

Routes:
  GET  /cache-admin                  — dashboard (stats, staleness table)
  POST /cache-admin/force-refresh    — wipes fetch timestamps → next screener run re-fetches all
  POST /cache-admin/refresh-symbol   — force single symbol re-fetch now
  GET  /cache-admin/verify-symbol    — compare DB price vs live yfinance for one symbol
  GET  /cache-admin/stale-list       — JSON list of stale symbols (for AJAX refresh)
"""

import os
import json
import threading
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, redirect, url_for, jsonify

from app.services.market_data_cache import (
    ind_cache, us_cache, _MARKET_CONFIG, _SETTLE_BUFFER_MINUTES
)

cache_admin_bp = Blueprint("cache_admin", __name__)

_lock      = threading.Lock()
_REFRESH   = {"active": False, "done": 0, "total": 0, "market": "", "error": None}

UTC = ZoneInfo("UTC")


def _normalise_symbol(symbol: str, market: str) -> str:
    """
    Ensure the symbol is in the exact format stored in the cache DB.
    The cache always stores IND symbols with .NS suffix (e.g. ATHERENERG.NS).
    Users may paste the clean version from a screener table (ATHERENERG) or
    the full version (ATHERENERG.NS) — both should work.
    US symbols are stored as-is (AAPL, BRK.B) — no suffix added.
    """
    sym = symbol.strip().upper()
    if market == "IND":
        if not sym.endswith(".NS") and not sym.endswith(".BSE"):
            sym = f"{sym}.NS"
    return sym



def _set_refresh(**kw):
    with _lock:
        _REFRESH.update(kw)


# ── Background force-refresh (re-fetches every stale symbol) ─────────────────

def _do_force_refresh(market: str):
    cache = ind_cache if market == "IND" else us_cache
    _set_refresh(active=True, done=0, total=0, market=market, error=None)

    # Step 1: mark all symbols as needing refresh
    try:
        count = cache.force_refresh_all(interval="1d")
        _set_refresh(total=count)
    except Exception as e:
        _set_refresh(active=False, error=str(e))
        return

    # Step 2: get all symbols that are now stale
    rows, _, _ = cache.inspect_symbols(interval="1d", limit=10000, stale_only=True)
    symbols    = [r["symbol"] for r in rows]
    _set_refresh(total=len(symbols))

    # Step 3: bulk-fetch via cache (triggers yfinance for all)
    def _prog(i, t, sym):
        _set_refresh(done=i, total=t)

    try:
        cache.get_price_history_bulk(symbols, interval="1d", lookback_days=5,
                                     progress_callback=_prog)
    except Exception as e:
        _set_refresh(active=False, error=str(e))
        return

    _set_refresh(active=False, done=len(symbols))


# ── Helper: live price from yfinance for verification ────────────────────────

def _live_price(symbol: str) -> dict:
    try:
        ticker = yf.Ticker(symbol)
        hist   = ticker.history(period="3d", interval="1d")
        if hist.empty:
            return {"error": "No data returned from yfinance"}
        last   = hist.iloc[-1]
        return {
            "date":   hist.index[-1].strftime("%Y-%m-%d"),
            "close":  round(float(last["Close"]), 2),
            "volume": int(last["Volume"]),
            "high":   round(float(last["High"]), 2),
            "low":    round(float(last["Low"]), 2),
        }
    except Exception as e:
        return {"error": str(e)}


def _cached_price(symbol: str, market: str) -> dict:
    cache = ind_cache if market == "IND" else us_cache
    try:
        df = cache._read_cached(symbol, "1d", lookback_days=3)
        if df is None or df.empty:
            return {"error": "Not in cache"}
        last = df.iloc[-1]
        return {
            "date":   df.index[-1].strftime("%Y-%m-%d"),
            "close":  round(float(last["Close"]), 2),
            "volume": int(last["Volume"]),
            "high":   round(float(last["High"]), 2),
            "low":    round(float(last["Low"]), 2),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@cache_admin_bp.route("/cache-admin")
def cache_admin_view():
    market     = request.args.get("market", "IND").upper()
    if market not in ("IND", "US"):
        market = "IND"

    cache      = ind_cache if market == "IND" else us_cache
    stale_only = request.args.get("stale_only") == "1"
    limit      = int(request.args.get("limit", 100))

    ind_stats  = ind_cache.cache_stats()
    us_stats   = us_cache.cache_stats()

    symbols, total_meta, settled_date = cache.inspect_symbols(
        interval="1d", limit=limit, stale_only=stale_only
    )

    # Market timing info for the UI
    cfg    = _MARKET_CONFIG[market]
    tz     = cfg["tz"]
    now_local = datetime.now(tz)
    settle_deadline = now_local.replace(
        hour=cfg["close_hour"],
        minute=cfg["close_minute"] + _SETTLE_BUFFER_MINUTES,
        second=0, microsecond=0
    )
    market_open  = now_local.replace(hour=9, minute=15, second=0) if market == "IND" \
                   else now_local.replace(hour=9, minute=30, second=0)
    market_closed = now_local >= settle_deadline

    return render_template(
        "cache_admin.html",
        market=market,
        ind_stats=ind_stats,
        us_stats=us_stats,
        symbols=symbols,
        total_meta=total_meta,
        settled_date=settled_date,
        stale_only=stale_only,
        limit=limit,
        now_local=now_local.strftime("%H:%M %Z"),
        now_date=now_local.strftime("%d-%b-%Y"),
        settle_time=settle_deadline.strftime("%H:%M %Z"),
        market_closed=market_closed,
        market_label=cfg["label"],
        is_refreshing=_REFRESH["active"],
    )


@cache_admin_bp.route("/cache-admin/force-refresh", methods=["POST"])
def cache_admin_force_refresh():
    """Mark all symbols stale then re-fetch in background."""
    market = request.form.get("market", "IND").upper()
    if not _REFRESH["active"]:
        t = threading.Thread(target=_do_force_refresh, args=(market,), daemon=True)
        t.start()
    return redirect(url_for("cache_admin.cache_admin_view", market=market, refreshing=1))


@cache_admin_bp.route("/cache-admin/refresh-status")
def cache_admin_refresh_status():
    with _lock:
        return jsonify(dict(_REFRESH))


@cache_admin_bp.route("/cache-admin/refresh-symbol", methods=["POST"])
def cache_admin_refresh_symbol():
    """Force-refresh a single symbol immediately (synchronous, small latency)."""
    raw    = request.form.get("symbol", "").strip().upper()
    market = request.form.get("market", "IND").upper()
    cache  = ind_cache if market == "IND" else us_cache
    symbol = _normalise_symbol(raw, market) if raw else ""

    if symbol:
        try:
            cache.force_refresh_symbol(symbol, interval="1d")
            cache.get_price_history(symbol, interval="1d", force_refresh=True)
            result = "ok"
        except Exception as e:
            result = str(e)
    else:
        result = "no symbol"

    return redirect(url_for("cache_admin.cache_admin_view",
                            market=market, refreshed=symbol, result=result))


@cache_admin_bp.route("/cache-admin/verify-symbol")
def cache_admin_verify_symbol():
    """Compare DB price vs live yfinance — returns JSON for AJAX."""
    symbol = request.args.get("symbol", "").strip().upper()
    market = request.args.get("market", "IND").upper()
    if not symbol:
        return jsonify({"error": "No symbol provided"})

    yf_sym = _normalise_symbol(symbol, market)
    live   = _live_price(yf_sym)
    cached = _cached_price(yf_sym, market)

    match  = None
    if "close" in live and "close" in cached:
        diff  = abs(live["close"] - cached["close"])
        pct   = round(diff / live["close"] * 100, 3) if live["close"] else 0
        match = {"diff": round(diff, 2), "pct": pct, "ok": pct < 0.5}

    return jsonify({
        "symbol": yf_sym,
        "live":   live,
        "cached": cached,
        "match":  match,
    })


@cache_admin_bp.route("/cache-admin/stale-list")
def cache_admin_stale_list():
    market = request.args.get("market", "IND").upper()
    cache  = ind_cache if market == "IND" else us_cache
    rows, total, settled = cache.inspect_symbols(interval="1d", limit=500, stale_only=True)
    return jsonify({"stale": rows, "total_meta": total, "settled_date": settled})