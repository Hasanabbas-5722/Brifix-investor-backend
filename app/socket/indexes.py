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


_fallback_feeder_running = False
_feeder_lock = threading.Lock()

def _run_index_feeder():
    """Background index broadcaster for clients in the 'indexes' and 'chart' rooms."""
    global _fallback_feeder_running
    import yfinance as yf
    import eventlet
    logger.info("Starting non-blocking background index broadcaster...")

    # Symbols and their corresponding tokens
    index_targets = [
        ("^NSEI", "99926000", "NIFTY 50"),
        ("^NSEBANK", "99926009", "BANK NIFTY"),
        ("^BSESN", "99919000", "SENSEX"),
        ("NIFTY_FIN_SERVICE.NS", "99926037", "FIN NIFTY"),
    ]

    while _fallback_feeder_running:
        try:
            for ticker_sym, token_id, index_name in index_targets:
                try:
                    t = yf.Ticker(ticker_sym)
                    fast = t.fast_info
                    ltp = float(fast.get('lastPrice', 0) or 0)
                    prev = float(fast.get('previousClose', 0) or ltp)
                    open_p = float(fast.get('open', 0) or prev)
                    day_h = float(fast.get('dayHigh', 0) or ltp)
                    day_l = float(fast.get('dayLow', 0) or ltp)

                    if ltp > 0:
                        socketio.emit(
                            "indexes_data",
                            {
                                "token": token_id,
                                "symbol": index_name,
                                "ticker": ticker_sym,
                                "ltp": round(ltp, 2),
                                "full_data": {
                                    "last_traded_price": round(ltp * 100),
                                    "closed_price": round(prev * 100),
                                    "open_price_of_the_day": round(open_p * 100),
                                    "high_price": round(day_h * 100),
                                    "low_price": round(day_l * 100),
                                }
                            },
                            room='indexes'
                        )

                        # Feed into realtime candle manager
                        realtime_candle_manager.process_tick(token_id, ltp, 1000)
                        realtime_candle_manager.process_tick(index_name, ltp, 1000)
                        realtime_candle_manager.process_tick(ticker_sym, ltp, 1000)

                except Exception as tick_err:
                    logger.debug(f"Tick error for {ticker_sym}: {tick_err}")

                eventlet.sleep(0.1)

        except Exception as e:
            logger.error(f"Error in background index broadcaster: {e}")

        eventlet.sleep(2)

def ensure_index_feeder():
    global _fallback_feeder_running
    with _feeder_lock:
        if not _fallback_feeder_running:
            _fallback_feeder_running = True
            try:
                import eventlet
                eventlet.spawn(_run_index_feeder)
            except Exception:
                t = threading.Thread(target=_run_index_feeder, daemon=True)
                t.start()


