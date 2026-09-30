from flask import Blueprint, request, jsonify
from app.utils.logger import get_logger
from app.services.nse_market_service import nse_market_service
import time
import threading

logger = get_logger(__name__)

chart_bp = Blueprint('chart', __name__)

SYMBOL_MAP = {
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
    'KOTAKBANK': 'KOTAKBANK.NS',
    'AXISBANK': 'AXISBANK.NS',
}


def resolve_ticker(symbol: str) -> str:
    clean = symbol.strip()
    clean_upper = clean.upper()
    if clean_upper in SYMBOL_MAP:
        return SYMBOL_MAP[clean_upper]
    if clean in SYMBOL_MAP:
        return SYMBOL_MAP[clean]
    if clean.startswith('^') or clean.endswith('.NS') or clean.endswith('.BO'):
        return clean
    return f"{clean_upper}.NS"


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

    if not period or (period == '1d' and interval in ['1m', '5m']):
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


_CACHE = {}
_CACHE_LOCK = threading.Lock()
HIST_CACHE_TTL = 10   # 10 seconds for live NSE chart candles


def get_cached(key: str):
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
    Get OHLCV candlestick data for a given symbol and interval backed by Real NSE India APIs.
    """
    raw_symbol = request.args.get('symbol', '^NSEI')
    raw_interval = request.args.get('interval', '5m')
    raw_period = request.args.get('period', '')

    ticker_sym = resolve_ticker(raw_symbol)
    interval, period = normalize_interval_period(raw_interval, raw_period)

    cache_key = f"chart_{ticker_sym}_{interval}_{period}"
    cached_res = get_cached(cache_key)
    if cached_res:
        return jsonify(cached_res)

    try:
        chart_res = nse_market_service.get_chart_candles(raw_symbol, interval=interval, period=period)
        candles = (chart_res or {}).get("candles") or []
        if not candles:
            return jsonify({
                'success': False,
                'error': f'No live NSE market data returned for symbol {raw_symbol}'
            }), 404

        try:
            from app.services.realtime_candle_manager import realtime_candle_manager
            from app.socket.indexes import resolve_symbol_to_token
            realtime_candle_manager.get_aggregator(raw_symbol, interval).prime_history(candles)
            if ticker_sym != raw_symbol:
                realtime_candle_manager.get_aggregator(ticker_sym, interval).prime_history(candles)
            token_id, _ = resolve_symbol_to_token(raw_symbol)
            if token_id and token_id not in (raw_symbol, ticker_sym):
                realtime_candle_manager.get_aggregator(token_id, interval).prime_history(candles)
        except Exception as seed_err:
            logger.debug(f"Could not prime candle manager: {seed_err}")

        response_data = {
            'success': True,
            'data': {
                'symbol': raw_symbol,
                'ticker': ticker_sym,
                'interval': interval,
                'period': period,
                'candles': candles,
                'currentPrice': chart_res['currentPrice'],
                'previousClose': chart_res['previousClose'],
                'dayHigh': chart_res['dayHigh'],
                'dayLow': chart_res['dayLow'],
                'change': chart_res['change'],
                'changePercent': chart_res['changePercent'],
                'source': 'NSE_LIVE',
            }
        }

        set_cached(cache_key, response_data, HIST_CACHE_TTL)
        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Error fetching NSE chart data for {raw_symbol}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
