"""Example MongoDB models for Users collection"""
from datetime import datetime
from bson import ObjectId
from app.extensions import connect_to_mongodb
from app.utils.logger import get_logger

logger = get_logger(__name__)


COLLECTION_NAME = "orders"


db = connect_to_mongodb()

class Orders:
    """Orders model for MongoDB"""

    def add_orders(data):
        try:
            add_order = db.orders.insert_one(data)
            return add_order
        except Exception as e:
            return str(e)

    
    
    
