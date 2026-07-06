from newsapi import NewsApiClient
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Init
newsapi = NewsApiClient(api_key='ff013076b7b84da89d19cd31339f058f')



class TopStockNews:
    def __init__(self):
        pass
    
    @staticmethod
    def TopIndianNews():
        response = newsapi.get_everything(
            q=(
                'NSE OR '
                'BSE OR '
                'Nifty OR '
                'Sensex OR '
                '"stock market" OR '
                '"share market" OR '
                'IPO'
            ),
            sort_by='publishedAt',
            language='en',
            page_size=20
        )

        # logger.info(f"response from the news api :: {response}")
        return response