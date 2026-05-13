import logging
from logging.handlers import RotatingFileHandler
import os
import time
from flask import request

def setup_logging(app):
    # Ensure logs directory exists
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    # -----------------------
    # General Application Logs
    # -----------------------
    app_handler = RotatingFileHandler(
        os.path.join(log_dir, "flask_app.log"),
        maxBytes=10*1024*1024, backupCount=5
    )
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    # -----------------------
    # Error Logs
    # -----------------------
    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=5*1024*1024, backupCount=3
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))

    # -----------------------
    # Access Logs (HTTP Requests + Status Codes + Latency)
    # -----------------------
    access_handler = RotatingFileHandler(
        os.path.join(log_dir, "access.log"),
        maxBytes=10*1024*1024, backupCount=5
    )
    access_handler.setLevel(logging.INFO)
    access_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    ))
    access_logger = logging.getLogger("access")
    access_logger.addHandler(access_handler)
    access_logger.setLevel(logging.INFO)

    # Attach handlers to app logger
    app.logger.addHandler(app_handler)
    app.logger.addHandler(error_handler)
    app.logger.setLevel(logging.INFO)

    # -----------------------
    # Middleware to log requests + responses + latency
    # -----------------------
    @app.before_request
    def start_timer():
        request.start_time = time.time()

    @app.after_request
    def log_response_info(response):
        # Calculate latency in ms
        duration = (time.time() - getattr(request, "start_time", time.time())) * 1000
        access_logger.info(
            f"{request.remote_addr} - {request.method} {request.path} "
            f"Status: {response.status_code} "
            f"Duration: {duration:.2f} ms "
            f"User-Agent: {request.user_agent.string}"
        )
        return response
