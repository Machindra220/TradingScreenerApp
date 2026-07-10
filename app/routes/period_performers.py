import os
import json
import io
import pandas as pd
import yfinance as yf
from datetime import datetime, date, timedelta
from flask import request, redirect, url_for, Blueprint, render_template, flash, send_file

period_performers_bp = Blueprint("period_performers", __name__)
delivery_bp = Blueprint("delivery", __name__)

# --- PATH COMPARTMENTALIZATION & CACHE CHANNELS ---
UPLOAD_FOLDER = os.path.abspath(os.path.join(os.getcwd(), 'uploads', 'period_performers'))
RESULTS_JSON = os.path.join(UPLOAD_FOLDER, 'last_period_performers_results.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Fixed lookback intervals (trading days)
LB_1M = 21
LB_3M = 55
LB_6M = 122

# ==============================================================================
# 📊 MODULE 1: OPTIMIZED SINGLE-PASS PERIOD PERFORMERS MATRIX
# ==============================================================================

def load_index_symbols(csv_path):
    """Safely extracts tickers from local files and strips any broken '$' prefixes."""
    if not os.path.exists(csv_path):
        print(f"⚠️ Reference file missing at: {csv_path}")
        return []
    try:
        df = pd.read_csv(csv_path)
        df.columns = df.columns.str.strip().str.lower()
        if 'symbol' in df.columns:
            return [str(s).strip().upper().replace('$', '') for s in df['symbol'].dropna().unique()]
    except Exception as e:
        print(f"Error loading {csv_path}: {e}")
    return []

def calculate_multi_period_performance(symbols, suffix=".NS", top_n=35, category_prefix="default", previous_ranks=None):
    """
    Downloads historical data in a single batch to calculate 1M, 3M, 
    and 6M returns using fast, local matrix operations.
    """
    if not symbols:
        return [], [], []
    if previous_ranks is None:
        previous_ranks = {}

    formatted_symbols = [f"{s}{suffix}" if not s.endswith(suffix) else s for s in symbols]
    
    # Single batch download captures all data at once
    data = yf.download(formatted_symbols, period="1y", interval="1d", auto_adjust=True, progress=False)
    if data.empty or 'Close' not in data:
        return [], [], []

    close_data = data['Close']
    if not isinstance(close_data, pd.DataFrame):
        close_data = close_data.to_frame()

    perf_1m_list, perf_3m_list, perf_6m_list = [], [], []

    for sym in symbols:
        yf_sym = f"{sym}{suffix}"
        if yf_sym not in close_data.columns:
            continue

        series = close_data[yf_sym].dropna()
        if len(series) < 10:
            continue

        curr_price = float(series.iloc[-1])

        # Local lookback slicing avoids redundant network calls
        if len(series) >= LB_6M:
            start_6m = float(series.iloc[-LB_6M])
            ret_6m = round(((curr_price - start_6m) / start_6m) * 100, 2)
            perf_6m_list.append({"symbol": sym, "start_price": round(start_6m, 2), "end_price": round(curr_price, 2), "return_pct": ret_6m})

        if len(series) >= LB_3M:
            start_3m = float(series.iloc[-LB_3M])
            ret_3m = round(((curr_price - start_3m) / start_3m) * 100, 2)
            perf_3m_list.append({"symbol": sym, "start_price": round(start_3m, 2), "end_price": round(curr_price, 2), "return_pct": ret_3m})

        if len(series) >= LB_1M:
            start_1m = float(series.iloc[-LB_1M])
            ret_1m = round(((curr_price - start_1m) / start_1m) * 100, 2)
            perf_1m_list.append({"symbol": sym, "start_price": round(start_1m, 2), "end_price": round(curr_price, 2), "return_pct": ret_1m})

    # Sort and rank top performers locally while calculating delta shifts
    def finalize_rankings(raw_list, timeframe_key):
        if not raw_list: return []
        df_res = pd.DataFrame(raw_list).sort_values(by="return_pct", ascending=False).head(top_n)
        df_res.reset_index(drop=True, inplace=True)
        
        final_list = []
        cat_full_key = f"{category_prefix}_{timeframe_key}"
        
        for idx, row in df_res.iterrows():
            current_rank = idx + 1
            sym = row["symbol"]
            
            # Extract previous rank position for rank change directional analysis
            prev_rank = previous_ranks.get(cat_full_key, {}).get(sym)
            
            if prev_rank is None:
                rank_change_text = "🆕 NEW"
                rank_change_class = "bg-emerald-50 text-emerald-700 border border-emerald-200 px-1.5 py-0.5 rounded font-bold"
            else:
                diff = int(prev_rank) - int(current_rank)
                if diff > 0:
                    rank_change_text = f"▲ Up {diff}"
                    rank_change_class = "text-green-600 font-bold"
                elif diff < 0:
                    rank_change_text = f"▼ Down {abs(diff)}"
                    rank_change_class = "text-rose-500 font-semibold"
                else:
                    rank_change_text = "■ Static"
                    rank_change_class = "text-gray-400 font-mono"

            item_dict = row.to_dict()
            item_dict["rank"] = current_rank
            item_dict["rank_change_text"] = rank_change_text
            item_dict["rank_change_class"] = rank_change_class
            final_list.append(item_dict)
            
        return final_list

    return finalize_rankings(perf_1m_list, "1m"), finalize_rankings(perf_3m_list, "3m"), finalize_rankings(perf_6m_list, "6m")

@period_performers_bp.route("/top-performers", methods=["GET"])
def period_performers_view():
    """GET requests restore data from disk cache to prevent layout flashing on revisit."""
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            try:
                c = json.load(f)
                return render_template(
                    "period_performers.html",
                    nifty_200_1m=c.get("nifty_200_1m", []), nifty_200_3m=c.get("nifty_200_3m", []), nifty_200_6m=c.get("nifty_200_6m", []),
                    nifty_500_1m=c.get("nifty_500_1m", []), nifty_500_3m=c.get("nifty_500_3m", []), nifty_500_6m=c.get("nifty_500_6m", []),
                    bse_200_1m=c.get("bse_200_1m", []), bse_200_3m=c.get("bse_200_3m", []), bse_200_6m=c.get("bse_200_6m", []),
                    bse_500_1m=c.get("bse_500_1m", []), bse_500_3m=c.get("bse_500_3m", []), bse_500_6m=c.get("bse_500_6m", []),
                    last_processed_time=c.get("last_processed_time"), summary_message=c.get("summary_message")
                )
            except Exception: pass

    return render_template("period_performers.html",
                           nifty_200_1m=[], nifty_200_3m=[], nifty_200_6m=[],
                           nifty_500_1m=[], nifty_500_3m=[], nifty_500_6m=[],
                           bse_200_1m=[], bse_200_3m=[], bse_200_6m=[],
                           bse_500_1m=[], bse_500_3m=[], bse_500_6m=[],
                           last_processed_time=None, summary_message=None)

@period_performers_bp.route("/period-performers/run", methods=["POST"])
def period_performers_run():
    """Runs single-pass multi-timeframe scans across all 4 reference indexes simultaneously."""
    previous_ranks = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, 'r') as f:
            try:
                old_cache = json.load(f)
                for cat in ["nifty_200", "nifty_500", "bse_200", "bse_500"]:
                    for tf in ["1m", "3m", "6m"]:
                        full_key = f"{cat}_{tf}"
                        previous_ranks[full_key] = {s["symbol"]: s["rank"] for s in old_cache.get(full_key, [])}
            except Exception: pass

    # Load raw tickers from storage files
    n200_syms = load_index_symbols("data/nifty_200.csv")
    n500_syms = load_index_symbols("data/nifty_500.csv")
    b200_syms = load_index_symbols("data/bse_200.csv")
    b500_syms = load_index_symbols("data/BSE_500.csv") 

    # Compute index subsets concurrently using single-pass logic (Increased top_n to 35)
    n200_1m, n200_3m, n200_6m = calculate_multi_period_performance(n200_syms, suffix=".NS", top_n=35, category_prefix="nifty_200", previous_ranks=previous_ranks)
    n500_1m, n500_3m, n500_6m = calculate_multi_period_performance(n500_syms, suffix=".NS", top_n=35, category_prefix="nifty_500", previous_ranks=previous_ranks)
    b200_1m, b200_3m, b200_6m = calculate_multi_period_performance(b200_syms, suffix=".BO", top_n=35, category_prefix="bse_200", previous_ranks=previous_ranks)
    b500_1m, b500_3m, b500_6m = calculate_multi_period_performance(b500_syms, suffix=".BO", top_n=35, category_prefix="bse_500", previous_ranks=previous_ranks)

    last_time = datetime.now().strftime("%d %b %Y %I:%M %p")
    summary = f"✅ Analysis Complete. Multi-Period metrics compiled successfully."

    payload = {
        "nifty_200_1m": n200_1m, "nifty_200_3m": n200_3m, "nifty_200_6m": n200_6m,
        "nifty_500_1m": n500_1m, "nifty_500_3m": n500_3m, "nifty_500_6m": n500_6m,
        "bse_200_1m": b200_1m, "bse_200_3m": b200_3m, "bse_200_6m": b200_6m,
        "bse_500_1m": b500_1m, "bse_500_3m": b500_3m, "bse_500_6m": b500_6m,
        "last_processed_time": last_time, "summary_message": summary
    }

    with open(RESULTS_JSON, 'w') as f:
        json.dump(payload, f)

    return redirect(url_for("performers.top_performers_view"))

