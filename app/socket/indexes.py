from app.socket import AngelOneWebSocket
from app.socket import SmartAPISocket
from app.utils.access_token_validate import validate_access_token
from . import socketio
from flask_socketio import emit, join_room, leave_room
from flask import request
from app.utils.logger import get_logger
import time
import json
import subprocess
import sys
import os
import threading
from app.socket.socket_manager import active_smartapi_sockets

logger = get_logger(__name__)

# ──────────────────────────────────────────────────
# Symbol <-> Angel One SmartAPI token mapping
# ──────────────────────────────────────────────────
SYMBOL_TOKEN_MAP = {
    '^NSEI': '99926000',       # NIFTY 50
    '^NSEBANK': '99926009',    # BANK NIFTY
}

TOKEN_SYMBOL_MAP = {v: k for k, v in SYMBOL_TOKEN_MAP.items()}


# ──────────────────────────────────────────────────
# WebSocket connection management
# ──────────────────────────────────────────────────

active_connections = {}
stop_flags = {}
chart_connections = {}
chart_stop_flags = {}
_chart_symbol_clients = {}  # { symbol: set(client_sids) }


@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'status': 'success', 'message': 'Connected to server'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info(f"Client disconnected: {request.sid}")

    # Clean up index streams
    if request.sid in active_connections:
        stop_flags[request.sid] = True
        del active_connections[request.sid]

    # Clean up chart streams
    chart_key = f"{request.sid}_chart"
    chart_connections.pop(chart_key, None)

    # Remove from chart symbol clients
    for sym, clients in _chart_symbol_clients.items():
        clients.discard(request.sid)

@socketio.on('disconnect')
def handle_disconnect():

    logger.info(f"Client disconnected: {request.sid}")

    user = getattr(request, "user", None)

    if not user:
        return

    client_code = user.get("angleClientCode")

    if client_code in active_smartapi_sockets:

        logger.info(
            f"Cleaning SmartAPI socket: {client_code}"
        )

        active_smartapi_sockets[
            client_code
        ].disconnect()

        del active_smartapi_sockets[
            client_code
        ]

# ──────────────────────────────────────────────────
# Index price streaming (Angel One real-time)
# ──────────────────────────────────────────────────

@socketio.on("subscribe_indexes")
@validate_access_token
def handle_subscribe_indexes(data):
    """Handle subscription to index updates via Angel One SmartAPI WebSocket."""
    logger.info(f"Client {request.sid} subscribed to indexes: {request.user}")
    logger.info(f"data: {data}")
    join_room('indexes')

    # yahoo_tokens = data.get('tokens', [])
    client_code = request.user['angleClientCode']

    if client_code in active_smartapi_sockets:

        logger.info(
            f"Using existing websocket for {client_code}"
        )

        a1_socket = active_smartapi_sockets[client_code]

    else:

        logger.info(
            f"Creating NEW websocket for {client_code}"
        )

        smartapi_connect = SmartAPISocket.on_connect(
            client_code,
            request.user["angleClientPin"],
            request.user["angleTotpSecret"],
            request.user["angleApiKey"]
        )

        jwt_token = smartapi_connect["data"]["jwtToken"]

        if jwt_token.startswith("Bearer "):
            jwt_token = jwt_token.split(" ")[1]

        feed_token = smartapi_connect["data"]["feedToken"]

        a1_socket = AngelOneWebSocket(
            jwt_token=jwt_token,
            api_key=request.user["angleApiKey"],
            client_code=client_code,
            feed_token=feed_token,
            tokens= data["tokens"] if data['tokens'] else [],
            room=request.sid
        )

            # STORE SOCKET
        active_smartapi_sockets[client_code] = a1_socket

            # CONNECT ONLY ONCE
        a1_socket.connect()
    
    a1_socket.subscribe_tokens(data["tokens"])

    # emit("indexes_data", {
    #     "message": "Successfully subscribed to indexes updates",
    #     "symbols": data['tokens'],
    #     "data": smartapi_connect
    # })

