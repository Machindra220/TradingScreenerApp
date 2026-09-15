"""
app/models_analytics.py
─────────────────────────
Phase 1 — Analytics DB model definitions.

Uses __bind_key__ = 'analytics' so Flask-SQLAlchemy writes to
data/trading_analytics/trading_analytics.db — NEVER to the main app DB.

SCOPE (Phase 1 only):
  ✅ DhanConnectionStatus — tracks last successful ping and auth state

NOT in this file (future phases):
  ❌ DhanTrade
  ❌ CompletedTrade
  ❌ TradeAnalytics
  ❌ DhanSyncLog
"""

from datetime import datetime
from app.extensions import db


class DhanConnectionStatus(db.Model):
    """
    Records each successful ping() result.
    Provides a lightweight audit trail of when the connection was last verified
    without storing any credential values.

    One row per check — not upserted, so you have a history of checks.
    Only stores safe, non-sensitive fields (masked client_id, balance).
    """
    __bind_key__ = 'analytics'
    __tablename__ = 'dhan_connection_status'

    id                = db.Column(db.Integer, primary_key=True)
    checked_at        = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    status            = db.Column(db.String(20), nullable=False)   # 'connected' | 'error'
    client_id_masked  = db.Column(db.String(20), nullable=True)    # e.g. "1105****00"
    available_balance = db.Column(db.Float,      nullable=True)
    error_message     = db.Column(db.Text,       nullable=True)    # safe message only

    def to_dict(self) -> dict:
        return {
            "id":                self.id,
            "checked_at":        self.checked_at.strftime("%d-%b-%Y %H:%M:%S"),
            "status":            self.status,
            "client_id_masked":  self.client_id_masked,
            "available_balance": self.available_balance,
            "error_message":     self.error_message,
        }