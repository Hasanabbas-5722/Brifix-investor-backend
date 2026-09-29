from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import secrets
import bcrypt
import jwt
from bson import ObjectId

from app.models.user import User, db
from app.services.broker_providers import BrokerConnectionRepository, redact_secrets
from app.utils.logger import get_logger

SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "brifix_investors_backend_secret_key")

logger = get_logger(__name__)


def create_access_token(user):
    now = datetime.now(timezone.utc)
    user_payload = {
        "user_id": str(user["_id"]),
        "email": user.get("email", ""),
        "phone": user.get("phone", ""),
        "isAdmin": user.get("isAdmin", False),
        "exp": now + timedelta(days=7),
        "iat": now,
        "jti": secrets.token_hex(8),
    }
    token = jwt.encode(user_payload, SECRET_KEY, algorithm="HS256")
    logger.info(f"Generated Brifix session token for user_id={user.get('_id')}")
    return token


class UserService:
    OTP_TTL_SECONDS = 300
    MAX_OTP_ATTEMPTS = 5

    def __init__(self):
        pass

    @staticmethod
    def _hash_otp(norm_phone: str, otp_code: str) -> str:
        return hashlib.sha256(f"{norm_phone}:{otp_code}:{SECRET_KEY}".encode("utf-8")).hexdigest()

    @classmethod
    def send_otp(cls, phone: str, allow_dev_hint: bool = False):
        """
        Initiate Mobile Number -> OTP -> Brifix Investor Account flow.
        Stores only the salted SHA-256 OTP hash in db.user_otps with a 5-minute TTL.
        """
        norm_phone = User.normalize_phone(phone)
        if not norm_phone or len(norm_phone) != 10 or norm_phone[0] not in "6789":
            return {
                "status": "failed",
                "error_code": "INVALID_PHONE",
                "message": "Please enter a valid 10-digit Indian mobile number.",
            }, 400

        now = datetime.now(timezone.utc)
        otp_code = f"{secrets.randbelow(900000) + 100000:06d}"
        otp_hash = cls._hash_otp(norm_phone, otp_code)
        expires_at = now + timedelta(seconds=cls.OTP_TTL_SECONDS)

        existing_user = User.find_by_phone(norm_phone)
        db.user_otps.update_one(
            {"phone": norm_phone},
            {
                "$set": {
                    "phone": norm_phone,
                    "otp_hash": otp_hash,
                    "attempts": 0,
                    "consumed": False,
                    "created_at": now,
                    "expires_at": expires_at,
                }
            },
            upsert=True,
        )

        logger.info(f"OTP dispatched to mobile +91-XXXXXX{norm_phone[-4:]} (existing_user={bool(existing_user)})")
        resp = {
            "status": "success",
            "phone": norm_phone,
            "masked_phone": f"+91-XXXXXX{norm_phone[-4:]}",
            "is_existing_user": bool(existing_user),
            "expires_in_seconds": cls.OTP_TTL_SECONDS,
            "message": f"6-digit verification OTP sent to +91-XXXXXX{norm_phone[-4:]}",
        }
        if allow_dev_hint and os.environ.get("FLASK_ENV", "development") != "production":
            resp["sandbox_otp"] = otp_code
        return resp, 200

    @classmethod
    def verify_otp(cls, phone: str, otp_code: str, name: str = None):
        """
        Verify 6-digit mobile OTP and either sign in existing Brifix Investor user
        or provision a new Brifix Investor account.
        """
        norm_phone = User.normalize_phone(phone)
        otp_clean = "".join(ch for ch in str(otp_code or "") if ch.isdigit())
        if not norm_phone or len(norm_phone) != 10:
            return {
                "status": "failed",
                "error_code": "INVALID_PHONE",
                "message": "Please provide a valid 10-digit mobile number.",
            }, 400
        if len(otp_clean) != 6:
            return {
                "status": "failed",
                "error_code": "INVALID_OTP_FORMAT",
                "message": "OTP must be a 6-digit numeric code.",
            }, 400

        record = db.user_otps.find_one({"phone": norm_phone})
        if not record or record.get("consumed"):
            return {
                "status": "failed",
                "error_code": "OTP_NOT_FOUND",
                "message": "No active OTP found for this mobile number. Please request a new OTP.",
            }, 400

        now = datetime.now(timezone.utc)
        expires_at = record.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not expires_at or expires_at <= now:
            return {
                "status": "failed",
                "error_code": "OTP_EXPIRED",
                "message": "OTP has expired. Please request a new OTP.",
            }, 400

        attempts = int(record.get("attempts") or 0)
        if attempts >= cls.MAX_OTP_ATTEMPTS:
            return {
                "status": "failed",
                "error_code": "OTP_MAX_ATTEMPTS",
                "message": "Maximum OTP verification attempts exceeded. Please request a new OTP.",
            }, 429

        expected_hash = str(record.get("otp_hash") or "")
        candidate_hash = cls._hash_otp(norm_phone, otp_clean)
        if not hmac.compare_digest(expected_hash, candidate_hash):
            db.user_otps.update_one({"phone": norm_phone}, {"$inc": {"attempts": 1}})
            return {
                "status": "failed",
                "error_code": "INVALID_OTP",
                "message": "Invalid OTP code. Please check and try again.",
            }, 401

        # Mark OTP consumed
        db.user_otps.update_one(
            {"phone": norm_phone},
            {"$set": {"consumed": True, "consumed_at": now}, "$unset": {"otp_hash": ""}},
        )

        # Lookup or create user
        user_obj = User.find_by_phone(norm_phone)
        is_new_user = False
        if not user_obj:
            is_new_user = True
            display_name = (name or "").strip() or f"Trader {norm_phone[-4:]}"
            synthetic_email = f"m.{norm_phone}@users.brifix.in"
            random_pw_hash = bcrypt.hashpw(secrets.token_urlsafe(24).encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            user_obj = User.create_user(
                name=display_name,
                email=synthetic_email,
                password_hash=random_pw_hash,
                phone=norm_phone,
            )
            if not user_obj:
                return {"status": "failed", "message": "Failed to create Brifix account"}, 500
            user_obj["_id"] = str(user_obj["_id"])
            user_obj["id"] = str(user_obj["_id"])

        jwt_token = create_access_token(user_obj)
        db.users.update_one(
            {"_id": ObjectId(str(user_obj["_id"]))},
            {
                "$set": {
                    "accessToken": jwt_token,
                    "updatedAt": datetime.utcnow(),
                },
                "$unset": {"lastRevokedToken": ""},
            },
        )
        User.invalidate_cache(user_obj["_id"])

        safe_user = User.sanitize_user_doc(user_obj)
        safe_user["accessToken"] = jwt_token
        safe_user["is_new_user"] = is_new_user
        safe_user["broker_connections"] = BrokerConnectionRepository.list_user_connections(str(user_obj["_id"]))

        return {
            "status": "success",
            "is_new_user": is_new_user,
            "data": {
                "message": "Account created and logged in" if is_new_user else "Logged in successfully",
                "is_new_user": is_new_user,
                "data": [safe_user],
            },
        }, (201 if is_new_user else 200)

    @staticmethod
    def logout(user_id: str, token: str = None):
        """Invalidate the current Brifix session token while keeping broker_connections intact."""
        if not user_id or not ObjectId.is_valid(str(user_id)):
            return {"status": "failed", "message": "Invalid user session"}, 400
        update_fields = {"updatedAt": datetime.utcnow()}
        if token:
            update_fields["lastRevokedToken"] = token
        db.users.update_one(
            {"_id": ObjectId(str(user_id))},
            {"$set": update_fields, "$unset": {"accessToken": ""}},
        )
        User.invalidate_cache(user_id)
        return {
            "status": "success",
            "message": "Logged out of Brifix Investor. Connected broker tokens remain encrypted at rest.",
        }, 200

    @staticmethod
    def register(name, email, password, phone=None):
        try:
            if not email or not password:
                return {"data": {"message": "Email and password are required"}}, 400

            existing = User.find_by_email(email)
            if existing:
                return {"data": {"message": "User with this email already exists"}}, 409

            hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            new_user = User.create_user(name=name, email=email, password_hash=hashed_password, phone=phone)
            if not new_user:
                return {"data": {"message": "Failed to create user"}}, 500

            token = create_access_token(new_user)
            User.update_token(new_user["_id"], token)
            safe_user = User.sanitize_user_doc(new_user)
            safe_user["accessToken"] = token
            safe_user["is_new_user"] = True
            safe_user["broker_connections"] = []

            return {
                "status": "success",
                "data": {
                    "message": "User registered successfully",
                    "data": [safe_user],
                },
            }, 201
        except Exception as e:
            logger.error(f"Error in register: {redact_secrets(str(e))}")
            return {"data": {"message": redact_secrets(str(e))}}, 500

    @staticmethod
    def login(email, password):
        try:
            user_obj = User.find_by_email(email)

            if not user_obj:
                return {"data": {"message": "User not found"}}, 404

            stored_password = user_obj.get("password")
            if not stored_password:
                return {"data": {"message": "Invalid password"}}, 401

            if isinstance(stored_password, str):
                stored_password = stored_password.encode("utf-8")

            try:
                hashed = bcrypt.checkpw(password.encode("utf-8"), stored_password)
            except Exception:
                hashed = password == user_obj.get("password")

            if not hashed:
                return {"data": {"message": "Invalid password"}}, 401

            jwt_token = create_access_token(user_obj)
            db.users.update_one(
                {"_id": ObjectId(str(user_obj["_id"]))},
                {
                    "$set": {
                        "accessToken": jwt_token,
                        "updatedAt": datetime.utcnow(),
                    },
                    "$unset": {
                        "lastRevokedToken": "",
                        "angleClientPin": "",
                        "angleTotpSecret": "",
                        "growwTotpSecret": "",
                    },
                },
            )
            User.invalidate_cache(user_obj["_id"])

            safe_user = User.sanitize_user_doc(user_obj)
            safe_user["accessToken"] = jwt_token
            safe_user["is_new_user"] = False
            safe_user["broker_connections"] = BrokerConnectionRepository.list_user_connections(str(user_obj["_id"]))

            return {
                "status": "success",
                "data": {
                    "message": "Users login successfull",
                    "data": [safe_user],
                },
            }, 200

        except Exception as e:
            logger.error(f"Error in login: {redact_secrets(str(e))}")
            return {"data": {"message": redact_secrets(str(e))}}, 500

    @staticmethod
    def forgot_password(email, password):
        try:
            user_obj = User.find_by_email(email)

            if not user_obj:
                return {"data": {"message": "User not found"}}, 404

            hashed_password = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
            reset_password = User.update_password(email, hashed_password)

            if not reset_password:
                logger.info("Failed to reset password")
                return {"data": {"message": "Failed to reset password"}}, 500

            return {"data": {"message": "Password reset successfully"}}, 200

        except Exception as e:
            logger.error(f"Error in forgot_password: {redact_secrets(str(e))}")
            return {"data": {"message": redact_secrets(str(e))}}, 500