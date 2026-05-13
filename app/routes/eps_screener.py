import os
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, request, flash
from datetime import datetime
from app.extensions import db
from app.models import EPSScreenerResult
from sqlalchemy import and_

eps_bp = Blueprint("eps", __name__)
eps_cache = {}

# 💾 Save EPS result to DB using SQLAlchemy
def save_to_db(entry):
    today = datetime.today().date()
    existing = EPSScreenerResult.query.filter_by(
        symbol_clean=entry["symbol_clean"],
        screener_date=today
    ).first()

    if existing:
        # Optional: update if needed
        return

    db_entry = EPSScreenerResult(
        symbol=entry["symbol"],
        symbol_clean=entry["symbol_clean"],
        screener_date=datetime.today().date(),
        price=float(entry["price"]),
        volume=int(entry["volume"]),
        delivery=float(entry["delivery"]),
        eps_growth_q1=float(entry["eps_growth"][0]),
        eps_growth_q2=float(entry["eps_growth"][1]),
        eps_growth_q3=float(entry["eps_growth"][2]),
        roc_21d=float(entry["roc_21d"]),
        rs_vs_index_21d=float(entry["rs_vs_index_21d"])
    )
    try:
        db.session.add(db_entry)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ DB commit error: {e}")

# 📊 EPS Screener Core
def fetch_eps_data(symbols):
    results = []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol)
            
            # 1. Fetch Quarterly Financials
            df_q_income = ticker.quarterly_income_stmt
            df_q_balance = ticker.quarterly_balance_sheet
            
            if df_q_income is None or df_q_income.empty or 'Net Income' not in df_q_income.index:
                print(f"Skipping {symbol}: Missing Income Statement")
                continue

            net_income = df_q_income.loc['Net Income']
            
            # 2. Calculate O'Neil YoY EPS Growth (Current Quarter vs same Q last year)
            # yfinance columns: [0] is latest, [4] is 4 quarters ago
            if len(net_income) >= 5:
                q_growth_latest = round(((net_income.iloc[0] - net_income.iloc[4]) / abs(net_income.iloc[4])) * 100, 2)
                # Previous 2 quarters for the "Q1|Q2|Q3" display
                q_growth_prev1 = round(((net_income.iloc[1] - net_income.iloc[5]) / abs(net_income.iloc[5])) * 100, 2) if len(net_income) >= 6 else 0
                q_growth_prev2 = round(((net_income.iloc[2] - net_income.iloc[6]) / abs(net_income.iloc[6])) * 100, 2) if len(net_income) >= 7 else 0
                eps_growth = [q_growth_prev2, q_growth_prev1, q_growth_latest]
            else:
                print(f"Skipping {symbol}: Insufficient historical quarters for YoY")
                continue

            # 3. Manual ROE Calculation (Annualized)
            roe = 0
            if df_q_balance is not None and not df_q_balance.empty:
                # Try common equity labels used by yfinance
                equity_labels = ['Stockholders Equity', 'Total Equity Gross Minority Interest']
                latest_equity = None
                for label in equity_labels:
                    if label in df_q_balance.index:
                        latest_equity = df_q_balance.loc[label].iloc[0]
                        break
                
                if latest_equity and latest_equity > 0:
                    # Annualize quarterly net income (Net Income * 4) / Total Equity
                    roe = round(((net_income.iloc[0] * 4) / latest_equity) * 100, 2)
            
            # Fallback to info if manual calculation failed
            if roe == 0:
                roe = round(ticker.info.get('returnOnEquity', 0) * 100, 2)

            # 4. Filter: O'Neil looks for 25%+ Growth and 17%+ ROE
            if q_growth_latest >= 20 or roe >= 15: # Slightly lower for screening visibility
                info = ticker.info
                hist = ticker.history(period="5d")
                
                entry = {
                    "symbol": symbol,
                    "symbol_clean": symbol.replace(".NS", ""),
                    "date": datetime.today().strftime("%d-%b-%Y"),
                    "price": round(info.get("currentPrice", 0), 2),
                    "volume": int(hist["Volume"].iloc[-1]) if not hist.empty else 0,
                    "delivery": round(info.get("averageVolume", 0), 2),
                    "eps_growth": eps_growth,
                    "roc_21d": roe, # This maps to your ROE% column
                    "rs_vs_index_21d": round(info.get("beta", 0) * 100, 2)
                }
                results.append(entry)
                save_to_db(entry)
                
        except Exception as e:
            print(f"⚠️ Error processing {symbol}: {e}")

    results.sort(key=lambda x: x["eps_growth"][2], reverse=True)
    return results

