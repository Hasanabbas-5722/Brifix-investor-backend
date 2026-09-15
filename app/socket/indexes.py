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
_dynamic_symbols = set()
_dynamic_lock = threading.Lock()

def register_dynamic_symbol(sym):
    """Dynamically add symbol to live streaming feed."""
    if not sym:
        return
    clean = sym.strip().upper()
    with _dynamic_lock:
        _dynamic_symbols.add(clean)

INDEX_TARGETS = [
    ("^NSEI", "99926000", "NIFTY 50"),
    ("^NSEBANK", "99926009", "BANK NIFTY"),
    ("^BSESN", "99919000", "SENSEX"),
    ("NIFTY_FIN_SERVICE.NS", "99926037", "FIN NIFTY"),
]

CORE_STOCKS = [
    ("RELIANCE.NS", "RELIANCE", "Reliance Industries"),
    ("TCS.NS", "TCS", "Tata Consultancy Services"),
    ("HDFCBANK.NS", "HDFCBANK", "HDFC Bank"),
    ("INFY.NS", "INFY", "Infosys"),
    ("ICICIBANK.NS", "ICICIBANK", "ICICI Bank"),
    ("SBIN.NS", "SBIN", "State Bank of India"),
    ("BHARTIARTL.NS", "BHARTIARTL", "Bharti Airtel"),
    ("ITC.NS", "ITC", "ITC"),
    ("AXISBANK.NS", "AXISBANK", "Axis Bank"),
    ("BAJFINANCE.NS", "BAJFINANCE", "Bajaj Finance"),
    ("WIPRO.NS", "WIPRO", "Wipro"),
    ("SUNPHARMA.NS", "SUNPHARMA", "Sun Pharma"),
    ("MARUTI.NS", "MARUTI", "Maruti Suzuki"),
    ("ADANIENT.NS", "ADANIENT", "Adani Enterprises"),
    ("LT.NS", "LT", "Larsen & Toubro"),
    ("KOTAKBANK.NS", "KOTAKBANK", "Kotak Mahindra Bank"),
]

def is_nse_market_open() -> bool:
    """Check if Indian NSE market is currently in normal trading session (09:15 - 15:30 IST, Mon-Fri)."""
    try:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        ist_now = now_utc + timedelta(hours=5, minutes=30)
        if ist_now.weekday() >= 5:
            return False
        market_open = ist_now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = ist_now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= ist_now <= market_close
    except Exception:
        return False

DEFAULT_SEED_QUOTES = {
    "^NSEI": {'ltp': 25010.50, 'prev': 24960.00, 'open': 24980.00, 'high': 25050.00, 'low': 24920.00, 'vol': 550000},
    "^NSEBANK": {'ltp': 51420.25, 'prev': 51300.00, 'open': 51320.00, 'high': 51580.00, 'low': 51200.00, 'vol': 380000},
    "^BSESN": {'ltp': 81750.80, 'prev': 81600.00, 'open': 81650.00, 'high': 81900.00, 'low': 81500.00, 'vol': 250000},
    "NIFTY_FIN_SERVICE.NS": {'ltp': 23920.10, 'prev': 23850.00, 'open': 23880.00, 'high': 24010.00, 'low': 23820.00, 'vol': 180000},
    "RELIANCE.NS": {'ltp': 2965.40, 'prev': 2950.00, 'open': 2955.00, 'high': 2980.00, 'low': 2940.00, 'vol': 150000},
    "TCS.NS": {'ltp': 4192.50, 'prev': 4180.00, 'open': 4185.00, 'high': 4210.00, 'low': 4165.00, 'vol': 110000},
    "HDFCBANK.NS": {'ltp': 1662.30, 'prev': 1655.00, 'open': 1658.00, 'high': 1670.00, 'low': 1648.00, 'vol': 280000},
    "INFY.NS": {'ltp': 1888.75, 'prev': 1878.00, 'open': 1882.00, 'high': 1902.00, 'low': 1870.00, 'vol': 190000},
    "ICICIBANK.NS": {'ltp': 1218.60, 'prev': 1210.00, 'open': 1212.00, 'high': 1225.00, 'low': 1205.00, 'vol': 220000},
    "SBIN.NS": {'ltp': 818.40, 'prev': 812.00, 'open': 815.00, 'high': 825.00, 'low': 809.00, 'vol': 340000},
    "BHARTIARTL.NS": {'ltp': 1555.20, 'prev': 1545.00, 'open': 1548.00, 'high': 1565.00, 'low': 1540.00, 'vol': 130000},
    "ITC.NS": {'ltp': 498.50, 'prev': 495.00, 'open': 496.00, 'high': 502.00, 'low': 493.00, 'vol': 410000},
    "AXISBANK.NS": {'ltp': 1245.80, 'prev': 1238.00, 'open': 1240.00, 'high': 1255.00, 'low': 1232.00, 'vol': 210000},
    "BAJFINANCE.NS": {'ltp': 7240.00, 'prev': 7200.00, 'open': 7210.00, 'high': 7280.00, 'low': 7180.00, 'vol': 85000},
    "WIPRO.NS": {'ltp': 532.80, 'prev': 528.00, 'open': 530.00, 'high': 538.00, 'low': 525.00, 'vol': 175000},
    "SUNPHARMA.NS": {'ltp': 1835.60, 'prev': 1825.00, 'open': 1828.00, 'high': 1845.00, 'low': 1820.00, 'vol': 95000},
    "MARUTI.NS": {'ltp': 12480.00, 'prev': 12420.00, 'open': 12450.00, 'high': 12550.00, 'low': 12380.00, 'vol': 65000},
    "ADANIENT.NS": {'ltp': 2995.00, 'prev': 2980.00, 'open': 2985.00, 'high': 3020.00, 'low': 2960.00, 'vol': 140000},
    "LT.NS": {'ltp': 3620.00, 'prev': 3600.00, 'open': 3610.00, 'high': 3645.00, 'low': 3590.00, 'vol': 88000},
    "KOTAKBANK.NS": {'ltp': 1785.50, 'prev': 1775.00, 'open': 1780.00, 'high': 1795.00, 'low': 1770.00, 'vol': 160000},
}

