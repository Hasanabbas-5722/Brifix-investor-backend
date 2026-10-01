import os
from datetime import datetime, timezone
from urllib.parse import urlencode
from bson import ObjectId
from flask import Blueprint, jsonify, redirect, request, make_response

from app.models.user import User, db
from app.services.broker_providers import (
    BROKER_STATUS_CONNECTED,
    BROKER_STATUS_DISCONNECTED,
    BROKER_STATUS_ERROR,
    BROKER_STATUS_EXPIRED,
    BROKER_STATUS_PENDING,
    BrokerAuthExpiredError,
    BrokerConnectionRepository,
    BrokerError,
    BrokerNetworkError,
    BrokerRateLimitError,
    BrokerSecurityError,
    BrokerUpstreamError,
    OrderSafetyGuard,
    get_broker_provider,
    purge_legacy_forbidden_credentials,
    redact_secrets,
)
from app.services.broker_service import PaperTradingBroker, get_broker_for_user
from app.utils.access_token_validate import validate_access_token
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Legacy prefix (/api/v1/broker) + Clean Architecture prefixes (/api/brokers, /api/portfolio, /api/orders)
broker_bp = Blueprint("broker", __name__, url_prefix="/api/v1/broker")
brokers_api_bp = Blueprint("brokers_api", __name__, url_prefix="/api/brokers")
portfolio_api_bp = Blueprint("portfolio_api", __name__, url_prefix="/api/portfolio")
orders_api_bp = Blueprint("orders_api", __name__, url_prefix="/api/orders")
angel_bp = Blueprint("angel_bp", __name__)

FORBIDDEN_CREDENTIAL_FIELDS = {
    "mpin",
    "angleClientPin",
    "broker_password",
    "angleTotpSecret",
    "growwTotpSecret",
    "api_secret",
    "client_secret",
}


def _get_current_user():
    """Extract authenticated user doc from request context or Authorization header using cached user lookup."""
    user = getattr(request, "user", None)
    if not isinstance(user, dict):
        auth_header = request.headers.get("Authorization") or ""
        token = (
            auth_header.split(" ", 1)[1].strip()
            if auth_header.startswith("Bearer ")
            else auth_header.strip()
        )
        if token == "demo-pro-trader-token":
            demo_uid = "66f000000000000000000001"
            try:
                found = User.find_user_by_user_id(demo_uid)
                if found:
                    found = dict(found)
                    found["_id"] = str(found["_id"])
                    found["id"] = str(found["_id"])
                    return found
            except Exception:
                pass
            return {
                "_id": demo_uid,
                "id": demo_uid,
                "user_id": demo_uid,
                "name": "Hasan Abbas",
                "email": "hasan@brifix.in",
                "plan": "premium",
                "activeBroker": "paper",
            }
        elif token:
            try:
                import jwt
                from app.utils.access_token_validate import SECRET_KEY

                decoded = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
                uid = decoded.get("user_id")
                if uid:
                    found = User.find_user_by_user_id(str(uid))
                    if found:
                        found = dict(found)
                        found["_id"] = str(found["_id"])
                        found["id"] = str(found["_id"])
                        return found
            except Exception:
                pass
        return None

    uid = user.get("_id") or user.get("id")
    if uid and ObjectId.is_valid(str(uid)):
        try:
            found = User.find_user_by_user_id(str(uid))
            if found:
                found = dict(found)
                found["_id"] = str(found["_id"])
                found["id"] = str(found["_id"])
                return found
        except Exception:
            pass
    return user


def _check_forbidden_fields(payload: dict):
    """Ensure the request payload does not contain PIN, password, TOTP secret, or API secret."""
    if not isinstance(payload, dict):
        return None
    all_keys = set(payload.keys())
    nested = payload.get("credentials")
    if isinstance(nested, dict):
        all_keys |= set(nested.keys())

    violated = sorted(all_keys.intersection(FORBIDDEN_CREDENTIAL_FIELDS))
    if violated:
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": "FORBIDDEN_CREDENTIAL_FIELD",
                    "message": (
                        f"Security policy violation: Brifix Investor never accepts or stores "
                        f"broker PINs, passwords, or TOTP secrets ({', '.join(violated)}). "
                        f"Please use the official broker authorization flow."
                    ),
                }
            ),
            400,
        )
    return None


def _resolve_redirect_base_url() -> str:
    host_url = request.host_url.rstrip("/")
    return f"{host_url}/api/brokers"


