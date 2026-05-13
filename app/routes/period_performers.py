from flask import Blueprint, render_template, flash
import pandas as pd
import yfinance as yf
import os
from functools import lru_cache
from datetime import datetime

period_performers_bp = Blueprint("period_performers", __name__)

@lru_cache(maxsize=128)
def get_return(symbol: str, period: str = "6mo", suffix: str = ".NS"):
    """
    Fetch OHLC history for the given period and return start/end prices and % return.
    """
    try:
        ticker = yf.Ticker(symbol + suffix)
        hist = ticker.history(period=period, interval="1d")
        if hist.empty or len(hist) < 2:
            return None
        start_price = float(hist["Close"].iloc[0])
        end_price = float(hist["Close"].iloc[-1])
        return {
            "start_price": round(start_price, 2),
            "end_price": round(end_price, 2),
            "return_pct": round(((end_price - start_price) / start_price) * 100, 2),
        }
    except Exception as e:
        print(f"Error fetching {symbol} ({period}): {e}")
        return None


def get_top_performers(csv_file: str, top_n: int, period: str, suffix: str):
    """
    Reads a CSV with a 'symbol' column and returns top N performers for the given period.
    """
    if not os.path.exists(csv_file):
        flash(f"CSV file not found: {csv_file}", "error")
        return []

    try:
        df = pd.read_csv(csv_file)
        df.columns = df.columns.str.strip().str.lower()
        if "symbol" not in df.columns:
            flash(f"'symbol' column missing in {csv_file}", "error")
            return []

        results = []
        for symbol in df["symbol"]:
            data = get_return(symbol, period=period, suffix=suffix)
            if data:
                results.append({
                    "symbol": symbol,
                    "start_price": data["start_price"],
                    "end_price": data["end_price"],
                    "return_pct": data["return_pct"],
                    "current_price": data["end_price"],
                })

        if not results:
            return []

        top_df = pd.DataFrame(results).sort_values(by="return_pct", ascending=False).head(top_n)
        top_df.reset_index(drop=True, inplace=True)
        top_df["rank"] = top_df.index + 1
        return top_df.to_dict(orient="records")
    except Exception as e:
        flash(f"Error processing {csv_file}: {e}", "error")
        return []


@period_performers_bp.route("/period-performers", methods=["GET"])
def period_performers_view():
    return render_template("period_performers.html",
                           nifty_200_6m=[], nifty_500_6m=[], bse_200_6m=[],
                           nifty_200_3m=[], nifty_500_3m=[], bse_200_3m=[],
                           nifty_200_1m=[], nifty_500_1m=[], bse_200_1m=[],
                           overlap_nifty_200=set(), overlap_nifty_500=set(), overlap_bse_200=set(),
                           last_processed_time=None,
                           summary_message=None)


@period_performers_bp.route("/period-performers/run", methods=["POST"])
def period_performers_run():
    # 6-Month Top 25
    nifty_200_6m = get_top_performers("data/nifty_200.csv", 25, "6mo", ".NS")
    nifty_500_6m = get_top_performers("data/nifty_500.csv", 25, "6mo", ".NS")
    bse_200_6m   = get_top_performers("data/bse_200.csv",   25, "6mo", ".BO")

    # 3-Month Top 20
    nifty_200_3m = get_top_performers("data/nifty_200.csv", 20, "3mo", ".NS")
    nifty_500_3m = get_top_performers("data/nifty_500.csv", 20, "3mo", ".NS")
    bse_200_3m   = get_top_performers("data/bse_200.csv",   20, "3mo", ".BO")

    # 1-Month Top 15
    nifty_200_1m = get_top_performers("data/nifty_200.csv", 15, "1mo", ".NS")
    nifty_500_1m = get_top_performers("data/nifty_500.csv", 15, "1mo", ".NS")
    bse_200_1m   = get_top_performers("data/bse_200.csv",   15, "1mo", ".BO")

    # Overlap analysis: stocks appearing in 1M, 3M, and 6M lists for each index
    n200_set_1m = set([s["symbol"] for s in nifty_200_1m])
    n200_set_3m = set([s["symbol"] for s in nifty_200_3m])
    n200_set_6m = set([s["symbol"] for s in nifty_200_6m])
    overlap_nifty_200 = n200_set_1m & n200_set_3m & n200_set_6m

    n500_set_1m = set([s["symbol"] for s in nifty_500_1m])
    n500_set_3m = set([s["symbol"] for s in nifty_500_3m])
    n500_set_6m = set([s["symbol"] for s in nifty_500_6m])
    overlap_nifty_500 = n500_set_1m & n500_set_3m & n500_set_6m

    bse_set_1m = set([s["symbol"] for s in bse_200_1m])
    bse_set_3m = set([s["symbol"] for s in bse_200_3m])
    bse_set_6m = set([s["symbol"] for s in bse_200_6m])
    overlap_bse_200 = bse_set_1m & bse_set_3m & bse_set_6m

    last_processed_time = datetime.now()
    summary_message = f"✅ Screeners completed at {last_processed_time.strftime('%d %b %Y %I:%M %p')}"

    return render_template("period_performers.html",
                           nifty_200_6m=nifty_200_6m, nifty_500_6m=nifty_500_6m, bse_200_6m=bse_200_6m,
                           nifty_200_3m=nifty_200_3m, nifty_500_3m=nifty_500_3m, bse_200_3m=bse_200_3m,
                           nifty_200_1m=nifty_200_1m, nifty_500_1m=nifty_500_1m, bse_200_1m=bse_200_1m,
                           overlap_nifty_200=overlap_nifty_200,
                           overlap_nifty_500=overlap_nifty_500,
                           overlap_bse_200=overlap_bse_200,
                           last_processed_time=last_processed_time,
                           summary_message=summary_message)