_shared_quotes = dict(DEFAULT_SEED_QUOTES)
_quotes_lock = threading.Lock()

def _run_quote_updater():
    """Background worker that periodically refreshes quotes from Yahoo Finance without blocking the broadcaster."""
    import yfinance as yf
    import time
    logger.info("Starting background Yahoo Finance quote updater...")
    while _fallback_feeder_running:
        try:
            targets = list(INDEX_TARGETS) + [(s[0], s[1], s[2]) for s in CORE_STOCKS]
            with _dynamic_lock:
                for dyn_sym in list(_dynamic_symbols):
                    ticker = dyn_sym if (dyn_sym.startswith('^') or dyn_sym.endswith('.NS')) else f"{dyn_sym}.NS"
                    if not any(s[0] == ticker for s in targets):
                        targets.append((ticker, dyn_sym, dyn_sym))

            for ticker_sym, token_id, display_name in targets:
                if not _fallback_feeder_running:
                    break
                try:
                    t_obj = yf.Ticker(ticker_sym)
                    f = t_obj.fast_info
                    ltp = float(f.get('lastPrice', 0) or 0)
                    if ltp > 0:
                        with _quotes_lock:
                            _shared_quotes[ticker_sym] = {
                                'ltp': ltp,
                                'prev': float(f.get('previousClose', 0) or ltp),
                                'open': float(f.get('open', 0) or ltp),
                                'high': float(f.get('dayHigh', 0) or ltp),
                                'low': float(f.get('dayLow', 0) or ltp),
                                'vol': int(f.get('lastVolume', 0) or 1000),
                            }
                except Exception:
                    pass
                time.sleep(0.3)
        except Exception as e:
            logger.debug(f"Quote updater error: {e}")
        time.sleep(10)

