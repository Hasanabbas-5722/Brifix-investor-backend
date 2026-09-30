import time
from urllib.parse import urlparse
from app.utils.logger import get_logger
from app.services.nse_market_service import nse_market_service

logger = get_logger(__name__)

COMMON_DOMAINS = {
    "RELIANCE": "ril.com",
    "TCS": "tcs.com",
    "HDFCBANK": "hdfcbank.com",
    "ICICIBANK": "icicibank.com",
    "INFY": "infosys.com",
    "SBIN": "sbi.co.in",
    "BHARTIARTL": "airtel.in",
    "ITC": "itcportal.com",
    "KOTAKBANK": "kotak.com",
    "LT": "larsentoubro.com",
    "AXISBANK": "axisbank.com",
    "BAJFINANCE": "bajajfinserv.in",
    "MARUTI": "marutisuzuki.com",
    "TATAMOTORS": "tatamotors.com",
    "SUNPHARMA": "sunpharma.com",
    "WIPRO": "wipro.com",
    "HCLTECH": "hcltech.com",
    "ADANIENT": "adanienterprises.com",
    "ADANIPORTS": "adaniports.com",
    "NTPC": "ntpc.co.in",
    "POWERGRID": "powergrid.in",
    "TITAN": "titancompany.in",
    "NESTLEIND": "nestle.in",
    "TATASTEEL": "tatasteel.com",
    "ONGC": "ongcindia.com",
    "JSWSTEEL": "jsw.in",
    "M&M": "mahindra.com",
    "COALINDIA": "coalindia.in",
    "BAJAJFINSV": "bajajfinserv.in",
    "TECHM": "techmahindra.com",
    "HINDALCO": "hindalco.com",
    "INDUSINDBK": "indusind.com",
    "DRREDDY": "drreddys.com",
    "DIVISLAB": "divislabs.com",
    "CIPLA": "cipla.com",
    "EICHERMOT": "eicher.in",
    "GRASIM": "grasim.com",
    "APOLLOHOSP": "apollohospitals.com",
    "BPCL": "bharatpetroleum.in",
    "HEROMOTOCO": "heromotocorp.com",
    "TATACONSUM": "tataconsumer.com",
    "SBILIFE": "sbilife.co.in",
    "BRITANNIA": "britannia.co.in",
    "BAJAJ-AUTO": "bajajauto.com",
    "HDFCLIFE": "hdfclife.com",
    "BANKBARODA": "bankofbaroda.in",
    "PNB": "pnbindia.in",
    "FEDERALBNK": "federalbank.co.in",
    "IDFCFIRSTB": "idfcfirstbank.com",
    "BANDHANBNK": "bandhanbank.com",
    "AUBANK": "aubank.in",
}

BANK_SYMBOLS = {
    "HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK",
    "INDUSINDBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB",
    "BANDHANBNK", "AUBANK", "CANBK", "UNIONBANK", "INDIANB", "RBLBANK",
}


def get_stock_logo(symbol, website=""):
    """Instant logo lookup without external scraping overhead."""
    clean_sym = symbol.replace(".NS", "").replace(".BO", "").strip().upper()
    domain = COMMON_DOMAINS.get(clean_sym)
    if not domain and website:
        try:
            domain = urlparse(website).netloc
        except Exception:
            domain = ""
    if domain:
        return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
    return f"https://ui-avatars.com/api/?name={clean_sym[:4]}&background=4f46e5&color=ffffff&bold=true&size=128"


def _decorate_mover(item: dict) -> dict:
    sym = str(item.get("symbol") or item.get("stockSymbol") or "").strip().upper()
    logo = get_stock_logo(sym)
    out = dict(item)
    out["symbol"] = sym
    out["stockSymbol"] = sym
    out["companyName"] = item.get("companyName") or sym
    out["ltp"] = round(float(item.get("ltp") or item.get("lastPrice") or 0.0), 2)
    out["lastPrice"] = out["ltp"]
    out["pChange"] = round(float(item.get("pChange") or item.get("perChange") or 0.0), 2)
    out["previousClose"] = round(float(item.get("previousClose") or item.get("prev_price") or out["ltp"]), 2)
    out["logo"] = logo
    out["stockinfo"] = {
        "shortName": sym,
        "logo": logo,
    }
    return out


class TopGainnerLosserervice:
    """Fast, real-NSE service for top market gainers and losers."""

    @staticmethod
    def get_nifty_gainner(user_data=None):
        try:
            _, gainers, _ = nse_market_service.fetch_live_stocks_and_movers()
            return [_decorate_mover(item) for item in (gainers or [])[:12]]
        except Exception as e:
            logger.error(f"Error in get_nifty_gainner: {e}")
            return []

    @staticmethod
    def get_banknifty_gainner(user_data=None):
        try:
            quotes, _, _ = nse_market_service.fetch_live_stocks_and_movers(required_symbols=list(BANK_SYMBOLS))
            quotes = quotes or {}
            bank_items = []
            for sym in BANK_SYMBOLS:
                q = quotes.get(sym) or quotes.get(f"{sym}.NS")
                if q and float(q.get("ltp") or 0) > 0:
                    bank_items.append({
                        "symbol": sym,
                        "companyName": q.get("companyName") or q.get("name") or sym,
                        "ltp": q.get("ltp"),
                        "lastPrice": q.get("ltp"),
                        "pChange": q.get("pChange") if q.get("pChange") is not None else q.get("changePercent", 0.0),
                        "previousClose": q.get("prev", q.get("ltp")),
                    })
            bank_items.sort(key=lambda x: float(x.get("pChange", 0.0)), reverse=True)
            pos = [b for b in bank_items if float(b.get("pChange", 0.0)) >= 0]
            chosen = pos if pos else bank_items
            return [_decorate_mover(item) for item in chosen[:10]]
        except Exception as e:
            logger.error(f"Error in get_banknifty_gainner: {e}")
            return []

    @staticmethod
    def get_nifty_losser(user_data=None):
        try:
            _, _, losers = nse_market_service.fetch_live_stocks_and_movers()
            return [_decorate_mover(item) for item in (losers or [])[:12]]
        except Exception as e:
            logger.error(f"Error in get_nifty_losser: {e}")
            return []

    @staticmethod
    def get_banknifty_losser(user_data=None):
        try:
            quotes, _, _ = nse_market_service.fetch_live_stocks_and_movers(required_symbols=list(BANK_SYMBOLS))
            quotes = quotes or {}
            bank_items = []
            for sym in BANK_SYMBOLS:
                q = quotes.get(sym) or quotes.get(f"{sym}.NS")
                if q and float(q.get("ltp") or 0) > 0:
                    bank_items.append({
                        "symbol": sym,
                        "companyName": q.get("companyName") or q.get("name") or sym,
                        "ltp": q.get("ltp"),
                        "lastPrice": q.get("ltp"),
                        "pChange": q.get("pChange") if q.get("pChange") is not None else q.get("changePercent", 0.0),
                        "previousClose": q.get("prev", q.get("ltp")),
                    })
            bank_items.sort(key=lambda x: float(x.get("pChange", 0.0)))
            neg = [b for b in bank_items if float(b.get("pChange", 0.0)) < 0]
            chosen = neg if neg else bank_items
            return [_decorate_mover(item) for item in chosen[:10]]
        except Exception as e:
            logger.error(f"Error in get_banknifty_losser: {e}")
            return []