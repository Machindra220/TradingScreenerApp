"""
app/services/dhan_trade_sync.py
────────────────────────────────
Phase 3 — Fetch historical Dhan trade executions.

SCOPE:
  ✅ Provider DTO (DhanRawTrade) — mirrors exact Dhan API field names
  ✅ Date-range fetch with pagination
  ✅ Page traversal until all records retrieved
  ✅ Duplicate protection via exchangeTradeId
  ✅ API response validation
  ✅ Normalized internal model (DhanRawTradeRecord via models_analytics)
  ✅ Sync result metadata (SyncResult)
  ✅ Safe error handling

NOT in this file (future phases):
  ❌ FIFO matching
  ❌ P&L calculations
  ❌ Completed trades
  ❌ Analytics / charts

DHAN API CONTRACT (v2):
  GET /trades/{from_date}/{to_date}/{page}
  from_date / to_date : YYYY-MM-DD
  page                : 0-based integer
  records per page    : up to 50
  last page signal    : empty list []
  max date range      : 90 days (Dhan hard limit)
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from app.services.dhan_client import DhanClient, DhanAPIError, DhanAuthError

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_RANGE_DAYS  = 90    # Dhan API hard limit
PAGE_SIZE       = 50    # Dhan API max records per page


# ── Provider DTO ──────────────────────────────────────────────────────────────

@dataclass
class DhanRawTrade:
    """
    Provider DTO — field names mirror the exact Dhan API response.
    Never mutate after construction; treat as read-only record.

    Optional fields (None when absent from response):
      - isin, carryForwardQuantity, exchangeTime, updateTime
      - drvExpiryDate, drvOptionType, drvStrikePrice (derivatives only)
    """
    # Identifiers
    dhanClientId:          str
    orderId:               str
    exchangeOrderId:       Optional[str]
    exchangeTradeId:       str            # dedup key — guaranteed unique by Dhan

    # Trade details
    transactionType:       str            # BUY | SELL
    exchangeSegment:       str            # NSE_EQ | BSE_EQ | NSE_FNO | etc.
    productType:           str            # CNC | INTRADAY | MARGIN | MTF | CO | BO
    orderType:             Optional[str]  # LIMIT | MARKET | SL | SL-M
    tradingSymbol:         str
    customSymbol:          Optional[str]
    securityId:            str
    tradedQuantity:        int
    tradedPrice:           float

    # Timestamps
    createTime:            Optional[str]  # 'YYYY-MM-DD HH:MM:SS' or similar
    updateTime:            Optional[str]
    exchangeTime:          Optional[str]

    # Instrument metadata
    isin:                  Optional[str]
    carryForwardQuantity:  Optional[int]

    # Derivatives (None for equity)
    drvExpiryDate:         Optional[str]
    drvOptionType:         Optional[str]  # CALL | PUT
    drvStrikePrice:        Optional[float]

    @classmethod
    def from_api_dict(cls, raw: dict) -> "DhanRawTrade":
        """
        Construct from a raw Dhan API response dict.
        All field reads use .get() with safe defaults — never assume a field
        is present even if documented, as Dhan may omit nulls.
        Raises ValueError if mandatory fields are missing.
        """
        trade_id = str(raw.get("exchangeTradeId") or "").strip()
        symbol   = str(raw.get("tradingSymbol")   or "").strip()
        tx_type  = str(raw.get("transactionType") or "").strip().upper()
        seg      = str(raw.get("exchangeSegment") or "").strip()
        sec_id   = str(raw.get("securityId")      or "").strip()

        if not trade_id:
            raise ValueError(
                f"Dhan trade missing exchangeTradeId — raw keys: {list(raw.keys())}"
            )
        if not symbol:
            raise ValueError(
                f"Dhan trade {trade_id} missing tradingSymbol"
            )
        if tx_type not in ("BUY", "SELL"):
            raise ValueError(
                f"Dhan trade {trade_id} has unexpected transactionType: {tx_type!r}"
            )

        qty = raw.get("tradedQuantity")
        try:
            qty = int(qty) if qty is not None else 0
        except (ValueError, TypeError):
            qty = 0

        price = raw.get("tradedPrice")
        try:
            price = float(price) if price is not None else 0.0
        except (ValueError, TypeError):
            price = 0.0

        cf_qty = raw.get("carryForwardQuantity")
        try:
            cf_qty = int(cf_qty) if cf_qty is not None else None
        except (ValueError, TypeError):
            cf_qty = None

        drv_strike = raw.get("drvStrikePrice")
        try:
            drv_strike = float(drv_strike) if drv_strike is not None else None
        except (ValueError, TypeError):
            drv_strike = None

        return cls(
            dhanClientId         = str(raw.get("dhanClientId")    or ""),
            orderId              = str(raw.get("orderId")          or ""),
            exchangeOrderId      = raw.get("exchangeOrderId"),
            exchangeTradeId      = trade_id,
            transactionType      = tx_type,
            exchangeSegment      = seg,
            productType          = str(raw.get("productType")      or ""),
            orderType            = raw.get("orderType"),
            tradingSymbol        = symbol,
            customSymbol         = raw.get("customSymbol"),
            securityId           = sec_id,
            tradedQuantity       = qty,
            tradedPrice          = price,
            createTime           = raw.get("createTime"),
            updateTime           = raw.get("updateTime"),
            exchangeTime         = raw.get("exchangeTime"),
            isin                 = raw.get("isin"),
            carryForwardQuantity = cf_qty,
            drvExpiryDate        = raw.get("drvExpiryDate"),
            drvOptionType        = raw.get("drvOptionType"),
            drvStrikePrice       = drv_strike,
        )


# ── Sync result metadata ──────────────────────────────────────────────────────

@dataclass
class SyncResult:
    """
    Metadata about a completed sync operation.
    Returned by DhanTradeSync.fetch_range() regardless of success/failure.
    """
    from_date:    date
    to_date:      date
    pages_fetched: int          = 0
    records_fetched: int        = 0   # total from API (including dupes)
    records_stored: int         = 0   # new records written to DB
    records_skipped: int        = 0   # dupes skipped
    records_invalid: int        = 0   # failed DTO validation
    status:       str           = "pending"  # pending|success|partial|error
    error:        Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "partial")

    def to_dict(self) -> dict:
        return {
            "from_date":       self.from_date.isoformat(),
            "to_date":         self.to_date.isoformat(),
            "pages_fetched":   self.pages_fetched,
            "records_fetched": self.records_fetched,
            "records_stored":  self.records_stored,
            "records_skipped": self.records_skipped,
            "records_invalid": self.records_invalid,
            "status":          self.status,
            "error":           self.error,
        }


# ── Sync service ──────────────────────────────────────────────────────────────

class DhanTradeSync:
    """
    Fetches historical trade executions from Dhan API for a date range.

    Usage:
        client = DhanClient.from_env()
        syncer = DhanTradeSync(client)
        result = syncer.fetch_range(
            from_date=date(2026, 4, 1),
            to_date=date(2026, 9, 15),
        )

    The service handles:
      - Date range splitting (90-day Dhan limit)
      - Page traversal until empty page signals end
      - DTO validation (from_api_dict)
      - Duplicate detection via exchangeTradeId
      - DB persistence via DhanRawTradeRecord model
    """

    def __init__(self, client: DhanClient):
        self._client = client

    # ── Date validation ───────────────────────────────────────────────────────

    @staticmethod
    def validate_date_range(from_date: date, to_date: date) -> None:
        """
        Raises ValueError with a clear message if dates are invalid.
        Called before any API request is made.
        """
        if not isinstance(from_date, date) or not isinstance(to_date, date):
            raise ValueError("from_date and to_date must be Python date objects.")
        if from_date > to_date:
            raise ValueError(
                f"from_date ({from_date}) must be on or before to_date ({to_date})."
            )
        if to_date > date.today():
            raise ValueError(
                f"to_date ({to_date}) cannot be in the future."
            )
        span = (to_date - from_date).days
        if span > MAX_RANGE_DAYS:
            raise ValueError(
                f"Date range ({span} days) exceeds Dhan's {MAX_RANGE_DAYS}-day limit. "
                f"Split the range into smaller chunks."
            )

    # ── Single page fetch ─────────────────────────────────────────────────────

    def _fetch_page(
        self, from_date: date, to_date: date, page: int
    ) -> list[dict]:
        """
        Fetch one page from Dhan API.
        Returns list of raw dicts, or [] on last page.
        Raises DhanAPIError / DhanAuthError on failure.
        """
        path   = f"/trades/{from_date.isoformat()}/{to_date.isoformat()}/{page}"
        result = self._client._get(path)

        # Dhan returns [] on last page, or a list of trade dicts
        if isinstance(result, list):
            return result
        # Some Dhan responses wrap in {"data": [...]}
        if isinstance(result, dict) and "data" in result:
            data = result["data"]
            return data if isinstance(data, list) else []
        # Unexpected shape
        raise DhanAPIError(
            f"Unexpected response shape from {path}: "
            f"expected list or {{\"data\": []}}, got {type(result).__name__}"
        )

    # ── Full range fetch ──────────────────────────────────────────────────────

    def fetch_range(
        self,
        from_date: date,
        to_date:   date,
        progress_callback=None,
    ) -> tuple[list[DhanRawTrade], SyncResult]:
        """
        Fetch all trades for the given date range, paginating automatically.

        Args:
            from_date:          Start date (inclusive)
            to_date:            End date (inclusive)
            progress_callback:  Optional callable(page, total_so_far) for
                                progress reporting — same pattern as cache

        Returns:
            (trades: list[DhanRawTrade], result: SyncResult)

        The returned trades list contains only VALID, UNIQUE trades.
        SyncResult.records_skipped counts duplicates.
        SyncResult.records_invalid counts validation failures.
        """
        self.validate_date_range(from_date, to_date)

        result = SyncResult(from_date=from_date, to_date=to_date)
        seen_trade_ids: set[str] = set()
        trades: list[DhanRawTrade] = []

        # Load already-stored trade IDs to prevent DB duplication
        seen_trade_ids = _load_existing_trade_ids(from_date, to_date)
        log.info(
            "[DhanTradeSync] Starting fetch %s→%s  (existing: %d IDs)",
            from_date, to_date, len(seen_trade_ids)
        )

        page = 0
        while True:
            log.debug("[DhanTradeSync] Fetching page %d", page)
            try:
                raw_page = self._fetch_page(from_date, to_date, page)
            except (DhanAPIError, DhanAuthError) as e:
                result.status = "error"
                result.error  = str(e)
                log.error("[DhanTradeSync] Page %d fetch failed: %s", page, e)
                break

            if not raw_page:
                # Empty list = last page signal from Dhan
                log.debug("[DhanTradeSync] Empty page at %d — done", page)
                break

            result.pages_fetched   += 1
            result.records_fetched += len(raw_page)

            for raw in raw_page:
                try:
                    dto = DhanRawTrade.from_api_dict(raw)
                except ValueError as e:
                    result.records_invalid += 1
                    log.warning("[DhanTradeSync] Invalid trade record: %s", e)
                    continue

                if dto.exchangeTradeId in seen_trade_ids:
                    result.records_skipped += 1
                    log.debug("[DhanTradeSync] Skipping duplicate: %s", dto.exchangeTradeId)
                    continue

                seen_trade_ids.add(dto.exchangeTradeId)
                trades.append(dto)
                result.records_stored += 1

            if progress_callback:
                progress_callback(page, result.records_stored)

            # Safety: if page returned < PAGE_SIZE, it's the last page
            if len(raw_page) < PAGE_SIZE:
                break

            page += 1

        if result.status == "pending":
            result.status = "success" if result.records_fetched > 0 else "success"

        log.info(
            "[DhanTradeSync] Done: pages=%d fetched=%d stored=%d "
            "skipped=%d invalid=%d",
            result.pages_fetched, result.records_fetched,
            result.records_stored, result.records_skipped,
            result.records_invalid,
        )
        return trades, result

    def fetch_range_chunked(
        self,
        from_date: date,
        to_date:   date,
        chunk_days: int = 30,
        progress_callback=None,
    ) -> tuple[list[DhanRawTrade], list[SyncResult]]:
        """
        Fetch a range longer than 90 days by splitting into chunks.
        Each chunk calls fetch_range() independently.
        Returns combined trades and list of per-chunk SyncResults.

        Example: 01-Apr-2025 → 15-Sep-2026 (>90 days) is split into
        30-day windows automatically.
        """
        if from_date > to_date:
            raise ValueError(
                f"from_date ({from_date}) must be before to_date ({to_date})."
            )

        all_trades:  list[DhanRawTrade] = []
        all_results: list[SyncResult]   = []

        cursor = from_date
        while cursor <= to_date:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), to_date)
            log.info(
                "[DhanTradeSync] Chunk: %s → %s", cursor, chunk_end
            )
            chunk_trades, chunk_result = self.fetch_range(
                cursor, chunk_end, progress_callback=progress_callback
            )
            all_trades.extend(chunk_trades)
            all_results.append(chunk_result)
            cursor = chunk_end + timedelta(days=1)

        return all_trades, all_results


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_existing_trade_ids(from_date: date, to_date: date) -> set[str]:
    """
    Load exchangeTradeId values already stored in DB for the date window.
    Returns empty set if DB is not available (e.g. during tests).
    """
    try:
        from app.models_analytics import DhanRawTradeRecord
        existing = DhanRawTradeRecord.query.filter(
            DhanRawTradeRecord.trade_date >= from_date,
            DhanRawTradeRecord.trade_date <= to_date,
        ).with_entities(DhanRawTradeRecord.exchange_trade_id).all()
        return {row[0] for row in existing if row[0]}
    except Exception as e:
        log.debug("[DhanTradeSync] Could not load existing IDs: %s", e)
        return set()


def persist_trades(
    trades: list[DhanRawTrade],
    sync_log_id: int | None = None,
) -> int:
    """
    Persist a list of validated, deduped DhanRawTrade DTOs to the DB.
    Returns count of records written.
    Raises nothing — errors are logged and skipped.
    """
    try:
        from app.extensions import db
        from app.models_analytics import DhanRawTradeRecord
    except ImportError as e:
        log.warning("[DhanTradeSync] DB not available: %s", e)
        return 0

    stored = 0
    for dto in trades:
        try:
            record = DhanRawTradeRecord.from_dto(dto, sync_log_id=sync_log_id)
            db.session.add(record)
            stored += 1
            if stored % 50 == 0:
                db.session.flush()
        except Exception as e:
            log.error(
                "[DhanTradeSync] Failed to persist trade %s: %s",
                dto.exchangeTradeId, e
            )

    if stored:
        db.session.commit()
        log.info("[DhanTradeSync] Persisted %d records", stored)
    return stored