INTERVAL_MAP = {
    '1m':  'ONE_MINUTE',
    '5m':  'FIVE_MINUTE',
    '15m': 'FIFTEEN_MINUTE',
    '30m': 'THIRTY_MINUTE',
    '1h':  'ONE_HOUR',
    '1d':  'ONE_DAY',
}

PERIOD_DAYS = {
    '1d': 1,
    '5d': 5,
    '1mo': 30,
    '3mo': 90,
    '6mo': 180,
    '1y': 365,
    '2y': 730,
}

# ──────────────────────────────────────────────────
# Real-Time Chart Subscription & Backfill Cache
# ──────────────────────────────────────────────────
import threading
from datetime import datetime, timedelta
from app.services.realtime_candle_manager import realtime_candle_manager

class BackfillCache:
    """Thread-safe TTL Cache to shield external APIs from duplicate historical requests."""
    def __init__(self, ttl=60):
        self.cache = {}
        self.ttl = ttl
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.cache:
                candles, expiry = self.cache[key]
                if time.time() < expiry:
                    return candles
                else:
                    del self.cache[key]
            return None

    def set(self, key, candles):
        with self.lock:
            self.cache[key] = (candles, time.time() + self.ttl)

backfill_cache = BackfillCache(ttl=60)

def resolve_symbol_to_token(symbol):
    """Resolves symbol (e.g. RELIANCE.NS, Nifty 50, Bank Nifty) to Angel One token and exchange."""
    direct_map = {
        '^NSEI': ('99926000', 'NSE'),       # Nifty 50
        'Nifty 50': ('99926000', 'NSE'),
        '^NSEBANK': ('99926009', 'NSE'),    # Nifty Bank
        'Nifty Bank': ('99926009', 'NSE'),
        'Bank Nifty': ('99926009', 'NSE'),
        'Finnifty': ('99926037', 'NSE'),
        '^CNXFIN': ('99926037', 'NSE'),
        'Midcpnifty': ('99926074', 'NSE'),
        '^CRSLMID': ('99926074', 'NSE'),
        'Nifty IT': ('99926008', 'NSE'),
        '^CNXIT': ('99926008', 'NSE'),
        'Nifty Auto': ('99926029', 'NSE'),
        '^CNXAUTO': ('99926029', 'NSE'),
        'Sensex': ('99919000', 'BSE'),
        '^BSESN': ('99919000', 'BSE')
    }

    if symbol in direct_map:
        return direct_map[symbol]

    clean_sym = symbol
    if clean_sym.endswith('.NS'):
        clean_sym = clean_sym[:-3] + '-EQ'
    elif not clean_sym.endswith('-EQ') and clean_sym.isalnum():
        clean_sym = clean_sym + '-EQ'

    # Search in index_data.csv
    try:
        from pathlib import Path
        import pandas as pd
        BASE_DIR = Path(__file__).resolve().parent
        csv_path = BASE_DIR / "index_data.csv"
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            match = df[df['symbol'].str.upper() == clean_sym.upper()]
            if not match.empty:
                token_val = str(match.iloc[0]['token'])
                exch_seg = str(match.iloc[0].get('exch_seg', 'NSE'))
                return token_val, exch_seg
    except Exception as e:
        logger.error(f"Error reading index_data.csv for token resolution: {e}")

    if symbol.isdigit():
        return symbol, 'NSE'

    return None, 'NSE'

def get_user_smart_connect(user):
    """Retrieve an active SmartConnect instance for a user."""
    client_code = user.get("angleClientCode")
    from app.socket import active_smart_connect_sessions
    if client_code in active_smart_connect_sessions:
        return active_smart_connect_sessions[client_code]["obj"]
    
    from app.socket import SmartAPISocket
    res = SmartAPISocket.on_connect(
        client_code,
        user.get("angleClientPin"),
        user.get("angleTotpSecret"),
        user.get("angleApiKey")
    )
    if client_code in active_smart_connect_sessions:
        return active_smart_connect_sessions[client_code]["obj"]
    return None

