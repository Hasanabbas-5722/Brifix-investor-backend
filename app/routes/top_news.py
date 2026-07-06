from app.utils.access_token_validate import validate_access_token
from app.services.top_news_service import TopStockNews
from flask import jsonify
from flask import Blueprint
from app.utils.logger import get_logger

logger = get_logger(__name__)

top_news_bp = Blueprint("top_news", __name__, url_prefix="/api/v1/")


@top_news_bp.route("/top_news", methods=["GET"])
@validate_access_token
def top_news():
    try:
        top_news = TopStockNews.TopIndianNews()

        return jsonify({
            "status": "success",
            "totalResult": top_news["totalResults"],
            "data": top_news["articles"]
        })
 
    except Exception as e:
        logger.error(f"Error from top news route :: {str(e)}")
        return jsonify({
            "status": "failed",
            "error": str(e)
        })
    # end try