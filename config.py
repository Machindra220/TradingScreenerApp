import os
from dotenv import load_dotenv
# Load environment variables from .env file
load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    SQLALCHEMY_DATABASE_URI = os.environ.get('SQLALCHEMY_DATABASE_URI')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ECHO = False

    SESSION_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    REMEMBER_COOKIE_SECURE = os.environ.get('FLASK_ENV') == 'production'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'  # or 'Strict' for tighter security

    PROPAGATE_EXCEPTIONS = True
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')


    MAIL_SERVER = os.getenv('MAIL_SERVER')
    MAIL_PORT = int(os.getenv('MAIL_PORT'))
    MAIL_USE_TLS = os.getenv('MAIL_USE_TLS') == 'True'
    MAIL_USERNAME = os.getenv('MAIL_USERNAME')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_DEFAULT_SENDER')

    # # Add inside the Config class, after SQLALCHEMY_DATABASE_URI
    # ANALYTICS_DB_PATH = os.path.join(
    #     os.path.dirname(os.path.abspath(__file__)),
    #     'data', 'trading_analytics', 'trading_analytics.db'
    # )
    # SQLALCHEMY_BINDS = {
    #     'analytics': f"sqlite:///{ANALYTICS_DB_PATH}"
    # }

    # Add inside the Config class, after SQLALCHEMY_DATABASE_URI
    ANALYTICS_DB_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'data', 'trading_analytics'
    )
    os.makedirs(ANALYTICS_DB_DIR, exist_ok=True)

    ANALYTICS_DB_PATH = os.path.join(ANALYTICS_DB_DIR, 'trading_analytics.db').replace('\\', '/')
    
    SQLALCHEMY_BINDS = {
        'analytics': f"sqlite:///{ANALYTICS_DB_PATH}"
    }