@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info(f"Client connected: {request.sid}")
    emit('connected', {'status': 'success', 'message': 'Connected to server'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection with complete cleanup"""
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

    user = getattr(request, "user", None)
    if user:
        client_code = user.get("angleClientCode")
        if client_code and client_code in active_smartapi_sockets:
            logger.info(f"Cleaning SmartAPI socket: {client_code}")
            try:
                active_smartapi_sockets[client_code].disconnect()
            except Exception:
                pass
            active_smartapi_sockets.pop(client_code, None)


# ──────────────────────────────────────────────────
# Index price streaming (Real-time)
# ──────────────────────────────────────────────────

@socketio.on("subscribe_indexes")
def handle_subscribe_indexes(data=None):
    """Handle subscription to index updates with fallback for non-broker users."""
    logger.info(f"Client {request.sid} subscribed to indexes: {data}")
    join_room('indexes')
    ensure_index_feeder()

    user = getattr(request, "user", {}) or {}
    client_code = user.get('angleClientCode')

    if not client_code:
        emit("indexes_status", {"status": "subscribed", "mode": "stream_active"})
        return

    try:
        if client_code in active_smartapi_sockets:
            logger.info(f"Using existing SmartAPI websocket for {client_code}")
            a1_socket = active_smartapi_sockets[client_code]
        else:
            logger.info(f"Creating NEW SmartAPI websocket for {client_code}")
            smartapi_connect = SmartAPISocket.on_connect(
                client_code,
                user.get("angleClientPin"),
                user.get("angleTotpSecret"),
                user.get("angleApiKey")
            )

            if not isinstance(smartapi_connect, dict) or not smartapi_connect.get("data"):
                logger.warning("Could not obtain SmartAPI session, using default stream.")
                return

            jwt_token = smartapi_connect["data"].get("jwtToken", "")
            if jwt_token.startswith("Bearer "):
                jwt_token = jwt_token.split(" ")[1]

            feed_token = smartapi_connect["data"].get("feedToken", "")
            tokens_to_sub = data.get("tokens", []) if (data and isinstance(data, dict)) else []

            a1_socket = AngelOneWebSocket(
                jwt_token=jwt_token,
                api_key=user.get("angleApiKey"),
                client_code=client_code,
                feed_token=feed_token,
                tokens=tokens_to_sub,
                room='indexes'
            )

            active_smartapi_sockets[client_code] = a1_socket
            a1_socket.connect()

        if data and isinstance(data, dict) and data.get("tokens"):
            a1_socket.subscribe_tokens(data["tokens"])
    except Exception as e:
        logger.error(f"Error subscribing to broker feed: {e}")

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
def handle_chart_data(data):
    from flask_socketio import emit, join_room
    import yfinance as yf

    logger.info(f"Client {request.sid} subscribing to real-time chart: {data}")
    ensure_index_feeder()

    symbol = data.get('symbol', 'Nifty 50') if isinstance(data, dict) else 'Nifty 50'
    interval = data.get('interval', '1d') if isinstance(data, dict) else '1d'

    # 1. Resolve Symbol to Angel One token & exchange
    token_id, exchange = resolve_symbol_to_token(symbol)
    if not token_id:
        token_id = symbol

    try:
        # 2. Join event-driven streaming Rooms
        room_token = f"chart_{token_id}_{interval}"
        room_symbol = f"chart_{symbol}_{interval}"
        join_room(room_token)
        join_room(room_symbol)
        join_room('indexes')
        logger.info(f"Joined client {request.sid} to chart rooms: {room_token}, {room_symbol}")

        request_key = f"{request.sid}_chart"
        chart_connections[request_key] = room_token

        # 3. Optional: If user has SmartAPI credentials, subscribe to broker feed
        user = getattr(request, 'user', None) or {}
        client_code = user.get('angleClientCode')
        if client_code:
            try:
                if client_code not in active_smartapi_sockets:
                    smartapi_connect = SmartAPISocket.on_connect(
                        client_code,
                        user.get("angleClientPin"),
                        user.get("angleTotpSecret"),
                        user.get("angleApiKey")
                    )
                    if isinstance(smartapi_connect, dict) and smartapi_connect.get("data"):
                        jwt_token = smartapi_connect["data"].get("jwtToken", "")
                        if jwt_token.startswith("Bearer "):
                            jwt_token = jwt_token.split(" ")[1]
                        feed_token = smartapi_connect["data"].get("feedToken", "")

                        a1_socket = AngelOneWebSocket(
                            jwt_token=jwt_token,
                            api_key=user.get("angleApiKey"),
                            client_code=client_code,
                            feed_token=feed_token,
                            tokens=[symbol],
                            room=request.sid
                        )
                        active_smartapi_sockets[client_code] = a1_socket
                        a1_socket.connect()
                else:
                    a1_socket = active_smartapi_sockets[client_code]

                if client_code in active_smartapi_sockets:
                    a1_socket = active_smartapi_sockets[client_code]
                    if symbol not in a1_socket.tokens:
                        a1_socket.tokens.append(symbol)
                        a1_socket.subscribe_tokens([symbol])
            except Exception as broker_err:
                logger.warning(f"Broker connection optional fallback: {broker_err}")

        emit('chart_status', {'status': 'subscribed', 'symbol': symbol, 'interval': interval}, to=request.sid)

    except Exception as e:
        logger.error(f"Error handling chart subscription: {e}")
        emit('chart_data', {'error': str(e)}, to=request.sid)

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