@socketio.on("subscribe_chart")
@validate_access_token
def handle_chart_data(data):
    from flask_socketio import emit, join_room
    import yfinance as yf

    logger.info(f"Client {request.sid} subscribing to real-time chart: {data}")

    symbol = data.get('symbol', 'Nifty 50')
    interval = data.get('interval', '1d')

    # 1. Resolve Symbol to Angel One token & exchange
    token_id, exchange = resolve_symbol_to_token(symbol)
    if not token_id:
        logger.error(f"Symbol '{symbol}' could not be resolved to an Angel One token.")
        emit('chart_data', {'error': f"Symbol '{symbol}' not found"}, to=request.sid)
        return

    # Map intervals to broker / standard names
    INTERVAL_MAP = {
        '1m':  'ONE_MINUTE',
        '5m':  'FIVE_MINUTE',
        '15m': 'FIFTEEN_MINUTE',
        '30m': 'THIRTY_MINUTE',
        '1h':  'ONE_HOUR',
        '1d':  'ONE_DAY',
    }

    broker_interval = INTERVAL_MAP.get(interval, 'ONE_DAY')

    try:
        # Cache lookup for historical backfill
        cache_key = f"{token_id}_{interval}"
        candles = backfill_cache.get(cache_key)

        aggregator = realtime_candle_manager.get_aggregator(token_id, interval)

        if not candles:
            candles = []
            # Calculate fromdate and todate for historical backfill (approx 300 periods)
            to_dt = datetime.now()
            if interval == '1d':
                from_dt = to_dt - timedelta(days=365)
            elif interval == '1h':
                from_dt = to_dt - timedelta(days=60)
            elif interval in ['15m', '30m']:
                from_dt = to_dt - timedelta(days=15)
            elif interval == '5m':
                from_dt = to_dt - timedelta(days=5)
            else:  # 1m
                from_dt = to_dt - timedelta(days=2)

            from_str = from_dt.strftime("%Y-%m-%d %H:%M")
            to_str = to_dt.strftime("%Y-%m-%d %H:%M")

            # Try to fetch historical data from broker API
            smart_obj = get_user_smart_connect(request.user)
            if smart_obj:
                try:
                    historic_params = {
                        "exchange": exchange,
                        "symboltoken": token_id,
                        "interval": broker_interval,
                        "fromdate": from_str,
                        "todate": to_str
                    }
                    logger.info(f"Fetching historic candles from Angel One: {historic_params}")
                    res = smart_obj.getCandleData(historic_params)
                    if res and res.get("status") and res.get("data"):
                        raw_data = res.get("data")
                        for row in raw_data:
                            if len(row) >= 6:
                                try:
                                    ts_str = row[0]
                                    if 'T' in ts_str:
                                        clean_ts = ts_str.split('+')[0].split('Z')[0]
                                        dt = datetime.strptime(clean_ts, "%Y-%m-%dT%H:%M:%S")
                                    else:
                                        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                                    ts_ms = int(dt.timestamp() * 1000)
                                except Exception as parse_err:
                                    logger.error(f"Error parsing candle date '{ts_str}': {parse_err}")
                                    continue

                                candles.append({
                                    'time': ts_ms,
                                    'open': float(row[1]),
                                    'high': float(row[2]),
                                    'low': float(row[3]),
                                    'close': float(row[4]),
                                    'volume': int(row[5])
                                })
                except Exception as broker_err:
                    logger.error(f"Failed to fetch historical candles from Angel One: {broker_err}")

            # Graceful yfinance fallback if Angel One returns no candles (e.g. BSE or index offline)
            if not candles:
                logger.info(f"Falling back to yfinance for history: symbol={symbol}")
                try:
                    yf_symbol_map = {
                        'Nifty 50': '^NSEI',
                        'Nifty Bank': '^NSEBANK',
                        'Bank Nifty': '^NSEBANK',
                        'Finnifty': '^CNXFIN',
                        'Midcpnifty': '^CRSLMID',
                        'Sensex': '^BSESN',
                        'Nifty IT': '^CNXIT',
                        'Nifty Auto': '^CNXAUTO'
                    }
                    yf_ticker = yf_symbol_map.get(symbol, symbol)
                    ticker = yf.Ticker(yf_ticker)
                    
                    yf_period = '1mo'
                    if interval == '1d':
                        yf_period = '6mo'
                    elif interval == '1m':
                        yf_period = '5d'

                    df = ticker.history(period=yf_period, interval=interval)
                    if not df.empty:
                        for idx, row in df.iterrows():
                            candles.append({
                                'time': int(idx.timestamp() * 1000),
                                'open': round(float(row['Open']), 2),
                                'high': round(float(row['High']), 2),
                                'low': round(float(row['Low']), 2),
                                'close': round(float(row['Close']), 2),
                                'volume': int(row['Volume']),
                            })
                except Exception as yf_err:
                    logger.error(f"yfinance fallback failed: {yf_err}")

            if candles:
                # Prime/seed aggregator in memory
                aggregator.prime_history(candles)
                backfill_cache.set(cache_key, candles)

        # 2. Get up-to-date candle list from aggregator (merges history + real-time ticks)
        full_candles = aggregator.get_candles()
        if not full_candles:
            emit('chart_data', {'error': 'No historical or real-time data available'}, to=request.sid)
            return

        current_price = full_candles[-1]['close']
        prev_close = full_candles[-2]['close'] if len(full_candles) > 1 else full_candles[-1]['open']

        # Send historical backfill
        emit('chart_data', {
            'type': 'history',
            'symbol': symbol,
            'token': token_id,
            'candles': full_candles,
            'currentPrice': round(float(current_price), 2),
            'previousClose': round(float(prev_close), 2)
        }, to=request.sid)

        # 3. Join event-driven streaming Room
        room_name = f"chart_{token_id}_{interval}"
        join_room(room_name)
        logger.info(f"Joined client {request.sid} to chart room: {room_name}")

        # Keep track of room to cleanup on unsubscribe
        request_key = f"{request.sid}_chart"
        chart_connections[request_key] = room_name

        # 4. Subscribe the central broker WebSocket to live ticks for this token
        client_code = request.user['angleClientCode']
        if client_code not in active_smartapi_sockets:
            smartapi_connect = SmartAPISocket.on_connect(
                client_code,
                request.user["angleClientPin"],
                request.user["angleTotpSecret"],
                request.user["angleApiKey"]
            )
            jwt_token = smartapi_connect["data"]["jwtToken"]
            if jwt_token.startswith("Bearer "):
                jwt_token = jwt_token.split(" ")[1]
            feed_token = smartapi_connect["data"]["feedToken"]

            a1_socket = AngelOneWebSocket(
                jwt_token=jwt_token,
                api_key=request.user["angleApiKey"],
                client_code=client_code,
                feed_token=feed_token,
                tokens=[symbol],
                room=request.sid
            )
            active_smartapi_sockets[client_code] = a1_socket
            a1_socket.connect()
        else:
            a1_socket = active_smartapi_sockets[client_code]

        # Dynamically add the symbol/token to the live feed if not present
        if symbol not in a1_socket.tokens:
            a1_socket.tokens.append(symbol)
            a1_socket.subscribe_tokens([symbol])

    except Exception as e:
        logger.error(f"Error handling chart subscription: {e}")
        emit('chart_data', {'error': str(e)}, to=request.sid)


@socketio.on("unsubscribe_indexes")
def handle_unsubscribe_indexes():
    """Handle unsubscription from index updates"""
    logger.info(f"Client {request.sid} unsubscribed from indexes")
    if request.sid in active_connections:
        stop_flags[request.sid] = True
    leave_room('indexes')
    emit("unsubscription_confirmed", {
        "message": "Successfully unsubscribed from indexes updates"
    })


def send_indexes_data(data):
    """Send index update to all subscribed clients"""
    socketio.emit('indexes_data', data, room='indexes')


# ──────────────────────────────────────────────────
# End of Indexes and Chart socket handlers
# ──────────────────────────────────────────────────


@socketio.on("unsubscribe_chart")
def handle_unsubscribe_chart(data=None):
    from flask_socketio import leave_room
    logger.info(f"Client {request.sid} unsubscribed from chart: {data}")
    
    chart_key = f"{request.sid}_chart"
    room_name = chart_connections.pop(chart_key, None)
    if room_name:
        leave_room(room_name)
        logger.info(f"Left chart room: {room_name}")