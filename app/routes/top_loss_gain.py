from app.services.top_loss_gain import TopGainnerLosserervice
from app.utils.access_token_validate import validate_access_token
import requests
from flask import request, jsonify, Blueprint
from app.utils.logger import get_logger
from concurrent.futures import ThreadPoolExecutor

logger = get_logger(__name__)


top_gain_loss = Blueprint("top_loss_gain", __name__, url_prefix="/api/v1/top")


@top_gain_loss.route('/nifty_gainner', methods=['GET'])
@validate_access_token
def top_nifty_gainner():
    try:
        data = request.user
        # logger.info(f"request data of top gainner and losse ::{data}")
        response = TopGainnerLosserervice.get_nifty_gainner(data)
        # logger.info(f"response ;;;;; {response}")
        return jsonify({
            "status": "success",
            "data": response
        })
    except Exception as e:
        logger.info(f"Error generate from top nifty gainner :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try


@top_gain_loss.route('/banknifty_gainner', methods=['GET'])
@validate_access_token
def top_banknifty_gainner():
    try:
        data = request.user
        # logger.info(f"request data of top gainner and losse ::{data}")
        response = TopGainnerLosserervice.get_banknifty_gainner(data)
        # logger.info(f"response ;;;;; {response}")
        return jsonify({
            "status": "success",
            "data": response
        })
    except Exception as e:
        logger.info(f"Error generate from top banknifty gainner :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try

@top_gain_loss.route('/nifty_losser', methods=["GET"])
def top_nifty_losser():
    try:
        logger.info(f"call api of nifty losser ::::::")
        nifty_losers = TopGainnerLosserervice.get_nifty_losser()
        return jsonify({
            "status": "success",
            "data": nifty_losers
        })
    except Exception as e:
        logger.error(f"Error from the top nifty losser :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try


@top_gain_loss.route('/banknifty_losser', methods=["GET"])
def top_banknifty_losser():
    try:
        banknifty_losers = TopGainnerLosserervice.get_banknifty_losser()
        # logger.info(f"bank nifty losser response -----> {banknifty_losers}")
        return jsonify({
            "status": "success",
            "data": banknifty_losers
        })
    except Exception as e:
        logger.error(f"Error from the top nifty losser :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try

@top_gain_loss.route('/get_top_gain_loss_dashboard', methods=['GET'])
def getDashboard():

    try:
       
        with ThreadPoolExecutor(max_workers=20) as executor:

            nifty_gainers_future = executor.submit(
                TopGainnerLosserervice.get_nifty_gainner
            )

            nifty_losers_future = executor.submit(
                TopGainnerLosserervice.get_nifty_losser
            )

            bank_gainers_future = executor.submit(
                TopGainnerLosserervice.get_banknifty_gainner
            )

            bank_losers_future = executor.submit(
                TopGainnerLosserervice.get_banknifty_losser
            )

            logger.info(f" nifty gainner future :::: {nifty_gainers_future}")

            response = {

                "nifty_gainers":
                    nifty_gainers_future.result(),

                "nifty_losers":
                    nifty_losers_future.result(),

                "bank_gainers":
                    bank_gainers_future.result(),

                "bank_losers":
                    bank_losers_future.result(),
            }

            return jsonify({
                "status": "success",
                "data": response
            })
    except Exception as e:
        logger.info(f"Error from get dashboard function :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try
