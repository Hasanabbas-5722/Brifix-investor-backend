import requests
from pathlib import Path
from flask_socketio import SocketIO, emit
from app.utils.logger import get_logger
import app
from SmartApi import SmartConnect
import pyotp

logger = get_logger(__name__)

logger.info("Initializing SocketIO...")

# Initialize SocketIO with eventlet (since eventlet is installed)
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode='eventlet',  # Use eventlet since it's installed
    # logger=True,
    # engineio_logger=True
)

from app.services.realtime_candle_manager import realtime_candle_manager
realtime_candle_manager.init_socketio(socketio)

active_smart_connect_sessions = {}

class SmartAPISocket:
    def __init__(self):
        self.socketio = socketio
    
    @staticmethod
    def on_connect(angle_client_code, angle_client_pin, angle_totp_secret, angle_api_key, force_new=False):
        try:
            if not force_new and angle_client_code in active_smart_connect_sessions:
                logger.info("Client already connected, returning cached session")
                return active_smart_connect_sessions[angle_client_code]["session"]

            logger.info("Client connecting...")
            totp = pyotp.TOTP(angle_totp_secret).now()
            obj = SmartConnect(api_key=angle_api_key)
            logger.info(f"totp: {totp}")
            session = obj.generateSession(angle_client_code, angle_client_pin, totp)
            logger.info(f"session tokemn: {session['data']['jwtToken'].split(' ')[1]}")
            # obj.setAccessToken(session["data"]["jwtToken"].split(" ")[1])
            # obj.setRefreshToken(session["data"]["refreshToken"])
            obj.setAccessToken(session["data"]["jwtToken"].split(" ")[1])
            obj.setRefreshToken(session["data"]["refreshToken"])
            obj.api_key = angle_api_key   # ← force re-set AFTER token calls
            rms = obj.rmsLimit()

            logger.info(f"RMS ::: {rms}")
            logger.info(f"obj token ::: {obj.access_token}")
            logger.info(f"obj apikey ::: {obj.api_key}")
            
            if session and session.get("status"):
                active_smart_connect_sessions[angle_client_code] = {
                    "obj": obj,
                    "session": session
                }

            logger.info(f"active smart connect seesssion ::: {active_smart_connect_sessions}")
                
            return session
        except Exception as e:
            return str(e)
    
    @staticmethod
    def get_historical_data(client_code, api_key, token, params):
        """Fetch historical data using an existing active token to avoid logging out other sessions"""
        logger.info(f"checking currenct connection of smart api : {active_smart_connect_sessions}")
        if client_code in active_smart_connect_sessions:
            obj = active_smart_connect_sessions[client_code]["obj"]
            obj.access_token = token
            obj.Authorization = f"Bearer {token}"
            logger.info(f"Using cached SmartConnect object for {client_code}")
        else:
            obj = SmartConnect(api_key=api_key)
            obj.setAccessToken(token)
            obj.access_token = token
            obj.Authorization = f"Bearer {token}"
            logger.info(f"Created new SmartConnect object for {client_code}")
            # logger.info(f"Obj for currenct client {client_code}: {obj.__dict__}")

        logger.info(f"Authorization: {obj.Authorization}")

        url = (
            "https://apiconnect.angelone.in"
            "/rest/secure/angelbroking/"
            "historical/v1/getCandleData"
        )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "106.193.147.98",
            "X-MACAddress": "01:01:01:01:00:00",

            # IMPORTANT
            "X-PrivateKey": api_key
        }

        payload = {
            "exchange": "NSE",
            "symboltoken": symbol_token,
            "interval": "ONE_DAY",
            "fromdate": params["fromdate"],
            "todate": params["todate"]
        }


        response = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload)
        )

        logger.info(
            f"STATUS CODE: {response.status_code}"
        )

        logger.info(
            f"RESPONSE: {response.text}"
        )

        logger.info(f"Object Data: {obj.__dict__}")
        logger.info(f"obj candle data :: {obj.getCandleData(params)}")
        return obj.getCandleData(params)


