from flask import Blueprint, request, jsonify
from bson import ObjectId
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.models.user import db
from app.services.broker_service import (
    encrypt_val,
    get_broker_for_user,
    AngelOneBroker,
    GrowwBroker,
    PaperTradingBroker
)

logger = get_logger(__name__)
broker_bp = Blueprint("broker", __name__, url_prefix="/api/v1/broker")


def _get_current_user():
    """Extract user doc from request context or token."""
    user = getattr(request, "user", None)
    if isinstance(user, dict):
        uid = user.get("_id") or user.get("id")
        if uid and ObjectId.is_valid(uid):
            return db.users.find_one({"_id": ObjectId(uid)}) or user
        return user

    # Fallback to query param or body email/userId if in dev
    email = request.args.get("email") or (request.get_json(silent=True) or {}).get("email")
    if email:
        return db.users.find_one({"email": email})
    return None


@broker_bp.route("/connect", methods=["POST"])
@validate_access_token
def connect_broker():
    """Verify broker credentials and set as active broker."""
    data = request.get_json(silent=True) or {}
    broker_type = (data.get("broker") or "paper").lower()
    creds = data.get("credentials") or {}

    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error": "Unauthorized or user not found"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))

    try:
        if broker_type == "angelone":
            code = (creds.get("client_code") or creds.get("angleClientCode") or "").strip()
            pin = str(creds.get("pin") or creds.get("angleClientPin") or "").strip()
            totp = (creds.get("totp_secret") or creds.get("angleTotpSecret") or "").strip()
            api_key = (creds.get("api_key") or creds.get("angleApiKey") or "").strip()

            if not all([code, pin, totp, api_key]):
                return jsonify({"status": "failed", "error": "All Angel One fields (Client Code, MPIN, TOTP Secret, API Key) are required"}), 400

            # Test connection live against Angel One
            test_broker = AngelOneBroker(code, pin, totp, api_key)
            prof = test_broker.get_profile()
            margin = test_broker.get_margin()

            # Encrypt and save
            update_doc = {
                "activeBroker": "angelone",
                "angleClientCode": code,
                "angleClientPin": encrypt_val(pin),
                "angleTotpSecret": encrypt_val(totp),
                "angleApiKey": encrypt_val(api_key)
            }
            db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update_doc})

            return jsonify({
                "status": "success",
                "message": f"Connected successfully to Angel One ({code})",
                "broker": "angelone",
                "profile": prof,
                "margin": margin
            })

        elif broker_type == "groww":
            api_key = (creds.get("api_key") or creds.get("growwApiKey") or "").strip()
            totp = (creds.get("totp_secret") or creds.get("growwTotpSecret") or "").strip()

            if not api_key or not totp:
                return jsonify({"status": "failed", "error": "Groww API Key and TOTP Secret are required"}), 400

            # Test connection
            test_broker = GrowwBroker(api_key, totp)
            prof = test_broker.get_profile()
            margin = test_broker.get_margin()

            update_doc = {
                "activeBroker": "groww",
                "growwApiKey": encrypt_val(api_key),
                "growwTotpSecret": encrypt_val(totp)
            }
            db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update_doc})

            return jsonify({
                "status": "success",
                "message": "Connected successfully to Groww",
                "broker": "groww",
                "profile": prof,
                "margin": margin
            })

        else:
            # Paper trading sandbox
            db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"activeBroker": "paper"}})
            paper = PaperTradingBroker(user_id)
            return jsonify({
                "status": "success",
                "message": "Switched to Paper Trading Sandbox (₹10,00,000 virtual balance)",
                "broker": "paper",
                "profile": paper.get_profile(),
                "margin": paper.get_margin()
            })

    except Exception as e:
        logger.error(f"Error connecting broker {broker_type}: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 400


@broker_bp.route("/status", methods=["GET"])
@validate_access_token
def get_broker_status():
    """Get active broker status and funds."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    broker = get_broker_for_user(user_doc)
    profile = broker.get_profile()
    margin = broker.get_margin()

    return jsonify({
        "status": "success",
        "active_broker": broker.broker_name,
        "profile": profile,
        "margin": margin
    })


@broker_bp.route("/disconnect", methods=["POST"])
@validate_access_token
def disconnect_broker():
    """Disconnect broker account and revert to safe Paper Trading mode."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"activeBroker": "paper"}})

    paper = PaperTradingBroker(user_id)
    return jsonify({
        "status": "success",
        "message": "Broker disconnected. Account reverted to Paper Trading Sandbox.",
        "active_broker": "paper",
        "profile": paper.get_profile(),
        "margin": paper.get_margin()
    })
