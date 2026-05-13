from flask import Blueprint, render_template,request
import pandas as pd
import yfinance as yf
from datetime import date, datetime, timedelta
from app.models import Stage2Stock
from app.routes.sector_analysis import analyze_sector
from app.extensions import db
from sqlalchemy import func
import os
import re

screener_bp = Blueprint("screener", __name__)

# 📦 slugify file names - only used in sector analysis
def slugify(text):
        return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')

# 📦 Fetch weekly data
def fetch_weekly_data(symbol, weeks=60):
    ticker = yf.Ticker(symbol)
    return ticker.history(period=f"{weeks}wk", interval="1wk")

# 📈 Compute relative strength
def compute_relative_strength(stock_df, index_df):
    rs = stock_df["Close"] / index_df["Close"]
    return rs.rolling(window=10).mean()

# 🧠 Stage 2 logic
def is_stage2(stock_symbol, index_symbol="^NSEI"):
    try:
        stock_df = fetch_weekly_data(stock_symbol)
        index_df = fetch_weekly_data(index_symbol)

        if stock_df.empty or index_df.empty or len(stock_df) < 35:
            return None

        stock_df["30w_ma"] = stock_df["Close"].rolling(window=30).mean()
        stock_df["vol_avg"] = stock_df["Volume"].rolling(window=10).mean()
        stock_df["rs"] = compute_relative_strength(stock_df, index_df)

        latest = stock_df.iloc[-1]
        prev = stock_df.iloc[-2]

        conditions = [
            latest["Close"] > latest["30w_ma"],
            latest["30w_ma"] > prev["30w_ma"],
            latest["Volume"] > latest["vol_avg"],
            latest["rs"] > prev["rs"]
        ]

        if all(conditions):
            return {
                "symbol": stock_symbol,
                "price": round(latest["Close"], 2),
                "30w_ma": round(latest["30w_ma"], 2),
                "volume": int(latest["Volume"]),
                "vol_avg": int(latest["vol_avg"]),
                "rs": round(latest["rs"], 2)
            }
    except Exception as e:
        print(f"Error screening {stock_symbol}: {e}")
    return None

