from logging import exception
from flask import request, jsonify
from app.utils.logger import get_logger
import requests
from nse import NSE
from pathlib import Path
import yfinance as yf
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin

logger = get_logger(__name__)


def get_website_logo(website_url):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/136.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
    }

    try:

        logger.info(f"website of logo :::: {website_url}")
        response = requests.get(
            website_url,
            headers=headers,
            timeout=10
        )
        logger.info(f"website of logo :::: {response.status_code}")
        if response.status_code != 200:
            # logger.error(f"Website not reachable: {response.status_code}")
            domain = urlparse(website_url).netloc

            logo_url = (
                f"https://www.google.com/s2/favicons"
                f"?domain={domain}&sz=256"
            )

            # print(logo_url)
            return logo_url

        soup = BeautifulSoup(response.text, "html.parser")

        logo = None

        # =========================
        # METHOD 1 - og:image
        # =========================

        og = soup.find("meta", property="og:image")

        if og and og.get("content"):

            logo = og.get("content")

            # logger.info(f"Found og:image => {logo}")

            return urljoin(website_url, logo)

        # =========================
        # METHOD 2 - twitter:image
        # =========================

        twitter = soup.find("meta", attrs={"name": "twitter:image"})

        if twitter and twitter.get("content"):

            logo = twitter.get("content")

            # logger.info(f"Found twitter:image => {logo}")

            return urljoin(website_url, logo)

        # =========================
        # METHOD 3 - favicon icon
        # =========================

        favicon = soup.find(
            "link",
            rel=lambda x: x and "icon" in x.lower()
        )

        if favicon and favicon.get("href"):

            logo = favicon.get("href")

            # logger.info(f"Found favicon => {logo}")

            return urljoin(website_url, logo)

        # =========================
        # METHOD 4 - apple-touch-icon
        # =========================

        apple = soup.find(
            "link",
            rel=lambda x: x and "apple-touch-icon" in x.lower()
        )

        if apple and apple.get("href"):

            logo = apple.get("href")

            # logger.info(f"Found apple-touch-icon => {logo}")

            return urljoin(website_url, logo)

        # =========================
        # METHOD 5 - shortcut icon
        # =========================

        shortcut = soup.find(
            "link",
            rel=lambda x: x and "shortcut icon" in x.lower()
        )

        if shortcut and shortcut.get("href"):

            logo = shortcut.get("href")

            # logger.info(f"Found shortcut icon => {logo}")

            return urljoin(website_url, logo)

        # =========================
        # METHOD 6 - default favicon.ico
        # =========================

        favicon_ico = urljoin(website_url, "/favicon.ico")

        check = requests.get(
            favicon_ico,
            headers=headers,
            timeout=5
        )

        if check.status_code == 200:

            # logger.info(f"Found favicon.ico => {favicon_ico}")

            return favicon_ico

        # =========================
        # METHOD 7 - manifest.json
        # =========================

        manifest = soup.find("link", rel="manifest")

        if manifest and manifest.get("href"):

            manifest_url = urljoin(
                website_url,
                manifest.get("href")
            )

            manifest_response = requests.get(
                manifest_url,
                headers=headers,
                timeout=5
            )

            if manifest_response.status_code == 200:

                manifest_json = manifest_response.json()

                icons = manifest_json.get("icons", [])

                if icons:

                    icon = icons[0].get("src")

                    if icon:

                        # logger.info(f"Found manifest icon => {icon}")

                        return urljoin(website_url, icon)

        # =========================
        # METHOD 8 - Clearbit fallback
        # =========================

        domain = website_url.replace("https://", "").replace(
            "http://",
            ""
        ).split("/")[0]

        clearbit_logo = (
            f"https://logo.clearbit.com/{domain}"
        )

        # logger.info(f"Using Clearbit fallback => {clearbit_logo}")

        return clearbit_logo

    except Exception as e:

        logger.error(f"Logo extraction failed: {e}")

        return None

DIR = Path(__file__).parent

nse = NSE(download_folder=DIR)
nifty_list = nse.listEquityStocksByIndex(index='NIFTY 50')
banknifty_list = nse.listEquityStocksByIndex(index='NIFTY BANK')

