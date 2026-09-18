# 📈 TradingScreenerApp

A secure, modular stock screening and trading analytics platform built with Flask. Covers NSE India and US markets with screeners, chart pages, scan automation, and a full trading analytics suite powered by Dhan Equity Tax Report imports.

---

## 🔧 Features

### Core
- ✅ Add / Edit / Delete trades with P&L tracking
- 📌 Pin and manage trading resources
- 📤 Export completed trades to CSV / XLSX / PDF
- 🧠 Filter by stock, date range, sort by profit
- 🕒 Watchlist, Open Trades, Trade History pages
- 📝 Notes page
- 📊 Simple statistics for trading performance tracking
- 🧮 Risk calculator based on investment value

### Screeners — NSE India
- 📈 Stage 2 / VOLAR India — Weinstein Stage Analysis with VOLAR metric, RS percentile, scan history, snapshot restore
- 📊 HH-HL India — higher-high / higher-low screener with window-based pivot detection
- 🚀 IPO Swing Screener — adaptive MAs, composite scoring, listing-age filtering
- 📦 Delivery Surge — delivery volume spike detection with history
- 📈 EPS Surge — earnings acceleration across last 3 quarters
- 🔺 Staircase Breakout — rising step structure above EMA 200 with composite quality score
- 📉 Trendline Breakout, 52-Week High Breakout, Volume Burst, Gap Up Opening
- 🏆 Top 20 Performers — Nifty 200, BSE 200, Nifty 500
- 🔄 Momentum Strategy — monthly rebalancing records

### Screeners — US Markets
- 📈 Stage 2 / VOLAR US — RS vs S&P 500, Stage 2 leader filtering
- 📊 HH-HL US — corrected RS formula, window-based pivot detection
- 🌍 IBD SmartSelect ratings (US)
- 📦 Delivery / Gap / Volume screeners (US)

### Chart Pages
- `chart_combined` — multi-indicator combined chart
- `chart_carousel` — carousel view across symbols
- `chart_weinstein` — Weinstein stage chart
- `chart_multiframe` — multi-timeframe view
- All charts use Lightweight Charts v4.2.3 (pinned — v5 has breaking API changes)

### Scan Automation
- ⚙️ Scan Suite — sequential automation runner for all screeners
- 🕒 APScheduler integration + `scan_runner.py` for Windows Task Scheduler
- 📋 Job order controlled by `uploads/scan_suite/suite_config.json`
- 📦 Last 30 days history of screener data

### Trading Analytics *(new — Tax Report based)*
- 📥 Import Dhan Equity Tax Report (CSV / XLS / XLSX) — replaces unreliable API sync
- 🔁 Deterministic dedup — re-importing same report is safe (SHA-1 key per trade)
- 📊 KPI Dashboard — Gross P&L, Win Rate, Avg Winner/Loser, Largest Winner/Loser, Avg Hold
- 📈 Cumulative P&L chart, Holding Period Distribution, Monthly P&L bar chart
- 🗂 Dedicated Completed Trades page — filter by type/outcome, sort all columns, export CSV/XLSX/PDF
- 📅 Monthly analytics — P&L, win rate, best/worst stock, MoM comparison
- 📆 Yearly analytics — P&L, win rate, best/worst month/stock, YoY comparison
- ⏱ Holding Period Analysis — 7 buckets (0–1d to 60+d), winner vs loser comparison, pattern detection
- 🧠 Trading Insights — 10 deterministic behavioral rules (no AI), neutral factual language
- 📤 Exports — CSV trades, XLSX trades, JSON analytics, CSV summary, PDF report
- 🔒 Import audit history — every upload logged with imported/duplicate/rejected counts

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| Backend | Flask (blueprints), Python 3.13 |
| Databases | PostgreSQL (main app), SQLite (analytics, market data cache) |
| Data | pandas, numpy, yfinance |
| Scheduling | APScheduler, Windows Task Scheduler (`scan_runner.py`) |
| Frontend | Tailwind CSS (Play CDN, dark mode), Alpine.js, Lightweight Charts v4.2.3 |
| Templating | Jinja2 |
| Exports | pandas, openpyxl, xlsxwriter, reportlab |

---

