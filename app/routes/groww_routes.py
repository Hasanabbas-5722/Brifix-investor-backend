from app.services.groww_service import Groww
from flask import jsonify
from app.utils.access_token_validate import validate_access_token
from flask import Blueprint, request
from app.utils.logger import get_logger


logger = get_logger(__name__)
groww = Blueprint("groww", __name__, url_prefix="/api/v1/groww")


@groww.route("/groww_user_profile", methods=["GET"])
@validate_access_token
def Stock_total_amount():
    try:
        user_data = request.user
        getprofile, get_fund_detail = Groww.GetRMS(user_data)

        return jsonify({
            "status": "success",
            "data": {
                "getProfile": getprofile,
                "getFundDetail": get_fund_detail
            }
        })

    except Exception as e:
        logger.info(f"Error from stock total amount ::: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })


@groww.route("/getOrderList", methods=["GET"])
@validate_access_token
def Order_list():
    try:
        getOrders = Groww.GetOrderList()

        return jsonify({
            "status": "success",
            "data": getOrders
        })
    except Exception as e:
        logger.info(f"Error from the groww order list :::: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })