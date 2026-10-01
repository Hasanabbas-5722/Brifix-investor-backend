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

    @app.route("/api/v1/realtime/snapshot", methods=["GET"])
    def realtime_market_snapshot():
        """
        Unified 1-Second Real-Time Market Snapshot Endpoint.
        Returns live NSE Indices, Core/Watchlist Equities, Top Movers, and F&O Option Chain LTPs
        in a single sub-20ms response. Also ensures serverless (Vercel) compatibility when WebSockets are unavailable.
        """
        import time as _time
        from flask import request as _req
        from app.socket.indexes import (
            get_market_session_info,
            refresh_shared_quotes_from_nse,
            INDEX_TARGETS,
            CORE_STOCKS,
            _shared_quotes,
            _quotes_lock,
            register_dynamic_symbol,
        )
        from app.services.nse_market_service import nse_market_service

        extra_syms_raw = _req.args.get("symbols", "")
        if extra_syms_raw:
            for s in extra_syms_raw.split(","):
                if s.strip():
                    register_dynamic_symbol(s.strip().upper())

        session = get_market_session_info()
        is_open = session.get("is_open", False)
        is_serverless = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

        with _quotes_lock:
            quotes = dict(_shared_quotes)

        # If serverless or cache cold, refresh with 0.85s max_age
        if not quotes or is_serverless:
            quotes = refresh_shared_quotes_from_nse(max_age_sec=0.85)
            nse_market_service.prewarm_all_option_chains(max_age_sec=2.0)

        now_ms = int(_time.time() * 1000)
        indices_out = []
        for ticker_sym, token_id, display_name in INDEX_TARGETS:
            q = quotes.get(ticker_sym) or quotes.get(token_id) or quotes.get(display_name)
            if not q or float(q.get("ltp") or 0) <= 0:
                continue
            ltp = round(float(q["ltp"]), 2)
            prev = round(float(q.get("prev") or ltp), 2)
            open_p = round(float(q.get("open") or prev), 2)
            high_p = round(float(q.get("high") or max(ltp, open_p)), 2)
            low_p = round(float(q.get("low") or min(ltp, open_p)), 2)
            change = round(ltp - prev, 2)
            p_change = round((change / prev * 100.0), 2) if prev > 0 else 0.0
            indices_out.append({
                "token": token_id,
                "symbol": display_name,
                "ticker": ticker_sym,
                "ltp": ltp,
                "change": change,
                "pChange": p_change,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "prevClose": prev,
                "is_market_open": is_open,
                "market_status": session.get("status", "CLOSED"),
                "timestamp_ms": now_ms,
            })

        stocks_out = {}
        for k, q in quotes.items():
            if k.endswith(".NS") or k.startswith("^") or k in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY 50", "BANK NIFTY", "FIN NIFTY", "INDIAVIX"):
                continue
            ltp = float(q.get("ltp") or 0.0)
            if ltp <= 0:
                continue
            prev = float(q.get("prev") or ltp)
            open_p = float(q.get("open") or prev)
            high_p = float(q.get("high") or max(ltp, open_p))
            low_p = float(q.get("low") or min(ltp, open_p))
            change = round(ltp - prev, 2)
            p_change = round((change / prev * 100.0), 2) if prev > 0 else 0.0
            stocks_out[k] = {
                "token": k,
                "symbol": k,
                "ltp": round(ltp, 2),
                "change": change,
                "pChange": p_change,
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "prevClose": round(prev, 2),
                "vol": int(q.get("vol") or 0),
                "is_market_open": is_open,
                "timestamp_ms": now_ms,
            }

        fno_opt_ticks = {}
        for idx_k in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            chain = nse_market_service._option_chain_cache.get(idx_k)
            if not chain and is_serverless:
                chain = nse_market_service.get_index_option_chain(idx_k, max_age_sec=3.0)
            if chain:
                u_ltp = float((quotes.get(idx_k) or {}).get("ltp") or chain.get("underlying_ltp") or 0.0)
                step = 100 if idx_k == "BANKNIFTY" else 50
                atm = int(round(u_ltp / step) * step) if u_ltp > 0 else 0
                s_map = chain.get("strikes") or {}
                atm_row = s_map.get(atm) or s_map.get(str(atm)) or {}
                ce_obj = atm_row.get("CE") or {}
                pe_obj = atm_row.get("PE") or {}
                nearby_strikes = {}
                for k_strike, row_val in s_map.items():
                    try:
                        ks_int = int(float(k_strike))
                        if atm == 0 or abs(ks_int - atm) <= step * 6:
                            nearby_strikes[str(ks_int)] = row_val
                    except Exception:
                        pass
                fno_opt_ticks[idx_k] = {
                    "underlying": idx_k,
                    "underlying_ltp": u_ltp,
                    "spot_ltp": u_ltp,
                    "atm_strike": atm,
                    "expiry": chain.get("expiry", ""),
                    "pcr": chain.get("pcr", 1.0),
                    "ce_ltp": float(ce_obj.get("ltp") or 0.0),
                    "pe_ltp": float(pe_obj.get("ltp") or 0.0),
                    "ce_iv": float(ce_obj.get("iv") or 0.0),
                    "pe_iv": float(pe_obj.get("iv") or 0.0),
                    "strikes": nearby_strikes,
                    "timestamp_ms": now_ms,
                }

        all_stock_list = list(stocks_out.values())
        gainers = sorted(all_stock_list, key=lambda x: x["pChange"], reverse=True)[:5]
        losers = sorted(all_stock_list, key=lambda x: x["pChange"])[:5]

        snapshot_payload = {
            "timestamp_ms": now_ms,
            "is_serverless": is_serverless,
            "market_status": session,
            "indices": indices_out,
            "stocks": all_stock_list,
            "stocks_map": stocks_out,
            "fno_option_ticks": fno_opt_ticks,
            "gainers": gainers,
            "losers": losers,
        }
        return jsonify({
            "success": True,
            "status": "success",
            "data": snapshot_payload,
            **snapshot_payload,
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
        angel_bp,
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
    app.register_blueprint(angel_bp)
    app.register_blueprint(brokers_api_bp)
    app.register_blueprint(brokers_api_bp, name="brokers_api_v1", url_prefix="/api/v1/brokers")
    app.register_blueprint(portfolio_api_bp)
    app.register_blueprint(portfolio_api_bp, name="portfolio_api_v1", url_prefix="/api/v1/portfolio")
    app.register_blueprint(orders_api_bp)
    app.register_blueprint(orders_api_bp, name="orders_api_v1", url_prefix="/api/v1/orders")
    app.register_blueprint(autotrade_bp)
    app.register_blueprint(fno_bp)

    if config_name != "testing":
        is_serverless_env = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
        if not is_serverless_env:
            # Ensure all auto-trade switches start OFF on persistent server boot so trades NEVER execute unless the user explicitly clicks Start Auto Trade
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