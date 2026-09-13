from flask import Blueprint, request, jsonify
import yfinance as yf
from app.utils.logger import get_logger

logger = get_logger(__name__)

chart_bp = Blueprint('chart', __name__)


@chart_bp.route('/chart-data', methods=['GET'])
def get_chart_data():
    """
    Get OHLCV candlestick data for a given symbol and interval.
    
    Query params:
        symbol: Stock symbol (e.g., 'RELIANCE.NS', '^NSEI', '^NSEBANK')
        interval: Candle interval - 1m, 5m, 15m, 30m, 1h, 1d, 1wk, 1mo
        period: Data period - 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, max
    """
    symbol = request.args.get('symbol', '^NSEI')
    interval = request.args.get('interval', '1d')
    period = request.args.get('period', '1mo')

    logger.info(f"Fetching chart data: symbol={symbol}, interval={interval}, period={period}")

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)

        if df.empty:
            return jsonify({'success': False, 'error': 'No data returned'}), 404

        candles = []
        for idx, row in df.iterrows():
            candles.append({
                'time': int(idx.timestamp() * 1000),  # milliseconds
                'open': round(float(row['Open']), 2),
                'high': round(float(row['High']), 2),
                'low': round(float(row['Low']), 2),
                'close': round(float(row['Close']), 2),
                'volume': int(row['Volume']),
            })

        # Get current price info
        fast = ticker.fast_info
        current_price = fast.get('lastPrice', candles[-1]['close'] if candles else 0)
        prev_close = fast.get('previousClose', candles[-1]['open'] if candles else 0)

        return jsonify({
            'success': True,
            'data': {
                'symbol': symbol,
                'candles': candles,
                'currentPrice': round(float(current_price), 2),
                'previousClose': round(float(prev_close), 2),
            }
        })

    except Exception as e:
        logger.error(f"Error fetching chart data: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
