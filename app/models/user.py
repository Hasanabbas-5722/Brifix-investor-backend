"""Example MongoDB models for Users collection"""
from datetime import datetime
from bson import ObjectId
from app.extensions import connect_to_mongodb
from app.utils.logger import get_logger

logger = get_logger(__name__)


COLLECTION_NAME = "users"


db = connect_to_mongodb()

import re

class User:
    """User model for MongoDB"""
    
    def __init__(self, name, email, phone=None, username=None, current_plan="Free", is_active=True, _id=None):
        self._id = _id or ObjectId()
        self.name = name
        self.username = username or email.split("@")[0] if email else ""
        self.email = email
        self.phone = phone
        self.current_plan = current_plan
        self.is_active = is_active
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            "_id": self._id,
            "name": self.name,
            "username": self.username,
            "email": self.email,
            "phone": self.phone,
            "currentPlan": self.current_plan,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def find_by_email(email):
        """Find user by email (case-insensitive)"""
        if not email:
            return None
        clean_email = email.strip().lower()
        user_data = db.users.find_one({"email": {"$regex": f"^{re.escape(clean_email)}$", "$options": "i"}})
        if not user_data:
            return None
        user_data["id"] = str(user_data["_id"])
        return user_data

    @staticmethod
    def find_by_username(username):
        """Find user by username (case-insensitive)"""
        if not username:
            return None
        clean_username = username.strip().lower()
        user_data = db.users.find_one({"username": {"$regex": f"^{re.escape(clean_username)}$", "$options": "i"}})
        if not user_data:
            return None
        user_data["id"] = str(user_data["_id"])
        return user_data

    @staticmethod
    def find_by_email_or_username(identifier):
        """Find user by either email or username (case-insensitive)"""
        if not identifier:
            return None
        clean_id = identifier.strip().lower()
        escaped = re.escape(clean_id)
        user_data = db.users.find_one({
            "$or": [
                {"email": {"$regex": f"^{escaped}$", "$options": "i"}},
                {"username": {"$regex": f"^{escaped}$", "$options": "i"}}
            ]
        })
        if not user_data:
            return None
        user_data["id"] = str(user_data["_id"])
        return user_data

    @staticmethod
    def create_user(
        name,
        email,
        password_hash,
        phone=None,
        username=None,
        current_plan="Free",
        trading_experience="Beginner",
        **kwargs
    ):
        """Create a new user in MongoDB with comprehensive profile and preference fields"""
        try:
            clean_email = email.strip().lower()
            clean_username = (username or clean_email.split("@")[0]).strip().lower()
            
            # Split full name into first and last name
            parts = (name or "").strip().split(" ", 1)
            first_name = parts[0] if parts else clean_username
            last_name = parts[1] if len(parts) > 1 else ""

            plan_cap = current_plan.capitalize() if current_plan else "Free"
            plan_lower = current_plan.lower() if current_plan else "free"

            doc = {
                "name": name or first_name,
                "firstName": first_name,
                "lastName": last_name,
                "username": clean_username,
                "email": clean_email,
                "password": password_hash,
                "phone": phone or "",
                "mobile": phone or "",
                "currentPlan": plan_cap,
                "plan": plan_lower,
                "tradingExperience": trading_experience or "Beginner",
                "status": "active",
                "is_active": True,
                "verifiedAccount": True,
                "isAdmin": False,
                "role": "user",
                "tradingMethod": "Manual",
                "accountMethod": "live",
                "loginMethod": "email",
                "deviceType": "Web",
                "watchlist": [],
                "portfolio": {
                    "cashBalance": 100000.0,
                    "investedValue": 0.0,
                    "holdings": []
                },
                "preferences": {
                    "theme": "dark",
                    "notifications": True,
                    "currency": "INR",
                    "emailAlerts": True
                },
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow(),
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }

            # Merge any extra optional attributes passed
            for k, v in kwargs.items():
                if k not in doc:
                    doc[k] = v

            result = db.users.insert_one(doc)
            doc["_id"] = result.inserted_id
            doc["id"] = str(result.inserted_id)
            return doc
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None

    @staticmethod
    def update_token(id, jwt_token):
        """Update user accessToken only"""
        try:
            result = db.users.update_one(
                {"_id": ObjectId(id)},
                {"$set": {
                    "accessToken": jwt_token,
                    "updatedAt": datetime.utcnow()
                }}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user token: {e}")
            return False

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

    @staticmethod
    def find_user_by_user_id(id):
        """Find user by id"""
        try:
            logger.info(f"id : {id}")
            user_data = db.users.find_one({"_id": ObjectId(id)})
            if not user_data:
                return None
            user_data["_id"] = str(user_data["_id"])
            user_data["id"] = str(user_data["_id"])
            return user_data
        except Exception as e:
            logger.error(f"Error finding user by id: {e}")
            return None
    
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