## 🚀 Getting Started

### Prerequisites
- Python 3.10+
- PostgreSQL (PGAdmin) — create a database and user with password
- Git

### Installation

```bash
git clone https://github.com/Machindra220/TradingScreenerApp.git
cd TradingScreenerApp
pip install -r requirements.txt
```

### Environment setup

Create a `.env` file in the project root:

```bash
SECRET_KEY=<your secret key>
DATABASE_URL=postgresql://username:password@localhost/db_name
SQLALCHEMY_TRACK_MODIFICATIONS=False
SQLALCHEMY_ECHO=False
FLASK_ENV=development
REMEMBER_COOKIE_SECURE=False
SESSION_COOKIE_SECURE=False
```

### Database setup

```bash
# Main app DB (PostgreSQL)
# Schema is at /app/db/schema.sql
psql -U username -d db_name -f app/db/schema.sql

# Analytics DB (SQLite — auto-created)
flask shell
>>> from app.extensions import db
>>> db.create_all(bind_key='analytics')
>>> exit()
```

### Run

```bash
flask run
# or for a specific port:
flask run --port=5005
```

---

## 📁 Project Structure

```
TradingScreenerApp/
├── app/
│   ├── routes/
│   │   └── trading_analytics.py      # Trading Analytics routes (14 routes)
│   ├── services/
│   │   ├── tax_report_parser.py      # Dhan Equity Tax Report parser
│   │   ├── tax_report_store.py       # Import to MatchedTrade with dedup
│   │   ├── holding_analyzer.py       # Holding period bucket analysis
│   │   ├── trade_analytics.py        # P&L, win rate, KPI computation
│   │   ├── period_analytics.py       # Monthly / yearly aggregation
│   │   ├── insights_engine.py        # 10 deterministic insight rules
│   │   ├── analytics_cache.py        # JSON file cache keyed by run_id
│   │   └── fifo_store.py             # MatchedTrade / OpenLot persistence
│   ├── models_analytics.py           # Analytics DB models (separate SQLite)
│   └── templates/
│       └── trading_analytics/
│           ├── dashboard.html        # Analytics dashboard
│           ├── trades.html           # Completed trades page
│           └── upload.html           # Tax report upload page
├── data/
│   ├── nifty_500.csv
│   ├── sp500.csv
│   └── trading_analytics/
│       └── trading_analytics.db      # SQLite analytics database
├── scripts/
│   └── migrate_analytics_db.py       # DB migration script
├── tests/
│   ├── test_dhan_phase5.py           # FifoEngine tests
│   ├── test_dhan_phase6.py           # TradeAnalyticsEngine tests
│   ├── test_dhan_phase8.py           # PeriodAnalytics tests
│   ├── test_dhan_phase9.py           # InsightsEngine tests
│   ├── test_dhan_phase11.py          # Export tests
│   ├── test_tax_report_parser.py     # Parser tests (35 tests)
│   ├── test_phase3_analytics.py      # Analytics pipeline tests (22 tests)
│   └── test_holding_analyzer.py      # Holding period tests (61 tests)
├── scan_runner.py                    # Windows Task Scheduler integration
├── requirements.txt
└── .env                              # Not committed — see format above
```

---

## 📊 Trading Analytics — Quick Start

1. Log in to [dhan.co](https://dhan.co) → Reports → Tax Report → Equity
2. Download your financial year report as **XLSX**
3. Go to `http://localhost:5005/trading-analytics/upload`
4. Drop the file → **Validate** → **Import**
5. View dashboard at `http://localhost:5005/trading-analytics`

---

## 🧪 Tests

```bash
python -m pytest tests/ -v
# Expected: 277+ passed
```

---

## 📌 Known Limitations

| Limitation | Detail |
|---|---|
| Dhan Tax Report only | Analytics sourced from manual report import, not live API |
| Gross P&L only | Brokerage, STT, GST not included in analytics |
| Calendar days | Holding period in calendar days, not trading days |
| NSE/BSE equity only | F&O and other segments not imported |
| Single account | One Dhan account per deployment |
| 90-day Dhan API limit | Tax report covers full financial year — no limit |

---

## 👤 Author

Built and maintained by **Machindra** — solo development.