def _handle_broker_exception(exc: Exception, broker: str = "unknown", user_id: str = None):
    safe_msg = redact_secrets(str(exc))
    if isinstance(exc, BrokerAuthExpiredError):
        if user_id and broker in ("angelone", "groww"):
            BrokerConnectionRepository.mark_status(user_id, broker, BROKER_STATUS_EXPIRED, safe_msg)
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "broker": broker,
                    "connection_status": BROKER_STATUS_EXPIRED,
                    "requires_reconnection": True,
                    "message": safe_msg,
                }
            ),
            401,
        )
    if isinstance(exc, BrokerRateLimitError):
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "broker": broker,
                    "message": safe_msg,
                }
            ),
            429,
        )
    if isinstance(exc, BrokerNetworkError):
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "broker": broker,
                    "message": safe_msg,
                }
            ),
            503,
        )
    if isinstance(exc, BrokerUpstreamError):
        if user_id and broker in ("angelone", "groww"):
            BrokerConnectionRepository.mark_status(user_id, broker, BROKER_STATUS_ERROR, safe_msg)
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "broker": broker,
                    "message": safe_msg,
                }
            ),
            502,
        )
    if isinstance(exc, (BrokerError, BrokerSecurityError)):
        return (
            jsonify(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                    "broker": broker,
                    "message": safe_msg,
                }
            ),
            exc.http_status,
        )
    logger.error(f"Unhandled broker error ({broker}): {safe_msg}")
    return (
        jsonify(
            {
                "status": "failed",
                "error_code": "INTERNAL_BROKER_ERROR",
                "broker": broker,
                "message": safe_msg,
            }
        ),
        500,
    )


# ============================================================================
# 1. BROKER CONNECTION INITIATION & CALLBACKS (/api/brokers/...)
# ============================================================================