# 🔘 Route: EPS Screener View
@eps_bp.route("/eps-screener")
def eps_screener_view():
    return render_template("eps_screener.html", results_csv=[], results_manual=[], source_name=None)

# 📂 Route: Screener from CSV
@eps_bp.route("/eps-screener/from-file", methods=["POST"])
def eps_screener_from_file():
    path = "data/MCAPge250cr-2.csv"
    if not os.path.exists(path):
        return render_template("eps_screener.html", results_csv=[], results_manual=[], error=f"⚠️ Source file not found: {path}")

    df = pd.read_csv(path)
    symbols = [s + ".NS" for s in df["symbol"].dropna().unique()]
    source_name = "MCAPge250cr-50 Only"
    cache_key = "|".join(sorted(symbols))

    if cache_key in eps_cache:
        results_csv = eps_cache[cache_key]
    else:
        results_csv = fetch_eps_data(symbols)
        eps_cache[cache_key] = results_csv

    return render_template("eps_screener.html", results_csv=results_csv, results_manual=[], source_name=source_name)

# 🔍 Route: Screener from Form
@eps_bp.route("/eps-screener/from-form", methods=["POST"])
def eps_screener_from_form():
    symbol = request.form.get("symbol", "").upper().strip()
    if not symbol:
        flash("⚠️ Please enter a stock symbol.", "error")
        return render_template("eps_screener.html", results_csv=[], results_manual=[])

    if not symbol.endswith(".NS"):
        symbol += ".NS"

    results_manual = fetch_eps_data([symbol])
    return render_template("eps_screener.html", results_csv=[], results_manual=results_manual)

# 🚀 Route: Stage 2 + EPS Screener
@eps_bp.route("/eps-screener/stage2-eps", methods=["POST"])
def eps_screener_stage2_eps():
    path = "data/stage2.csv"
    if not os.path.exists(path):
        flash(f"⚠️ Stage 2 source file not found: {path}", "error")
        return render_template("eps_screener.html", results_csv=[], results_manual=[], source_name=None)

    df = pd.read_csv(path)
    symbols = [s + ".NS" if not s.endswith(".NS") else s for s in df["symbol"].dropna().unique()]
    source_name = "Stage 2 + EPS Surge"
    cache_key = "stage2|" + "|".join(sorted(symbols))

    if cache_key in eps_cache:
        results_csv = eps_cache[cache_key]
        summary = f"✅ Loaded {len(results_csv)} stocks from cache."
    else:
        results_csv = fetch_eps_data(symbols)
        eps_cache[cache_key] = results_csv
        summary = f"✅ Found {len(results_csv)} stocks with EPS surge from Stage 2 list."

    if not results_csv:
        flash("⚠️ No stocks met the EPS surge criteria.", "warning")

    return render_template("eps_screener.html", results_csv=results_csv, results_manual=[], source_name=source_name, summary=summary)

@eps_bp.route("/eps-screener/history", methods=["GET", "POST"])
def eps_screener_history():
    symbol_filter = request.form.get("symbol", "").upper().strip() if request.method == "POST" else ""
    date_filter = request.form.get("date", "").strip() if request.method == "POST" else ""

    try:
        query = EPSScreenerResult.query

        if symbol_filter:
            query = query.filter(EPSScreenerResult.symbol_clean == symbol_filter)

        if date_filter:
            query = query.filter(EPSScreenerResult.screener_date == date_filter)

        rows = query.order_by(
            EPSScreenerResult.screener_date.desc(),
            EPSScreenerResult.eps_growth_q3.desc()
        ).all()

    except Exception as e:
        flash(f"⚠️ Error loading history: {str(e)}", "error")
        rows = []

    return render_template("eps_surge_history.html", rows=rows, symbol_filter=symbol_filter, date_filter=date_filter)