import json
import threading
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from app.utils.logger import get_logger
from pandas import pandas as pd
import numpy as np

logger = get_logger(__name__)


def find_index_token(token_name):
    # df = pd.read_json(
    #     "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    # )
    BASE_DIR = Path(__file__).resolve().parent

    csv_path = BASE_DIR / "index_data.csv"
    df = pd.read_csv(csv_path)
    df.to_csv("index_data.csv")
    # logger.info(f"df: {df['symbol'], df['token']}")
    logger.info(f"type of token {token_name}")
    logger.info(f"data: {df[df['symbol'].isin(token_name)]['token'].tolist()}")
    # logger.info(f"token {df[df['symbol'].isin(token_name)]['token'].values[0].tolist()}")
    
    token = df[df['symbol'].isin(token_name)]['token'].tolist()
    return token 

class AngelOneWebSocket:

    def __init__(
        self,
        jwt_token,
        api_key,
        client_code,
        feed_token,
        tokens,
        room
    ):

        self.jwt_token = jwt_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token

        self.tokens = tokens
        self.room = room
        self.is_connected = False

        self.correlation_id = f"{client_code}_stream"

        self.sws = SmartWebSocketV2(
            self.jwt_token,
            self.api_key,
            self.client_code,
            self.feed_token
        )

        # callbacks
        self.sws.on_open = self.on_open
        self.sws.on_data = self.on_data
        self.sws.on_error = self.on_error
        self.sws.on_close = self.on_close

    # ==========================================
    # CONNECT
    # ==========================================
    def connect(self):

        logger.info("Connecting SmartAPI WebSocket...")
        # find_index_token()
        threading.Thread(
            target=self.sws.connect,
            daemon=True
        ).start()

    # ==========================================
    # ON OPEN
    # ==========================================
    def on_open(self, wsapp):

        logger.info("SmartAPI WebSocket Connected")

        self.is_connected = True

        self.subscribe_tokens(self.tokens)

    # ==========================================
    # SUBSCRIBE TOKENS
    # ==========================================
    def subscribe_tokens(self,token=[]):
        if not self.is_connected:

            logger.warning("Socket not connected yet")

            return
        token = find_index_token(self.tokens)
        token_list = [
            {
                "exchangeType": 1,
                "tokens": token
            }
        ]

        logger.info(f"Subscribing tokens: {token_list}")

        self.sws.subscribe(
            self.correlation_id,
            2,
            token_list
        )

    # ==========================================
    # RECEIVE LIVE DATA
    # ==========================================
    def on_data(self, wsapp, message):

        try:

            # logger.info(f"LIVE DATA: {message}")

            token = message.get("token")

            ltp = message.get("last_traded_price")

            if ltp:
                ltp = ltp / 100

            # Route tick to real-time candle manager
            if token and ltp:
                volume = message.get("last_traded_quantity", message.get("volume", 0))
                realtime_candle_manager.process_tick(token, ltp, volume)

            response = {
                "token": token,
                "ltp": ltp,
                "full_data": message
            }

            # logger.info(f"EMIT DATA: {response}")

            socketio.emit(
                "indexes_data",
                response,
                room='indexes'
            )

        except Exception as e:
            logger.exception(f"on_data error: {e}")

    # ==========================================
    # ERROR
    # ==========================================
    def on_error(self, wsapp, error):

        logger.exception(f"SmartAPI WebSocket Error: {error}")

    # ==========================================
    # CLOSE
    # ==========================================
    def on_close(
        self,
        wsapp,
        close_status_code=None,
        close_msg=None
    ):

        logger.warning(
            f"Closed: {close_status_code} | {close_msg}"
        )   
    # ==========================================
    # DISCONNECT
    # ==========================================
    def disconnect(self):

        try:

            self.is_connected = False

            self.sws.close_connection()

            logger.info(
                f"SmartAPI socket disconnected: {self.client_code}"
            )

        except Exception as e:

            logger.exception(e)




