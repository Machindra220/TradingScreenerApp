from flask import request, redirect, url_for, Blueprint, render_template, flash
import pandas as pd
import yfinance as yf
import os
from functools import lru_cache
from datetime import datetime, timedelta
from sqlalchemy import and_
from app.extensions import db
from app.models import DeliverySurgeStock

performers_bp = Blueprint("performers", __name__)

@lru_cache(maxsize=128)
def get_1yr_return(symbol, suffix=".NS"):
    try:
        ticker = yf.Ticker(symbol + suffix)
        hist = ticker.history(period="1y", interval="1d")
        if hist.empty or len(hist) < 2:
            return None
        start_price = hist["Close"].iloc[0]
        end_price = hist["Close"].iloc[-1]
        last_processed_time = datetime.now()  # or from your data pipeline
        return {
            "start_price": round(start_price, 2),
            "end_price": round(end_price, 2),
            "return_pct": round(((end_price - start_price) / start_price) * 100, 2),
            "last_processed_time": last_processed_time 
        }
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None

def get_top_performers(csv_file, top_n=12, suffix=".NS"):
    if not os.path.exists(csv_file):
        flash(f"CSV file not found: {csv_file}", "error")
        return []

    try:
        df = pd.read_csv(csv_file)
        df.columns = df.columns.str.strip().str.lower()
        results = []

        for symbol in df["symbol"]:
            data = get_1yr_return(symbol, suffix)
            if data:
                results.append({
                    "symbol": symbol,
                    "start_price": float(data["start_price"]),
                    "end_price": float(data["end_price"]),
                    "return_pct": float(data["return_pct"]),
                    "current_price": float(data["end_price"]),
                })

        top_df = pd.DataFrame(results).sort_values(by="return_pct", ascending=False).head(top_n)
        top_df.reset_index(drop=True, inplace=True)
        top_df["rank"] = top_df.index + 1
        return top_df.to_dict(orient="records")
    except Exception as e:
        flash(f"Error processing {csv_file}: {e}", "error")
        return []

@performers_bp.route("/top-performers", methods=["GET"])
def top_performers_view():
    return render_template("top_performers.html",
                           nifty_200=[],
                           nifty_500=[],
                           bse_200=[],
                           overlap_n200_bse=set(),
                           overlap_n200_n500=set(),
                           overlap_bse_n500=set(),
                           overlap_all=set(),
                           last_processed_time=None,
                           summary_message=None)

@performers_bp.route("/top-performers", methods=["POST"])
def top_performers_process():
    nifty_200 = get_top_performers("data/nifty_200.csv", top_n=25, suffix=".NS")
    nifty_500 = get_top_performers("data/nifty_500.csv", top_n=25, suffix=".NS")
    bse_200 = get_top_performers("data/bse_200.csv", top_n=25, suffix=".BO")

    n200_set = set([s["symbol"] for s in nifty_200])
    n500_set = set([s["symbol"] for s in nifty_500])
    bse_set = set([s["symbol"] for s in bse_200])
    
    overlap_n200_bse = n200_set & bse_set
    overlap_n200_n500 = n200_set & n500_set
    overlap_bse_n500 = bse_set & n500_set
    overlap_all = n200_set & bse_set & n500_set
    last_processed_time = datetime.now()

    summary_message = f"✅ Screener completed at {last_processed_time.strftime('%d %b %Y %I:%M %p')}"

    return render_template("top_performers.html",
                           nifty_200=nifty_200,
                           nifty_500=nifty_500,
                           bse_200=bse_200,
                           overlap_n200_bse=overlap_n200_bse,
                           overlap_n200_n500=overlap_n200_n500,
                           overlap_bse_n500=overlap_bse_n500,
                           overlap_all=overlap_all,
                           last_processed_time=last_processed_time,
                           summary_message=summary_message)


@performers_bp.route("/upload-csv", methods=["POST"])
def upload_csv():
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a valid CSV file.", "error")
        return redirect(url_for("performers.top_performers"))

    save_path = os.path.join("data", file.filename)
    file.save(save_path)
    flash(f"Uploaded {file.filename} successfully.", "info")
    return redirect(url_for("performers.top_performers"))

# delivery_surge screener code -MCAPge250cr/nifty_750
def load_nifty500_tickers():
    df = pd.read_csv("data/MCAPge250cr.csv")
    df.columns = df.columns.str.strip().str.lower()
    return [s + ".NS" for s in df["symbol"].dropna().unique()]

def analyze_stock(ticker, benchmark_hist=None):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        market_cap = info.get("marketCap", 0)
        if market_cap < 100 * 10**7:
            return None

        hist = stock.history(period="30d")
        if hist.empty or len(hist) < 22:
            return None

        latest = hist.iloc[-1]
        avg_volume = hist["Volume"][:-1].mean()
        delivery_spike = latest["Volume"] / avg_volume

        roc = ((latest["Close"] - hist["Close"].iloc[-22]) / hist["Close"].iloc[-22]) * 100

        # ✅ Use cached benchmark history
        if benchmark_hist is None or benchmark_hist.empty or len(benchmark_hist) < 22:
            return None
        benchmark_roc = ((benchmark_hist["Close"].iloc[-1] - benchmark_hist["Close"].iloc[-22]) / benchmark_hist["Close"].iloc[-22]) * 100
        rs_vs_index = roc - benchmark_roc

        return {
            "ticker": ticker,
            "current_price": float(latest["Close"]),
            "price_change": float(latest["Close"] - latest["Open"]),
            "price_change_pct": round(float((latest["Close"] - latest["Open"]) / latest["Open"]) * 100, 2),
            "volume": int(latest["Volume"]),
            "delivery_spike": round(float(delivery_spike), 2),
            "market_cap": int(market_cap),
            "roc_21d": round(float(roc), 2),
            "rs_vs_index_21d": round(float(rs_vs_index), 2)
        }

    except Exception as e:
        print(f"Error analyzing {ticker}: {e}")
        return None