@period_performers_bp.route("/period-performers/export-all", methods=["GET"])
def export_all_excel():
    import io
    from flask import send_file

        # Collect all datasets
    nifty_200_6m = get_top_performers("data/nifty_200.csv", 25, "6mo", ".NS")
    nifty_500_6m = get_top_performers("data/nifty_500.csv", 25, "6mo", ".NS")
    bse_200_6m   = get_top_performers("data/bse_200.csv",   25, "6mo", ".BO")

    nifty_200_3m = get_top_performers("data/nifty_200.csv", 20, "3mo", ".NS")
    nifty_500_3m = get_top_performers("data/nifty_500.csv", 20, "3mo", ".NS")
    bse_200_3m   = get_top_performers("data/bse_200.csv",   20, "3mo", ".BO")

    nifty_200_1m = get_top_performers("data/nifty_200.csv", 15, "1mo", ".NS")
    nifty_500_1m = get_top_performers("data/nifty_500.csv", 15, "1mo", ".NS")
    bse_200_1m   = get_top_performers("data/bse_200.csv",   15, "1mo", ".BO")

    # Create Excel with multiple sheets
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        pd.DataFrame(nifty_200_6m).to_excel(writer, sheet_name="Nifty200_6M", index=False)
        pd.DataFrame(nifty_500_6m).to_excel(writer, sheet_name="Nifty500_6M", index=False)
        pd.DataFrame(bse_200_6m).to_excel(writer, sheet_name="BSE200_6M", index=False)

        pd.DataFrame(nifty_200_3m).to_excel(writer, sheet_name="Nifty200_3M", index=False)
        pd.DataFrame(nifty_500_3m).to_excel(writer, sheet_name="Nifty500_3M", index=False)
        pd.DataFrame(bse_200_3m).to_excel(writer, sheet_name="BSE200_3M", index=False)

        pd.DataFrame(nifty_200_1m).to_excel(writer, sheet_name="Nifty200_1M", index=False)
        pd.DataFrame(nifty_500_1m).to_excel(writer, sheet_name="Nifty500_1M", index=False)
        pd.DataFrame(bse_200_1m).to_excel(writer, sheet_name="BSE200_1M", index=False)

        # Optional: add overlap summary sheet
        overlap_summary = {
            "Nifty200_Overlap": list(set([s["symbol"] for s in nifty_200_1m]) &
                                     set([s["symbol"] for s in nifty_200_3m]) &
                                     set([s["symbol"] for s in nifty_200_6m])),
            "Nifty500_Overlap": list(set([s["symbol"] for s in nifty_500_1m]) &
                                     set([s["symbol"] for s in nifty_500_3m]) &
                                     set([s["symbol"] for s in nifty_500_6m])),
            "BSE200_Overlap": list(set([s["symbol"] for s in bse_200_1m]) &
                                   set([s["symbol"] for s in bse_200_3m]) &
                                   set([s["symbol"] for s in bse_200_6m]))
        }
        pd.DataFrame(dict([(k, pd.Series(v)) for k, v in overlap_summary.items()])).to_excel(
            writer, sheet_name="Overlaps", index=False
        )

    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="period_performers.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