@period_performers_bp.route("/period-performers/export")
def export_all_excel():
    """Generates spreadsheets directly from your saved JSON cache without recalculating data."""
    if not os.path.exists(RESULTS_JSON):
        flash("No calculated cache records found to compile.", "error")
        return redirect(url_for("performers.top_performers_view"))

    with open(RESULTS_JSON, 'r') as f:
        cache = json.load(f)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for sheet_key in ["nifty_200_1m", "nifty_200_3m", "nifty_200_6m",
                          "nifty_500_1m", "nifty_500_3m", "nifty_500_6m",
                          "bse_200_1m", "bse_200_3m", "bse_200_6m",
                          "bse_500_1m", "bse_500_3m", "bse_500_6m"]:
            data_list = cache.get(sheet_key, [])
            if data_list:
                # Strip helper rendering properties from output spreadsheets
                clean_list = []
                for item in data_list:
                    d = item.copy()
                    d.pop('rank_change_text', None)
                    d.pop('rank_change_class', None)
                    clean_list.append(d)
                pd.DataFrame(clean_list).to_excel(writer, sheet_name=sheet_key[:31], index=False)

    output.seek(0)
    return send_file(output, as_attachment=True, download_name=f"Period_Performers_Snapshot_{datetime.now().strftime('%Y%m%d')}.xlsx")

