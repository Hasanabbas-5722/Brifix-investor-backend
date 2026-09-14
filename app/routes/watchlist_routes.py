"""
Watchlist Routes
================
Provides live quotes, symbol management, and multi-watchlist support.
GET  /api/v1/watchlist           -> Returns watchlist with live prices, change %, day high/low
POST /api/v1/watchlist/add       -> Add a stock to user's watchlist
POST /api/v1/watchlist/remove    -> Remove a stock from watchlist
GET  /api/v1/watchlist/search    -> Search stocks / indices to add
"""

import time
from flask import Blueprint, request, jsonify
import yfinance as yf
from app.utils.logger import get_logger
from app.extensions import connect_to_mongodb

logger = get_logger(__name__)

watchlist_bp = Blueprint("watchlist", __name__, url_prefix="/api/v1")

# Symbol to Yahoo ticker map
SYMBOL_MAP = {
    'NIFTY 50': '^NSEI',
    'NIFTY50': '^NSEI',
    '^NSEI': '^NSEI',
    'BANK NIFTY': '^NSEBANK',
    'BANKNIFTY': '^NSEBANK',
    'SENSEX': '^BSESN',
    'FIN NIFTY': 'NIFTY_FIN_SERVICE.NS',
    'RELIANCE': 'RELIANCE.NS',
    'TCS': 'TCS.NS',
    'HDFCBANK': 'HDFCBANK.NS',
    'INFY': 'INFY.NS',
    'ICICIBANK': 'ICICIBANK.NS',
    'SBIN': 'SBIN.NS',
    'BHARTIARTL': 'BHARTIARTL.NS',
    'ITC': 'ITC.NS',
    'TATAMOTORS': 'TMCV.NS',
    'TMCV': 'TMCV.NS',
    'BAJFINANCE': 'BAJFINANCE.NS',
    'MARUTI': 'MARUTI.NS',
    'WIPRO': 'WIPRO.NS',
    'SUNPHARMA': 'SUNPHARMA.NS',
    'ADANIENT': 'ADANIENT.NS',
    'TATASTEEL': 'TATASTEEL.NS',
    'HINDUNILVR': 'HINDUNILVR.NS',
    'KOTAKBANK': 'KOTAKBANK.NS',
    'AXISBANK': 'AXISBANK.NS',
    'LT': 'LT.NS',
    'TITAN': 'TITAN.NS',
    'ASIANPAINT': 'ASIANPAINT.NS',
}

COMPANY_NAMES = {
    'NIFTY 50': 'Nifty 50 Index',
    'BANK NIFTY': 'Nifty Bank Index',
    'SENSEX': 'BSE Sensex 30',
    'FIN NIFTY': 'Nifty Financial Services',
    'RELIANCE': 'Reliance Industries Ltd',
    'TCS': 'Tata Consultancy Services',
    'HDFCBANK': 'HDFC Bank Ltd',
    'INFY': 'Infosys Ltd',
    'ICICIBANK': 'ICICI Bank Ltd',
    'SBIN': 'State Bank of India',
    'BHARTIARTL': 'Bharti Airtel Ltd',
    'ITC': 'ITC Limited',
    'TATAMOTORS': 'Tata Motors CV',
    'BAJFINANCE': 'Bajaj Finance Ltd',
    'MARUTI': 'Maruti Suzuki India Ltd',
    'WIPRO': 'Wipro Ltd',
    'SUNPHARMA': 'Sun Pharmaceutical Industries',
    'ADANIENT': 'Adani Enterprises Ltd',
    'TATASTEEL': 'Tata Steel Ltd',
    'HINDUNILVR': 'Hindustan Unilever Ltd',
    'KOTAKBANK': 'Kotak Mahindra Bank Ltd',
    'AXISBANK': 'Axis Bank Ltd',
    'LT': 'Larsen & Toubro Ltd',
    'TITAN': 'Titan Company Ltd',
    'ASIANPAINT': 'Asian Paints Ltd',
}

DEFAULT_STOCKS = [
    'NIFTY 50', 'RELIANCE', 'TCS', 'HDFCBANK',
    'ICICIBANK', 'INFY', 'BHARTIARTL', 'TATAMOTORS'
]

# Cache quotes for 5 seconds to provide blazing fast response
_QUOTE_CACHE = {}
CACHE_TTL = 5.0


def _resolve_ticker(sym: str) -> str:
    s = sym.strip().upper()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if not s.endswith('.NS') and not s.endswith('.BO') and not s.startswith('^'):
        return f"{s}.NS"
    return s


