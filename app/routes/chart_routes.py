from flask import Blueprint, request, jsonify
import yfinance as yf
from app.utils.logger import get_logger
import time
import threading

logger = get_logger(__name__)

chart_bp = Blueprint('chart', __name__)

# ─────────────────────────────────────────────────────────────
# Symbol Normalization Dictionary
# Maps user-facing / frontend tickers to valid Yahoo Finance tickers
# ─────────────────────────────────────────────────────────────
SYMBOL_MAP = {
    # Indices
    'NIFTY 50': '^NSEI',
    'NIFTY50': '^NSEI',
    '^NSEI': '^NSEI',
    'BANK NIFTY': '^NSEBANK',
    'BANKNIFTY': '^NSEBANK',
    '^NSEBANK': '^NSEBANK',
    'SENSEX': '^BSESN',
    '^BSESN': '^BSESN',
    'FIN NIFTY': 'NIFTY_FIN_SERVICE.NS',
    'FINNIFTY': 'NIFTY_FIN_SERVICE.NS',
    'NIFTY_FIN_SERVICE.NS': 'NIFTY_FIN_SERVICE.NS',
    'MIDCPNIFTY': '^CRSLMID',
    'NIFTY IT': '^CNXIT',
    'NIFTY AUTO': '^CNXAUTO',
    # Key Equities
    'RELIANCE': 'RELIANCE.NS',
    'TCS': 'TCS.NS',
    'HDFC BANK': 'HDFCBANK.NS',
    'HDFCBANK': 'HDFCBANK.NS',
    'INFOSYS': 'INFY.NS',
    'INFY': 'INFY.NS',
    'ICICI BANK': 'ICICIBANK.NS',
    'ICICIBANK': 'ICICIBANK.NS',
    'SBI': 'SBIN.NS',
    'SBIN': 'SBIN.NS',
    'BHARTI AIRTEL': 'BHARTIARTL.NS',
    'BHARTIARTL': 'BHARTIARTL.NS',
    'ITC': 'ITC.NS',
    'TATA MOTORS': 'TMCV.NS',
    'TATAMOTORS': 'TMCV.NS',
    'TATAMOTORS.NS': 'TMCV.NS',
    'TMCV': 'TMCV.NS',
    'TMPV': 'TMPV.NS',
    'BAJAJ FINANCE': 'BAJFINANCE.NS',
    'BAJFINANCE': 'BAJFINANCE.NS',
    'MARUTI': 'MARUTI.NS',
    'WIPRO': 'WIPRO.NS',
    'SUN PHARMA': 'SUNPHARMA.NS',
    'SUNPHARMA': 'SUNPHARMA.NS',
    'ADANI ENT.': 'ADANIENT.NS',
    'ADANIENT': 'ADANIENT.NS',
    'TATA STEEL': 'TATASTEEL.NS',
    'HINDUNILVR': 'HINDUNILVR.NS',
    'HINDUSTAN UNILEVER': 'HINDUNILVR.NS',
    'L&T': 'LT.NS',
    'LT': 'LT.NS',
    'TATASTEEL': 'TATASTEEL.NS',
    'LT': 'LT.NS',
    'KOTAKBANK': 'KOTAKBANK.NS',
    'AXISBANK': 'AXISBANK.NS',
}

def resolve_ticker(symbol: str) -> str:
    """Normalize input symbol to Yahoo Finance ticker string."""
    clean = symbol.strip()
    clean_upper = clean.upper()
    if clean_upper in SYMBOL_MAP:
        return SYMBOL_MAP[clean_upper]
    if clean in SYMBOL_MAP:
        return SYMBOL_MAP[clean]
    if clean.startswith('^') or clean.endswith('.NS') or clean.endswith('.BO'):
        return clean
    return f"{clean_upper}.NS"

# ─────────────────────────────────────────────────────────────
# Allowed interval & period constraints in Yahoo Finance:
# 1m: max 7 days
# 2m, 5m, 15m, 30m: max 60 days
# 1h: max 730 days
# 1d, 5d, 1wk, 1mo, 3mo: max any
# ─────────────────────────────────────────────────────────────
INTERVAL_PERIOD_DEFAULTS = {
    '1m': '5d',
    '5m': '5d',
    '15m': '1mo',
    '30m': '1mo',
    '1h': '1mo',
    '1d': '1mo',
    '1wk': '1y',
    '1mo': '2y',
}

def normalize_interval_period(interval: str, period: str) -> tuple[str, str]:
    interval = interval.lower() if interval else '1d'
    if interval not in INTERVAL_PERIOD_DEFAULTS:
        interval = '1d'

    if not period or period == '1d' and interval in ['1m', '5m']:
        period = INTERVAL_PERIOD_DEFAULTS[interval]
    else:
        period = period.lower()

    if interval == '1m':
        period = '5d'
    elif interval in ['5m', '15m', '30m'] and period not in ['5d', '1mo']:
        period = '1mo'
    elif interval == '1h' and period not in ['5d', '1mo', '3mo']:
        period = '1mo'

    return interval, period

# ─────────────────────────────────────────────────────────────
# In-Memory High Speed TTL Cache
# ─────────────────────────────────────────────────────────────
_CACHE = {}
_CACHE_LOCK = threading.Lock()
HIST_CACHE_TTL = 30   # 30 seconds for historical candles
QUOTE_CACHE_TTL = 3   # 3 seconds for current price