@period_performers_bp.route("/upload-csv", methods=["POST"])
def upload_csv():
    file = request.files.get("csv_file")
    if not file or not file.filename.endswith(".csv"):
        flash("Please upload a valid CSV file.", "error")
        return redirect(url_for("performers.top_performers_view"))

    save_path = os.path.join("data", file.filename)
    file.save(save_path)
    flash(f"Uploaded {file.filename} successfully.", "info")
    return redirect(url_for("performers.top_performers_view"))


# ==============================================================================
# 🎯 MODULE 2: DELIVERY SURGE SCREENER MODULE (WITH DATABASE LOGGING)
# ==============================================================================

# Imported dynamically inside extension blocks to prevent circular framework bindings
from app.extensions import db
from app.models import DeliverySurgeStock

def load_nifty500_tickers():
    df = pd.read_csv("data/MCAPge250cr.csv")
    df.columns = df.columns.str.strip().str.lower()
    return [str(s).strip().upper().replace('$', '') + ".NS" for s in df["symbol"].dropna().unique()]

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

@delivery_bp.route("/delivery-surge", methods=["GET"])
def delivery_surge_view():
    return render_template("delivery_surge.html", stocks=[], summary_message=None, last_processed_time=None, sort_by="delivery_spike")

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

    return render_template("delivery_surge.html", stocks=stocks, summary_message=summary_message, last_processed_time=datetime.now(), sort_by=sort_by)

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

    return render_template("delivery_history.html", stocks=enriched, symbol_filter=symbol_filter, date_filter=date_filter)