"""
app/models_analytics.py
─────────────────────────
Analytics DB model definitions.
Uses __bind_key__ = 'analytics' → data/trading_analytics/trading_analytics.db

Phase 1: DhanConnectionStatus
Phase 3: DhanSyncLog, DhanRawTradeRecord
"""

from datetime import datetime, timezone, date as date_type
from app.extensions import db


class DhanConnectionStatus(db.Model):
    """Phase 1 — records each successful ping() result."""
    __bind_key__ = 'analytics'
    __tablename__ = 'dhan_connection_status'

    id                = db.Column(db.Integer, primary_key=True)
    checked_at        = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    status            = db.Column(db.String(20), nullable=False)
    client_id_masked  = db.Column(db.String(20), nullable=True)
    available_balance = db.Column(db.Float,      nullable=True)
    error_message     = db.Column(db.Text,       nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "checked_at":        self.checked_at.strftime("%d-%b-%Y %H:%M:%S"),
            "status":            self.status,
            "client_id_masked":  self.client_id_masked,
            "available_balance": self.available_balance,
            "error_message":     self.error_message,
        }


class DhanSyncLog(db.Model):
    """
    Phase 3 — Records each historical trade sync attempt.
    One row per sync run, regardless of success/failure.
    """
    __bind_key__ = 'analytics'
    __tablename__ = 'dhan_sync_log'

    id               = db.Column(db.Integer, primary_key=True)
    started_at       = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finished_at      = db.Column(db.DateTime, nullable=True)
    from_date        = db.Column(db.Date,     nullable=False)
    to_date          = db.Column(db.Date,     nullable=False)
    status           = db.Column(db.String(20), nullable=False, default="running")
    pages_fetched    = db.Column(db.Integer,  default=0)
    records_fetched  = db.Column(db.Integer,  default=0)
    records_stored   = db.Column(db.Integer,  default=0)
    records_skipped  = db.Column(db.Integer,  default=0)
    records_invalid  = db.Column(db.Integer,  default=0)
    error_message    = db.Column(db.Text,     nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "started_at":      self.started_at.strftime("%d-%b-%Y %H:%M:%S"),
            "finished_at":     self.finished_at.strftime("%d-%b-%Y %H:%M:%S") if self.finished_at else None,
            "from_date":       self.from_date.isoformat(),
            "to_date":         self.to_date.isoformat(),
            "status":          self.status,
            "pages_fetched":   self.pages_fetched,
            "records_fetched": self.records_fetched,
            "records_stored":  self.records_stored,
            "records_skipped": self.records_skipped,
            "records_invalid": self.records_invalid,
            "error_message":   self.error_message,
        }


class DhanRawTradeRecord(db.Model):
    """
    Phase 3 — Internal normalized trade record.
    Mapped from DhanRawTrade DTO via from_dto().

    Naming: snake_case internal names → mapped from camelCase Dhan fields.
    exchangeTradeId is the unique dedup key (guaranteed unique by Dhan).
    """
    __bind_key__ = 'analytics'
    __tablename__ = 'dhan_raw_trades'

    id                   = db.Column(db.Integer, primary_key=True)

    # Dhan identifiers
    dhan_client_id       = db.Column(db.String(50),  nullable=False, index=True)
    order_id             = db.Column(db.String(50),  nullable=True,  index=True)
    exchange_order_id    = db.Column(db.String(50),  nullable=True)
    exchange_trade_id    = db.Column(db.String(50),  nullable=False,
                                     unique=True, index=True)   # dedup key

    # Trade details
    transaction_type     = db.Column(db.String(5),   nullable=False)   # BUY | SELL
    exchange_segment     = db.Column(db.String(20),  nullable=False)
    product_type         = db.Column(db.String(20),  nullable=True)
    order_type           = db.Column(db.String(20),  nullable=True)
    trading_symbol       = db.Column(db.String(50),  nullable=False, index=True)
    custom_symbol        = db.Column(db.String(50),  nullable=True)
    security_id          = db.Column(db.String(30),  nullable=True)
    isin                 = db.Column(db.String(20),  nullable=True,  index=True)

    # Execution
    traded_quantity      = db.Column(db.Integer,     nullable=False)
    traded_price         = db.Column(db.Float,       nullable=False)
    trade_value          = db.Column(db.Float,       nullable=False)   # qty × price
    carry_forward_qty    = db.Column(db.Integer,     nullable=True)

    # Timestamps
    trade_date           = db.Column(db.Date,        nullable=True,  index=True)
    create_time          = db.Column(db.DateTime,    nullable=True)
    exchange_time        = db.Column(db.DateTime,    nullable=True)
    update_time          = db.Column(db.DateTime,    nullable=True)

    # Derivatives (None for equity)
    drv_expiry_date      = db.Column(db.String(20),  nullable=True)
    drv_option_type      = db.Column(db.String(10),  nullable=True)   # CALL | PUT
    drv_strike_price     = db.Column(db.Float,       nullable=True)

    # Metadata
    sync_log_id          = db.Column(db.Integer,
                                     db.ForeignKey('dhan_sync_log.id'),
                                     nullable=True)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def from_dto(cls, dto, sync_log_id: int | None = None) -> "DhanRawTradeRecord":
        """
        Map a DhanRawTrade DTO into this internal model.
        Parses timestamps safely — returns None on parse failure.
        """
        from app.services.dhan_trade_sync import DhanRawTrade
        assert isinstance(dto, DhanRawTrade)

        def _parse_dt(raw: str | None) -> datetime | None:
            if not raw:
                return None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                        "%d-%m-%Y %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(str(raw)[:19], fmt)
                except ValueError:
                    continue
            return None

        create_dt   = _parse_dt(dto.createTime)
        exchange_dt = _parse_dt(dto.exchangeTime)
        update_dt   = _parse_dt(dto.updateTime)
        trade_date  = create_dt.date() if create_dt else None

        qty   = dto.tradedQuantity or 0
        price = dto.tradedPrice    or 0.0

        return cls(
            dhan_client_id    = dto.dhanClientId,
            order_id          = dto.orderId,
            exchange_order_id = dto.exchangeOrderId,
            exchange_trade_id = dto.exchangeTradeId,
            transaction_type  = dto.transactionType,
            exchange_segment  = dto.exchangeSegment,
            product_type      = dto.productType,
            order_type        = dto.orderType,
            trading_symbol    = dto.tradingSymbol,
            custom_symbol     = dto.customSymbol,
            security_id       = dto.securityId,
            isin              = dto.isin,
            traded_quantity   = qty,
            traded_price      = price,
            trade_value       = round(qty * price, 2),
            carry_forward_qty = dto.carryForwardQuantity,
            trade_date        = trade_date,
            create_time       = create_dt,
            exchange_time     = exchange_dt,
            update_time       = update_dt,
            drv_expiry_date   = dto.drvExpiryDate,
            drv_option_type   = dto.drvOptionType,
            drv_strike_price  = dto.drvStrikePrice,
            sync_log_id       = sync_log_id,
        )

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "exchange_trade_id": self.exchange_trade_id,
            "order_id":          self.order_id,
            "symbol":            self.trading_symbol,
            "isin":              self.isin,
            "exchange":          self.exchange_segment,
            "product":           self.product_type,
            "side":              self.transaction_type,
            "quantity":          self.traded_quantity,
            "price":             self.traded_price,
            "trade_value":       self.trade_value,
            "trade_date":        self.trade_date.isoformat() if self.trade_date else None,
            "create_time":       self.create_time.strftime("%Y-%m-%d %H:%M:%S") if self.create_time else None,
        }


