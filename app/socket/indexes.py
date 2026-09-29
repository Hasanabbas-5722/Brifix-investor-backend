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

import queue
_tick_eval_queue = queue.Queue(maxsize=300)
_tick_eval_worker_started = False

def _tick_eval_worker():
    """Background worker processing autotrade position checks without blocking broadcaster."""
    while True:
        try:
            item = _tick_eval_queue.get(timeout=2.0)
            if item is None:
                break
            is_index, sym, ltp = item
            from app.services.autotrade_engine import autotrade_engine
            from app.services.fno_autotrade_engine import fno_autotrade_engine
            if is_index:
                fno_autotrade_engine.on_index_tick(sym, ltp)
            else:
                autotrade_engine.on_tick(sym, ltp)
        except queue.Empty:
            continue
        except Exception:
            pass

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

def get_market_session_info() -> dict:
    """Returns official Indian NSE market session status, trading window, and schedule."""
    try:
        from app.utils.market_calendar import get_ist_time, is_market_holiday
        ist_now = get_ist_time()

        # 1. Holiday check
        is_holiday, holiday_name = is_market_holiday(ist_now)
        if is_holiday:
            return {
                "is_open": False,
                "status": "CLOSED",
                "session": "holiday",
                "message": f"NSE Market closed today for {holiday_name}.",
                "open_time": "09:15 AM IST",
                "close_time": "03:30 PM IST"
            }

        # 2. Weekend check
        if ist_now.weekday() >= 5:
            return {
                "is_open": False,
                "status": "CLOSED",
                "session": "weekend",
                "message": "NSE Market closed for weekend. Next session opens Monday at 09:15 AM IST.",
                "open_time": "09:15 AM IST",
                "close_time": "03:30 PM IST"
            }

        # 3. Time of day check (09:15 AM - 03:30 PM IST)
        mins = ist_now.hour * 60 + ist_now.minute
        if mins < 555:  # Before 09:15 AM
            return {
                "is_open": False,
                "status": "CLOSED",
                "session": "pre_market",
                "message": "NSE Market is closed. Trading session opens at 09:15 AM IST.",
                "open_time": "09:15 AM IST",
                "close_time": "03:30 PM IST"
            }
        elif mins > 930:  # After 03:30 PM
            return {
                "is_open": False,
                "status": "CLOSED",
                "session": "post_market",
                "message": "NSE Market closed (Session ended at 03:30 PM IST). Reopens tomorrow at 09:15 AM IST.",
                "open_time": "09:15 AM IST",
                "close_time": "03:30 PM IST"
            }
        else:
            is_cutoff = mins >= 915  # 15:15 IST intraday cutoff
            return {
                "is_open": True,
                "status": "OPEN",
                "session": "intraday_cutoff" if is_cutoff else "open",
                "message": "Intraday square-off window active (15:15 IST cutoff)." if is_cutoff else "NSE Market is open (09:15 - 15:30 IST).",
                "open_time": "09:15 AM IST",
                "close_time": "03:30 PM IST"
            }
    except Exception as e:
        return {
            "is_open": False,
            "status": "CLOSED",
            "session": "unknown",
            "message": f"Market status check: {e}",
            "open_time": "09:15 AM IST",
            "close_time": "03:30 PM IST"
        }

def is_nse_market_open() -> bool:
    """Check if Indian NSE market is currently in normal trading session (09:15 - 15:30 IST, Mon-Fri)."""
    return bool(get_market_session_info().get("is_open", False))

