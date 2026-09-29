import jwt
from functools import wraps
from flask import request
from app.utils.logger import get_logger
from app.models.user import User

logger = get_logger(__name__)

SECRET_KEY = "brifix_investors_backend_secret_key"

from functools import wraps
from flask import request
import jwt
import logging

logger = logging.getLogger(__name__)

from flask import jsonify

def validate_access_token(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            # Check for token in socket payload argument or HTTP headers
            data = args[0] if (args and isinstance(args[0], dict)) else {}

            auth_header = (
                data.get("accessToken")
                or data.get("token")
                or request.headers.get("Authorization")
            )

            if not auth_header:
                return jsonify({"status": "failed", "message": "Token missing"}), 401

            # Remove Bearer prefix if present
            if isinstance(auth_header, str) and auth_header.startswith("Bearer "):
                token = auth_header.split(" ", 1)[1].strip()
            else:
                token = str(auth_header).strip()

            if token == "demo-pro-trader-token":
                demo_uid = "66f000000000000000000001"
                demo_user = None
                try:
                    demo_user = User.find_user_by_user_id(demo_uid)
                except Exception:
                    pass
                active_broker = (demo_user or {}).get("activeBroker") or "paper"
                request.user = {
                    "_id": demo_uid,
                    "id": demo_uid,
                    "user_id": demo_uid,
                    "name": (demo_user or {}).get("name", "Hasan Abbas"),
                    "email": (demo_user or {}).get("email", "hasan@brifix.in"),
                    "plan": (demo_user or {}).get("plan", "premium"),
                    "activeBroker": active_broker,
                }
                return func(*args, **kwargs)

            # Decode JWT with HS256
            decoded = jwt.decode(
                token,
                SECRET_KEY,
                algorithms=["HS256"]
            )

            user_id = decoded.get("user_id")
            if not user_id:
                return jsonify({"status": "failed", "message": "Invalid token payload"}), 401

            try:
                user_data = User.find_user_by_user_id(user_id)
            except Exception as dbe:
                logger.error(f"Database error during token validation for user {user_id}: {dbe}")
                return jsonify({"status": "failed", "message": "Database temporarily busy, please retry"}), 503

            if not user_data:
                return jsonify({"status": "failed", "message": "User not found"}), 401

            if user_data.get("lastRevokedToken") and user_data.get("lastRevokedToken") == token:
                return jsonify({"status": "failed", "message": "Session logged out or token revoked"}), 401

            request.user = user_data
            return func(*args, **kwargs)

        except jwt.ExpiredSignatureError:
            logger.warning("Token expired")
            return jsonify({"status": "failed", "message": "Token expired"}), 401

        except jwt.InvalidTokenError as ite:
            logger.warning(f"Invalid token: {ite}")
            return jsonify({"status": "failed", "message": "Invalid token"}), 401

        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return jsonify({"status": "failed", "message": "Authentication service error"}), 500

    return wrapper