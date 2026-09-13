from app.extensions import get_groww_client
from app.socket import SmartAPISocket
from app.socket.socket_manager import active_smartapi_sockets
from flask import request
import SmartApi
from app.utils.logger import get_logger
from SmartApi import SmartConnect
from app.socket import active_smart_connect_sessions
from app.models.order import Orders


logger = get_logger(__name__)


def _get_client():
    try:
        return get_groww_client()
    except Exception as e:
        logger.warning(f"Could not connect to Groww: {e}")
        return None


class Groww:
    def __init__(self):
        pass

    def GetRMS(data):
        try:
            groww = _get_client()
            if not groww:
                return None, None
            logger.info(f"get active_smart_groww_sessions :::: {groww}")

            get_profile = groww.get_user_profile()
            
            get_fund_detail = groww.get_available_margin_details()
            logger.info(f"user profile ::::: {get_profile}")

            return get_profile, get_fund_detail
            
                

        except Exception as e:
            logger.info(f"Error from get RMS detail ::: {str(e)}")



    def GetOrderList():
        try:
            groww = _get_client()
            if not groww:
                return "Groww client unavailable"
            quote_response = groww.get_quote(
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                trading_symbol="NIFTY"
            )
            logger.info(f"quotes response =====> {quote_response}")
            return quote_response
        except Exception as e:
            logger.info(f"Error from get order list ::: {str(e)}")
            return str(e)
        # end try
            