# 🧪 Screen and rank
def screen_stage2(symbols):
    results = []
    for symbol in symbols:
        data = is_stage2(symbol)
        if data and "rs" in data:
            results.append(data)

    if not results:
        return pd.DataFrame()  # return empty DataFrame safely

    df = pd.DataFrame(results)

    if "rs" not in df.columns or df["rs"].isnull().all():
        print("⚠️ No valid 'rs' values found in results.")
        return pd.DataFrame()  # avoid sorting if rs is missing or all null

    df = df[df["rs"].notnull()]  # drop rows with missing rs
    df.sort_values(by="rs", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    df["rank"] = df.index + 1
    return df


# 💾 Save to DB
def save_screened_stocks(df):
    now = datetime.now()
    updated, inserted = 0, 0

    for _, row in df.iterrows():
        existing = Stage2Stock.query.filter_by(symbol=row["symbol"], date=now.date()).first()

        if existing:
            existing.price = row["price"]
            existing.ma_30w = row["30w_ma"]
            existing.volume = row["volume"]
            existing.vol_avg = row["vol_avg"]
            existing.rs = row["rs"]
            updated += 1
        else:
            stock = Stage2Stock(
                symbol=row["symbol"],
                date=now,  # full datetime
                price=row["price"],
                ma_30w=row["30w_ma"],
                volume=row["volume"],
                vol_avg=row["vol_avg"],
                rs=row["rs"]
            )
            db.session.add(stock)
            inserted += 1

    db.session.commit()
    return updated, inserted

# 🧹 Auto-delete old records
def delete_old_stage2_records():
    cutoff = date.today() - timedelta(days=30)
    deleted = Stage2Stock.query.filter(Stage2Stock.date < cutoff).delete()
    db.session.commit()
    print(f"🧹 Deleted {deleted} old Stage 2 records older than 30 days.")

# 🔁 Get persistence map
def get_persistent_stage2_stocks():
    cutoff = date.today() - timedelta(days=30)
    counts = db.session.query(
        Stage2Stock.symbol,
        func.count(Stage2Stock.date).label("days_present")
    ).filter(Stage2Stock.date >= cutoff).group_by(Stage2Stock.symbol).all()
    return {symbol: days for symbol, days in counts}

# 🌐 Main stage2 screener route
@screener_bp.route("/stage2", methods=["GET"])
def stage2_view():
    return render_template("stage2.html",
                           stocks=[],
                           last_processed_time=None,
                           source_name="MCAPge250cr",
                           summary_message=None,
                           error=None)

@screener_bp.route("/stage2", methods=["POST"])
def stage2_process():
    path = "data/MCAPge250cr.csv"
    if not os.path.exists(path):
        return render_template("stage2.html", error=f"⚠️ Source file not found: {path}")

    df = pd.read_csv(path)
    symbols = [s + ".NS" for s in df["symbol"].dropna().unique()]
    source_name = "MCAPge250cr"

    results = screen_stage2(symbols)
    delete_old_stage2_records()

    presence_map = get_persistent_stage2_stocks()
    enriched = []
    for stock in results.to_dict(orient="records"):
        stock["symbol_clean"] = stock["symbol"].replace(".NS", "")
        days = presence_map.get(stock["symbol"], 0)
        stock["persistence"] = f"{days} days"
        stock["tag"] = (
            "🔥 30D" if days >= 30 else
            "📆 15D" if days >= 15 else
            "🕒 7D" if days >= 7 else
            "⏳ 3D" if days >= 3 else ""
        )
        enriched.append(stock)

    last_processed_time = datetime.now().strftime("%d %b %Y %I:%M %p")
    updated, inserted = save_screened_stocks(results)
    summary_message = f"✅ Updated {updated} stocks, added {inserted} new"

    return render_template("stage2.html", stocks=enriched,
                           last_processed_time=last_processed_time,
                           source_name=source_name,
                           summary_message=summary_message,
                           error=None)

# 📦 Saved stocks view
@screener_bp.route("/stage2/saved")
def stage2_saved():
    symbol_filter = request.args.get("symbol", "").strip().upper()
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    preset = request.args.get("preset", "")
    single = request.args.get("single", "")

    # Default cutoff: last 30 days
    cutoff = date.today() - timedelta(days=30)
    query = Stage2Stock.query.filter(Stage2Stock.date >= cutoff)

    # Apply preset quick filters
    if preset:
        try:
            days = int(preset)
            cutoff = date.today() - timedelta(days=days)
            query = Stage2Stock.query.filter(Stage2Stock.date >= cutoff)
        except:
            pass

    # Apply symbol filter
    if symbol_filter:
        query = query.filter(Stage2Stock.symbol.ilike(f"%{symbol_filter}%"))

    # Apply date range filters
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            query = query.filter(Stage2Stock.date >= start_dt)
        except:
            pass

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = query.filter(Stage2Stock.date <= end_dt)
        except:
            pass

    # Fetch filtered stocks
    stocks = query.order_by(Stage2Stock.date.desc()).all()

    # ✅ Apply "Show Latest Only" AFTER all filters
    if single == "true":
        latest_map = {}
        for stock in stocks:
            if stock.symbol not in latest_map:
                latest_map[stock.symbol] = stock
        stocks = list(latest_map.values())

    # Persistence counts
    counts = db.session.query(
        Stage2Stock.symbol,
        func.count(Stage2Stock.date).label("days_present")
    ).filter(Stage2Stock.date >= cutoff).group_by(Stage2Stock.symbol).all()
    presence_map = {symbol: days for symbol, days in counts}

    # Enrich for display
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
            "ma_30w": stock.ma_30w,
            "volume": stock.volume,
            "vol_avg": stock.vol_avg,
            "rs": stock.rs,
            "days_present": days,
            "tag": tag
        })

    return render_template("stage2_saved.html",
                           stocks=enriched,
                           symbol_filter=symbol_filter,
                           start_date=start_date,
                           end_date=end_date,
                           preset=preset,
                           single=single)