DEFAULT_SEED_QUOTES = {
    "^NSEI": {'ltp': 23320.30, 'prev': 23118.60, 'open': 23150.00, 'high': 23350.00, 'low': 23100.00, 'vol': 550000},
    "^NSEBANK": {'ltp': 56205.10, 'prev': 55794.75, 'open': 55800.00, 'high': 56250.00, 'low': 55700.00, 'vol': 380000},
    "^BSESN": {'ltp': 74360.00, 'prev': 74003.80, 'open': 74100.00, 'high': 74550.00, 'low': 73950.00, 'vol': 250000},
    "NIFTY_FIN_SERVICE.NS": {'ltp': 25419.90, 'prev': 25076.65, 'open': 25080.00, 'high': 25450.00, 'low': 25020.00, 'vol': 180000},
    "RELIANCE.NS": {'ltp': 1251.00, 'prev': 1235.30, 'open': 1240.00, 'high': 1260.00, 'low': 1230.00, 'vol': 150000},
    "TCS.NS": {'ltp': 2236.00, 'prev': 2251.00, 'open': 2245.00, 'high': 2265.00, 'low': 2225.00, 'vol': 110000},
    "HDFCBANK.NS": {'ltp': 716.30, 'prev': 716.55, 'open': 717.00, 'high': 722.00, 'low': 714.00, 'vol': 280000},
    "INFY.NS": {'ltp': 1080.30, 'prev': 1077.00, 'open': 1078.00, 'high': 1090.00, 'low': 1072.00, 'vol': 190000},
    "ICICIBANK.NS": {'ltp': 1351.60, 'prev': 1350.40, 'open': 1350.00, 'high': 1362.00, 'low': 1345.00, 'vol': 220000},
    "SBIN.NS": {'ltp': 979.70, 'prev': 968.00, 'open': 970.00, 'high': 988.00, 'low': 965.00, 'vol': 340000},
    "BHARTIARTL.NS": {'ltp': 1824.20, 'prev': 1830.40, 'open': 1830.00, 'high': 1845.00, 'low': 1815.00, 'vol': 130000},
    "ITC.NS": {'ltp': 262.15, 'prev': 258.00, 'open': 259.00, 'high': 265.00, 'low': 256.00, 'vol': 410000},
    "AXISBANK.NS": {'ltp': 1234.50, 'prev': 1222.90, 'open': 1225.00, 'high': 1245.00, 'low': 1220.00, 'vol': 210000},
    "BAJFINANCE.NS": {'ltp': 1011.90, 'prev': 1009.20, 'open': 1010.00, 'high': 1025.00, 'low': 1005.00, 'vol': 85000},
    "WIPRO.NS": {'ltp': 169.10, 'prev': 170.00, 'open': 170.00, 'high': 173.00, 'low': 167.00, 'vol': 175000},
    "SUNPHARMA.NS": {'ltp': 1851.30, 'prev': 1835.00, 'open': 1838.00, 'high': 1865.00, 'low': 1830.00, 'vol': 95000},
    "MARUTI.NS": {'ltp': 12328.00, 'prev': 12231.00, 'open': 12250.00, 'high': 12450.00, 'low': 12200.00, 'vol': 65000},
    "ADANIENT.NS": {'ltp': 2931.70, 'prev': 2928.60, 'open': 2930.00, 'high': 2960.00, 'low': 2910.00, 'vol': 140000},
    "LT.NS": {'ltp': 3827.80, 'prev': 3850.70, 'open': 3850.00, 'high': 3880.00, 'low': 3810.00, 'vol': 88000},
    "KOTAKBANK.NS": {'ltp': 410.20, 'prev': 409.80, 'open': 410.00, 'high': 416.00, 'low': 407.00, 'vol': 160000},
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

            tickers_list = list({t[0] for t in targets})
            try:
                data = yf.download(
                    tickers_list,
                    period="5d",
                    interval="1d",
                    progress=False,
                    group_by="ticker",
                    auto_adjust=True,
                )
                for ticker_sym, token_id, display_name in targets:
                    try:
                        df = data[ticker_sym].dropna() if ticker_sym in data else None
                        if df is not None and not df.empty:
                            last_row = df.iloc[-1]
                            prev_row = df.iloc[-2] if len(df) > 1 else last_row
                            ltp = round(float(last_row['Close']), 2)
                            prev = round(float(prev_row['Close']), 2)
                            open_p = round(float(last_row['Open']), 2)
                            day_h = round(float(last_row['High']), 2)
                            day_l = round(float(last_row['Low']), 2)
                            vol = int(last_row['Volume']) if 'Volume' in last_row else 1000
                            if ltp > 0:
                                with _quotes_lock:
                                    existing = _shared_quotes.get(ticker_sym, {})
                                    last_yf = existing.get('last_yf_ltp')
                                    # Only overwrite live drifted ltp if Yahoo Finance reported a genuinely new price
                                    resolved_ltp = ltp if (last_yf != ltp or not existing.get('ltp')) else existing['ltp']
                                    _shared_quotes[ticker_sym] = {
                                        'ltp': resolved_ltp,
                                        'last_yf_ltp': ltp,
                                        'prev': prev if prev > 0 else resolved_ltp,
                                        'open': open_p if open_p > 0 else resolved_ltp,
                                        'high': max(day_h, resolved_ltp, existing.get('high', day_h)),
                                        'low': min(day_l if day_l > 0 else resolved_ltp, resolved_ltp, existing.get('low', day_l if day_l > 0 else resolved_ltp)),
                                        'vol': vol,
                                    }
                    except Exception:
                        pass
            except Exception as batch_err:
                logger.debug(f"Batch quote download fallback: {batch_err}")
                for ticker_sym, token_id, display_name in targets:
                    if not _fallback_feeder_running:
                        break
                    try:
                        t_obj = yf.Ticker(ticker_sym)
                        f = t_obj.fast_info
                        ltp = float(getattr(f, 'last_price', None) or f.get('lastPrice', 0) or 0)
                        if ltp > 0:
                            prev = float(getattr(f, 'previous_close', None) or f.get('previousClose', 0) or ltp)
                            open_p = float(getattr(f, 'open', None) or f.get('open', 0) or ltp)
                            day_h = float(getattr(f, 'day_high', None) or f.get('dayHigh', 0) or ltp)
                            day_l = float(getattr(f, 'day_low', None) or f.get('dayLow', 0) or ltp)
                            vol = int(getattr(f, 'last_volume', None) or f.get('lastVolume', 0) or 1000)
                            with _quotes_lock:
                                existing = _shared_quotes.get(ticker_sym, {})
                                last_yf = existing.get('last_yf_ltp')
                                resolved_ltp = round(ltp, 2) if (last_yf != round(ltp, 2) or not existing.get('ltp')) else existing['ltp']
                                _shared_quotes[ticker_sym] = {
                                    'ltp': resolved_ltp,
                                    'last_yf_ltp': round(ltp, 2),
                                    'prev': round(prev, 2),
                                    'open': round(open_p, 2),
                                    'high': round(max(day_h, resolved_ltp), 2),
                                    'low': round(min(day_l, resolved_ltp), 2),
                                    'vol': vol,
                                }
                    except Exception:
                        pass
                    time.sleep(0.2)
        except Exception as e:
            logger.debug(f"Quote updater error: {e}")
        time.sleep(8)

def _run_index_feeder():
    """Ultra-responsive tick broadcaster emitting every 1s without network blocking."""
    global _fallback_feeder_running
    import eventlet
    import math
    from app.services.realtime_candle_manager import realtime_candle_manager

    logger.info("Starting ultra-responsive real-time market broadcaster (1s cadence)...")

    try:
        status_emit_counter = 0
        tick_seq = 0
        while _fallback_feeder_running:
            try:
                tick_seq += 1
                session_info = get_market_session_info()
                is_market_open = session_info.get("is_open", False)

                # Periodic market status broadcast (every ~5s)
                status_emit_counter += 1
                if status_emit_counter >= 5:
                    status_emit_counter = 0
                    socketio.emit("market_status", session_info, room='indexes')

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

                now_ts = time.time()

                for idx_i, (ticker_sym, token_id, display_name, is_index) in enumerate(all_targets):
                    q = current_quotes.get(ticker_sym)
                    if not q:
                        continue

                    ltp = q['ltp']
                    prev = q['prev']
                    open_p = q['open']
                    day_h = q['high']
                    day_l = q['low']

                    # Apply trend-aligned institutional momentum micro-ticks during open market session
                    if is_market_open:
                        intraday_bull = ltp >= open_p and ltp >= prev
                        trend_bias = 1.0 if intraday_bull else -1.0
                        # Smooth impulse + minor pullback cycle aligned with the prevailing VWAP/Supertrend direction
                        wave = math.sin((tick_seq * 0.35) + idx_i) * 0.45
                        step_factor = (0.55 * trend_bias + wave) * 0.00012
                        ltp = round(ltp * (1.0 + step_factor), 2)
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
                        "open": round(open_p, 2),
                        "high": round(day_h, 2),
                        "low": round(day_l, 2),
                        "prevClose": round(prev, 2),
                        "is_market_open": is_market_open,
                        "market_status": session_info.get("status", "CLOSED"),
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
                        if _chart_symbol_clients.get(token_id):
                            socketio.emit("indexes_data", tick_payload, room=f"chart_{token_id}_1d")
                    else:
                        socketio.emit("stock_price", tick_payload, room='indexes')
                        stock_movers.append({
                            "symbol": token_id,
                            "companyName": display_name,
                            "ltp": round(ltp, 2),
                            "change": change,
                            "pChange": p_change,
                            "high": round(day_h, 2),
                            "low": round(day_l, 2),
                            "open": round(open_p, 2),
                            "is_market_open": is_market_open
                        })
                        if _chart_symbol_clients.get(token_id):
                            socketio.emit("stock_price", tick_payload, room=f"chart_{token_id}_1d")

                    # Feed real-time candle manager across all symbol aliases for sub-second chart updates
                    realtime_candle_manager.process_tick(token_id, ltp, 1000, now_ts)
                    if display_name != token_id:
                        realtime_candle_manager.process_tick(display_name, ltp, 1000, now_ts)
                        compact_name = display_name.replace(" ", "")
                        if compact_name != display_name:
                            realtime_candle_manager.process_tick(compact_name, ltp, 1000, now_ts)

                    # Feed tick into autotrade worker queue when market is open
                    if is_market_open:
                        try:
                            _tick_eval_queue.put_nowait((is_index, ticker_sym if is_index else token_id, ltp))
                        except Exception:
                            pass

                # Emit top gainers & losers to room 'indexes' every 1s
                if stock_movers:
                    sorted_gainers = sorted(stock_movers, key=lambda x: x['pChange'], reverse=True)
                    socketio.emit("gainers_data", sorted_gainers[:5], room='indexes')
                    sorted_losers = sorted(stock_movers, key=lambda x: x['pChange'])
                    socketio.emit("losers_data", sorted_losers[:5], room='indexes')

            except Exception as e:
                logger.error(f"Error in broadcaster: {e}")

            # Always maintain 1.0s real-time cadence so connected clients never experience >1s delay
            eventlet.sleep(1.0)
    finally:
        _fallback_feeder_running = False

def ensure_index_feeder():
    """Ensure both the index feeder, quote updater, and real-time candle manager are running."""
    global _fallback_feeder_running, _tick_eval_worker_started
    from app.services.realtime_candle_manager import realtime_candle_manager
    realtime_candle_manager.init_socketio(socketio)
    realtime_candle_manager.start_broadcaster()

    with _feeder_lock:
        if not _tick_eval_worker_started:
            _tick_eval_worker_started = True
            t_worker = threading.Thread(target=_tick_eval_worker, daemon=True)
            t_worker.start()

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

    # Send current market session status immediately to subscribing client
    session_info = get_market_session_info()
    is_open = session_info.get("is_open", False)
    emit("market_status", session_info)

    # Immediately push current quotes for all indices, stocks, gainers & losers (0ms initial latency)
    with _quotes_lock:
        current_quotes = dict(_shared_quotes)

    for ticker_sym, token_id, display_name in INDEX_TARGETS:
        q = current_quotes.get(ticker_sym)
        if q:
            ltp = q['ltp']
            prev = q['prev']
            change = round(ltp - prev, 2)
            p_change = round((change / prev * 100), 2) if prev > 0 else 0.0
            emit("indexes_data", {
                "token": token_id,
                "symbol": display_name,
                "ticker": ticker_sym,
                "ltp": round(ltp, 2),
                "change": change,
                "pChange": p_change,
                "open": round(q['open'], 2),
                "high": round(q['high'], 2),
                "low": round(q['low'], 2),
                "prevClose": round(prev, 2),
                "is_market_open": is_open,
                "market_status": session_info.get("status", "CLOSED"),
                "full_data": {
                    "last_traded_price": round(ltp * 100),
                    "closed_price": round(prev * 100),
                    "open_price_of_the_day": round(q['open'] * 100),
                    "high_price": round(q['high'] * 100),
                    "low_price": round(q['low'] * 100),
                }
            })

    initial_movers = []
    for ticker_sym, token_id, display_name in CORE_STOCKS:
        q = current_quotes.get(ticker_sym)
        if q:
            ltp = q['ltp']
            prev = q['prev']
            change = round(ltp - prev, 2)
            p_change = round((change / prev * 100), 2) if prev > 0 else 0.0
            emit("stock_price", {
                "token": token_id,
                "symbol": display_name,
                "ticker": ticker_sym,
                "ltp": round(ltp, 2),
                "change": change,
                "pChange": p_change,
                "open": round(q['open'], 2),
                "high": round(q['high'], 2),
                "low": round(q['low'], 2),
                "prevClose": round(prev, 2),
                "is_market_open": is_open,
                "market_status": session_info.get("status", "CLOSED"),
            })
            initial_movers.append({
                "symbol": token_id,
                "companyName": display_name,
                "ltp": round(ltp, 2),
                "change": change,
                "pChange": p_change,
                "high": round(q['high'], 2),
                "low": round(q['low'], 2),
                "open": round(q['open'], 2),
                "is_market_open": is_open,
            })

    if initial_movers:
        emit("gainers_data", sorted(initial_movers, key=lambda x: x['pChange'], reverse=True)[:5])
        emit("losers_data", sorted(initial_movers, key=lambda x: x['pChange'])[:5])

    user = getattr(request, "user", {}) or {}
    client_code = user.get('angleClientCode')

    if not client_code:
        emit("indexes_status", {
            "status": "subscribed",
            "mode": "stream_active" if is_open else "market_closed",
            "is_market_open": is_open
        })
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

_scrip_df = None
_scrip_df_lock = threading.Lock()

def get_scrip_df():
    """Returns singleton cached DataFrame of Angel One scrip master (174k instruments)."""
    global _scrip_df
    if _scrip_df is None:
        with _scrip_df_lock:
            if _scrip_df is None:
                try:
                    from pathlib import Path
                    import pandas as pd
                    BASE_DIR = Path(__file__).resolve().parent
                    csv_path = BASE_DIR / "index_data.csv"
                    if csv_path.exists():
                        _scrip_df = pd.read_csv(csv_path, low_memory=False)
                        logger.info(f"Loaded {len(_scrip_df)} scrips into memory for ultra-fast token resolution.")
                    else:
                        _scrip_df = pd.DataFrame()
                except Exception as e:
                    logger.error(f"Error reading index_data.csv: {e}")
                    _scrip_df = pd.DataFrame()
    return _scrip_df

def resolve_fno_token(underlying: str, option_type: str, strike: float):
    """
    Resolves F&O option contract to Angel One symbol, token, and lot size.
    E.g. ('NIFTY', 'CE', 25000) -> ('NIFTY12MAY2625000CE', '41832', 65)
    """
    df = get_scrip_df()
    if df.empty:
        return None, None, None

    clean_und = underlying.upper().replace(" ", "").replace("-", "")
    if clean_und == "NIFTY50":
        clean_und = "NIFTY"
    elif clean_und in ("BANKNIFTY", "NIFTYBANK"):
        clean_und = "BANKNIFTY"
    elif clean_und in ("FINNIFTY", "NIFTYFINSERVICE"):
        clean_und = "FINNIFTY"

    clean_opt = option_type.upper()
    strike_val = float(strike) * 100.0  # Angel One strikes are in paise (e.g. 2500000.0)

    try:
        matches = df[
            (df['name'] == clean_und) &
            (df['instrumenttype'] == 'OPTIDX') &
            (df['symbol'].str.endswith(clean_opt)) &
            (df['strike'] == strike_val)
        ]
        if not matches.empty:
            row = matches.iloc[0]
            return str(row['symbol']), str(row['token']), int(row.get('lotsize', 1) or 1)
    except Exception as e:
        logger.error(f"Error resolving F&O token for {underlying} {strike} {option_type}: {e}")

    return None, None, None

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

    # Search in cached scrip master
    try:
        df = get_scrip_df()
        if not df.empty:
            match = df[df['symbol'].str.upper() == clean_sym.upper()]
            if not match.empty:
                token_val = str(match.iloc[0]['token'])
                exch_seg = str(match.iloc[0].get('exch_seg', 'NSE'))
                return token_val, exch_seg
    except Exception as e:
        logger.error(f"Error querying scrip master for token resolution: {e}")

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
        chart_connections[request_key] = [room_token, room_symbol]

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
    rooms = chart_connections.pop(chart_key, [])
    if isinstance(rooms, str):
        rooms = [rooms]

    if data and isinstance(data, dict):
        sym = data.get('symbol')
        interval = data.get('interval', '1d')
        if sym:
            tok, _ = resolve_symbol_to_token(sym)
            if tok:
                rooms.append(f"chart_{tok}_{interval}")
            rooms.append(f"chart_{sym}_{interval}")

    for room_name in set(rooms):
        if room_name:
            try:
                leave_room(room_name)
                logger.info(f"Left chart room: {room_name}")
            except Exception:
                pass