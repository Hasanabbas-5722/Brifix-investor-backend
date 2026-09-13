from app.services.top_loss_gain import TopGainnerLosserervice
from flask import request, jsonify, Blueprint
from app.utils.logger import get_logger
from concurrent.futures import ThreadPoolExecutor

logger = get_logger(__name__)

top_gain_loss = Blueprint("top_loss_gain", __name__, url_prefix="/api/v1/top")


@top_gain_loss.route('/nifty_gainner', methods=['GET'])
def top_nifty_gainner():
    try:
        response = TopGainnerLosserervice.get_nifty_gainner()
        return jsonify({
            "status": "success",
            "data": response
        })
    except Exception as e:
        logger.error(f"Error in top_nifty_gainner: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500


@top_gain_loss.route('/banknifty_gainner', methods=['GET'])
def top_banknifty_gainner():
    try:
        response = TopGainnerLosserervice.get_banknifty_gainner()
        return jsonify({
            "status": "success",
            "data": response
        })
    except Exception as e:
        logger.error(f"Error in top_banknifty_gainner: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500


@top_gain_loss.route('/nifty_losser', methods=["GET"])
def top_nifty_losser():
    try:
        nifty_losers = TopGainnerLosserervice.get_nifty_losser()
        return jsonify({
            "status": "success",
            "data": nifty_losers
        })
    except Exception as e:
        logger.error(f"Error in top_nifty_losser: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500


@top_gain_loss.route('/banknifty_losser', methods=["GET"])
def top_banknifty_losser():
    try:
        banknifty_losers = TopGainnerLosserervice.get_banknifty_losser()
        return jsonify({
            "status": "success",
            "data": banknifty_losers
        })
    except Exception as e:
        logger.error(f"Error in top_banknifty_losser: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500


@top_gain_loss.route('/get_top_gain_loss_dashboard', methods=['GET'])
def getDashboard():
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            nifty_gainers_future = executor.submit(TopGainnerLosserervice.get_nifty_gainner)
            nifty_losers_future = executor.submit(TopGainnerLosserervice.get_nifty_losser)
            bank_gainers_future = executor.submit(TopGainnerLosserervice.get_banknifty_gainner)
            bank_losers_future = executor.submit(TopGainnerLosserervice.get_banknifty_losser)

            response = {
                "nifty_gainers": nifty_gainers_future.result(),
                "nifty_losers": nifty_losers_future.result(),
                "bank_gainers": bank_gainers_future.result(),
                "bank_losers": bank_losers_future.result(),
            }

            return jsonify({
                "status": "success",
                "data": response
            })
    except Exception as e:
        logger.error(f"Error in getDashboard: {e}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        }), 500
