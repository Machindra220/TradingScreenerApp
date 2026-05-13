from app.extensions import db
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timezone
from itsdangerous import URLSafeTimedSerializer
from flask import current_app
# from sqlalchemy import DateTime


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)

    trades = db.relationship('Trade', backref='user', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_reset_token(self, expires_sec=1800):
        """Generate a secure token for password reset."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        return s.dumps(self.id)

    @staticmethod
    def verify_reset_token(token):
        """Verify the token and return the user if valid."""
        s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        try:
            user_id = s.loads(token, max_age=1800)
        except Exception:
            return None
        return User.query.get(user_id)


class Trade(db.Model):
    __tablename__ = 'trades'

    id = db.Column(db.Integer, primary_key=True)
    stock_name = db.Column(db.String(20), nullable=False)
    entry_note = db.Column(db.Text)  # Optional note at entry
    journal = db.Column(db.Text)     # Optional summary or combined notes
    entry_date = db.Column(db.Date)  # First buy date
    exit_date = db.Column(db.Date)   # Final sell date
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(10), nullable=False, default="Open")
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    strategy_tag = db.Column(db.String(50), nullable=True)   # ✅ New column for strategy tag
    entries = db.relationship('TradeEntry', backref='trade', lazy=True)
    exits = db.relationship('TradeExit', backref='trade', lazy=True)
    @property
    def pnl(self):
        if self.status.lower() != 'closed':
            return 0

        total_entry = sum(e.price * e.quantity for e in self.entries if e.price and e.quantity)
        total_exit = sum(x.price * x.quantity for x in self.exits if x.price and x.quantity)

        return round(total_exit - total_entry, 2)
    @property
    def realized_profit(self):
        if not self.exits or not self.entries:
            return 0

        total_entry = sum(e.price * e.quantity for e in self.entries if e.price and e.quantity)
        total_quantity = sum(e.quantity for e in self.entries if e.quantity)

        if total_quantity == 0:
            return 0

        avg_entry_price = total_entry / total_quantity

        profit = sum((x.price - avg_entry_price) * x.quantity for x in self.exits if x.price and x.quantity)
        return round(profit, 2)


class TradeEntry(db.Model):
    __tablename__ = 'trade_entries'
    id = db.Column(db.Integer, primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False)
    trade_id = db.Column(db.Integer, db.ForeignKey('trades.id'), nullable=False)
    invested_amount = db.Column(db.Numeric, nullable=False, server_default=db.FetchedValue())
    note = db.Column(db.Text)  # ✅ Trade entry Note

class TradeExit(db.Model):
    __tablename__ = 'trade_exits'
    id = db.Column(db.Integer, primary_key=True)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False)
    trade_id = db.Column(db.Integer, db.ForeignKey('trades.id'), nullable=False)
    exit_amount = db.Column(db.Numeric, nullable=False, server_default=db.FetchedValue())
    note = db.Column(db.Text)  # ✅ Trade Exit Note

class Resource(db.Model):
    __tablename__ = 'resources'  # ✅ Match your actual table name

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(255), nullable=False)
    note = db.Column(db.Text)
    category = db.Column(db.String(50))
    tags = db.Column(db.String(100))
    pinned = db.Column(db.Boolean, default=False)
    last_accessed = db.Column(db.DateTime)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))  # ✅ match your users table

class DayNote(db.Model):
    __tablename__ = 'day_notes'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    summary = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    images = db.relationship('NoteImage', backref='note', cascade='all, delete-orphan')

class NoteImage(db.Model):
    __tablename__ = 'note_images'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    note_id = db.Column(db.Integer, db.ForeignKey('day_notes.id'))


class Watchlist(db.Model):
    __tablename__ = 'watchlist'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    stock_name = db.Column(db.String(50), nullable=False)
    target_price = db.Column(db.Numeric(10, 2), nullable=False)
    stop_loss = db.Column(db.Numeric(10, 2), nullable=False)
    expected_move = db.Column(db.Numeric(10, 2), nullable=False)
    setup_type = db.Column(db.String(30), nullable=False)
    confidence = db.Column(db.String(10))
    date_added = db.Column(db.Date, nullable=False, default=date.today)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='Open')
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    user = db.relationship('User', backref='watchlist_items')

# Stage2Stock models.py
class Stage2Stock(db.Model):
    __tablename__ = 'stage2_stocks'
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    price = db.Column(db.Float)
    ma_30w = db.Column(db.Float)
    volume = db.Column(db.BigInteger)
    vol_avg = db.Column(db.BigInteger)
    rs = db.Column(db.Float)
    date = db.Column(db.DateTime, nullable=False)

# models Momentum

class MomentumPortfolio(db.Model):
    __tablename__ = 'momentum_portfolio'
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    buy_price = db.Column(db.Numeric(10, 2), nullable=False)
    buy_date = db.Column(db.Date, nullable=False)
    source_rank = db.Column(db.Integer, nullable=False)
    holding_status = db.Column(db.String(10), default='active')  # 'active' or 'removed'
# models Momentum Trades
class MomentumTrade(db.Model):
    __tablename__ = 'momentum_trades'
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(4), nullable=False)  # BUY or SELL
    price = db.Column(db.Numeric(10, 2), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    trade_date = db.Column(db.Date, nullable=False)
    profit_loss_pct = db.Column(db.Numeric(6, 2))   # % return (only for SELL)
    profit_loss_value = db.Column(db.Numeric(12, 2))  # ₹ gain/loss (only for SELL)
    notes = db.Column(db.Text)
    portfolio_id = db.Column(db.Integer, db.ForeignKey("momentum_portfolio.id"))
    portfolio = db.relationship("MomentumPortfolio", backref="trades")


# models Delivery Surge Stock
class DeliverySurgeStock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    date = db.Column(db.Date, nullable=False)
    price = db.Column(db.Float)
    volume = db.Column(db.BigInteger)
    delivery_spike = db.Column(db.Float)
    roc_21d = db.Column(db.Float)
    rs_vs_index_21d = db.Column(db.Float)

    def __repr__(self):
        return f"<DeliverySurgeStock {self.symbol} @ {self.date}>"

#Stage2DeliveryStock modal
class Stage2DeliveryStock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    date = db.Column(db.Date, nullable=False)
    price = db.Column(db.Float)
    volume = db.Column(db.BigInteger)
    delivery_spike = db.Column(db.Float)
    roc_21d = db.Column(db.Float)
    rs_vs_index_21d = db.Column(db.Float)

    def __repr__(self):
        return f"<Stage2DeliveryStock {self.symbol} @ {self.date}>"

class EPSScreenerResult(db.Model):
    __tablename__ = 'eps_screener_results'

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False)
    symbol_clean = db.Column(db.String(20), nullable=False)
    screener_date = db.Column(db.Date, nullable=False)
    price = db.Column(db.Numeric(10, 2))
    volume = db.Column(db.BigInteger)
    delivery = db.Column(db.Numeric(20, 2))
    eps_growth_q1 = db.Column(db.Numeric(12, 2))
    eps_growth_q2 = db.Column(db.Numeric(12, 2))
    eps_growth_q3 = db.Column(db.Numeric(12, 2))
    roc_21d = db.Column(db.Numeric(6, 2))
    rs_vs_index_21d = db.Column(db.Numeric(6, 2))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<EPSScreenerResult {self.symbol_clean} @ {self.screener_date}>"