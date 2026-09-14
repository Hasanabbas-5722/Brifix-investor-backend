from app.utils.serialize import serialize_mongo
from app.services.user_services import UserService
from flask import Blueprint, request, jsonify
from app.utils.logger import get_logger

logger = get_logger(__name__)

user_bp = Blueprint("user", __name__, url_prefix="/api/v1/users")


@user_bp.route('/register', methods=['POST'])
def user_register():
    user_data = request.get_json(silent=True) or {}
    logger.info(f"user_register data: {user_data.get('email')}")

    name = (user_data.get("name") or user_data.get("fullName") or "").strip()
    email = user_data.get("email", "").strip().lower()
    username = (user_data.get("username") or "").strip().lower()
    password = user_data.get("password", "")
    phone = (user_data.get("phone") or user_data.get("mobile") or "").strip()
    current_plan = user_data.get("currentPlan") or user_data.get("plan") or "Free"
    trading_experience = user_data.get("tradingExperience", "Beginner")

    if not email or not password:
        return jsonify({"status": "failed", "message": "Email and password are required"}), 400

    if not name:
        name = username or email.split("@")[0]

    user_response, status_code = UserService.register(
        name=name,
        email=email,
        password=password,
        phone=phone,
        username=username,
        current_plan=current_plan,
        trading_experience=trading_experience
    )
    user_response = serialize_mongo(user_response)
    return jsonify(user_response), status_code


@user_bp.route('/login', methods=['POST'])
def user_login():
    user_data = request.get_json(silent=True) or {}
    logger.info(f"user_data: {user_data.get('email') or user_data.get('username')}")
    
    identifier = (user_data.get("email") or user_data.get("username") or user_data.get("identifier") or "").strip()
    password = user_data.get("password")

    if not identifier or not password:
        return jsonify({"message": "Missing email/username or password"}), 400
    
    user_response, status_code = UserService.login(identifier, password)
    user_response = serialize_mongo(user_response)
    return jsonify(user_response), status_code


@user_bp.route('/me', methods=['GET'])
def get_current_user():
    """Get current user details by token or email query param"""
    auth_header = request.headers.get("Authorization", "")
    email = request.args.get("email")
    user_data = None

    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            import jwt
            from app.services.user_services import SECRET_KEY
            decoded = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            if decoded and decoded.get("email"):
                from app.models.user import User
                user_data = User.find_by_email(decoded["email"])
        except Exception as e:
            logger.warning(f"Could not decode token: {e}")

    if not user_data and email:
        from app.models.user import User
        user_data = User.find_by_email(email)

    if not user_data:
        return jsonify({"status": "failed", "message": "User not found"}), 404

    user_data.pop("password", None)
    user_data = serialize_mongo(user_data)
    return jsonify({"status": "success", "data": user_data}), 200


@user_bp.route('/forgot-password', methods=['POST'])
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


@user_bp.route('/plan', methods=['GET'])
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


@user_bp.route('/plan', methods=['POST'])
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