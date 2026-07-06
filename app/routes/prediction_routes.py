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
