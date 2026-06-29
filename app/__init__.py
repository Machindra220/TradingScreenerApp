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
    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(app)
    cache.init_app(app)
    mail.init_app(app)  # ✅ Initialize Flask-Mail

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


    app.register_blueprint(adaptive_4d_bp)
    app.register_blueprint(chart_combined_bp)
    app.register_blueprint(chart_us_bp)
    app.register_blueprint(ibd_engine_ind_bp)
    app.register_blueprint(rs_roc_us_bp)
    app.register_blueprint(rs_roc_bp)
    app.register_blueprint(earnings_bp)
    app.register_blueprint(period_performers_bp)
    app.register_blueprint(vcp_bp, url_prefix="/vcp")
    app.register_blueprint(eps_bp, url_prefix="/eps")
    app.register_blueprint(stage2_delivery_bp)
    app.register_blueprint(momentum_bp)
    app.register_blueprint(screener_bp, url_prefix="/screener")
    app.register_blueprint(risk_bp, url_prefix='/tools')
    app.register_blueprint(calendar_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(trades_bp)
    app.register_blueprint(stats_bp, url_prefix='/stats')
    app.register_blueprint(export_bp)
    app.register_blueprint(resources_bp, url_prefix='/resources')    
    app.register_blueprint(notes_bp)
    app.register_blueprint(watchlist_bp)
    app.register_blueprint(performers_bp, url_prefix="/performers")
    app.register_blueprint(delivery_bp, url_prefix="/delivery")
    # app.register_blueprint(static_pages)
    app.register_blueprint(volar_bp)    
    app.register_blueprint(screener_us_bp)
    app.register_blueprint(screener_india_bp)
    app.register_blueprint(hh_hl_bp)
    app.register_blueprint(ll_lh_bp)
    app.register_blueprint(volar_us_bp)
    app.register_blueprint(volar_us_adaptive_bp)
    app.register_blueprint(volar_ind_adaptive_bp)
    app.register_blueprint(ai_analyst_bp)
    app.register_blueprint(chart_bp)
    app.register_blueprint(gap_vol_bp)
    app.register_blueprint(gap_vol_india_bp)
    app.register_blueprint(trendline_bp)
    app.register_blueprint(ibd_engine_us_bp)
    app.register_blueprint(stage2_launchpad_bp)


    # Home route
    @app.route('/')
    @login_required
    def home():
        return render_template('home.html')

    # Inject pinned resources for navbar or sidebar
    @app.context_processor
    def inject_pinned_resources():
        if login_manager._login_disabled or not hasattr(app, 'login_manager'):
            return dict(pinned_resources=[])
        if current_user.is_authenticated:
            pinned = Resource.query.filter_by(user_id=current_user.id, pinned=True).order_by(Resource.title).all()
            return dict(pinned_resources=pinned)
        return dict(pinned_resources=[])

    # ✅ Inject CSRF token globally for manual forms
    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf())

    # ✅ Enable logging
    setup_logging(app)
    metrics = PrometheusMetrics(app, path='/metrics', default=True)

    return app
#app = create_app()
