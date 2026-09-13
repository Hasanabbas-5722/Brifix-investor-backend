import time
from urllib.parse import urlparse
from pathlib import Path
import yfinance as yf
from nse import NSE
from app.utils.logger import get_logger

logger = get_logger(__name__)

DIR = Path(__file__).parent

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


_CACHE = {
    "nifty_gainers": {"data": [], "timestamp": 0},
    "banknifty_gainers": {"data": [], "timestamp": 0},
    "nifty_losers": {"data": [], "timestamp": 0},
    "banknifty_losers": {"data": [], "timestamp": 0},
}
CACHE_TTL = 60  # seconds

_nse_instance = None

def get_nse():
    global _nse_instance
    if _nse_instance is None:
        try:
            _nse_instance = NSE(download_folder=DIR)
        except Exception as e:
            logger.error(f"Failed to initialize NSE: {e}")
    return _nse_instance


class TopGainnerLosserervice:
    """Fast, cached service for top market gainers and losers."""

    @staticmethod
    def _fetch_from_yfinance(symbols):
        """Fallback when NSE API is unavailable (e.g. outside market hours)."""
        results = []
        try:
            tickers = [f"{s}.NS" for s in symbols]
            data = yf.download(tickers, period="2d", progress=False, group_by="ticker")
            for sym in symbols:
                ticker_sym = f"{sym}.NS"
                if ticker_sym in data:
                    sub = data[ticker_sym]
                    if len(sub) >= 2:
                        prev_close = float(sub["Close"].iloc[-2])
                        curr_close = float(sub["Close"].iloc[-1])
                        p_change = round(((curr_close - prev_close) / prev_close) * 100, 2)
                        results.append({
                            "symbol": sym,
                            "stockSymbol": sym,
                            "companyName": sym,
                            "ltp": round(curr_close, 2),
                            "lastPrice": round(curr_close, 2),
                            "pChange": p_change,
                            "previousClose": round(prev_close, 2),
                            "logo": get_stock_logo(sym),
                            "stockinfo": {
                                "shortName": sym,
                                "logo": get_stock_logo(sym),
                            }
                        })
        except Exception as e:
            logger.error(f"yfinance fallback error: {e}")
        return results

    @staticmethod
    def get_nifty_gainner(user_data=None):
        now = time.time()
        if _CACHE["nifty_gainers"]["data"] and (now - _CACHE["nifty_gainers"]["timestamp"] < CACHE_TTL):
            return _CACHE["nifty_gainers"]["data"]

        try:
            nse = get_nse()
            stock_list = []
            if nse:
                nifty_list = nse.listEquityStocksByIndex(index='NIFTY 50')
                gainers = nse.gainers(data=nifty_list)
                for item in (gainers or [])[:10]:
                    sym = item.get("symbol", "")
                    if "NIFTY" not in sym:
                        item["logo"] = get_stock_logo(sym)
                        item["stockinfo"] = {
                            "shortName": item.get("symbol", ""),
                            "logo": item["logo"]
                        }
                        stock_list.append(item)

            if not stock_list:
                sample = ["ADANIENT", "TATAMOTORS", "BHARTIARTL", "RELIANCE", "SBIN"]
                stock_list = TopGainnerLosserervice._fetch_from_yfinance(sample)
                stock_list.sort(key=lambda x: x.get("pChange", 0), reverse=True)

            _CACHE["nifty_gainers"] = {"data": stock_list, "timestamp": now}
            return stock_list
        except Exception as e:
            logger.error(f"Error in get_nifty_gainner: {e}")
            return _CACHE["nifty_gainers"]["data"] or []

    @staticmethod
    def get_banknifty_gainner(user_data=None):
        now = time.time()
        if _CACHE["banknifty_gainers"]["data"] and (now - _CACHE["banknifty_gainers"]["timestamp"] < CACHE_TTL):
            return _CACHE["banknifty_gainers"]["data"]

        try:
            nse = get_nse()
            stock_list = []
            if nse:
                banknifty_list = nse.listEquityStocksByIndex(index='NIFTY BANK')
                gainers = nse.gainers(data=banknifty_list)
                for item in (gainers or [])[:10]:
                    sym = item.get("symbol", "")
                    if "NIFTY" not in sym:
                        item["logo"] = get_stock_logo(sym)
                        item["stockinfo"] = {
                            "shortName": item.get("symbol", ""),
                            "logo": item["logo"]
                        }
                        stock_list.append(item)

            if not stock_list:
                sample = ["ICICIBANK", "AXISBANK", "SBIN", "KOTAKBANK", "HDFCBANK"]
                stock_list = TopGainnerLosserervice._fetch_from_yfinance(sample)
                stock_list.sort(key=lambda x: x.get("pChange", 0), reverse=True)

            _CACHE["banknifty_gainers"] = {"data": stock_list, "timestamp": now}
            return stock_list
        except Exception as e:
            logger.error(f"Error in get_banknifty_gainner: {e}")
            return _CACHE["banknifty_gainers"]["data"] or []

    @staticmethod
    def get_nifty_losser(user_data=None):
        now = time.time()
        if _CACHE["nifty_losers"]["data"] and (now - _CACHE["nifty_losers"]["timestamp"] < CACHE_TTL):
            return _CACHE["nifty_losers"]["data"]

        try:
            nse = get_nse()
            stock_list = []
            if nse:
                nifty_list = nse.listEquityStocksByIndex(index='NIFTY 50')
                losers = nse.losers(data=nifty_list)
                for item in (losers or [])[:10]:
                    sym = item.get("symbol", "")
                    if "NIFTY" not in sym:
                        item["logo"] = get_stock_logo(sym)
                        item["stockinfo"] = {
                            "shortName": item.get("symbol", ""),
                            "logo": item["logo"]
                        }
                        stock_list.append(item)

            if not stock_list:
                sample = ["WIPRO", "INFY", "SUNPHARMA", "NESTLEIND", "HDFCBANK"]
                stock_list = TopGainnerLosserervice._fetch_from_yfinance(sample)
                stock_list.sort(key=lambda x: x.get("pChange", 0))

            _CACHE["nifty_losers"] = {"data": stock_list, "timestamp": now}
            return stock_list
        except Exception as e:
            logger.error(f"Error in get_nifty_losser: {e}")
            return _CACHE["nifty_losers"]["data"] or []

    @staticmethod
    def get_banknifty_losser(user_data=None):
        now = time.time()
        if _CACHE["banknifty_losers"]["data"] and (now - _CACHE["banknifty_losers"]["timestamp"] < CACHE_TTL):
            return _CACHE["banknifty_losers"]["data"]

        try:
            nse = get_nse()
            stock_list = []
            if nse:
                banknifty_list = nse.listEquityStocksByIndex(index='NIFTY BANK')
                losers = nse.losers(data=banknifty_list)
                for item in (losers or [])[:10]:
                    sym = item.get("symbol", "")
                    if "NIFTY" not in sym:
                        item["logo"] = get_stock_logo(sym)
                        item["stockinfo"] = {
                            "shortName": item.get("symbol", ""),
                            "logo": item["logo"]
                        }
                        stock_list.append(item)

            if not stock_list:
                sample = ["BANDHANBNK", "IDFCFIRSTB", "FEDERALBNK", "PNB", "BANKBARODA"]
                stock_list = TopGainnerLosserervice._fetch_from_yfinance(sample)
                stock_list.sort(key=lambda x: x.get("pChange", 0))

            _CACHE["banknifty_losers"] = {"data": stock_list, "timestamp": now}
            return stock_list
        except Exception as e:
            logger.error(f"Error in get_banknifty_losser: {e}")
            return _CACHE["banknifty_losers"]["data"] or []

    