def filter_delivery_surge_stocks(save_to_db=True):
    tickers = load_nifty500_tickers()
    benchmark_hist = yf.Ticker("^NSEI").history(period="30d")
    today = datetime.today().date()
    results = []
    inserted = 0
    updated = 0

    for ticker in tickers:
        data = analyze_stock(ticker, benchmark_hist=benchmark_hist)
        if not data:
            continue
        if (
            data["price_change"] > 0 and
            data["volume"] > 20000 and
            data["delivery_spike"] >= 4
        ):
            results.append(data)

            if save_to_db:
                existing = DeliverySurgeStock.query.filter(
                    and_(
                        DeliverySurgeStock.symbol == ticker,
                        DeliverySurgeStock.date == today
                    )
                ).first()

                if existing:
                    fields = {
                        "price": data["current_price"],
                        "volume": data["volume"],
                        "delivery_spike": data["delivery_spike"],
                        "roc_21d": data["roc_21d"],
                        "rs_vs_index_21d": data["rs_vs_index_21d"]
                    }
                    changed = False
                    for field, new_val in fields.items():
                        if getattr(existing, field) != new_val:
                            setattr(existing, field, new_val)
                            changed = True
                    if changed:
                        db.session.add(existing)
                        updated += 1
                else:
                    db.session.add(DeliverySurgeStock(
                        symbol=ticker,
                        date=today,
                        price=data["current_price"],
                        volume=data["volume"],
                        delivery_spike=data["delivery_spike"],
                        roc_21d=data["roc_21d"],
                        rs_vs_index_21d=data["rs_vs_index_21d"]
                    ))
                    inserted += 1

    if save_to_db:
        db.session.commit()

    summary_message = f"✅ Updated {updated} stocks, added {inserted} new"
    return results, summary_message


# app/routes/performers.py
delivery_bp = Blueprint("delivery", __name__)

@delivery_bp.route("/delivery-surge", methods=["GET"])
def delivery_surge_view():
    return render_template("delivery_surge.html",
                           stocks=[],
                           summary_message=None,
                           last_processed_time=None,
                           sort_by="delivery_spike")

@delivery_bp.route("/delivery-surge", methods=["POST"])
def delivery_surge_process():
    sort_by = request.form.get("sort", "delivery_spike")

    stocks, summary_message = filter_delivery_surge_stocks(save_to_db=True)

    if sort_by == "roc":
        stocks.sort(key=lambda x: x["roc_21d"], reverse=True)
    elif sort_by == "rs":
        stocks.sort(key=lambda x: x["rs_vs_index_21d"], reverse=True)
    else:
        stocks.sort(key=lambda x: x["delivery_spike"], reverse=True)

    return render_template("delivery_surge.html",
                           stocks=stocks,
                           summary_message=summary_message,
                           last_processed_time=datetime.now(),
                           sort_by=sort_by)



# Delivery Surge History Route
@delivery_bp.route("/delivery/history")
def delivery_history():
    cutoff = datetime.today().date() - timedelta(days=30)
    symbol_filter = request.args.get("symbol", "").upper().strip()
    date_filter = request.args.get("date", "").strip()

    query = DeliverySurgeStock.query.filter(DeliverySurgeStock.date >= cutoff)

    if symbol_filter:
        query = query.filter(DeliverySurgeStock.symbol.ilike(f"%{symbol_filter}%"))

    if date_filter:
        try:
            parsed_date = datetime.strptime(date_filter, "%Y-%m-%d").date()
            query = query.filter(DeliverySurgeStock.date == parsed_date)
        except ValueError:
            flash("⚠️ Invalid date format. Please use YYYY-MM-DD.", "error")

    stocks = query.order_by(DeliverySurgeStock.date.desc()).all()

    counts = db.session.query(
        DeliverySurgeStock.symbol,
        db.func.count(DeliverySurgeStock.date).label("days_present")
    ).filter(DeliverySurgeStock.date >= cutoff).group_by(DeliverySurgeStock.symbol).all()

    presence_map = {symbol: days for symbol, days in counts}

    enriched = []
    for stock in stocks:
        days = presence_map.get(stock.symbol, 0)
        tag = (
            "🔥 30D" if days >= 30 else
            "📆 15D" if days >= 15 else
            "🕒 7D" if days >= 7 else
            "⏳ 3D" if days >= 3 else ""
        )
        enriched.append({
            "date": stock.date,
            "symbol": stock.symbol,
            "symbol_clean": stock.symbol.replace(".NS", ""),
            "price": stock.price,
            "volume": stock.volume,
            "delivery_spike": stock.delivery_spike,
            "roc_21d": stock.roc_21d,
            "rs_vs_index_21d": stock.rs_vs_index_21d,
            "days_present": days,
            "tag": tag
        })

    return render_template("delivery_history.html",
                           stocks=enriched,
                           symbol_filter=symbol_filter,
                           date_filter=date_filter)
