from app.utils.serialize import serialize_mongo
from app.services.user_services import UserService
from flask import Blueprint, request, jsonify
from app.utils.logger import get_logger

logger = get_logger(__name__)

user_bp = Blueprint("user", __name__, url_prefix="/api/v1/users")


@user_bp.route('/login', methods=['POST'])
def user_login():
    user_data = request.get_json(silent=True) or {}
    logger.info(f"user_data: {user_data}")
    
    email = user_data.get("email") or user_data.get("username")
    password = user_data.get("password")

    if not email or not password:
        return jsonify({"message": "Missing email or password"}), 400
    
    user_response, status_code = UserService().login(email, password)
    logger.info(f"user_response: {user_response}, status_code: {status_code}")
    user_response = serialize_mongo(user_response)
    logger.info(f"Serialized user_response: {user_response}")   
    return jsonify(user_response), status_code


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