# 📊 Sector analysis route
@screener_bp.route("/sector-analysis", methods=["GET"])
def sector_analysis_view():
    return render_template("sector_analysis.html",
                           sectors=[],
                           summary_message=None,
                           error=None)

@screener_bp.route("/sector-analysis", methods=["POST"])
def sector_analysis_process():
    path = "data/sector_list.csv"
    if not os.path.exists(path):
        return render_template("sector_analysis.html", error=f"⚠️ Source file not found: {path}", sectors=[])

    try:
        df = pd.read_csv(path)
    except Exception as e:
        return render_template("sector_analysis.html", error=f"⚠️ Failed to read CSV: {e}", sectors=[])

    if "index_symbol" not in df.columns:
        return render_template("sector_analysis.html", error="⚠️ 'index_symbol' column missing in sector_list.csv", sectors=[])

    results = []
    for _, row in df.iterrows():
        index_symbol = row["index_symbol"]
        if pd.isna(index_symbol) or not str(index_symbol).strip():
            continue

        data = analyze_sector(index_symbol)
        if data:
            data["sector"] = row["sector"]
            results.append(data)

    tag_order = {"🔥 Strong": 1, "🌱 Emerging": 2, "⚠️ Weak": 3, "⏸ Neutral": 4}
    results.sort(key=lambda x: tag_order.get(x.get("tag", ""), 5))

    for i, sector in enumerate(results, start=1):
        sector["serial"] = i

    summary_message = f"✅ Sector analysis completed at {datetime.now().strftime('%d %b %Y %I:%M %p')}"

    return render_template("sector_analysis.html",
                           sectors=results,
                           summary_message=summary_message,
                           error=None)

# ✅ Validate sector files on startup
def validate_sector_files(sector_csv_path="data/sector_list.csv", sector_dir="data/sectors"):
    errors = []

    try:
        df = pd.read_csv(sector_csv_path)
    except Exception as e:
        errors.append(f"Failed to read sector list: {e}")
        df = None

    if df is not None and "sector" not in df.columns:
        errors.append("'sector' column missing in sector_list.csv")
        return errors

    if df is not None:
        for sector in df["sector"].dropna().unique():
            filename = slugify(sector) + ".csv"
            path = os.path.join(sector_dir, filename)

            if not os.path.exists(path):
                errors.append(f"Missing file: {path}")
            else:
                try:
                    pd.read_csv(path)
                except Exception as e:
                    errors.append(f"Failed to read {path}: {e}")

    if errors:
        print("\n Sector File Validation Report:")
        for err in errors:
            print(err)
    else:
        print("All sector files validated successfully.")

# Run validation once when screener.py is loaded
validate_sector_files()

# ✅ End of Validation of sector files

# 🔍 Sector-wise stocks
@screener_bp.route("/sector-analysis/<sector>")
def sector_stocks(sector):
    path = f"data/sectors/{slugify(sector)}.csv"
    if not os.path.exists(path):
        return render_template("sector_stocks.html", error=f"⚠️ Sector file not found: {path}", sector=sector)

    df = pd.read_csv(path)
    symbols = [s + ".NS" for s in df["symbol"].dropna().unique()]
    results = screen_stage2(symbols)

    if results.empty:
        return render_template("sector_stocks.html", error="⚠️ No valid Stage 2 stocks found for this sector.", sector=sector)

    presence_map = get_persistent_stage2_stocks()
    enriched = []
    for stock in results.to_dict(orient="records"):
        stock["symbol_clean"] = stock["symbol"].replace(".NS", "")
        days = presence_map.get(stock["symbol"], 0)
        stock["persistence"] = f"{days} days"
        stock["tag"] = (
            "🔥 30D" if days >= 30 else
            "📆 15D" if days >= 15 else
            "🕒 7D" if days >= 7 else
            "⏳ 3D" if days >= 3 else ""
        )
        enriched.append(stock)

    return render_template("sector_stocks.html", sector=sector, stocks=enriched)



