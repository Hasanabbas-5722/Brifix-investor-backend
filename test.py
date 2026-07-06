# # import yfinance as yf

# # stock = yf.Ticker("RELIANCE.NS")

# # data = stock.info
# # # print(data["longName"])
# # # print(data["currentPrice"])
# # # print(data["open"])
# # # print(data["previousClose"])

# # print("Stock:", stock)
# # print("data ::::::", data["website"])
# # print("data logo url  ::::::", data.get("logo_url"))

# # import urllib.parse

# # website = data.get("website")
# # domain = urllib.parse.urlparse(website).netloc

# # logo_url = f"https://logo.clearbit.com/{domain}"
# # print(logo_url)

# # import finnhub
# # finnhub_client = finnhub.Client(api_key="d7piv19r01qlb0aa5qrgd7piv19r01qlb0aa5qs0")

# # print(finnhub_client.company_profile2(symbol='RELIANCE'))

# # import requests
# # from bs4 import BeautifulSoup

# # url = "https://www.ril.com"

# # headers = {
# #     "User-Agent": "Mozilla/5.0"
# # }

# # response = requests.get(url, headers=headers)
# # soup = BeautifulSoup(response.text, "html.parser")

# # # Try og:image
# # logo = None
# # og = soup.find("meta", property="og:image")
# # if og:
# #     logo = og.get("content")
    
# # favicon = soup.find("link", rel="icon")

# # if favicon:
# #     print("Favicon:", favicon.get("href"))
    
# # print("Logo:", logo)




# # stock new api for this project 

# from newsapi import NewsApiClient

# # Init
# newsapi = NewsApiClient(api_key='ff013076b7b84da89d19cd31339f058f')

# # /v2/everything
# # India business news
# response = newsapi.get_everything(

#     q=(
#         'NSE OR '
#         'BSE OR '
#         'Nifty OR '
#         'Sensex OR '
#         '"stock market" OR '
#         '"share market" OR '
#         'IPO'
#     ),

#     sort_by='publishedAt',

#     language='en',

#     page_size=20
# )

# print(response)




# package import statement
from SmartApi import SmartConnect #or from SmartApi.smartConnect import SmartConnect
import pyotp
from logzero import logger

api_key = 'R2ogcWTx code'
username = "PRJR2770"
pwd = '7860'
smartApi = SmartConnect(api_key)
try:
    token = "ZN7VKB7VNZ7CSUJQMIJDAO3HAA"
    totp = pyotp.TOTP(token).now()
except Exception as e:
    logger.error("Invalid Token: The provided token is not valid.")
    raise e

correlation_id = "abcde"
data = smartApi.generateSession(username, pwd, totp)

print(data)

if data['status'] == False:
    logger.error(data)
    
else:
    # login api call
    # logger.info(f"You Credentials: {data}")
    authToken = data['data']['jwtToken']
    refreshToken = data['data']['refreshToken']
    # fetch the feedtoken
    feedToken = smartApi.getfeedToken()
    # fetch User Profile
    res = smartApi.getProfile(refreshToken)
    print(res)
    
    rms = smartApi.rmsLimit()
    print(rms)

    smartApi.generateToken(refreshToken)
    res=res['data']['exchanges']

    #place order
    try:
        orderparams = {
            "variety": "NORMAL",
            "tradingsymbol": "SBIN-EQ",
            "symboltoken": "3045",
            "transactiontype": "BUY",
            "exchange": "NSE",
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "19500",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": "1"
            }
        # Method 1: Place an order and return the order ID
        orderid = smartApi.placeOrder(orderparams)
        logger.info(f"PlaceOrder : {orderid}")
        # Method 2: Place an order and return the full response
        response = smartApi.placeOrderFullResponse(orderparams)
        logger.info(f"PlaceOrder : {response}")
    except Exception as e:
        logger.exception(f"Order placement failed: {e}")

    #gtt rule creation
    try:
        gttCreateParams={
                "tradingsymbol" : "SBIN-EQ",
                "symboltoken" : "3045",
                "exchange" : "NSE", 
                "producttype" : "MARGIN",
                "transactiontype" : "BUY",
                "price" : 100000,
                "qty" : 10,
                "disclosedqty": 10,
                "triggerprice" : 200000,
                "timeperiod" : 365
            }
        rule_id=smartApi.gttCreateRule(gttCreateParams)
        logger.info(f"The GTT rule id is: {rule_id}")
    except Exception as e:
        logger.exception(f"GTT Rule creation failed: {e}")
        
    #gtt rule list
    try:
        status=["FORALL"] #should be a list
        page=1
        count=10
        lists=smartApi.gttLists(status,page,count)
    except Exception as e:
        logger.exception(f"GTT Rule List failed: {e}")

    #Historic api
    try:
        historicParam={
        "exchange": "NSE",
        "symboltoken": "3045",
        "interval": "ONE_MINUTE",
        "fromdate": "2021-02-08 09:00", 
        "todate": "2021-02-08 09:16"
        }
        smartApi.getCandleData(historicParam)
    except Exception as e:
        logger.exception(f"Historic Api failed: {e}")
    #logout
    try:
        logout=smartApi.terminateSession('Your Client Id')
        logger.info("Logout Successfull")
    except Exception as e:
        logger.exception(f"Logout failed: {e}")