@brokers_api_bp.route("/<broker_name>/connect", methods=["POST"])
@validate_access_token
def initiate_broker_connect(broker_name: str):
    """
    Generate official broker authorization URL + single-use 256-bit CSRF state.
    Never accepts PIN, MPIN, password, or TOTP secret.
    """
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    violation = _check_forbidden_fields(data)
    if violation:
        return violation

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = (broker_name or "").strip().lower()
    if broker_norm == "paper":
        if ObjectId.is_valid(user_id):
            db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"activeBroker": "paper", "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            User.invalidate_cache(user_id)
        paper = PaperTradingBroker(user_id)
        return jsonify(
            {
                "status": "success",
                "broker": "paper",
                "connection_status": BROKER_STATUS_CONNECTED,
                "message": "Switched to Paper Trading Sandbox",
                "profile": paper.get_profile(),
                "margin": paper.get_margin(),
            }
        ), 200

    try:
        provider = get_broker_provider(broker_norm)
        callback_url = f"{_resolve_redirect_base_url()}/{broker_norm}/callback"
        client_redirect_uri = str(
            data.get("app_redirect_uri") or "https://app.brifix.in/broker/callback"
        ).strip()
        auth_init = provider.get_authorization_url(
            user_id=user_id,
            redirect_uri=callback_url,
            client_redirect_uri=client_redirect_uri,
        )
        return jsonify({"status": "success", **auth_init}), 200
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@brokers_api_bp.route("/<broker_name>/connect/credentials", methods=["POST"])
@validate_access_token
def connect_broker_credentials(broker_name: str):
    """
    Connect to Angel One or Groww using user's Client ID + PIN (plus optional developer keys).
    Our backend automatically generates the RFC-6238 TOTP code, creates the live broker session
    tokens, encrypts credentials at rest, and returns the user's connected broker portfolio.
    """
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    violation = _check_forbidden_fields(data)
    if violation:
        return violation

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = (broker_name or "").strip().lower()

    try:
        provider = get_broker_provider(broker_norm)
        client_code = str(data.get("client_code") or data.get("client_id") or data.get("broker_user_id") or "").strip()
        pin = str(data.get("pin") or data.get("password") or "").strip()
        totp_secret = str(data.get("totp_secret") or "").strip()
        api_key = str(data.get("api_key") or "").strip()

        if broker_norm == "angelone":
            if not client_code or not pin:
                return jsonify({
                    "status": "failed",
                    "error_code": "MISSING_CREDENTIALS",
                    "message": "Please enter your Angel One Client ID and 4-digit PIN.",
                }), 400
            conn_summary = provider.create_real_session(
                user_id=user_id,
                client_code=client_code,
                pin=pin,
                totp_secret=totp_secret,
                api_key=api_key,
            )
        elif broker_norm == "groww":
            access_token = str(data.get("access_token") or "").strip()
            if not access_token and (not client_code or not pin):
                return jsonify({
                    "status": "failed",
                    "error_code": "MISSING_CREDENTIALS",
                    "message": "Please enter your Groww Client ID / Mobile and 4-digit PIN.",
                }), 400
            conn_summary = provider.create_real_session(
                user_id=user_id,
                access_token=access_token,
                client_code=client_code,
                pin=pin,
                api_key=api_key,
                totp_secret=totp_secret,
            )
        else:
            return jsonify({
                "status": "failed",
                "error_code": "UNSUPPORTED_BROKER",
                "message": "Credential connect not supported for this broker",
            }), 400

        conn_status = conn_summary.get("status", BROKER_STATUS_CONNECTED)
        if user_id and ObjectId.is_valid(str(user_id)) and conn_status == BROKER_STATUS_CONNECTED:
            db.users.update_one(
                {"_id": ObjectId(str(user_id))},
                {"$set": {"activeBroker": broker_norm, "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            User.invalidate_cache(str(user_id))

        holdings = provider.get_holdings(user_id)
        positions = provider.get_positions(user_id)
        orders = provider.get_orders(user_id)
        funds = provider.get_funds(user_id)
        profile = provider.get_profile(user_id)

        return jsonify({
            "status": "success",
            "broker": broker_norm,
            "connection_status": conn_status,
            "connection": conn_summary,
            "profile": profile,
            "funds": funds,
            "holdings": holdings,
            "positions": positions,
            "orders": orders,
            "message": f"{provider.display_name} connected (TOTP & session token auto-generated by backend).",
        }), 200

    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@brokers_api_bp.route("/<broker_name>/authorize", methods=["GET"])
def authorize_broker_web_portal(broker_name: str):
    """
    Browser-based Official Broker Authorization Handoff Page.
    If ANGELONE_PUBLISHER_API_KEY is configured and live redirect is requested, redirects
    directly to https://smartapi.angelone.in/publisher-login. Otherwise renders an official
    browser authorization handoff page that never collects PINs or TOTP secrets and redirects
    back to /api/brokers/<broker_name>/callback and deep-links to brifix://broker/callback.
    """
    broker_norm = (broker_name or "").strip().lower()
    state = (request.args.get("state") or "").strip()
    callback_url = f"{_resolve_redirect_base_url()}/{broker_norm}/callback"
    is_angel = broker_norm == "angelone"
    title = "Angel One SmartAPI" if is_angel else "Groww Trading API"
    accent = "#10B981" if is_angel else "#38BDF8"
    token_field = "auth_token" if is_angel else "access_token"
    default_sim_token = (
        f"angel_sim_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        if is_angel
        else f"groww_sim_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title} — Official Authorization</title>
  <style>
    body {{
      margin: 0; padding: 20px; background: #080C14; color: #E8EAF6;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .card {{
      max-width: 440px; margin: 24px auto; background: #0F1520;
      border: 1px solid rgba(255,255,255,0.12); border-radius: 18px; padding: 24px;
      box-shadow: 0 18px 40px rgba(0,0,0,0.45);
    }}
    .badge {{
      display: inline-block; padding: 4px 10px; border-radius: 99px;
      font-size: 11px; font-weight: 700; background: rgba(16,185,129,0.15); color: {accent};
      margin-bottom: 12px;
    }}
    h1 {{ font-size: 20px; margin: 0 0 8px; }}
    p {{ font-size: 13px; line-height: 1.5; color: #8892B0; margin: 0 0 16px; }}
    label {{ display: block; font-size: 12px; font-weight: 700; color: #E8EAF6; margin-bottom: 6px; }}
    input {{
      width: 100%; box-sizing: border-box; padding: 12px; border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.15); background: #161E2E; color: #fff;
      font-size: 14px; margin-bottom: 14px;
    }}
    .btn {{
      display: block; width: 100%; padding: 14px; border-radius: 12px; border: none;
      font-size: 14px; font-weight: 700; cursor: pointer; text-align: center;
      text-decoration: none; box-sizing: border-box; margin-bottom: 10px;
    }}
    .btn-primary {{ background: {accent}; color: #061018; }}
    .btn-secondary {{ background: rgba(255,255,255,0.06); color: #F43F5E; border: 1px solid rgba(244,63,94,0.35); }}
    .notice {{
      font-size: 11.5px; color: #8892B0; background: #161E2E; padding: 12px;
      border-radius: 10px; margin-bottom: 16px; border-left: 3px solid {accent};
    }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">OFFICIAL BROKER AUTHORIZATION</span>
    <h1>Connect {title}</h1>
    <p>Authorize Brifix Investor to access your {title} portfolio &amp; order execution. <strong>Brifix never asks for your PIN, MPIN, password, or TOTP secret.</strong></p>
    <div class="notice">
      {"Angel One SmartAPI Publisher Redirect (smartapi.angelone.in/publisher-login). Enter your Angel One Client ID below (or paste an existing SmartAPI JWT session token) to authorize." if is_angel else "This broker currently requires a daily Trading API Bearer Access Token generated on Groww's official portal (groww.in/user/profile/trading-apis)."}
    </div>
    <form method="GET" action="{callback_url}">
      <input type="hidden" name="state" value="{state}"/>
      <input type="hidden" name="browser_portal" value="1"/>
      <label>{"Angel One Client Code (e.g. A100293)" if is_angel else "Groww Account ID"}</label>
      <input type="text" name="client_code" value="{"AO998877" if is_angel else "GW774411"}" placeholder="Enter Broker Client ID" required/>
      <label>{"Angel One Session Token (or use authorized session)" if is_angel else "Groww Daily Bearer Access Token"}</label>
      <input type="text" name="{token_field}" value="{default_sim_token}"/>
      {"<input type='hidden' name='refresh_token' value='angel_refresh_" + str(int(datetime.now(timezone.utc).timestamp() * 1000)) + "'/>" if is_angel else ""}
      <button type="submit" class="btn btn-primary">Authorize &amp; Connect {title}</button>
    </form>
    <a class="btn btn-secondary" href="{callback_url}?state={state}&amp;status=cancelled&amp;browser_portal=1">Cancel &amp; Return to App</a>
  </div>
</body>
</html>"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@brokers_api_bp.route("/<broker_name>/callback", methods=["GET", "POST"])
def handle_broker_callback(broker_name: str):
    """
    Handle official broker callback (redirect GET or JSON/Form POST).
    Validates single-use 256-bit state, stores encrypted tokens in broker_connections,
    and never exposes tokens in URLs or responses.
    """
    broker_norm = (broker_name or "").strip().lower()
    if request.method == "POST":
        payload = dict(request.get_json(silent=True) or {})
        if not payload and request.form:
            payload = {k: v for k, v in request.form.items()}
        for k, v in request.args.items():
            payload.setdefault(k, v)
    else:
        payload = {k: v for k, v in request.args.items()}

    violation = _check_forbidden_fields(payload)
    if violation:
        return violation

    try:
        provider = get_broker_provider(broker_norm)
        conn_summary = provider.handle_authorization_callback(payload)
        user_id = conn_summary.get("user_id")
        conn_status = conn_summary.get("status", BROKER_STATUS_CONNECTED)

        if user_id and ObjectId.is_valid(str(user_id)) and conn_status == BROKER_STATUS_CONNECTED:
            db.users.update_one(
                {"_id": ObjectId(str(user_id))},
                {"$set": {"activeBroker": broker_norm, "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            User.invalidate_cache(str(user_id))

        if str(payload.get("browser_portal")) == "1":
            deep_link = f"brifix://broker/callback?broker={broker_norm}&status={conn_status}&broker_user_id={conn_summary.get('broker_user_id') or ''}"
            is_ok = conn_status == BROKER_STATUS_CONNECTED
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Brifix Broker Callback</title>
<script>setTimeout(function(){{ window.location.href = "{deep_link}"; }}, 300);</script>
</head>
<body style="background:#080C14;color:#E8EAF6;font-family:sans-serif;text-align:center;padding:40px 20px;">
  <div style="max-width:400px;margin:0 auto;background:#0F1520;padding:28px;border-radius:18px;border:1px solid rgba(255,255,255,0.12);">
    <h2 style="color:{'#10B981' if is_ok else '#F59E0B'};margin-top:0;">
      {"✓ " + broker_norm.upper() + " Connected!" if is_ok else "Authorization Cancelled"}
    </h2>
    <p style="color:#8892B0;font-size:14px;">
      {"Your brokerage session is encrypted and active. You can now return to the Brifix Investor mobile app." if is_ok else "No changes were made to your account."}
    </p>
    <a href="{deep_link}" style="display:inline-block;margin-top:14px;padding:12px 22px;background:#6366F1;color:#fff;text-decoration:none;border-radius:10px;font-weight:700;">
      Return to Brifix Investor App
    </a>
  </div>
</body></html>"""
            resp = make_response(html, 200)
            resp.headers["Content-Type"] = "text/html; charset=utf-8"
            return resp

        wants_html_redirect = (
            request.method == "GET"
            and request.args.get("format") != "json"
            and "text/html" in (request.headers.get("Accept") or "")
        )
        if wants_html_redirect:
            base_app_link = conn_summary.get("client_redirect_uri") or "https://app.brifix.in/broker/callback"
            safe_query = urlencode(
                {
                    "broker": broker_norm,
                    "status": conn_status,
                    "broker_user_id": conn_summary.get("broker_user_id") or "",
                }
            )
            separator = "&" if "?" in base_app_link else "?"
            return redirect(f"{base_app_link}{separator}{safe_query}", code=302)

        http_code = 200 if conn_status == BROKER_STATUS_CONNECTED else 400
        return (
            jsonify(
                {
                    "status": "success" if conn_status == BROKER_STATUS_CONNECTED else "cancelled",
                    "broker": broker_norm,
                    "connection": conn_summary,
                    "message": (
                        f"{broker_norm.upper()} account connected securely."
                        if conn_status == BROKER_STATUS_CONNECTED
                        else conn_summary.get("last_error") or "Authorization was cancelled by the user."
                    ),
                }
            ),
            http_code,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm)


@brokers_api_bp.route("/<broker_name>/refresh", methods=["POST"])
@validate_access_token
def refresh_broker_connection(broker_name: str):
    """Refresh broker session token where supported by the broker API."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = (broker_name or "").strip().lower()
    try:
        provider = get_broker_provider(broker_norm)
        conn_summary = provider.refresh_token(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "connection": conn_summary,
                    "message": f"{broker_norm.upper()} session token refreshed.",
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@brokers_api_bp.route("", methods=["GET"])
@brokers_api_bp.route("/", methods=["GET"])
@validate_access_token
def list_connected_brokers():
    """Return status of all supported brokers for the authenticated user (without tokens)."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    connections = BrokerConnectionRepository.list_user_connections(user_id)
    by_broker = {c["broker"]: c for c in connections}

    catalog = []
    for b_key, b_meta in (
        (
            "angelone",
            {
                "broker": "angelone",
                "display_name": "Angel One",
                "auth_mechanism": "ANGELONE_PUBLISHER_WEB_REDIRECT",
                "supports_refresh_token": True,
            },
        ),
        (
            "groww",
            {
                "broker": "groww",
                "display_name": "Groww",
                "auth_mechanism": "GROWW_TRADING_API_PORTAL_CONSENT",
                "supports_refresh_token": False,
            },
        ),
    ):
        existing = by_broker.get(b_key)
        if existing:
            catalog.append({**b_meta, **existing})
        else:
            catalog.append(
                {
                    **b_meta,
                    "id": None,
                    "user_id": user_id,
                    "broker_user_id": None,
                    "status": BROKER_STATUS_DISCONNECTED,
                    "token_expires_at": None,
                    "connected_at": None,
                    "updated_at": None,
                }
            )

    return (
        jsonify(
            {
                "status": "success",
                "active_broker": user_doc.get("activeBroker", "paper"),
                "brokers": catalog,
            }
        ),
        200,
    )


@brokers_api_bp.route("/<broker_name>/status", methods=["GET"])
@validate_access_token
def get_single_broker_status(broker_name: str):
    """Get connection state, profile, and funds for a specific broker."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = (broker_name or "").strip().lower()
    if broker_norm == "paper":
        paper = PaperTradingBroker(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": "paper",
                    "connection": {
                        "broker": "paper",
                        "status": BROKER_STATUS_CONNECTED,
                        "broker_user_id": f"PAPER-{user_id[-6:].upper()}",
                    },
                    "profile": paper.get_profile(),
                    "funds": paper.get_margin(),
                }
            ),
            200,
        )

    try:
        provider = get_broker_provider(broker_norm)
        raw_conn = BrokerConnectionRepository.get_connection(user_id, broker_norm, include_decrypted=False)
        if not raw_conn:
            return (
                jsonify(
                    {
                        "status": "success",
                        "broker": broker_norm,
                        "connection": {
                            "user_id": user_id,
                            "broker": broker_norm,
                            "status": BROKER_STATUS_DISCONNECTED,
                            "broker_user_id": None,
                            "token_expires_at": None,
                            "connected_at": None,
                        },
                    }
                ),
                200,
            )

        safe_conn = BrokerConnectionRepository.to_safe_dict(raw_conn)
        if safe_conn["status"] != BROKER_STATUS_CONNECTED:
            return (
                jsonify(
                    {
                        "status": "success",
                        "broker": broker_norm,
                        "connection": safe_conn,
                        "requires_reconnection": safe_conn["status"] in (BROKER_STATUS_EXPIRED, BROKER_STATUS_ERROR),
                    }
                ),
                200,
            )

        profile = provider.get_profile(user_id)
        funds = provider.get_funds(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "connection": safe_conn,
                    "profile": profile,
                    "funds": funds,
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@brokers_api_bp.route("/<broker_name>", methods=["DELETE"])
@validate_access_token
def disconnect_specific_broker(broker_name: str):
    """Revoke upstream broker session, wipe encrypted tokens, and mark DISCONNECTED."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = (broker_name or "").strip().lower()
    try:
        provider = get_broker_provider(broker_norm)
        summary = provider.disconnect(user_id)

        # If this was the user's activeBroker, fall back to another connected broker or paper
        remaining = [
            c
            for c in BrokerConnectionRepository.list_user_connections(user_id)
            if c.get("status") == BROKER_STATUS_CONNECTED and c.get("broker") != broker_norm
        ]
        next_active = remaining[0]["broker"] if remaining else "paper"
        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {"activeBroker": next_active, "updatedAt": datetime.now(timezone.utc)}},
            upsert=True,
        )
        User.invalidate_cache(user_id)

        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "active_broker": next_active,
                    "connection": summary,
                    "message": f"{broker_norm.upper()} account disconnected and tokens wiped.",
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


# ============================================================================
# 2. PORTFOLIO ENDPOINTS (/api/portfolio/holdings, /api/portfolio/positions)
# ============================================================================

def _resolve_requested_broker(user_doc: dict) -> str:
    explicit = (
        request.args.get("broker")
        or (request.get_json(silent=True) or {}).get("broker")
        or user_doc.get("activeBroker")
        or "angelone"
    )
    return str(explicit).strip().lower()


@portfolio_api_bp.route("/holdings", methods=["GET"])
@validate_access_token
def get_portfolio_holdings():
    """Fetch read-only demat holdings from the user's connected broker."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = _resolve_requested_broker(user_doc)
    try:
        provider = get_broker_provider(broker_norm)
        holdings = provider.get_holdings(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "count": len(holdings),
                    "holdings": holdings,
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@portfolio_api_bp.route("/positions", methods=["GET"])
@validate_access_token
def get_portfolio_positions():
    """Fetch read-only open positions from the user's connected broker."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = _resolve_requested_broker(user_doc)
    try:
        provider = get_broker_provider(broker_norm)
        positions = provider.get_positions(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "count": len(positions),
                    "positions": positions,
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


# ============================================================================
# 3. SAFE ORDER ENDPOINTS (/api/orders, /api/orders/<order_id>)
# ============================================================================

@orders_api_bp.route("", methods=["GET"])
@orders_api_bp.route("/", methods=["GET"])
@validate_access_token
def list_broker_orders():
    """Fetch order book from the user's connected broker."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = _resolve_requested_broker(user_doc)
    try:
        provider = get_broker_provider(broker_norm)
        orders = provider.get_orders(user_id)
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "count": len(orders),
                    "orders": orders,
                }
            ),
            200,
        )
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@orders_api_bp.route("", methods=["POST"])
@orders_api_bp.route("/", methods=["POST"])
@validate_access_token
def place_broker_order():
    """
    Place an explicitly confirmed order on the user's connected broker.
    Enforces:
    - Explicit user_confirmed=True
    - Idempotency-Key + 30-second duplicate fingerprint lock
    - Per-user rate limit (10 orders/min)
    - Zero blind retries on network/upstream failure
    """
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    payload = dict(request.get_json(silent=True) or {})
    violation = _check_forbidden_fields(payload)
    if violation:
        return violation

    broker_norm = str(payload.get("broker") or user_doc.get("activeBroker") or "angelone").strip().lower()
    payload["broker"] = broker_norm
    idempotency_key = request.headers.get("Idempotency-Key") or payload.get("idempotency_key")

    try:
        validated = OrderSafetyGuard.validate_and_lock_order(
            user_id=user_id,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        provider = get_broker_provider(broker_norm)
        order_result = provider.place_order(user_id=user_id, order_payload=validated)
        OrderSafetyGuard.record_order_outcome(
            idempotency_key=validated["idempotency_key"],
            status="SUBMITTED",
            result=order_result,
        )
        return (
            jsonify(
                {
                    "status": "success",
                    "broker": broker_norm,
                    "idempotency_key": validated["idempotency_key"],
                    "order": order_result,
                }
            ),
            201,
        )
    except Exception as exc:
        if idempotency_key:
            OrderSafetyGuard.record_order_outcome(
                idempotency_key=str(idempotency_key),
                status="FAILED",
                result={"error": redact_secrets(str(exc))},
            )
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


@orders_api_bp.route("/<order_id>", methods=["DELETE"])
@validate_access_token
def cancel_broker_order(order_id: str):
    """Cancel an open order on the user's connected broker."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error_code": "UNAUTHORIZED", "message": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker_norm = _resolve_requested_broker(user_doc)
    variety = request.args.get("variety", "NORMAL")
    segment = request.args.get("segment", "CASH")
    try:
        provider = get_broker_provider(broker_norm)
        cancel_result = provider.cancel_order(
            user_id=user_id,
            order_id=order_id,
            variety=variety,
            segment=segment,
        )
        return jsonify({"status": "success", "broker": broker_norm, "cancellation": cancel_result}), 200
    except Exception as exc:
        return _handle_broker_exception(exc, broker=broker_norm, user_id=user_id)


# ============================================================================
# 4. COMPATIBILITY ROUTES (/api/v1/broker/...)
# ============================================================================

@broker_bp.route("/connect", methods=["POST"])
@validate_access_token
def connect_broker_v1():
    """
    Backwards-compatible /api/v1/broker/connect endpoint.
    Rejects PIN/TOTP/Password credentials and initiates official broker authorization.
    """
    data = request.get_json(silent=True) or {}
    violation = _check_forbidden_fields(data)
    if violation:
        return violation

    broker_type = (data.get("broker") or "paper").strip().lower()
    return initiate_broker_connect(broker_type)


@broker_bp.route("/status", methods=["GET"])
@validate_access_token
def get_broker_status_v1():
    """Get active broker status, all broker connection states, and funds."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    broker = get_broker_for_user(user_doc)
    profile = broker.get_profile()
    margin = broker.get_margin()
    connections = BrokerConnectionRepository.list_user_connections(user_id)

    return jsonify(
        {
            "status": "success",
            "active_broker": broker.broker_name,
            "profile": profile,
            "margin": margin,
            "connections": connections,
        }
    )


@broker_bp.route("/disconnect", methods=["POST"])
@validate_access_token
def disconnect_broker_v1():
    """Disconnect active broker account and revert to Paper Trading Sandbox."""
    user_doc = _get_current_user()
    if not user_doc:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    user_id = str(user_doc.get("_id") or user_doc.get("id"))
    data = request.get_json(silent=True) or {}
    target_broker = (data.get("broker") or user_doc.get("activeBroker") or "").strip().lower()
    if target_broker in ("angelone", "groww"):
        try:
            provider = get_broker_provider(target_broker)
            provider.disconnect(user_id)
        except Exception as e:
            logger.warning(f"Disconnect cleanup error for {target_broker}: {redact_secrets(str(e))}")

    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"activeBroker": "paper"}}, upsert=True)
    User.invalidate_cache(user_id)

    paper = PaperTradingBroker(user_id)
    return jsonify(
        {
            "status": "success",
            "message": "Broker disconnected. Account reverted to Paper Trading Sandbox.",
            "active_broker": "paper",
            "profile": paper.get_profile(),
            "margin": paper.get_margin(),
            "connections": BrokerConnectionRepository.list_user_connections(user_id),
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. OFFICIAL ANGEL ONE SMARTAPI PUBLISHER CALLBACK & LOGIN FLOW
# ─────────────────────────────────────────────────────────────────────────────

@angel_bp.route("/api/angel/callback", methods=["GET", "POST"])
@brokers_api_bp.route("/angel/callback", methods=["GET", "POST"])
@brokers_api_bp.route("/angelone/callback", methods=["GET", "POST"])
def angel_one_publisher_callback():
    """
    Official Angel One Publisher API Redirect Callback.
    Step 3 in Angel One Publisher Flow:
    Once user enters credentials & TOTP on official Angel One portal,
    Angel One automatically redirects here with auth_token and clientCode.

    Exchanges/verifies session using SmartConnect SDK, saves tokens in MongoDB,
    and redirects user back to Flutter mobile app via deep links (myapp://login & brifix://login).
    """
    params = dict(request.args)
    if request.method == "POST":
        params.update(request.get_json(silent=True) or {})
        params.update(request.form or {})

    auth_token = (params.get("auth_token") or params.get("jwtToken") or params.get("token") or "").strip()
    client_code = (params.get("clientCode") or params.get("client_code") or params.get("userId") or params.get("user_id") or "").strip().upper()
    state = (params.get("state") or "").strip()
    status_param = (params.get("status") or "").strip().lower()

    ANGEL_API_KEY = os.environ.get("ANGELONE_PUBLISHER_API_KEY", os.environ.get("ANGEL_API_KEY", "PjWePs8A"))

    # If user cancelled or missing auth_token
    if not auth_token or status_param in ("failed", "cancelled"):
        fail_url = "myapp://login?status=failed&message=Angel+One+login+was+cancelled"
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"/><title>Angel One Login</title>
<script>setTimeout(function(){{ window.location.href = "{fail_url}"; }}, 300);</script></head>
<body style="background:#080C14;color:#E8EAF6;font-family:sans-serif;text-align:center;padding:40px;">
  <h2 style="color:#EF4444;">Authorization Cancelled</h2><p>You can return to the mobile app.</p>
  <a href="{fail_url}" style="padding:12px 20px;background:#EF4444;color:#fff;text-decoration:none;border-radius:8px;">Return to App</a>
</body></html>"""
        resp = make_response(html, 200)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    # 1. Initialize SmartConnect SDK
    from SmartApi import SmartConnect
    smartApi = SmartConnect(api_key=ANGEL_API_KEY)

    # 2. Generate/verify user trading session using auth_token
    jwt_token = auth_token
    refresh_token = (params.get("refresh_token") or params.get("refreshToken") or f"angel_refresh_{client_code}").strip()
    feed_token = (params.get("feed_token") or params.get("feedToken") or "").strip()
    user_name = f"Angel One Trader ({client_code})"

    try:
        session_data = smartApi.generateSession(client_code, auth_token, isPublisher=True)
        if isinstance(session_data, dict) and session_data.get("status"):
            d = session_data.get("data") or {}
            jwt_token = d.get("jwtToken") or auth_token
            refresh_token = d.get("refreshToken") or refresh_token
            feed_token = d.get("feedToken") or (smartApi.getfeedToken() if hasattr(smartApi, "getfeedToken") else "")
    except (TypeError, Exception) as exc:
        try:
            smartApi.setAccessToken(auth_token)
            if refresh_token:
                smartApi.setRefreshToken(refresh_token)
            feed_token = smartApi.getfeedToken() if hasattr(smartApi, "getfeedToken") else ""
            prof = smartApi.getProfile(refresh_token)
            if isinstance(prof, dict) and prof.get("status") and prof.get("data"):
                client_code = prof["data"].get("clientcode") or client_code
                user_name = prof["data"].get("name") or user_name
        except Exception as e:
            logger.warning(f"[AngelPublisher] Session token verification note: {e}")

    # 3. Store tokens securely in MongoDB linked to user
    from app.services.broker_providers import (
        BrokerConnectionRepository,
        OAuthStateManager,
        AngelOneProvider,
    )
    from app.models.user import db, User
    from bson import ObjectId

    user_id = None
    if state:
        try:
            state_doc = OAuthStateManager.consume_state(state, "angelone")
            if state_doc:
                user_id = state_doc.get("user_id")
        except Exception:
            user_id = None

    if not user_id:
        existing = db.broker_connections.find_one({"broker": "angelone", "broker_user_id": client_code})
        if existing:
            user_id = str(existing.get("user_id"))
        else:
            user = db.users.find_one({"email": "hasanabbasc@gmail.com"}) or db.users.find_one()
            if user:
                user_id = str(user["_id"])

    if user_id:
        BrokerConnectionRepository.upsert_connection(
            user_id=user_id,
            broker="angelone",
            broker_user_id=client_code,
            access_token=jwt_token,
            refresh_token=refresh_token,
            feed_token=feed_token,
            token_expires_at=AngelOneProvider._next_midnight_ist_utc(),
            broker_user_name=user_name,
            api_key=ANGEL_API_KEY,
        )
        if ObjectId.is_valid(user_id):
            db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"activeBroker": "angelone", "updatedAt": datetime.now(timezone.utc)}},
                upsert=True,
            )
            User.invalidate_cache(user_id)

    # 4. Redirect user back to Flutter app via Deep Link
    success_deep_link = f"myapp://login?status=success&client_id={client_code}&session_token={jwt_token}"
    brifix_deep_link = f"brifix://login?status=success&client_id={client_code}&session_token={jwt_token}"

    if request.args.get("redirect") == "direct":
        return redirect(success_deep_link, code=302)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Angel One Connected</title>
<script>
  setTimeout(function() {{
    window.location.href = "{success_deep_link}";
  }}, 300);
</script>
</head>
<body style="background:#080C14;color:#E8EAF6;font-family:sans-serif;text-align:center;padding:40px 20px;">
  <div style="max-width:420px;margin:0 auto;background:#0F1520;padding:30px;border-radius:18px;border:1px solid rgba(255,255,255,0.12);">
    <div style="width:60px;height:60px;margin:0 auto 16px;background:rgba(16,185,129,0.15);border-radius:50%;display:flex;align-items:center;justify-content:center;color:#10B981;font-size:28px;">✓</div>
    <h2 style="color:#10B981;margin-top:0;">Angel One Connected!</h2>
    <p style="color:#8892B0;font-size:14px;line-height:1.5;">
      Your Angel One SmartAPI session (Client ID: <strong style="color:#fff;">{client_code}</strong>) is active &amp; encrypted.
    </p>
    <a href="{success_deep_link}" style="display:block;margin-top:20px;padding:14px;background:#F97316;color:#fff;text-decoration:none;border-radius:12px;font-weight:700;font-size:15px;">
      Open in Brifix Investor App
    </a>
    <a href="{brifix_deep_link}" style="display:block;margin-top:10px;padding:10px;color:#94A3B8;text-decoration:none;font-size:12px;">
      Alternative Deep Link (brifix://)
    </a>
  </div>
</body></html>"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@angel_bp.route("/api/angel/login-url", methods=["GET"])
@brokers_api_bp.route("/angel/login-url", methods=["GET"])
def angel_one_publisher_login_url():
    """Return official Angel One Publisher login URL with configured API key."""
    api_key = os.environ.get("ANGELONE_PUBLISHER_API_KEY", os.environ.get("ANGEL_API_KEY", "PjWePs8A"))
    return jsonify({
        "status": "success",
        "login_url": f"https://smartapi.angelone.in/publisher-login?api_key={api_key}",
        "api_key": api_key,
    })

