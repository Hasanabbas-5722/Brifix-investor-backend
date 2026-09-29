from flask import Blueprint, request, jsonify
from app.utils.serialize import serialize_mongo
from app.services.user_services import UserService
from app.utils.access_token_validate import validate_access_token
from app.utils.logger import get_logger

logger = get_logger(__name__)

user_bp = Blueprint("user", __name__, url_prefix="/api/v1/users")


@user_bp.route("/otp/send", methods=["POST"])
def send_mobile_otp():
    """Send 6-digit OTP to user's mobile number for Brifix Investor login/registration."""
    data = request.get_json(silent=True) or {}
    phone = str(data.get("phone") or data.get("mobile") or "").strip()
    allow_dev_hint = (
        request.headers.get("X-Brifix-Test-Mode") == "1"
        or bool(data.get("sandbox_mode"))
    )
    response, status_code = UserService.send_otp(phone, allow_dev_hint=allow_dev_hint)
    return jsonify(serialize_mongo(response)), status_code


@user_bp.route("/otp/verify", methods=["POST"])
def verify_mobile_otp():
    """Verify 6-digit mobile OTP and return Brifix Investor JWT + user profile."""
    data = request.get_json(silent=True) or {}
    phone = str(data.get("phone") or data.get("mobile") or "").strip()
    otp = str(data.get("otp") or data.get("code") or "").strip()
    name = str(data.get("name") or "").strip() or None
    response, status_code = UserService.verify_otp(phone=phone, otp_code=otp, name=name)
    return jsonify(serialize_mongo(response)), status_code


@user_bp.route("/logout", methods=["POST"])
@validate_access_token
def user_logout():
    """Log out of Brifix Investor session while retaining encrypted broker connections."""
    user = getattr(request, "user", None) or {}
    user_id = str(user.get("_id") or user.get("id") or "")
    auth_header = request.headers.get("Authorization") or ""
    token = auth_header.split(" ", 1)[1].strip() if auth_header.startswith("Bearer ") else auth_header.strip()
    response, status_code = UserService.logout(user_id=user_id, token=token)
    return jsonify(response), status_code


@user_bp.route("/register", methods=["POST"])
def user_register():
    user_data = request.get_json(silent=True) or {}
    logger.info(f"user_register request for email={user_data.get('email')}")

    name = user_data.get("name", "").strip()
    email = user_data.get("email", "").strip()
    password = user_data.get("password", "")
    phone = user_data.get("phone", "")

    if not email or not password:
        return jsonify({"status": "failed", "message": "Email and password are required"}), 400

    if not name:
        name = email.split("@")[0]

    user_response, status_code = UserService.register(name, email, password, phone)
    user_response = serialize_mongo(user_response)
    return jsonify(user_response), status_code


@user_bp.route("/login", methods=["POST"])
def user_login():
    user_data = request.get_json(silent=True) or {}
    email = user_data.get("email") or user_data.get("username")
    password = user_data.get("password")

    if not email or not password:
        return jsonify({"message": "Missing email or password"}), 400

    logger.info(f"user_login request for email={email}")
    user_response, status_code = UserService().login(email, password)
    user_response = serialize_mongo(user_response)
    return jsonify(user_response), status_code


@user_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    user_data = request.get_json(silent=True) or {}
    email = user_data.get("email")
    password = user_data.get("password")

    if not email:
        return jsonify({"message": "Missing email"}), 400

    if not password:
        return jsonify({"message": "Missing password"}), 400

    user_response, status_code = UserService().forgot_password(email, password)
    return jsonify(user_response), status_code


@user_bp.route("/plan", methods=["GET"])
def get_user_plan():
    """Returns the current subscription plan for the user."""
    email = request.args.get("email")
    plan = "free"
    if email:
        try:
            from app.models.user import db
            user = db.users.find_one({"email": email})
            if user and user.get("plan"):
                plan = user["plan"]
        except Exception as e:
            logger.warning(f"Could not load plan for {email}: {e}")
    return jsonify({"status": "success", "plan": plan})


@user_bp.route("/plan", methods=["POST"])
def update_user_plan():
    """Updates the subscription plan for the user (free / pro / premium)."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan", "free").lower()
    if plan not in ("free", "pro", "premium"):
        return jsonify({"status": "failed", "error": "Invalid plan. Must be free, pro, or premium"}), 400

    email = data.get("email")
    if email:
        try:
            from app.models.user import db
            db.users.update_one({"email": email}, {"$set": {"plan": plan}}, upsert=True)
        except Exception as e:
            logger.warning(f"Could not update plan for {email}: {e}")

    return jsonify({"status": "success", "plan": plan, "message": f"Successfully updated to {plan} plan"})