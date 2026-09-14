from app.socket import SmartAPISocket
from app.models.user import User
from app.utils.logger import get_logger
import bcrypt
from datetime import datetime, timedelta, timezone
import jwt

SECRET_KEY = "brifix_investors_backend_secret_key"

logger = get_logger(__name__)

def create_access_token(user):
    now = datetime.now(timezone.utc)
    user_payload = {
        "user_id": str(user["_id"]),
        "email": user.get("email", ""),
        "username": user.get("username", ""),
        "name": user.get("name", ""),
        "plan": user.get("currentPlan", user.get("plan", "Free")),
        "isAdmin": user.get("isAdmin", False),
        "exp": now + timedelta(days=7),  # 7-day token for good user experience
        "iat": now,
    }
    token = jwt.encode(user_payload, SECRET_KEY, algorithm="HS256")
    logger.info(f"Generated token for user {user.get('email')}")
    return token


class UserService:
    def __init__(self):
        pass

    @staticmethod
    def register(name, email, password, phone=None, username=None, current_plan="Free", trading_experience="Beginner", **kwargs):
        try:
            if not email or not password:
                return {"data": {"message": "Email and password are required"}}, 400

            clean_email = email.strip().lower()
            clean_username = (username or clean_email.split("@")[0]).strip().lower()

            if len(password) < 6:
                return {"data": {"message": "Password must be at least 6 characters long"}}, 400

            existing_email = User.find_by_email(clean_email)
            if existing_email:
                return {"data": {"message": "An account with this email already exists"}}, 409

            if username:
                existing_username = User.find_by_username(clean_username)
                if existing_username:
                    return {"data": {"message": "Username is already taken. Please choose another"}}, 409

            hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            new_user = User.create_user(
                name=name,
                email=clean_email,
                password_hash=hashed_password,
                phone=phone,
                username=clean_username,
                current_plan=current_plan,
                trading_experience=trading_experience,
                **kwargs
            )
            if not new_user:
                return {"data": {"message": "Failed to create user"}}, 500

            token = create_access_token(new_user)
            User.update_token(new_user["_id"], token)
            new_user["accessToken"] = token
            new_user.pop("password", None)

            return {
                "status": "success",
                "data": {
                    "message": "User registered successfully",
                    "data": [new_user]
                }
            }, 201
        except Exception as e:
            logger.error(f"Error in register: {e}")
            return {"data": {"message": str(e)}}, 500

    @staticmethod
    def login(identifier, password):
        try:
            if not identifier or not password:
                return {"data": {"message": "Email/username and password are required"}}, 400

            user_obj = User.find_by_email_or_username(identifier)

            if not user_obj:
                return {"data": {"message": "No account found with this email or username"}}, 404

            stored_password = user_obj.get("password")
            if not stored_password:
                return {"data": {"message": "Invalid password"}}, 401

            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')

            try:
                hashed = bcrypt.checkpw(password.encode('utf-8'), stored_password)
            except Exception:
                # Fallback in case plain text password was saved historically
                hashed = (password == user_obj.get("password"))

            if not hashed:
                return {"data": {"message": "Incorrect password. Please try again"}}, 401

            # Check if Angel One credentials exist
            has_angle_creds = all([
                user_obj.get("angleClientCode"),
                user_obj.get("angleClientPin"),
                user_obj.get("angleTotpSecret"),
                user_obj.get("angleApiKey")
            ])

            if has_angle_creds:
                try:
                    smart_api_socket = SmartAPISocket()
                    smart_api_data = smart_api_socket.on_connect(
                        user_obj["angleClientCode"],
                        user_obj["angleClientPin"],
                        user_obj["angleTotpSecret"],
                        user_obj["angleApiKey"]
                    )
                    if isinstance(smart_api_data, dict) and smart_api_data.get("status") and smart_api_data.get("data"):
                        jwt_val = smart_api_data["data"].get("jwtToken", "")
                        user_obj["angleJwtToken"] = jwt_val.split(" ")[1] if " " in jwt_val else jwt_val
                        user_obj["angleRefreshToken"] = smart_api_data["data"].get("refreshToken", "")
                        user_obj["angleFeedToken"] = smart_api_data["data"].get("feedToken", "")
                except Exception as ex:
                    logger.warning(f"Could not connect to Angel One broker: {ex}")

            jwt_token = create_access_token(user_obj)
            user_obj["accessToken"] = jwt_token

            if user_obj.get("angleJwtToken"):
                User.update(
                    user_obj.get("angleJwtToken", ""),
                    user_obj.get("angleRefreshToken", ""),
                    user_obj.get("angleFeedToken", ""),
                    user_obj["_id"],
                    jwt_token
                )
            else:
                User.update_token(user_obj["_id"], jwt_token)

            # Remove sensitive password hash from response
            user_obj.pop("password", None)

            return {
                "status": "success",
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