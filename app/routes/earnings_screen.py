import os
import pandas as pd
import yfinance as yf
from flask import Blueprint, render_template, flash

earnings_bp = Blueprint('earnings', __name__)

def get_oniel_data(ticker_symbol):
    try:
        stock = yf.Ticker(ticker_symbol)
        
        # Use income_stmt instead of the deprecated .earnings
        # This is more reliable for Indian (.NS) stocks
        df_income = stock.quarterly_income_stmt
        
        if df_income is None or df_income.empty:
            print(f"Skipping {ticker_symbol}: No income statement found.")
            return None

        # Fetch Net Income (O'Neil often used this as a proxy for EPS growth)
        if 'Net Income' not in df_income.index:
            return None
            
        net_income = df_income.loc['Net Income']
        
        # Ensure we have at least 5 quarters to do a YoY comparison (Current vs Same Q last year)
        if len(net_income) < 5:
            return None

        # Calculate Growth: (Current Quarter / Same Quarter Last Year) - 1
        # In yfinance, iloc[0] is usually the most recent quarter
        q_growth = (net_income.iloc[0] / net_income.iloc[4]) - 1
        
        # Annual Growth (using the annual income statement)
        df_annual = stock.income_stmt
        if df_annual is not None and not df_annual.empty and len(df_annual.columns) >= 2:
            a_growth = (df_annual.loc['Net Income'].iloc[0] / df_annual.loc['Net Income'].iloc[1]) - 1
        else:
            a_growth = 0

        # Fetch ROE from info
        info = stock.info
        roe = info.get('returnOnEquity', 0)

        # O'Neil Criteria: 25% Quarterly, 25% Annual, 17% ROE
        if q_growth >= 0.25 and a_growth >= 0.25 and roe >= 0.17:
            return {
                "symbol": ticker_symbol.replace(".NS", ""),
                "name": info.get('shortName', 'N/A'),
                "q_growth": round(q_growth * 100, 2),
                "a_growth": round(a_growth * 100, 2),
                "roe": round(roe * 100, 2),
                "price": info.get('currentPrice', 0)
            }
        
    except Exception as e:
        print(f"Error processing {ticker_symbol}: {str(e)}")
        return None
    return None

@earnings_bp.route('/screener/earnings', methods=["GET", "POST"])
def earnings_screener():
    # Path logic from your uploaded eps_screener.py
    path = "data/MCAPge250cr-2.csv"
    
    if not os.path.exists(path):
        flash(f"⚠️ Source file not found: {path}", "error")
        return render_template('earnings_screener.html', stocks=[], source_name=None)

    # Read symbols from CSV
    df = pd.read_csv(path)
    # Appending .NS for Indian markets as per your current logic
    symbols = [s + ".NS" if not str(s).endswith(".NS") else s for s in df["symbol"].dropna().unique()]
    
    screened_stocks = []
    for symbol in symbols:
        data = get_oniel_data(symbol)
        if data:
            screened_stocks.append(data)

    # Sort by Quarterly Growth descending
    screened_stocks.sort(key=lambda x: x["q_growth"], reverse=True)

    return render_template(
        'earnings_screener.html', 
        stocks=screened_stocks, 
        source_name="MCAP > 250cr List"
    )