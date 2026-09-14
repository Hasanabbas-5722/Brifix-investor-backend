"""
Stock Prediction Routes
=======================
GET  /api/v1/predict?symbol=RELIANCE&exchange=NSE

Returns a fully JSON-serializable prediction result including:
  - Company fundamentals
  - ML model predictions (RF, XGBoost, SVR, ARIMA, LSTM)
  - Ensemble prediction & confidence
  - Price targets (1D, 5D, 15D, 30D)
  - Stop-loss, Risk/Reward, Support & Resistance
  - Technical signals (RSI, MACD, ADX, etc.)
  - Overall trade signal (BUY / SELL / HOLD)
"""

from flask import Blueprint, request, jsonify
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.services.stock_prediction_service import StockPredictionService

logger = get_logger(__name__)

predict_bp = Blueprint("predict", __name__, url_prefix="/api/v1")


from app.services.push_service import PushNotificationService


@predict_bp.route("/notifications/vapid-public-key", methods=["GET"])
def get_vapid_public_key():
    """Returns the base64 URL-safe VAPID public key for browser Web Push registration."""
    try:
        pub_key = PushNotificationService.get_public_key()
        return jsonify({
            "status": "success",
            "publicKey": pub_key
        })
    except Exception as e:
        logger.error(f"[get_vapid_public_key] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@predict_bp.route("/notifications/subscribe", methods=["POST"])
def subscribe_push():
    """Stores a browser's Web Push subscription object in MongoDB."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        subscription = data.get("subscription")
        if not subscription:
            return jsonify({"status": "failed", "error": "subscription payload is required"}), 400

        user_id = data.get("userId")
        success = PushNotificationService.save_subscription(subscription, user_id=user_id)
        return jsonify({
            "status": "success" if success else "failed",
            "message": "Subscription registered successfully"
        })
    except Exception as e:
        logger.error(f"[subscribe_push] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@predict_bp.route("/predict/top-pick", methods=["GET"])
def get_top_stock_pick():
    """Returns the single highest-confidence AI recommended stock for today."""
    try:
        picks = StockPredictionService.get_daily_recommendations()
        top_pick = picks[0] if picks else None
        return jsonify({
            "status": "success",
            "data": top_pick
        })
    except Exception as e:
        logger.error(f"[get_top_stock_pick] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@predict_bp.route("/predict/notify-top-pick", methods=["POST"])
def notify_top_stock_pick():
    """
    Triggers Web Push notification delivering today's #1 AI-recommended stock pick.
    Can send to a specific target subscription (for immediate user test) or broadcast to all.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        target_sub = data.get("subscription")
        result = PushNotificationService.notify_top_ai_pick(target_subscription=target_sub)
        return jsonify({
            "status": "success",
            "result": result
        })
    except Exception as e:
        logger.error(f"[notify_top_stock_pick] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@predict_bp.route("/predict/daily-picks", methods=["GET"])
@predict_bp.route("/daily-picks", methods=["GET"])
def daily_stock_picks():
    """
    Returns AI suggestions for which stocks to purchase for today.
    Ranked list of opportunities with Target, Stop Loss, Expected Return, and Confidence.
    """
    try:
        picks = StockPredictionService.get_daily_recommendations()
        return jsonify({
            "status": "success",
            "data": picks
        })
    except Exception as e:
        logger.error(f"[daily_stock_picks] Error: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500


@predict_bp.route("/predict", methods=["GET"])
@validate_access_token
def predict_stock():
    """
    Query params:
        symbol   (str) : NSE/BSE ticker, e.g. RELIANCE, TCS, HDFCBANK
        exchange (str) : NSE (default) or BSE
    """
    try:
        symbol   = request.args.get("symbol", "").strip().upper()
        exchange = request.args.get("exchange", "NSE").strip().upper()

        if not symbol:
            return jsonify({
                "status": "failed",
                "error": "symbol query parameter is required. e.g. ?symbol=RELIANCE"
            }), 400

        if exchange not in ("NSE", "BSE"):
            return jsonify({
                "status": "failed",
                "error": "exchange must be NSE or BSE"
            }), 400

        logger.info(f"[predict_stock] symbol={symbol}, exchange={exchange}")

        result = StockPredictionService.predict(symbol=symbol, exchange=exchange)

        return jsonify({
            "status": "success",
            "data": result
        })

    except ValueError as ve:
        logger.error(f"[predict_stock] ValueError: {str(ve)}")
        return jsonify({
            "status": "failed",
            "error": str(ve)
        }), 404

    except Exception as e:
        logger.error(f"[predict_stock] Unexpected error: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500
