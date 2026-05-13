from flask import Blueprint, render_template, request, flash, session
from flask_wtf.csrf import validate_csrf, CSRFError

risk_bp = Blueprint('risk', __name__)

@risk_bp.route('/risk', methods=['GET', 'POST'])
def risk_calculator():
    result = None
    
    # Initialize history in session if it doesn't exist
    if 'history' not in session:
        session['history'] = []

    if request.method == 'POST':
        try:
            validate_csrf(request.form.get('csrf_token'))

            investment = float(request.form['investment'])
            current_price = float(request.form['current_price'])
            sl_price = float(request.form['sl_price'])

            risk_per_share = current_price - sl_price
            risk_pct_of_price = round((risk_per_share / current_price) * 100, 2) if current_price > 0 else 0

            def calc_qty(risk_pct):
                max_affordable_qty = int(investment / current_price)
                risk_amount = investment * risk_pct
                risk_per_share = current_price - sl_price
                max_risk_qty = int(risk_amount / risk_per_share) if risk_per_share > 0 else 0
                quantity = min(max_affordable_qty, max_risk_qty)

                return {
                    'risk_pct': risk_pct * 100,
                    'risk_amount': round(risk_amount, 2),
                    'quantity': quantity,
                    'max_affordable_qty': max_affordable_qty
                }

            result = {
                'investment': investment,
                'current_price': current_price,
                'sl_price': sl_price,
                'risk_per_share': round(risk_per_share, 2),
                'risk_pct_of_price': risk_pct_of_price,
                'levels': [calc_qty(p) for p in [0.05, 0.04, 0.03, 0.02, 0.01]]
            }

            # --- UPDATED CACHE LOGIC ---
            history = session.get('history', [])

            # Create a simplified version for the history table
            history_entry = {
                'investment': investment,
                'current_price': current_price,
                'sl_price': sl_price,
                'risk_per_share': round(risk_per_share, 2),
                'risk_pct_of_price': risk_pct_of_price,
                # Store the suggested quantities for quick reference in history
                'qty_1pct': result['levels'][4]['quantity'], # 1% level
                'qty_2pct': result['levels'][3]['quantity'], # 2% level
                'qty_3pct': result['levels'][2]['quantity'], # 3% level
                'qty_4pct': result['levels'][1]['quantity'], # 4% level
                'qty_5pct': result['levels'][0]['quantity'], # 5% level
                'max_qty': result['levels'][0]['max_affordable_qty']
            }

            history.insert(0, history_entry)
            session['history'] = history[:8]
            session.modified = True
            # -------------------

        except CSRFError:
            flash("Invalid or missing CSRF token.", "danger")
        except (ValueError, ZeroDivisionError):
            result = {'error': 'Invalid input. Ensure prices are greater than zero.'}

    return render_template('risk_calculator.html', result=result, history=session.get('history', []))