class TopGainnerLosserervice:
    def __init__(self):
        pass
    
    def get_nifty_gainner():
        try:            
            logger.info(f"Enter get nifty gainner function :::::::")
            # Working directory
            # list = nse.listEquityStocksByIndex(index='NIFTY 50')
            banknifty_losser_list = nse.gainers(data=nifty_list)
            # logger.info(f"list :::: {list}")
            stock_list = []
            for item in banknifty_losser_list[0:11]:
                # logger.info(f"symbol ::: {item["symbol"]}")
                # logger.info(f"symbol data ::: {item}")

                if not "NIFTY 50" in item["symbol"]:
                    # logger.info(f"symbol full :: {item["symbol"] + ".NS"}")
                    stock = yf.Ticker(item["symbol"] + ".NS")
                    # logger.info(f"stock ::: {stock.info["website"]}")
                    logo = get_website_logo(stock.info["website"])
                    # if stock.info["website"]:

                    #     domain = urlparse(
                    #         stock.info["website"]
                    #     ).netloc

                    #     # FASTEST METHOD
                    #     logo_url = (
                    #         f"https://www.google.com/s2/favicons"
                    #         f"?domain={domain}&sz=256"
                    #     )

                    # print("Logo:", logo)
                    stock.info["logo"] = logo
                    item['stockinfo'] = stock.info
                    
                    stock_list.append(item)

            # logger.info(f"stock list of nse api ===>{stock_list}")
            # logger.info(f"status of nse api ===>{status}")

            nse.exit() # close requests session
            return stock_list
        except Exception as e:
            logger.info(f"Error from top nifty services : {e}")
            return str(e)

    
    def get_banknifty_gainner():
        try:
            logger.info(f"Enter get bank nifty gainner function :::::::")
             # Working directory
            # list = nse.listEquityStocksByIndex(index='NIFTY 50')
            banknifty_losser_list = nse.gainers(data=banknifty_list)
            # logger.info(f"list :::: {list}")
            stock_list = []
            for item in banknifty_losser_list[0:11]:
                # logger.info(f"symbol ::: {item["symbol"]}")
                # logger.info(f"symbol data ::: {item}")

                if not "NIFTY BANK" in item["symbol"]:
                    # logger.info(f"symbol full :: {item["symbol"] + ".NS"}")
                    stock = yf.Ticker(item["symbol"] + ".NS")
                    # logger.info(f"stock ::: {stock.info["website"]}")
                    logo = get_website_logo(stock.info["website"])
                    # print("Logo:", logo)
                    stock.info["logo"] = logo
                    item['stockinfo'] = stock.info
                    
                    stock_list.append(item)
            nse.exit() # close requests session
            return stock_list
        except Exception as e:
            logger.info(f"Error from get banknifty gainner :: {e}")
        # end try

    @staticmethod
    def get_nifty_losser():
        try:
            logger.info(f"Enter get nifty losser function :::::::")
             # Working directory
            # list = nse.listEquityStocksByIndex(index='NIFTY 50')
            # logger.info(f"list :::: {list}")
            banknifty_losser_list = nse.losers(data=nifty_list)
            # logger.info(f"nifty losers list ::: {banknifty_losser_list}")
            stock_list = []
            for item in banknifty_losser_list[0:11]:
                # logger.info(f"symbol ::: {item["symbol"]}")
                # logger.info(f"symbol data ::: {item}")

                if not "NIFTY 50" in item["symbol"]:
                    # logger.info(f"symbol full :: {item["symbol"] + ".NS"}")
                    stock = yf.Ticker(item["symbol"] + ".NS")
                    # logger.info(f"stock ::: {stock.info["website"]}")
                    logo = get_website_logo(stock.info["website"])
                    # print("Logo:", logo)
                    stock.info["logo"] = logo
                    item['stockinfo'] = stock.info
                    
                    stock_list.append(item)
            nse.exit() # close requests session
            # logger.info(f"list of nifty losser ====> {stock_list}")
            return stock_list
        except Exception as e:
            logger.info(f"Error from get banknifty lossser :: {e}")
        # end try

    
    def get_banknifty_losser():
        try:
            logger.info(f"Enter get bank nifty losser function :::::::")
             # Working directory
            banknifty_losser_list = nse.losers(data=banknifty_list)
            # logger.info(f"banknifty losers list ::: {banknifty_losser_list}")
            # logger.info(f"list :::: {list}")
            stock_list = []
            for item in banknifty_losser_list[1:11]:
                # logger.info(f"symbol ::: {item["symbol"]}")
                # logger.info(f"symbol data ::: {item}")

                if not "NIFTY BANK" in item["symbol"]:
                    # logger.info(f"symbol full :: {item["symbol"] + ".NS"}")
                    stock = yf.Ticker(item["symbol"] + ".NS")
                    # logger.info(f"stock ::: {stock.info["website"]}")
                    logo = get_website_logo(stock.info["website"])
                    # print("Logo:", logo)
                    stock.info["logo"] = logo
                    item['stockinfo'] = stock.info
                    
                    stock_list.append(item)
            nse.exit() # close requests session
            return stock_list
        except Exception as e:
            logger.info(f"Error from get banknifty losser :: {e}")
        # end try

    