class TradeExecution(db.Model):
    """
    Phase 4 — Provider-independent internal trade execution model.

    The rest of the application uses ONLY this model — never DhanRawTradeRecord
    or any Dhan-specific field names. This decouples business logic from the
    Dhan API contract: if the provider changes (e.g. Zerodha, ICICI), only
    the normalizer changes, not the consuming code.

    Dedup key: (source, provider_trade_id)
      - source           = 'dhan' | 'zerodha' | 'manual' etc.
      - provider_trade_id = the provider's own unique trade ID
      Together they guarantee no duplicates across repeated syncs or providers.
    """
    __bind_key__ = 'analytics'
    __tablename__ = 'trade_executions'

    id                  = db.Column(db.Integer, primary_key=True)

    # Provider identity — dedup key
    source              = db.Column(db.String(30),  nullable=False, index=True)
    provider_trade_id   = db.Column(db.String(80),  nullable=False, index=True)
    provider_order_id   = db.Column(db.String(80),  nullable=True)

    # Instrument — provider-independent names
    symbol              = db.Column(db.String(50),  nullable=False, index=True)
    isin                = db.Column(db.String(20),  nullable=True,  index=True)
    security_id         = db.Column(db.String(30),  nullable=True)
    exchange            = db.Column(db.String(30),  nullable=True)

    # Execution
    side                = db.Column(db.String(5),   nullable=False)   # BUY | SELL
    quantity            = db.Column(db.Integer,     nullable=False)
    price               = db.Column(db.Float,       nullable=False)
    trade_value         = db.Column(db.Float,       nullable=False)   # qty × price

    # Time
    traded_at           = db.Column(db.DateTime,    nullable=True,  index=True)
    trade_date          = db.Column(db.Date,        nullable=True,  index=True)

    # Classification
    product_type        = db.Column(db.String(30),  nullable=True)
    instrument_type     = db.Column(db.String(20),  nullable=True,
                                    default="EQUITY")  # EQUITY | FUTURES | OPTIONS

    # Derivatives (None for equity)
    expiry_date         = db.Column(db.String(20),  nullable=True)
    option_type         = db.Column(db.String(10),  nullable=True)   # CALL | PUT
    strike_price        = db.Column(db.Float,       nullable=True)

    # Metadata
    synced_at           = db.Column(db.DateTime,    default=datetime.utcnow, nullable=False)
    raw_trade_id        = db.Column(db.Integer,
                                    db.ForeignKey('dhan_raw_trades.id'),
                                    nullable=True)   # back-reference to raw record

    # Unique constraint — prevents duplicates on repeated syncs
    __table_args__ = (
        db.UniqueConstraint('source', 'provider_trade_id',
                            name='uq_trade_source_provider_id'),
    )

    def to_dict(self) -> dict:
        return {
            "id":                 self.id,
            "source":             self.source,
            "provider_trade_id":  self.provider_trade_id,
            "provider_order_id":  self.provider_order_id,
            "symbol":             self.symbol,
            "isin":               self.isin,
            "security_id":        self.security_id,
            "exchange":           self.exchange,
            "side":               self.side,
            "quantity":           self.quantity,
            "price":              self.price,
            "trade_value":        self.trade_value,
            "traded_at":          self.traded_at.strftime("%Y-%m-%d %H:%M:%S") if self.traded_at else None,
            "trade_date":         self.trade_date.isoformat() if self.trade_date else None,
            "product_type":       self.product_type,
            "instrument_type":    self.instrument_type,
            "expiry_date":        self.expiry_date,
            "option_type":        self.option_type,
            "strike_price":       self.strike_price,
            "synced_at":          self.synced_at.strftime("%Y-%m-%d %H:%M:%S") if self.synced_at else None,
        }