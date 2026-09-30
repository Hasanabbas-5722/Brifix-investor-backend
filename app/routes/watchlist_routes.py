"""
Watchlist Routes (Real NSE Edition)
===================================
Provides live NSE quotes, symbol management, and multi-watchlist support.
GET  /api/v1/watchlist           -> Returns watchlist with real NSE prices, change %, day high/low
POST /api/v1/watchlist/add       -> Add a stock to user's watchlist
POST /api/v1/watchlist/remove    -> Remove a stock from watchlist
GET  /api/v1/watchlist/search    -> Search stocks / indices to add
"""

import time
from flask import Blueprint, request, jsonify
from app.utils.logger import get_logger
from app.extensions import connect_to_mongodb
from app.services.nse_market_service import nse_market_service

logger = get_logger(__name__)

watchlist_bp = Blueprint("watchlist", __name__, url_prefix="/api/v1")

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

_QUOTE_CACHE = {}
CACHE_TTL = 5.0


def _resolve_ticker(sym: str) -> str:
    s = sym.strip().upper()
    if s in SYMBOL_MAP:
        return SYMBOL_MAP[s]
    if not s.endswith('.NS') and not s.endswith('.BO') and not s.startswith('^'):
        return f"{s}.NS"
    return s


def _build_sparkline(prev_p: float, open_p: float, low_p: float, high_p: float, ltp: float) -> list:
    if ltp <= 0:
        return []
    p = prev_p if prev_p > 0 else ltp
    o = open_p if open_p > 0 else p
    l = low_p if low_p > 0 else min(o, ltp)
    h = high_p if high_p > 0 else max(o, ltp)
    mid1 = round((o + l) / 2.0, 2) if ltp >= o else round((o + h) / 2.0, 2)
    mid2 = round((l + h) / 2.0, 2)
    mid3 = round((h + ltp) / 2.0, 2) if ltp >= o else round((l + ltp) / 2.0, 2)
    return [round(p, 2), round(o, 2), mid1, round(l if ltp >= o else h, 2), mid2, mid3, round(ltp, 2)]


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
        indices_map = {}
        stocks_map = {}
        try:
            indices_map = nse_market_service.fetch_live_indices() or {}
        except Exception as e:
            logger.debug(f"Watchlist indices fetch error: {e}")

        try:
            stocks_map, _, _ = nse_market_service.fetch_live_stocks_and_movers(required_symbols=to_fetch)
            stocks_map = stocks_map or {}
        except Exception as e:
            logger.debug(f"Watchlist stocks fetch error: {e}")

        from app.socket.indexes import _shared_quotes

        for sym in to_fetch:
            ytick = _resolve_ticker(sym)
            q = (
                indices_map.get(sym)
                or indices_map.get(ytick)
                or stocks_map.get(sym)
                or stocks_map.get(ytick)
                or _shared_quotes.get(ytick)
                or _shared_quotes.get(sym)
            )
            if not q or float(q.get("ltp") or 0) <= 0:
                q = nse_market_service.fetch_single_equity_quote(sym)

            if q and float(q.get("ltp") or 0) > 0:
                ltp = round(float(q.get("ltp") or 0.0), 2)
                prev = round(float(q.get("prev") or ltp), 2)
                chg = round(float(q.get("change") if q.get("change") is not None else (ltp - prev)), 2)
                chg_pct = round(
                    float(q.get("changePercent") if q.get("changePercent") is not None else ((chg / prev * 100.0) if prev > 0 else 0.0)),
                    2
                )
                high_p = round(float(q.get("high") or ltp), 2)
                low_p = round(float(q.get("low") or ltp), 2)
                open_p = round(float(q.get("open") or prev), 2)
                vol = int(q.get("vol") or 0)

                item = {
                    "symbol": sym,
                    "ticker": ytick,
                    "name": COMPANY_NAMES.get(sym) or q.get("name") or sym,
                    "price": ltp,
                    "change": chg,
                    "change_pct": chg_pct,
                    "high": high_p,
                    "low": low_p,
                    "open": open_p,
                    "volume": vol,
                    "sparkline": _build_sparkline(prev, open_p, low_p, high_p, ltp),
                    "is_index": sym.startswith('^') or 'NIFTY' in sym or 'SENSEX' in sym,
                    "source": "NSE_LIVE",
                    "updated_at": int(now)
                }
                _QUOTE_CACHE[sym] = {'time': now, 'data': item}
                results.append(item)

    order_dict = {s.upper(): idx for idx, s in enumerate(symbols)}
    results.sort(key=lambda x: order_dict.get(x["symbol"], 999))
    return results


@watchlist_bp.route("/watchlist", methods=["GET"])
@watchlist_bp.route("/watchlist/list", methods=["GET"])
def get_watchlist():
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

    single_quote = _fetch_quotes([symbol])
    return jsonify({
        "status": "success",
        "message": f"Added {symbol} to watchlist",
        "item": single_quote[0] if single_quote else None
    })


@watchlist_bp.route("/watchlist/remove", methods=["POST"])
def remove_from_watchlist():
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
