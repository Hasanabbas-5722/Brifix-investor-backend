"""Example MongoDB models for Users collection"""
from datetime import datetime
import time
import copy
import threading
from bson import ObjectId
from pymongo.errors import PyMongoError
from app.extensions import connect_to_mongodb
from app.utils.logger import get_logger

logger = get_logger(__name__)


COLLECTION_NAME = "users"

# In-memory user cache to eliminate DB load on rapid page refreshes
_USER_CACHE_TTL = 30.0
_user_cache = {}  # {user_id_str: (user_dict_copy, expire_time)}
_cache_lock = threading.Lock()


class _DatabaseProxy:
    """Dynamic proxy that always delegates to the live MongoDB database connection."""
    def _target(self):
        return connect_to_mongodb()

    def __getattr__(self, name):
        target = self._target()
        if target is None:
            raise RuntimeError(f"MongoDB connection is not established when accessing '{name}'")
        return getattr(target, name)

    def __getitem__(self, name):
        target = self._target()
        if target is None:
            raise RuntimeError(f"MongoDB connection is not established when accessing collection '{name}'")
        return target[name]

    def __bool__(self):
        return self._target() is not None


db = _DatabaseProxy()


def _get_db():
    return connect_to_mongodb()

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
    def normalize_phone(phone: str) -> str:
        """Normalize Indian or international phone number to canonical 10-digit string."""
        if not phone:
            return ""
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        return digits

    @staticmethod
    def sanitize_user_doc(user_data: dict) -> dict:
        """Strip password hashes and any legacy broker secrets from user dictionary."""
        if not isinstance(user_data, dict):
            return user_data
        cleaned = dict(user_data)
        for forbidden in (
            "password",
            "angleClientPin",
            "angleTotpSecret",
            "angleApiKey",
            "angleJwtToken",
            "angleRefreshToken",
            "angleFeedToken",
            "growwApiKey",
            "growwTotpSecret",
            "lastRevokedToken",
        ):
            cleaned.pop(forbidden, None)
        return cleaned

    @staticmethod
    def find_by_phone(phone: str):
        """Find user by phone number with retry resilience."""
        norm_phone = User.normalize_phone(phone)
        if not norm_phone:
            return None
        for attempt in range(3):
            try:
                database = _get_db()
                if database is None:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                user_data = database.users.find_one(
                    {"phone": {"$in": [norm_phone, f"+91{norm_phone}", str(phone).strip()]}}
                )
                if not user_data:
                    return None
                user_data["_id"] = str(user_data["_id"])
                user_data["id"] = str(user_data["_id"])
                return user_data
            except Exception as e:
                logger.warning(f"Transient error finding user by phone (attempt {attempt + 1}/3): {e}")
                time.sleep(0.15 * (attempt + 1))
        return None

    @staticmethod
    def find_by_email(email):
        """Find user by email with retry resilience"""
        logger.info(f"Looking up user by email: {email}")
        for attempt in range(3):
            try:
                database = _get_db()
                if database is None:
                    time.sleep(0.1 * (attempt + 1))
                    continue
                user_data = database.users.find_one({"email": email})
                if not user_data:
                    return None
                user_data["_id"] = str(user_data["_id"])
                user_data["id"] = str(user_data["_id"])
                return user_data
            except Exception as e:
                logger.warning(f"Transient error finding user by email {email} (attempt {attempt + 1}/3): {e}")
                time.sleep(0.15 * (attempt + 1))
        return None

    @staticmethod
    def create_user(name, email, password_hash, phone=None):
        """Create a new user in MongoDB"""
        try:
            norm_phone = User.normalize_phone(phone) if phone else None
            doc = {
                "name": name,
                "email": email,
                "password": password_hash,
                "phone": norm_phone,
                "isAdmin": False,
                "is_active": True,
                "activeBroker": "paper",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            result = _get_db().users.insert_one(doc)
            doc["_id"] = result.inserted_id
            doc["id"] = str(result.inserted_id)
            return doc
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            return None

    @classmethod
    def invalidate_cache(cls, user_id=None):
        """Invalidate user in-memory cache."""
        with _cache_lock:
            if user_id:
                _user_cache.pop(str(user_id), None)
            else:
                _user_cache.clear()

    @classmethod
    def update_token(cls, id, jwt_token):
        """Update user accessToken only"""
        try:
            result = _get_db().users.update_one(
                {"_id": ObjectId(id)},
                {"$set": {
                    "accessToken": jwt_token,
                    "updatedAt": datetime.utcnow()
                }}
            )
            cls.invalidate_cache(id)
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user token: {e}")
            return False

    @classmethod
    def update(cls, angle_jwt_token, angle_refresh_token, angle_feed_token, id, jwt_token):
        """Update user"""
        try:
            result = _get_db().users.update_one(
                {"_id": ObjectId(id)},
                {"$set": {
                    "angleJwtToken": angle_jwt_token,
                    "angleRefreshToken": angle_refresh_token,
                    "angleFeedToken": angle_feed_token,
                    "updatedAt": datetime.utcnow(),
                    "accessToken": jwt_token
                }}
            )
            cls.invalidate_cache(id)
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating user: {e}")
            return False

    @classmethod
    def find_user_by_user_id(cls, id):
        """Find user by id with in-memory caching and resilient retries."""
        if not id:
            return None

        user_id_str = str(id)
        now = time.time()

        # 1. Fast RAM path: Return cached user object to eliminate DB load on parallel page refreshes
        with _cache_lock:
            cached = _user_cache.get(user_id_str)
            if cached:
                cached_data, exp = cached
                if now < exp:
                    return copy.deepcopy(cached_data)
                else:
                    _user_cache.pop(user_id_str, None)

        # 2. Database path with retry resilience against momentary network/SSL glitches
        last_exception = None
        for attempt in range(3):
            try:
                database = _get_db()
                if database is None:
                    time.sleep(0.1 * (attempt + 1))
                    continue

                user_data = database.users.find_one({"_id": ObjectId(user_id_str)})
                if not user_data:
                    return None

                user_data["_id"] = str(user_data["_id"])
                user_data["id"] = str(user_data["_id"])

                # Store snapshot in RAM cache
                with _cache_lock:
                    _user_cache[user_id_str] = (copy.deepcopy(user_data), now + _USER_CACHE_TTL)

                return user_data

            except PyMongoError as pe:
                last_exception = pe
                logger.warning(f"Transient PyMongo error querying user {user_id_str} (attempt {attempt + 1}/3): {pe}")
                time.sleep(0.15 * (attempt + 1))
            except Exception as e:
                last_exception = e
                logger.error(f"Error finding user by id {user_id_str}: {e}")
                break

        if last_exception:
            logger.error(f"Failed to query user {user_id_str} after 3 retries: {last_exception}")
            raise RuntimeError(f"Database connection error: {last_exception}")

        return None
    
    @classmethod
    def update_password(cls, email, new_password):
        """Update user password"""
        try:
            result = _get_db().users.update_one(
                {"email": email},
                {"$set": {
                    "password": new_password,
                    "updatedAt": datetime.utcnow()
                }}
            )
            cls.invalidate_cache()
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating password: {e}")
            return False
    
    def __repr__(self):
        return f"<User {self.email}>"
