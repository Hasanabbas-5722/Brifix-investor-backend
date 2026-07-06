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

groww = get_groww_client()


class Groww:
    def __init__(self):
        pass

    def GetRMS(data):
        try:
            logger.info(f"get active_smart_groww_sessions :::: {groww}")

            get_profile = groww.get_user_profile()
            
            get_fund_detail = groww.get_available_margin_details()
            logger.info(f"user profile ::::: {get_profile}")

            return get_profile, get_fund_detail
            
                

        except Exception as e:
            logger.info(f"Error from get RMS detail ::: {str(e)}")



    def GetOrderList():
        try:
            # get_order_list = groww.get_order_list(
            #     page = 0, # Optional: Page number for paginated results
            #     page_size = 100 # Optional: Number of orders to fetch per page (default is 100)
            # )
            # logger.info(f"get order list :::: {get_order_list}")
            # for item in get_order_list:
            #     Orders.add_orders(item)
            quote_response = groww.get_quote(
                exchange=groww.EXCHANGE_NSE,
                segment=groww.SEGMENT_CASH,
                trading_symbol="NIFTY"
            )
            logger.info(f"quotes response =====> {quote_response}")
            return 
        except Exception as e:
            logger.info(f"Error from get order list ::: {str(e)}")
            return str(e)
        # end try
            