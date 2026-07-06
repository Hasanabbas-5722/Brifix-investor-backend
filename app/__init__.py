from flask import jsonify
from app.extensions import get_groww_client
from app.socket import SmartAPISocket
from flask import Flask
from . import extensions
from .socket import socketio
import os
from flask_socketio import SocketIO, emit
from nse import NSE


from .routes.user_routes import user_bp
from .routes.top_loss_gain import top_gain_loss
from .routes.top_news import top_news_bp
from .routes.prediction_routes import predict_bp
from .routes.groww_routes import groww
from pathlib import Path
from app.utils.logger import get_logger

DIR = Path(__file__).parent

nse = NSE(download_folder=DIR)

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
        print("nse.status",nse.status())
        return jsonify({"market_status": nse.status()})

    # Initialize MongoDB
    extensions.connect_to_mongodb()
    logger.info("mongodb connected succesfully ")
    # Initialize SocketIO with the app
    socketio.init_app(app)
    
    logger.info("Socket io connected succesfully ")
    # SmartAPISocket.on_connect()
    get_groww_client()

    # Register Blueprints
    from .routes.chart_routes import chart_bp
    app.register_blueprint(chart_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(top_gain_loss)
    app.register_blueprint(top_news_bp)
    app.register_blueprint(predict_bp)
    app.register_blueprint(groww)

    return app