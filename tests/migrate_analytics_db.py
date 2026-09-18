"""
scripts/migrate_analytics_db.py
─────────────────────────────────
One-time migration: add columns introduced in Phase 3 to an existing
trading_analytics.db that was created before those columns existed.

Run once from the project root:
    python scripts/migrate_analytics_db.py

Safe to run multiple times — uses ALTER TABLE IF NOT EXISTS pattern
(SQLite ignores the command if column already exists via try/except).
"""

import os
import sqlite3

_PROJECT_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, 'data', 'trading_analytics', 'trading_analytics.db')


def add_column(conn, table, column, col_type, default=None):
    """Add a column if it doesn't already exist."""
    try:
        if default is not None:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        print(f"  ✅ Added {table}.{column}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e):
            print(f"  ⏭  {table}.{column} already exists")
        else:
            raise


def migrate():
    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}")
        print("Run 'flask shell' → db.create_all(bind_key='analytics') first.")
        return

    print(f"Migrating: {DB_PATH}\n")
    conn = sqlite3.connect(DB_PATH)

    # ── dhan_sync_log ─────────────────────────────────────────────────────────
    # Create table if it doesn't exist at all
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dhan_sync_log (
            id INTEGER PRIMARY KEY,
            started_at DATETIME,
            finished_at DATETIME,
            from_date DATE NOT NULL,
            to_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            pages_fetched INTEGER DEFAULT 0,
            records_fetched INTEGER DEFAULT 0,
            records_stored INTEGER DEFAULT 0,
            records_skipped INTEGER DEFAULT 0,
            records_invalid INTEGER DEFAULT 0,
            error_message TEXT
        )
    """)
    # Add any missing columns (for DBs created with older schema)
    add_column(conn, 'dhan_sync_log', 'started_at',      'DATETIME')
    add_column(conn, 'dhan_sync_log', 'finished_at',     'DATETIME')
    add_column(conn, 'dhan_sync_log', 'pages_fetched',   'INTEGER', 0)
    add_column(conn, 'dhan_sync_log', 'records_fetched', 'INTEGER', 0)
    add_column(conn, 'dhan_sync_log', 'records_stored',  'INTEGER', 0)
    add_column(conn, 'dhan_sync_log', 'records_skipped', 'INTEGER', 0)
    add_column(conn, 'dhan_sync_log', 'records_invalid', 'INTEGER', 0)
    add_column(conn, 'dhan_sync_log', 'error_message',   'TEXT')

    # ── trade_executions ──────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_executions (
            id INTEGER PRIMARY KEY,
            source VARCHAR(30) NOT NULL,
            provider_trade_id VARCHAR(80) NOT NULL,
            provider_order_id VARCHAR(80),
            symbol VARCHAR(50) NOT NULL,
            isin VARCHAR(20),
            security_id VARCHAR(30),
            exchange VARCHAR(30),
            side VARCHAR(5) NOT NULL,
            quantity INTEGER NOT NULL,
            price FLOAT NOT NULL,
            trade_value FLOAT NOT NULL,
            traded_at DATETIME,
            trade_date DATE,
            product_type VARCHAR(30),
            instrument_type VARCHAR(20) DEFAULT 'EQUITY',
            expiry_date VARCHAR(20),
            option_type VARCHAR(10),
            strike_price FLOAT,
            synced_at DATETIME,
            raw_trade_id INTEGER,
            UNIQUE(source, provider_trade_id)
        )
    """)

    # ── matched_trades ────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matched_trades (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(30),
            instrument_type VARCHAR(20) DEFAULT 'EQUITY',
            quantity INTEGER NOT NULL,
            buy_price FLOAT NOT NULL,
            buy_value FLOAT NOT NULL,
            buy_date DATE,
            buy_traded_at DATETIME,
            buy_trade_id VARCHAR(80) NOT NULL,
            sell_price FLOAT NOT NULL,
            sell_value FLOAT NOT NULL,
            sell_date DATE,
            sell_traded_at DATETIME,
            sell_trade_id VARCHAR(80) NOT NULL,
            gross_pnl FLOAT NOT NULL,
            holding_days INTEGER,
            product_type VARCHAR(30),
            matched_at DATETIME,
            run_id VARCHAR(40)
        )
    """)

    # ── open_lots ─────────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS open_lots (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR(50) NOT NULL,
            exchange VARCHAR(30),
            instrument_type VARCHAR(20) DEFAULT 'EQUITY',
            quantity INTEGER NOT NULL,
            buy_price FLOAT NOT NULL,
            cost_basis FLOAT NOT NULL,
            buy_date DATE,
            buy_traded_at DATETIME,
            buy_trade_id VARCHAR(80) NOT NULL,
            product_type VARCHAR(30),
            matched_at DATETIME,
            run_id VARCHAR(40)
        )
    """)

    conn.commit()
    conn.close()
    print("\nMigration complete ✅")
    print("Restart Flask, then click Sync Now on the Trading Analytics page.")


if __name__ == "__main__":
    migrate()


def migrate_phase3():
    """Phase 3 additions: source, upload_id, unique_key on matched_trades."""
    if not os.path.exists(DB_PATH):
        print("DB not found — run migrate() first.")
        return

    conn = sqlite3.connect(DB_PATH)
    print("\nPhase 3 migration:")
    add_column(conn, 'matched_trades', 'source',     "VARCHAR(30) DEFAULT 'fifo'")
    add_column(conn, 'matched_trades', 'upload_id',  "INTEGER")
    add_column(conn, 'matched_trades', 'unique_key', "VARCHAR(100)")

    # tax_report_uploads table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tax_report_uploads (
            id INTEGER PRIMARY KEY,
            uploaded_at DATETIME,
            filename VARCHAR(255) NOT NULL,
            report_period VARCHAR(50),
            period_from DATE,
            period_to DATE,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            total_rows INTEGER DEFAULT 0,
            imported_rows INTEGER DEFAULT 0,
            duplicate_rows INTEGER DEFAULT 0,
            rejected_rows INTEGER DEFAULT 0,
            error_message TEXT
        )
    """)
    print("  ✅ tax_report_uploads table created (or already exists)")
    conn.commit()
    conn.close()
    print("Phase 3 migration complete ✅")


if __name__ == "__main__":
    migrate()
    migrate_phase3()