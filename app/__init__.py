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
            from app.socket.indexes import get_market_session_info
            session = get_market_session_info()
            return jsonify({
                "is_open": session.get("is_open", False),
                "status": session.get("status", "Closed"),
                "status_detail": session.get("message", "Market session"),
                "market_status": [{"market": "Capital Market", "marketStatus": "Open" if session.get("is_open") else "Closed"}],
                "schedule": {
                    "open_time": session.get("open_time", "09:15 AM IST"),
                    "close_time": session.get("close_time", "03:30 PM IST"),
                    "session": session.get("session", "closed"),
                    "next_session_label": session.get("message", "Opens at 09:15 AM IST")
                }
            })
        except Exception as e:
            logger.warning(f"Error evaluating market status: {e}")
            return jsonify({
                "is_open": False,
                "status": "Closed",
                "status_detail": "Market closed · Opens at 09:15 AM IST",
                "market_status": [{"market": "Capital Market", "marketStatus": "Closed"}]
            })

    # Initialize MongoDB
    extensions.connect_to_mongodb()
    logger.info("mongodb connected succesfully ")
    # Initialize SocketIO with the app
    socketio.init_app(app)
    
    logger.info("Socket io connected succesfully ")

    # Register Blueprints
    from .routes.chart_routes import chart_bp
    from .routes.watchlist_routes import watchlist_bp
    from .routes.broker_routes import (
        broker_bp,
        brokers_api_bp,
        portfolio_api_bp,
        orders_api_bp,
    )
    from .routes.autotrade_routes import autotrade_bp
    from .routes.fno_routes import fno_bp
    from .services.autotrade_engine import autotrade_engine
    from .services.fno_autotrade_engine import fno_autotrade_engine

    app.register_blueprint(chart_bp)
    app.register_blueprint(watchlist_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(top_gain_loss)
    app.register_blueprint(top_news_bp)
    app.register_blueprint(predict_bp)
    app.register_blueprint(groww)
    app.register_blueprint(broker_bp)
    app.register_blueprint(brokers_api_bp)
    app.register_blueprint(brokers_api_bp, name="brokers_api_v1", url_prefix="/api/v1/brokers")
    app.register_blueprint(portfolio_api_bp)
    app.register_blueprint(portfolio_api_bp, name="portfolio_api_v1", url_prefix="/api/v1/portfolio")
    app.register_blueprint(orders_api_bp)
    app.register_blueprint(orders_api_bp, name="orders_api_v1", url_prefix="/api/v1/orders")
    app.register_blueprint(autotrade_bp)
    app.register_blueprint(fno_bp)

    if config_name != "testing":
        # Ensure all auto-trade switches start OFF on server boot so trades NEVER execute unless the user explicitly clicks Start Auto Trade
        try:
            from app.models.user import db as _mongo_db
            _mongo_db.autotrade_configs.update_many({}, {"$set": {"enabled": False}})
            _mongo_db.fno_autotrade_configs.update_many({}, {"$set": {"enabled": False}})
            logger.info("[OK] Reset all Auto Stock & Auto F&O switches to disabled on startup (requires explicit user Start).")
        except Exception as _reset_err:
            logger.warning(f"Could not reset auto-trade switches on startup: {_reset_err}")

        # Start automated trading background engines
        autotrade_engine.start()
        fno_autotrade_engine.start()

        # Auto-start real-time 1-second market broadcaster & candle streamer
        try:
            from app.services.realtime_candle_manager import realtime_candle_manager
            from app.socket.indexes import ensure_index_feeder
            realtime_candle_manager.init_socketio(socketio)
            ensure_index_feeder()
            logger.info("[OK] Real-time market data & candle broadcaster automatically started.")
        except Exception as feeder_err:
            logger.warning(f"Could not auto-start index feeder in create_app: {feeder_err}")

    @app.route("/api/v1/system/reset-fresh", methods=["POST"])
    def system_reset_fresh():
        """Wipe all trade positions, trade histories, daily P&L configs, and paper sandbox data for a completely fresh ₹0 start."""
        try:
            from app.models.user import db as _mongo_db
            from app.utils.market_calendar import get_ist_time
            today_str = get_ist_time().strftime("%Y-%m-%d")

            r_fno_pos = _mongo_db.fno_autotrade_positions.delete_many({})
            r_eq_pos = _mongo_db.autotrade_positions.delete_many({})
            r_fno_cfg = _mongo_db.fno_autotrade_configs.update_many(
                {},
                {"$set": {"enabled": False, "dailyRealizedPnL": 0.0, "lastResetDate": today_str}}
            )
            r_eq_cfg = _mongo_db.autotrade_configs.update_many(
                {},
                {"$set": {"enabled": False, "dailyRealizedPnL": 0.0, "lastResetDate": today_str}}
            )
            _mongo_db.paper_accounts.delete_many({})
            _mongo_db.paper_orders.delete_many({})
            _mongo_db.paper_positions.delete_many({})
            _mongo_db.predictions.delete_many({})

            return jsonify({
                "status": "success",
                "message": "Database cleared for a fresh start. Today's P&L reset to ₹0.00 and Auto-Trade set to OFF.",
                "deleted_fno_positions": r_fno_pos.deleted_count,
                "deleted_stock_positions": r_eq_pos.deleted_count,
                "today_date_ist": today_str,
            })
        except Exception as e:
            logger.error(f"[system_reset_fresh] Error: {e}")
            return jsonify({"status": "failed", "error": str(e)}), 500

    @app.route("/api/v1/live-quotes", methods=["GET"])
    def get_live_nse_quotes():
        """Returns real-time NSE India quotes for all major indices and equities."""
        from app.socket.indexes import _shared_quotes, _quotes_lock, refresh_shared_quotes_from_nse
        with _quotes_lock:
            has_quotes = len(_shared_quotes) > 0
        if not has_quotes:
            refresh_shared_quotes_from_nse(include_equities=True)
        with _quotes_lock:
            quotes_copy = dict(_shared_quotes)
        return jsonify({
            "status": "success",
            "source": "NSE_LIVE",
            "count": len(quotes_copy),
            "quotes": quotes_copy,
        })

    @app.route("/api/v1/getAllHolding", methods=["GET"])
    def get_all_holding():
        """Returns live equity holdings from the user's active connected broker (Angel One / Groww) or Paper Account (zero static holdings)."""
        from app.socket.indexes import _shared_quotes, _quotes_lock
        from app.routes.broker_routes import _get_current_user
        from app.services.broker_providers import (
            BrokerConnectionRepository,
            LiveBrokerAccountManager,
            get_broker_provider,
            token_crypto,
        )
        from app.models.user import db as _mongo_db

        user_doc = _get_current_user()
        active_broker = "paper"
        user_id = None
        if isinstance(user_doc, dict):
            user_id = str(user_doc.get("_id") or user_doc.get("id") or "")
            active_broker = str(user_doc.get("activeBroker") or "paper").lower().strip()

        if user_id and active_broker in ("angelone", "groww"):
            conn = BrokerConnectionRepository.get_connection(user_id, active_broker)
            if conn and conn.get("status") == "CONNECTED":
                try:
                    provider = get_broker_provider(active_broker)
                    broker_holdings = provider.get_holdings(conn)
                    if not broker_holdings:
                        access_tok = token_crypto.decrypt(conn.get("access_token_encrypted") or "")
                        if access_tok.startswith("angel_sim_") or access_tok.startswith("groww_sim_"):
                            acc = LiveBrokerAccountManager.ensure_live_account(
                                user_id,
                                active_broker,
                                conn.get("broker_user_id", f"{active_broker.upper()}-TRADER"),
                            )
                            broker_holdings = LiveBrokerAccountManager.enrich_holdings_with_live_quotes(
                                acc.get("holdings", []),
                                active_broker,
                            )
                    funds = provider.get_funds(conn)
                    return jsonify({
                        "status": "success",
                        "broker": active_broker,
                        "broker_user_id": conn.get("broker_user_id", ""),
                        "is_paper": False,
                        "funds": funds,
                        "data": broker_holdings,
                    })
                except Exception as broker_err:
                    logger.warning(f"[getAllHolding] Live broker fetch warning ({active_broker}): {broker_err}")

        with _quotes_lock:
            quotes = dict(_shared_quotes)

        # Strictly return only real open paper positions for this user (no static dummy holdings)
        enriched = []
        if user_id:
            open_docs = list(_mongo_db.autotrade_positions.find({"userId": user_id, "status": "OPEN"}))
            for d in open_docs:
                sym = d.get("symbol", "")
                qty = int(d.get("quantity", 0))
                avg_p = float(d.get("entryPrice", 0.0))
                q = quotes.get(f"{sym}.NS") or quotes.get(sym) or {}
                ltp = round(float(q.get("ltp") or d.get("currentPrice") or avg_p), 2)
                prev = round(float(q.get("prev") or avg_p), 2)
                chg = round(ltp - prev, 2)
                p_chg = round((chg / prev) * 100, 2) if prev > 0 else 0.0
                enriched.append({
                    "symbol": sym,
                    "name": q.get("name", sym),
                    "quantity": qty,
                    "averagePrice": avg_p,
                    "ltp": ltp,
                    "change": chg,
                    "pChange": p_chg,
                    "sector": "NSE Equity",
                    "broker": "paper",
                })

        return jsonify({
            "status": "success",
            "broker": "paper",
            "is_paper": True,
            "data": enriched,
        })

    @app.route("/api/v1/createOrder", methods=["POST"])
    def create_order():
        """Executes a live/paper equity order on the user's active broker at current real-time market price."""
        from flask import request as req
        from app.socket.indexes import _shared_quotes, _quotes_lock
        from app.routes.broker_routes import _get_current_user
        from app.services.broker_service import get_broker_for_user

        body = req.get_json(silent=True) or {}
        symbol = str(body.get("symbol", "RELIANCE")).upper().replace(".NS", "")
        side = str(body.get("side") or body.get("transactionType") or "BUY").upper()
        qty = int(body.get("quantity") or body.get("qty") or 10)
        product = str(body.get("product", "MIS")).upper()
        order_type = str(body.get("orderType", "MARKET")).upper()
        with _quotes_lock:
            q = _shared_quotes.get(f"{symbol}.NS") or _shared_quotes.get(symbol) or {}
        fill_price = round(float(body.get("price") or q.get("ltp") or 1000.0), 2)

        user_doc = _get_current_user()
        broker_name = "paper"
        order_id = f"ORD-{int(os.times().elapsed * 1000)}"
        if isinstance(user_doc, dict):
            try:
                broker_inst = get_broker_for_user(user_doc=user_doc)
                broker_name = getattr(broker_inst, "broker_name", "paper")
                res = broker_inst.place_order(
                    symbol=symbol,
                    transaction_type=side,
                    quantity=qty,
                    price=fill_price,
                    order_type=order_type,
                )
                if isinstance(res, dict) and res.get("order_id"):
                    order_id = str(res["order_id"])
                if isinstance(res, dict) and res.get("fill_price"):
                    fill_price = round(float(res["fill_price"]), 2)
            except Exception as ord_err:
                logger.warning(f"[createOrder] Broker execution fallback: {ord_err}")

        return jsonify({
            "status": "success",
            "broker": broker_name,
            "orderId": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "product": product,
            "orderType": order_type,
            "fillPrice": fill_price,
            "message": f"[{broker_name.upper()}] {side} Order Executed: {qty}x {symbol} @ ₹{fill_price:,.2f} ({product})",
        })

    return app