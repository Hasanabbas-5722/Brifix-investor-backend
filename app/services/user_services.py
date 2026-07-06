from app.socket import SmartAPISocket
from app.models.user import User
from app.utils.logger import get_logger
import bcrypt
from datetime import datetime, timedelta, timezone
import jwt

SECRET_KEY = "brifix_investors_backend_secret_key"

logger = get_logger(__name__)

now = datetime.now(timezone.utc)

def create_access_token(user):
    user_payload = {
        "user_id": str(user["_id"]),
        "email": user["email"],
        "isAdmin": user["isAdmin"],
        "exp" : now + timedelta(hours=2),
        "iat" : now,
    }
    token = jwt.encode(user_payload, SECRET_KEY, algorithm="HS256")

    logger.info(f"token: {token}")

    return token


class UserService:
    def __init__(self):
        pass

    @staticmethod
    def login(email, password):
        try:
            user_obj = User.find_by_email(email)
        
            logger.info(f"user_obj: {type(user_obj)}")
            if not user_obj:
                return {"data": {"message": "User not found"}}, 404

            # Assuming password is plain text for now if not starting with $2b$ or verify bcrypt
            stored_password = user_obj["password"]
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')
            hashed = bcrypt.checkpw(password.encode('utf-8'), stored_password)
            logger.info(f"hashed : {hashed}")
            if not hashed:
                return {"data": {"message": "Invalid password"}}, 401
            logger.info(f"user_obj: {user_obj}")
            

            smart_api_socket = SmartAPISocket()
            smart_api_data = smart_api_socket.on_connect(user_obj["angleClientCode"], user_obj["angleClientPin"], user_obj["angleTotpSecret"], user_obj["angleApiKey"])

            logger.info(f"smart_api_data: {smart_api_data}")
            
            if smart_api_data["data"]["jwtToken"] is None or smart_api_data["data"]["refreshToken"] is None or smart_api_data["data"]["feedToken"] is None:
                logger.info("Failed to login to Angle API")

            user_obj["angleJwtToken"] = smart_api_data["data"]["jwtToken"].split(" ")[1]
            user_obj["angleRefreshToken"] = smart_api_data["data"]["refreshToken"]
            user_obj["angleFeedToken"] = smart_api_data["data"]["feedToken"]

            jwt_token = create_access_token(user_obj)

            updated_user = User.update(user_obj["angleJwtToken"], user_obj["angleRefreshToken"], user_obj["angleFeedToken"], user_obj["_id"], jwt_token)
            logger.info(f"updated_user: {updated_user}")
            user_obj["accessToken"] = jwt_token
            if not updated_user:
                logger.info("Failed to update user")
                return {"data": {"message": "Failed to update user"}}, 500

            # Remove sensitive password hash from response
            user_obj.pop("password", None)

            return {
                "data": {
                    "message": "Users login successfull",
                    "data": [user_obj]
                }
            }, 200

        except Exception as e:
            logger.error(f"Error in login: {e}")
            return {"data": {"message": str(e)}}, 500
        
    @staticmethod
    def forgot_password(email, password):
        try:
            user_obj = User.find_by_email(email)
        
            if not user_obj:
                return {"data": {"message": "User not found"}}, 404
            
            # Update the user's password
            hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
            user_obj["password"] = hashed_password
            reset_password = User.update_password(email, hashed_password)

            if not reset_password:
                logger.info("Failed to reset password")
                return {"data": {"message": "Failed to reset password"}}, 500
            
            # Here you would typically send a password reset email to the user
            # For simplicity, we will just return a success message
            return {"data": {"message": "Password reset successfully"}}, 200

        except Exception as e:
            logger.error(f"Error in forgot_password: {e}")
            return {"data": {"message": str(e)}}, 500