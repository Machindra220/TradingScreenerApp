"""
app/services/trade_normalizer.py
──────────────────────────────────
Phase 4 — Convert provider-specific DTOs into provider-independent
TradeExecution model instances.

TIMEZONE POLICY:
  All timestamps from the Dhan API are in IST (Asia/Kolkata, UTC+5:30).
  They are stored as NAIVE Python datetimes (no tzinfo) to avoid
  SQLite TZ-awareness issues and to prevent accidental UTC conversion
  that would shift trade_date across midnight boundaries.
  Example: a trade at 23:55 IST on Dec 31 must remain Dec 31,
  not become Jan 1 UTC. All date comparisons use naive dates.
  If TZ-aware storage is needed in future, apply tz conversion at the
  API ingestion layer (dhan_trade_sync.py), not here.

This is the ONLY place in the codebase that knows about Dhan field names.
All other code (analytics, screeners, future features) uses TradeExecution
and never imports DhanRawTrade or any Dhan-specific names.

If the provider changes (Zerodha, ICICI, Upstox, manual CSV), add a new
normalizer function here — nothing else changes.

Scope (Phase 4):
  ✅ normalize_dhan_trade(dto) → TradeExecution
  ✅ normalize_dhan_batch(dtos) → list[TradeExecution]
  ✅ Instrument type detection (EQUITY / FUTURES / OPTIONS)
  ✅ Symbol cleaning

NOT here:
  ❌ FIFO
  ❌ P&L
  ❌ DB persistence (that is TradeExecutionStore's job)
"""

import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# Provider identifier used in TradeExecution.source
SOURCE_DHAN = "dhan"

# Dhan exchange segment → clean exchange name mapping
_EXCHANGE_MAP = {
    "NSE_EQ":   "NSE",
    "BSE_EQ":   "BSE",
    "NSE_FNO":  "NSE",
    "BSE_FNO":  "BSE",
    "NSE_CURR": "NSE",
    "BSE_CURR": "BSE",
    "MCX_COMM": "MCX",
    "IDX_I":    "NSE",
}

# Dhan exchange segment → instrument type
_INSTRUMENT_MAP = {
    "NSE_EQ":   "EQUITY",
    "BSE_EQ":   "EQUITY",
    "NSE_FNO":  "FUTURES",   # refined below using drvOptionType
    "BSE_FNO":  "FUTURES",
    "NSE_CURR": "CURRENCY",
    "BSE_CURR": "CURRENCY",
    "MCX_COMM": "COMMODITY",
    "IDX_I":    "INDEX",
}


def normalize_dhan_trade(dto, raw_trade_id: Optional[int] = None):
    """
    Map a DhanRawTrade DTO to a provider-independent TradeExecution instance.

    Args:
        dto:           DhanRawTrade dataclass from dhan_trade_sync.py
        raw_trade_id:  Optional FK back to DhanRawTradeRecord.id

    Returns:
        TradeExecution (not yet persisted — caller must call store.upsert())

    Raises:
        ValueError if mandatory fields cannot be resolved.
    """
    from app.models_analytics import TradeExecution

    # ── Resolve exchange and instrument type ─────────────────────────────────
    seg            = (dto.exchangeSegment or "").upper().strip()
    exchange       = _EXCHANGE_MAP.get(seg, seg)   # fallback: keep raw segment
    instrument_type = _INSTRUMENT_MAP.get(seg, "EQUITY")

    # Refine FNO: FUTURES vs OPTIONS based on drvOptionType
    if seg in ("NSE_FNO", "BSE_FNO") and dto.drvOptionType:
        opt = (dto.drvOptionType or "").strip().upper()
        if opt in ("CALL", "PUT"):
            instrument_type = "OPTIONS"

    # ── Symbol cleaning ──────────────────────────────────────────────────────
    # Use tradingSymbol as canonical; fall back to customSymbol
    symbol = (dto.tradingSymbol or dto.customSymbol or "").strip().upper()
    if not symbol:
        raise ValueError(
            f"Cannot normalize trade {dto.exchangeTradeId}: "
            "tradingSymbol and customSymbol are both empty."
        )

    # ── Timestamps ───────────────────────────────────────────────────────────
    traded_at  = _parse_dhan_timestamp(dto.createTime)
    trade_date = traded_at.date() if traded_at else None

    # ── Trade value ──────────────────────────────────────────────────────────
    qty   = dto.tradedQuantity or 0
    price = dto.tradedPrice    or 0.0

    return TradeExecution(
        source             = SOURCE_DHAN,
        provider_trade_id  = dto.exchangeTradeId,
        provider_order_id  = dto.orderId or None,
        symbol             = symbol,
        isin               = dto.isin or None,
        security_id        = dto.securityId or None,
        exchange           = exchange,
        side               = dto.transactionType,   # BUY | SELL — already validated
        quantity           = qty,
        price              = price,
        trade_value        = round(qty * price, 2),
        traded_at          = traded_at,
        trade_date         = trade_date,
        product_type       = dto.productType or None,
        instrument_type    = instrument_type,
        expiry_date        = dto.drvExpiryDate  or None,
        option_type        = dto.drvOptionType  or None,
        strike_price       = dto.drvStrikePrice or None,
        raw_trade_id       = raw_trade_id,
    )


def normalize_dhan_batch(
    dtos: list,
    raw_trade_id: Optional[int] = None,
) -> tuple[list, int]:
    """
    Normalize a list of DhanRawTrade DTOs.

    Returns:
        (executions, invalid_count)
        invalid_count = number of DTOs that failed normalization (logged, not raised)
    """
    executions = []
    invalid    = 0

    for dto in dtos:
        try:
            exe = normalize_dhan_trade(dto, raw_trade_id=raw_trade_id)
            executions.append(exe)
        except (ValueError, AttributeError, TypeError) as e:
            invalid += 1
            log.warning(
                "[TradeNormalizer] Skipping trade %s: %s",
                getattr(dto, "exchangeTradeId", "?"), e
            )

    log.info(
        "[TradeNormalizer] Normalized %d trades (%d invalid)",
        len(executions), invalid
    )
    return executions, invalid


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_dhan_timestamp(raw: Optional[str]) -> Optional[datetime]:
    """
    Parse Dhan timestamp string to datetime.
    Dhan uses multiple formats depending on endpoint — try all known ones.
    Returns None on any parse failure rather than raising.
    """
    if not raw:
        return None
    cleaned = str(raw).strip()[:19]   # truncate sub-second precision
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    log.debug("[TradeNormalizer] Could not parse timestamp: %r", raw)
    return None