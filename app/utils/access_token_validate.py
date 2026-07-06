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

def validate_access_token(func):

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            logger.info(f"Access token validation wrapper: {args}")
            logger.info(f"Access token validation wrapper: {kwargs}")

            # Socket payload
            data = args[0] if args else {}

            logger.info(f"Access token validation data: {data}")

            auth_header = (
                data.get("accessToken")
                if data
                else request.headers.get("Authorization")
            )

            logger.info(f"Access token validation auth_header: {auth_header}")

            if not auth_header:
                return {"message": "Token missing"}, 401

            # Remove Bearer
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            else:
                token = auth_header

            logger.info(f"Access token validation token: {token}")

            # Decode JWT
            decoded = jwt.decode(
                token,
                SECRET_KEY,
                algorithms=["HS256"],
                options={"verify_signature": False}
            )

            logger.info(f"Access token validation decoded: {decoded}")

            # If DB call is async
            user_data = User.find_user_by_user_id(decoded["user_id"])

            if not user_data:
                return {"message": "Token is not valid"}, 401

            logger.info(f"Access token validation user_data: {user_data}")

            request.user = user_data

            # IMPORTANT
            return func(*args, **kwargs)

        except jwt.ExpiredSignatureError:
            logger.exception("Token expired")
            return {"message": "Token expired"}, 401

        except Exception as e:
            logger.exception(f"Token validation error: {e}")
            return {"message": "Invalid token"}, 401

    return wrapper