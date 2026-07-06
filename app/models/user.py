"""Example MongoDB models for Users collection"""
from datetime import datetime
from bson import ObjectId
from app.extensions import connect_to_mongodb
from app.utils.logger import get_logger

logger = get_logger(__name__)


COLLECTION_NAME = "users"


db = connect_to_mongodb()

class User:
    """User model for MongoDB"""
    
    def __init__(self, name, email, phone=None, is_active=True, _id=None):
        self._id = _id or ObjectId()
        self.name = name
        self.email = email
        self.phone = phone
        self.is_active = is_active
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            "_id": self._id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def find_by_email(email):
        """Find user by email"""
        logger.info(f"email : {email}")
        user_data = db.users.find_one({"email": email})
        logger.info(f"user dta :::: {user_data}")
        user_data["id"] = str(user_data["_id"])

        if not user_data:
            return None
        return user_data

    @staticmethod
    def update(angle_jwt_token, angle_refresh_token, angle_feed_token, id, jwt_token):
        """Update user"""
        try:
            result = db.users.update_one(
                {"_id": ObjectId(id)},
                {"$set": {
                    "angleJwtToken": angle_jwt_token,
                    "angleRefreshToken": angle_refresh_token,
                    "angleFeedToken": angle_feed_token,
                    "updatedAt": datetime.utcnow(),
                    "accessToken": jwt_token
                }}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user: {e}")
            return False

    def find_user_by_user_id(id):
        """Find user by id"""
        logger.info(f"id : {id}")
        user_data = db.users.find_one({"_id": ObjectId(id)})
        user_data["_id"] = str(user_data["_id"])
        if not user_data:
            return None
        return user_data
    
    @staticmethod
    def update_password(email, new_password):
        """Update user password"""
        try:
            result = db.users.update_one(
                {"email": email},
                {"$set": {
                    "password": new_password,
                    "updatedAt": datetime.utcnow()
                }}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating password: {e}")
            return False
    
    def __repr__(self):
        return f"<User {self.email}>"