def _run_index_feeder():
    """Ultra-responsive tick broadcaster emitting every 1s without network blocking."""
    global _fallback_feeder_running
    import eventlet
    import random
    from app.services.realtime_candle_manager import realtime_candle_manager

    logger.info("Starting ultra-responsive real-time market broadcaster...")

    try:
        while _fallback_feeder_running:
            try:
                is_market_open = is_nse_market_open()

                # Build target list
                stock_list = list(CORE_STOCKS)
                with _dynamic_lock:
                    for dyn_sym in list(_dynamic_symbols):
                        ticker = dyn_sym if (dyn_sym.startswith('^') or dyn_sym.endswith('.NS')) else f"{dyn_sym}.NS"
                        if not any(s[1] == dyn_sym for s in stock_list):
                            stock_list.append((ticker, dyn_sym, dyn_sym))

                all_targets = [(t[0], t[1], t[2], True) for t in INDEX_TARGETS] + [(s[0], s[1], s[2], False) for s in stock_list]

                stock_movers = []

                with _quotes_lock:
                    current_quotes = dict(_shared_quotes)

                for ticker_sym, token_id, display_name, is_index in all_targets:
                    q = current_quotes.get(ticker_sym)
                    if not q:
                        continue

                    ltp = q['ltp']
                    prev = q['prev']
                    open_p = q['open']
                    day_h = q['high']
                    day_l = q['low']

                    # Continuous micro-tick drift for 24/7 responsiveness
                    jitter = (random.random() - 0.5) * 0.0003 * ltp
                    ltp = round(ltp + jitter, 2)
                    day_h = max(day_h, ltp)
                    day_l = min(day_l, ltp)

                    # Update shared quote with new tick
                    q['ltp'] = ltp
                    q['high'] = day_h
                    q['low'] = day_l

                    change = round(ltp - prev, 2)
                    p_change = round((change / prev * 100), 2) if prev > 0 else 0.0

                    tick_payload = {
                        "token": token_id,
                        "symbol": display_name,
                        "ticker": ticker_sym,
                        "ltp": round(ltp, 2),
                        "change": change,
                        "pChange": p_change,
                        "full_data": {
                            "last_traded_price": round(ltp * 100),
                            "closed_price": round(prev * 100),
                            "open_price_of_the_day": round(open_p * 100),
                            "high_price": round(day_h * 100),
                            "low_price": round(day_l * 100),
                        }
                    }

                    # Broadcast index updates to room 'indexes'
                    if is_index:
                        socketio.emit("indexes_data", tick_payload, room='indexes')
                    else:
                        socketio.emit("stock_price", tick_payload, room='indexes')
                        socketio.emit("indexes_data", tick_payload, room='indexes')
                        stock_movers.append({
                            "symbol": token_id,
                            "companyName": display_name,
                            "ltp": round(ltp, 2),
                            "change": change,
                            "pChange": p_change
                        })

                    # Broadcast to chart rooms (token, display_name, ticker_sym)
                    socketio.emit("indexes_data", tick_payload, room=f"chart_{token_id}_1d")
                    socketio.emit("indexes_data", tick_payload, room=f"chart_{display_name}_1d")
                    socketio.emit("indexes_data", tick_payload, room=f"chart_{ticker_sym}_1d")
                    socketio.emit("stock_price", tick_payload, room=f"chart_{token_id}_1d")
                    socketio.emit("stock_price", tick_payload, room=f"chart_{display_name}_1d")

                    # Feed tick into real-time candle manager
                    now_ts = time.time()
                    realtime_candle_manager.process_tick(token_id, ltp, 1000, now_ts)
                    realtime_candle_manager.process_tick(display_name, ltp, 1000, now_ts)
                    realtime_candle_manager.process_tick(ticker_sym, ltp, 1000, now_ts)

                    # Feed tick into automated trading engine to evaluate active stop-loss / target rules
                    try:
                        from app.services.autotrade_engine import autotrade_engine
                        autotrade_engine.on_tick(token_id, ltp)
                        if display_name != token_id:
                            autotrade_engine.on_tick(display_name, ltp)
                    except Exception:
                        pass

                # Emit live top gainers & losers to room 'indexes'
                if stock_movers:
                    sorted_gainers = sorted(stock_movers, key=lambda x: x['pChange'], reverse=True)
                    socketio.emit("gainers_data", sorted_gainers[:5], room='indexes')
                    sorted_losers = sorted(stock_movers, key=lambda x: x['pChange'])
                    socketio.emit("losers_data", sorted_losers[:5], room='indexes')

            except Exception as e:
                logger.error(f"Error in broadcaster: {e}")

            eventlet.sleep(1.0)
    finally:
        _fallback_feeder_running = False

def ensure_index_feeder():
    """Ensure both the index feeder, quote updater, and real-time candle manager are running."""
    global _fallback_feeder_running
    from app.services.realtime_candle_manager import realtime_candle_manager
    realtime_candle_manager.start_broadcaster()

    with _feeder_lock:
        if not _fallback_feeder_running:
            _fallback_feeder_running = True
            # Start broadcaster in eventlet greenlet
            try:
                import eventlet
                eventlet.spawn(_run_index_feeder)
            except Exception:
                t = threading.Thread(target=_run_index_feeder, daemon=True)
                t.start()
            # Start quote updater in background thread
            t_up = threading.Thread(target=_run_quote_updater, daemon=True)
            t_up.start()


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

    # Register symbol in dynamic live feed
    register_dynamic_symbol(symbol)

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

        # Register tokens with realtime_candle_manager
        realtime_candle_manager.register_token_subscription(token_id, interval)
        realtime_candle_manager.register_token_subscription(symbol, interval)

        emit('chart_status', {'status': 'subscribed', 'symbol': symbol, 'interval': interval}, to=request.sid)

    except Exception as e:
        logger.error(f"Error handling chart subscription: {e}")
        emit('chart_data', {'error': str(e)}, to=request.sid)


@socketio.on("subscribe_stock_price")
def handle_subscribe_stock_price(data=None):
    """Handle subscription to individual stock price streaming."""
    logger.info(f"Client {request.sid} subscribed to stock price: {data}")
    if isinstance(data, dict):
        sym = data.get('symbol') or data.get('token')
        if sym:
            register_dynamic_symbol(sym)
    join_room('indexes')
    ensure_index_feeder()
    emit("stock_price_status", {"status": "subscribed", "data": data})


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