def get_cached(key: str, ttl: int):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry:
            val, exp = entry
            if time.time() < exp:
                return val
            del _CACHE[key]
    return None

def set_cached(key: str, val, ttl: int):
    with _CACHE_LOCK:
        _CACHE[key] = (val, time.time() + ttl)


@chart_bp.route('/chart-data', methods=['GET'])
@chart_bp.route('/api/v1/chart-data', methods=['GET'])
def get_chart_data():
    """
    Get OHLCV candlestick data for a given symbol and interval.
    Query params:
        symbol: Stock symbol (e.g., 'NIFTY 50', 'NIFTY50', '^NSEI', 'RELIANCE', 'TCS')
        interval: 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo
        period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, max
    """
    raw_symbol = request.args.get('symbol', '^NSEI')
    raw_interval = request.args.get('interval', '1d')
    raw_period = request.args.get('period', '')

    ticker_sym = resolve_ticker(raw_symbol)
    interval, period = normalize_interval_period(raw_interval, raw_period)

    cache_key = f"chart_{ticker_sym}_{interval}_{period}"
    cached_res = get_cached(cache_key, HIST_CACHE_TTL)
    if cached_res:
        return jsonify(cached_res)

    logger.info(f"Fetching live chart data: {raw_symbol} -> {ticker_sym} (interval={interval}, period={period})")

    try:
        ticker = yf.Ticker(ticker_sym)
        df = ticker.history(period=period, interval=interval)

        if df.empty:
            logger.warning(f"Empty data for {ticker_sym} at {period}, attempting fallback")
            df = ticker.history(period='1mo', interval='1d' if interval not in ['1m', '5m', '15m'] else interval)

        if df.empty:
            set_cached(cache_key, {'success': False, 'error': f'No market data returned for symbol {raw_symbol}'}, HIST_CACHE_TTL)
            return jsonify({
                'success': False,
                'error': f'No market data returned for symbol {raw_symbol}'
            }), 404

        candles = []
        for idx, row in df.iterrows():
            utc_ts = int(idx.timestamp())
            # TradingView Lightweight Charts formats horizontal axis using UTC methods.
            # Convert timestamp to exchange market time (IST = UTC+05:30, +19800 seconds)
            # so the chart displays genuine Indian stock market hours (09:15 to 15:30).
            tz_offset = 19800
            if hasattr(idx, 'utcoffset') and idx.utcoffset() is not None:
                tz_offset = int(idx.utcoffset().total_seconds())

            ts_seconds = utc_ts + tz_offset
            candles.append({
                'time': ts_seconds,
                'open': round(float(row['Open']), 2),
                'high': round(float(row['High']), 2),
                'low': round(float(row['Low']), 2),
                'close': round(float(row['Close']), 2),
                'volume': int(row.get('Volume', 0)),
            })

        quote_key = f"quote_{ticker_sym}"
        fast_quote = get_cached(quote_key, QUOTE_CACHE_TTL)
        if not fast_quote:
            try:
                fast = ticker.fast_info
                current_price = fast.get('lastPrice') or (candles[-1]['close'] if candles else 0)
                prev_close = fast.get('previousClose') or (candles[-2]['close'] if len(candles) > 1 else candles[-1]['open'])
                day_high = fast.get('dayHigh') or (candles[-1]['high'] if candles else current_price)
                day_low = fast.get('dayLow') or (candles[-1]['low'] if candles else current_price)
                fast_quote = {
                    'currentPrice': round(float(current_price), 2),
                    'previousClose': round(float(prev_close), 2),
                    'dayHigh': round(float(day_high), 2),
                    'dayLow': round(float(day_low), 2),
                }
                set_cached(quote_key, fast_quote, QUOTE_CACHE_TTL)
            except Exception as q_err:
                logger.warning(f"Error fetching fast_info for {ticker_sym}: {q_err}")
                current_price = candles[-1]['close'] if candles else 0
                prev_close = candles[-2]['close'] if len(candles) > 1 else candles[-1]['open']
                fast_quote = {
                    'currentPrice': round(float(current_price), 2),
                    'previousClose': round(float(prev_close), 2),
                    'dayHigh': round(float(candles[-1]['high']), 2),
                    'dayLow': round(float(candles[-1]['low']), 2),
                }

        if candles and fast_quote['currentPrice'] > 0:
            last_candle = candles[-1]
            last_candle['close'] = fast_quote['currentPrice']
            last_candle['high'] = max(last_candle['high'], fast_quote['currentPrice'])
            last_candle['low'] = min(last_candle['low'], fast_quote['currentPrice'])

        response_data = {
            'success': True,
            'data': {
                'symbol': raw_symbol,
                'ticker': ticker_sym,
                'interval': interval,
                'period': period,
                'candles': candles,
                'currentPrice': fast_quote['currentPrice'],
                'previousClose': fast_quote['previousClose'],
                'dayHigh': fast_quote['dayHigh'],
                'dayLow': fast_quote['dayLow'],
                'change': round(fast_quote['currentPrice'] - fast_quote['previousClose'], 2),
                'changePercent': round(
                    ((fast_quote['currentPrice'] - fast_quote['previousClose']) / fast_quote['previousClose'] * 100)
                    if fast_quote['previousClose'] > 0 else 0,
                    2
                ),
            }
        }

        set_cached(cache_key, response_data, HIST_CACHE_TTL)
        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Error fetching chart data for {raw_symbol}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