def _fetch_quotes(symbols: list) -> list:
    now = time.time()
    results = []
    to_fetch = []

    for s in symbols:
        clean = s.strip().upper()
        cached = _QUOTE_CACHE.get(clean)
        if cached and (now - cached['time'] < CACHE_TTL):
            results.append(cached['data'])
        else:
            to_fetch.append(clean)

    if to_fetch:
        ticker_map = {s: _resolve_ticker(s) for s in to_fetch}
        tickers_list = list(set(ticker_map.values()))

        try:
            # Batch download 5-day data for quotes and sparkline
            data = yf.download(
                tickers_list,
                period="5d",
                interval="1d",
                progress=False,
                group_by="ticker",
                auto_adjust=True
            )

            for sym in to_fetch:
                ytick = ticker_map[sym]
                try:
                    df = data[ytick].dropna() if ytick in data else None
                    if df is not None and not df.empty:
                        last_row = df.iloc[-1]
                        prev_row = df.iloc[-2] if len(df) > 1 else last_row

                        close = round(float(last_row['Close']), 2)
                        prev_close = round(float(prev_row['Close']), 2)
                        chg = round(close - prev_close, 2)
                        chg_pct = round((chg / prev_close) * 100, 2) if prev_close else 0.0

                        sparkline = [round(float(c), 2) for c in df['Close'].tail(7).tolist()]

                        item = {
                            "symbol": sym,
                            "ticker": ytick,
                            "name": COMPANY_NAMES.get(sym, sym),
                            "price": close,
                            "change": chg,
                            "change_pct": chg_pct,
                            "high": round(float(last_row['High']), 2),
                            "low": round(float(last_row['Low']), 2),
                            "open": round(float(last_row['Open']), 2),
                            "volume": int(last_row['Volume']) if 'Volume' in last_row else 0,
                            "sparkline": sparkline,
                            "is_index": sym.startswith('^') or 'NIFTY' in sym or 'SENSEX' in sym,
                            "updated_at": int(now)
                        }
                        _QUOTE_CACHE[sym] = {'time': now, 'data': item}
                        results.append(item)
                        continue
                except Exception as e:
                    logger.warning(f"Error parsing quote for {sym}: {e}")

                # Fallback item if symbol fetch fails
                fallback = {
                    "symbol": sym,
                    "ticker": ytick,
                    "name": COMPANY_NAMES.get(sym, sym),
                    "price": 0.0,
                    "change": 0.0,
                    "change_pct": 0.0,
                    "high": 0.0,
                    "low": 0.0,
                    "open": 0.0,
                    "volume": 0,
                    "sparkline": [],
                    "is_index": False,
                    "updated_at": int(now)
                }
                results.append(fallback)
        except Exception as e:
            logger.error(f"Batch quote download failed: {e}")

    # Maintain original symbol order
    order_dict = {s.upper(): idx for idx, s in enumerate(symbols)}
    results.sort(key=lambda x: order_dict.get(x["symbol"], 999))
    return results


@watchlist_bp.route("/watchlist", methods=["GET"])
@watchlist_bp.route("/watchlist/list", methods=["GET"])
def get_watchlist():
    """
    Returns user's watchlist stocks with live market quotes and sparkline trends.
    Query params:
        email (str, optional)
        tab   (str, optional) e.g. 'Main', 'Tech'
    """
    email = request.args.get("email")
    tab = request.args.get("tab", "Main").strip()

    symbols = list(DEFAULT_STOCKS)

    if email:
        try:
            db = connect_to_mongodb()
            if db is not None:
                doc = db.watchlists.find_one({"email": email, "tab": tab})
                if doc and "symbols" in doc and doc["symbols"]:
                    symbols = doc["symbols"]
        except Exception as e:
            logger.warning(f"Failed to read watchlist from DB: {e}")

    quotes = _fetch_quotes(symbols)
    return jsonify({
        "status": "success",
        "tab": tab,
        "count": len(quotes),
        "data": quotes
    })


@watchlist_bp.route("/watchlist/add", methods=["POST"])
def add_to_watchlist():
    """
    Adds a stock to user's watchlist.
    JSON Body:
        symbol (str, required)
        email  (str, optional)
        tab    (str, optional)
    """
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "").strip().upper()
    email = data.get("email", "").strip()
    tab = data.get("tab", "Main").strip()

    if not symbol:
        return jsonify({"status": "failed", "error": "Symbol is required"}), 400

    if email:
        try:
            db = connect_to_mongodb()
            if db is not None:
                db.watchlists.update_one(
                    {"email": email, "tab": tab},
                    {"$addToSet": {"symbols": symbol}},
                    upsert=True
                )
        except Exception as e:
            logger.error(f"Error adding to watchlist DB: {e}")

    # Fetch live quote for the added symbol
    single_quote = _fetch_quotes([symbol])
    return jsonify({
        "status": "success",
        "message": f"Added {symbol} to watchlist",
        "item": single_quote[0] if single_quote else None
    })


@watchlist_bp.route("/watchlist/remove", methods=["POST"])
def remove_from_watchlist():
    """
    Removes a stock from user's watchlist.
    JSON Body:
        symbol (str, required)
        email  (str, optional)
        tab    (str, optional)
    """
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol", "").strip().upper()
    email = data.get("email", "").strip()
    tab = data.get("tab", "Main").strip()

    if not symbol:
        return jsonify({"status": "failed", "error": "Symbol is required"}), 400

    if email:
        try:
            db = connect_to_mongodb()
            if db is not None:
                db.watchlists.update_one(
                    {"email": email, "tab": tab},
                    {"$pull": {"symbols": symbol}}
                )
        except Exception as e:
            logger.error(f"Error removing from watchlist DB: {e}")

    return jsonify({
        "status": "success",
        "message": f"Removed {symbol} from watchlist"
    })


@watchlist_bp.route("/watchlist/search", methods=["GET"])
def search_symbols():
    """
    Searches available stocks and indices to add to watchlist.
    Query param: q (str)
    """
    q = request.args.get("q", "").strip().upper()
    all_symbols = list(COMPANY_NAMES.keys())

    if q:
        filtered = [
            {"symbol": s, "name": COMPANY_NAMES[s]}
            for s in all_symbols
            if q in s or q in COMPANY_NAMES[s].upper()
        ]
    else:
        filtered = [{"symbol": s, "name": COMPANY_NAMES[s]} for s in all_symbols[:15]]

    return jsonify({
        "status": "success",
        "results": filtered
    })
