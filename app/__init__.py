from flask import Flask, render_template
from config import Config
from flask_login import current_user, login_required
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect, generate_csrf  # ✅ Add this line
from app.extensions import db, login_manager, csrf, cache, mail  # ✅ Include mail
from app.models import Resource
from .logging_config import setup_logging   # ✅ import your logging setup
from prometheus_flask_exporter import PrometheusMetrics

load_dotenv()

def create_app():
    flask_app = Flask(__name__)
    flask_app.config.from_object(Config)

    # Initialize extensions
    db.init_app(flask_app)
    login_manager.init_app(flask_app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(flask_app)
    cache.init_app(flask_app)
    mail.init_app(flask_app)  # ✅ Initialize Flask-Mail
    # Import analytics models so the analytics DB tables are created
    import app.models_analytics  # noqa: F401

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.trades import trades_bp
    from app.routes.stats import stats_bp
    from app.routes.export import export_bp
    from app.routes.resources import resources_bp
    from app.routes.notes import notes_bp
    from app.routes.calendar import calendar_bp
    from app.routes.watchlist import watchlist_bp
    from app.routes.risk_calculator import risk_bp
    from app.routes.performers import performers_bp, delivery_bp
    from app.routes.screener import screener_bp
    from app.routes.momentum_strategy import momentum_bp
    from app.routes.stage2_delivery import stage2_delivery_bp
    from app.routes.eps_screener import eps_bp
    # from routes.static_pages import static_pages
    from app.routes.vcp_screener import vcp_bp    
    from app.routes.period_performers import period_performers_bp
    from app.routes.volar_stage2_ind_screener import volar_bp
    from app.routes.earnings_screen import earnings_bp
    from app.routes.stage2_screener_us import screener_us_bp
    from app.routes.stage2_india import screener_india_bp
    from app.routes.hh_hl_india import hh_hl_bp
    from app.routes.ll_lh_india import ll_lh_bp
    from app.routes.volar_stage2_us_screener import volar_us_bp
    from app.routes.adaptive_volar_us_scr import volar_us_adaptive_bp
    from app.routes.adaptive_volar_ind_scr import volar_ind_adaptive_bp
    from app.routes.ai_analyst import ai_analyst_bp
    from app.routes.rs_roc_ind_screener import rs_roc_bp
    from app.routes.rs_roc_us_screener import rs_roc_us_bp
    from app.routes.chart import chart_bp
    from app.routes.gap_volume_us_screener import gap_vol_bp
    from app.routes.gap_volume_india_screener import gap_vol_india_bp
    from app.routes.trendline_screener import trendline_bp
    from app.routes.ibd_rating_engine_us import ibd_engine_us_bp
    from app.routes.ibd_rating_engine_ind import ibd_engine_ind_bp
    from app.routes.stage2_launchpad_screener import stage2_launchpad_bp
    from app.routes.chart_us import chart_us_bp
    from app.routes.chart_combined import chart_combined_bp
    from app.routes.adaptive_rs_4d_screener import adaptive_4d_bp
    from app.routes.chart_carousel import chart_carousel_bp
    from app.routes.chart_weinstein import chart_weinstein_bp
    from app.routes.chart_multiframe import chart_multiframe_bp
    from app.routes.ai_engine import ai_engine_bp
    from app.routes.hh_hl_us import hh_hl_us_bp
    from app.routes.ipo_screener import ipo_screener_bp
    from app.routes.minervini_ind_screener import minervini_bp
    from app.routes.delivery_surge_screener import delivery_surge_bp
    from app.routes.us_volume_surge_screener import us_vol_surge_bp
    from app.routes.quant_screeners import quant_screeners_bp
    from app.routes.momentum_scanner  import momentum_scan_bp
    from app.routes.position_tracker  import position_tracker_bp
    from app.routes.trade_journal     import trade_journal_bp
    from app.routes.cache_admin import cache_admin_bp
    from app.routes.quant_screeners_us import quant_screeners_us_bp
    from app.routes.ma_screener import ma_screener_bp
    from app.routes.universal_screener import universal_bp
    from app.routes.scan_suite import scan_suite_bp, init_scheduler
    from app.routes.staircase_screener import staircase_bp
    from app.routes.trading_analytics import trading_analytics_bp
    from app.routes.chart_ind import chart_ind_bp

    flask_app.register_blueprint(chart_ind_bp)
    flask_app.register_blueprint(trading_analytics_bp)
    flask_app.register_blueprint(staircase_bp)
    flask_app.register_blueprint(scan_suite_bp)
    init_scheduler(flask_app)
    flask_app.register_blueprint(universal_bp)
    flask_app.register_blueprint(ma_screener_bp)
    flask_app.register_blueprint(quant_screeners_us_bp)
    flask_app.register_blueprint(cache_admin_bp)
    flask_app.register_blueprint(momentum_scan_bp)
    flask_app.register_blueprint(position_tracker_bp)
    flask_app.register_blueprint(trade_journal_bp)
    flask_app.register_blueprint(quant_screeners_bp)
    flask_app.register_blueprint(us_vol_surge_bp, url_prefix="/us-vol")
    flask_app.register_blueprint(minervini_bp)
    flask_app.register_blueprint(ipo_screener_bp)
    flask_app.register_blueprint(hh_hl_us_bp)
    flask_app.register_blueprint(ai_engine_bp)
    flask_app.register_blueprint(chart_multiframe_bp)
    flask_app.register_blueprint(chart_weinstein_bp)
    flask_app.register_blueprint(chart_carousel_bp)
    flask_app.register_blueprint(adaptive_4d_bp)
    flask_app.register_blueprint(chart_combined_bp)
    flask_app.register_blueprint(chart_us_bp)
    flask_app.register_blueprint(ibd_engine_ind_bp)
    flask_app.register_blueprint(rs_roc_us_bp)
    flask_app.register_blueprint(rs_roc_bp)
    flask_app.register_blueprint(earnings_bp)
    flask_app.register_blueprint(period_performers_bp)
    flask_app.register_blueprint(vcp_bp, url_prefix="/vcp")
    flask_app.register_blueprint(eps_bp, url_prefix="/eps")
    flask_app.register_blueprint(stage2_delivery_bp)
    flask_app.register_blueprint(momentum_bp)
    flask_app.register_blueprint(delivery_surge_bp)
    flask_app.register_blueprint(screener_bp, url_prefix="/screener")
    flask_app.register_blueprint(risk_bp, url_prefix='/tools')
    flask_app.register_blueprint(calendar_bp)
    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(trades_bp)
    flask_app.register_blueprint(stats_bp, url_prefix='/stats')
    flask_app.register_blueprint(export_bp)
    flask_app.register_blueprint(resources_bp, url_prefix='/resources')    
    flask_app.register_blueprint(notes_bp)
    flask_app.register_blueprint(watchlist_bp)
    flask_app.register_blueprint(performers_bp, url_prefix="/performers")
    flask_app.register_blueprint(delivery_bp, url_prefix="/delivery")
    # flask_app.register_blueprint(static_pages)
    flask_app.register_blueprint(volar_bp)    
    flask_app.register_blueprint(screener_us_bp)
    flask_app.register_blueprint(screener_india_bp)
    flask_app.register_blueprint(hh_hl_bp)
    flask_app.register_blueprint(ll_lh_bp)
    flask_app.register_blueprint(volar_us_bp)
    flask_app.register_blueprint(volar_us_adaptive_bp)
    flask_app.register_blueprint(volar_ind_adaptive_bp)
    flask_app.register_blueprint(ai_analyst_bp)
    flask_app.register_blueprint(chart_bp)
    flask_app.register_blueprint(gap_vol_bp)
    flask_app.register_blueprint(gap_vol_india_bp)
    flask_app.register_blueprint(trendline_bp)
    flask_app.register_blueprint(ibd_engine_us_bp)
    flask_app.register_blueprint(stage2_launchpad_bp)


    # Home route
    @flask_app.route('/')
    @login_required
    def home():
        return render_template('home.html')

    # Inject pinned resources for navbar or sidebar
    @flask_app.context_processor
    def inject_pinned_resources():
        if login_manager._login_disabled or not hasattr(flask_app, 'login_manager'):
            return dict(pinned_resources=[])
        if current_user.is_authenticated:
            pinned = Resource.query.filter_by(user_id=current_user.id, pinned=True).order_by(Resource.title).all()
            return dict(pinned_resources=pinned)
        return dict(pinned_resources=[])

    # ✅ Inject CSRF token globally for manual forms
    @flask_app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf())

    # ✅ Enable logging
    setup_logging(flask_app)
    metrics = PrometheusMetrics(flask_app, path='/metrics', default=True)

    return flask_app
#app = create_app()