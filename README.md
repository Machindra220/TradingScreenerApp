# 📈 Trading Journal

A secure, modular trading journal built with Flask. Track trades, pin resources, export history, and analyze performance.

## 🔧 Features

- ✅ Add/Edit/Delete trades with P&L tracking
- 📌 Pin and manage trading tools
- 📤 Export completed trades to Excel with filters
- 🧠 Filter by stock, date range, and sort by profit
- 🖥️ Compact UI with icon-only actions
- 🕒 Last accessed tracking for resources
- 🕒 Watchlist page
- 🕒 Open Trades
- 🕒 Trades history Page
- 🕒 Notes page 
- 🕒 Simple statistics for our trading performance tracking
- 🖥️ Enhanced trade history view with action buttons (v1.1)
- 🧮 Risk calculator according our investment value
- 📊 Screeners - Sector Analysis, Delivery Surge, Stage2 Delivery Surge, EPS Surge for last 3 Quarters
- 📦 Last 30 days history of Screeners Data
- 📈 Top 20 Performers from Nifty 200, Bse 200 and Nifty 500
- 📊 Momentum Strategy and its recrods according to 1 Month Rebalancing

## 🚀 Getting Started Steps

Database Schema is placed in /app/db/schema.sql
DB can be created using that schema.sql

```bash
git clone https://github.com/Machindra220/Trading-Journal-App.git
cd Trading-Journal-App
pip install -r requirements.txt
flask run
```
Make sure to add secrets to your .env file.
.env file format as below.

```bash
SECRET_KEY=<your secret key here>
DATABASE_URL=<postgresql://username:password@localhost/db_name>
SQLALCHEMY_TRACK_MODIFICATIONS=False
SQLALCHEMY_ECHO=False
FLASK_ENV=<production / dev >
REMEMBER_COOKIE_SECURE=< True / False >
SESSION_COOKIE_SECURE=< True / False >
```
