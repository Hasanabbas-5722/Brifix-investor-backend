from flask import jsonify
from app.extensions import get_groww_client
from app.socket import SmartAPISocket
from flask import Flask
from . import extensions
from .socket import socketio
import os
from flask_socketio import SocketIO, emit
try:
    from nse import NSE
    _NSE_AVAILABLE = True
except ImportError:
    NSE = None
    _NSE_AVAILABLE = False


from .routes.user_routes import user_bp
from .routes.top_loss_gain import top_gain_loss
from .routes.top_news import top_news_bp
from .routes.prediction_routes import predict_bp
from .routes.groww_routes import groww
from pathlib import Path
from app.utils.logger import get_logger

DIR = Path(__file__).parent

# Vercel's /var/task is read-only; only /tmp is writable at runtime.
# Use /tmp as the NSE download folder so it can write cookies/cache files.
_NSE_DOWNLOAD_DIR = Path(os.environ.get("NSE_DOWNLOAD_DIR", "/tmp"))
try:
    _NSE_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

if _NSE_AVAILABLE:
    try:
        nse = NSE(download_folder=_NSE_DOWNLOAD_DIR)
    except Exception as _e:
        nse = None
else:
    nse = None

logger = get_logger(__name__)

def create_app(config_name=None):
    app = Flask(__name__)

    # Load configuration
    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    if config_name == "testing":
        from .config import TestingConfig
        app.config.from_object(TestingConfig)
    elif config_name == "production":
        from .config import ProductionConfig
        app.config.from_object(ProductionConfig)
    else:
        from .config import DevelopmentConfig
        app.config.from_object(DevelopmentConfig)

    @app.route("/api/v1/market_status", methods=["GET"])
    def market_status():
        try:
            if nse is None:
                raise RuntimeError("NSE client not initialized")
            status = nse.status()
            return jsonify({"market_status": status})
        except Exception as e:
            logger.warning(f"Error fetching market status from NSE: {e}")
            return jsonify({"market_status": [{"market": "Capital Market", "marketStatus": "Closed"}]})

    # Initialize MongoDB
    extensions.connect_to_mongodb()
    logger.info("mongodb connected succesfully ")
    # Initialize SocketIO with the app
    socketio.init_app(app)
    
    logger.info("Socket io connected succesfully ")

    # Register Blueprints
    from .routes.chart_routes import chart_bp
    from .routes.watchlist_routes import watchlist_bp
    from .routes.broker_routes import broker_bp
    from .routes.autotrade_routes import autotrade_bp
    from .services.autotrade_engine import autotrade_engine

    app.register_blueprint(chart_bp)
    app.register_blueprint(watchlist_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(top_gain_loss)
    app.register_blueprint(top_news_bp)
    app.register_blueprint(predict_bp)
    app.register_blueprint(groww)
    app.register_blueprint(broker_bp)
    app.register_blueprint(autotrade_bp)

    # Start automated trading background engine
    autotrade_engine.start()

    return app