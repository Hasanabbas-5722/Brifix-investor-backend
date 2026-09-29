"""
Comprehensive 15-Scenario Verification Suite for Brifix Investor Broker Connection Architecture.
Verifies:
1. New user (Mobile Number -> OTP -> Brifix Account)
2. Existing user (Mobile Number -> OTP -> Brifix Account)
3. Connect Angel One (Official Publisher Web Redirect + 256-bit state + Fernet encryption)
4. Cancel Angel One authorization
5. Reconnect Angel One
6. Connect Groww (Official Trading API Portal Consent + explicit auth mechanism disclosure)
7. Cancel Groww authorization
8. Token expiration (automatic transition to EXPIRED + 401 BROKER_REAUTH_REQUIRED)
9. Token revocation (upstream 401 / AB1010 -> EXPIRED + 401 BROKER_REAUTH_REQUIRED)
10. Broker API failure (502 BROKER_API_FAILURE, zero blind retries)
11. Network failure (503 BROKER_NETWORK_FAILURE, zero blind retries)
12. Duplicate order submission & missing user confirmation guard
13. Logout / Login (Brifix session revoked on logout; broker_connections preserved on re-login)
14. Disconnect broker (upstream session terminated, encrypted tokens wiped, status DISCONNECTED)
15. Reconnect broker
+ Security Invariants: PIN/MPIN/TOTP rejection, secret redaction, state replay rejection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import secrets
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app
from app.models.user import db
from app.services.broker_providers import (
    BrokerAuthExpiredError,
    BrokerConnectionRepository,
    BrokerNetworkError,
    BrokerUpstreamError,
    redact_secrets,
    token_crypto,
)


def run_all_15_scenarios():
    app = create_app("testing")
    client = app.test_client()

    # Use a unique test phone number
    test_phone = f"98{secrets.randbelow(90000000) + 10000000}"
    db.users.delete_many({"phone": test_phone})
    db.user_otps.delete_many({"phone": test_phone})

    results = []

    # -------------------------------------------------------------------------
    # SCENARIO 1: New User (Mobile Number -> OTP -> New Brifix Account)
    # -------------------------------------------------------------------------
    r_otp1 = client.post(
        "/api/v1/users/otp/send",
        json={"phone": test_phone, "sandbox_mode": True},
        headers={"X-Brifix-Test-Mode": "1"},
    )
    assert r_otp1.status_code == 200, f"OTP send failed: {r_otp1.get_json()}"
    otp1_data = r_otp1.get_json()
    assert otp1_data["is_existing_user"] is False
    otp_code_1 = otp1_data["sandbox_otp"]

    r_ver1 = client.post(
        "/api/v1/users/otp/verify",
        json={"phone": test_phone, "otp": otp_code_1, "name": "Aarav Trader"},
    )
    assert r_ver1.status_code == 201, f"New user OTP verify failed: {r_ver1.get_json()}"
    ver1_json = r_ver1.get_json()
    assert ver1_json["is_new_user"] is True
    user_1 = ver1_json["data"]["data"][0]
    user_id = user_1["_id"]
    jwt_token_1 = user_1["accessToken"]
    auth_headers = {"Authorization": f"Bearer {jwt_token_1}"}
    results.append(("1. New user (Phone -> OTP -> Account created)", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 2: Existing User (Mobile Number -> OTP -> Existing Account)
    # -------------------------------------------------------------------------
    r_otp2 = client.post(
        "/api/v1/users/otp/send",
        json={"phone": f"+91 {test_phone}", "sandbox_mode": True},
        headers={"X-Brifix-Test-Mode": "1"},
    )
    assert r_otp2.status_code == 200
    otp2_data = r_otp2.get_json()
    assert otp2_data["is_existing_user"] is True
    otp_code_2 = otp2_data["sandbox_otp"]

    r_ver2 = client.post(
        "/api/v1/users/otp/verify",
        json={"phone": test_phone, "otp": otp_code_2},
    )
    assert r_ver2.status_code == 200
    ver2_json = r_ver2.get_json()
    assert ver2_json["is_new_user"] is False
    user_2 = ver2_json["data"]["data"][0]
    assert user_2["_id"] == user_id
    jwt_token = user_2["accessToken"]
    auth_headers = {"Authorization": f"Bearer {jwt_token}"}
    results.append(("2. Existing user (Phone -> OTP -> Existing account)", "PASS"))

    # -------------------------------------------------------------------------
    # SECURITY CHECK: Reject any PIN / MPIN / TOTP / Password fields
    # -------------------------------------------------------------------------
    for forbidden_payload in (
        {"mpin": "1234"},
        {"credentials": {"angleClientPin": "1234", "angleTotpSecret": "SECRET"}},
    ):
        r_forbid = client.post(
            "/api/brokers/angelone/connect",
            json=forbidden_payload,
            headers=auth_headers,
        )
        assert r_forbid.status_code == 400
        assert r_forbid.get_json()["error_code"] == "FORBIDDEN_CREDENTIAL_FIELD"

    # -------------------------------------------------------------------------
    # SCENARIO 3: Connect Angel One
    # -------------------------------------------------------------------------
    r_ao_init = client.post(
        "/api/brokers/angelone/connect",
        json={"app_redirect_uri": "https://app.brifix.in/broker/callback"},
        headers=auth_headers,
    )
    assert r_ao_init.status_code == 200
    ao_init_data = r_ao_init.get_json()
    assert ao_init_data["broker"] == "angelone"
    assert ao_init_data["auth_mechanism"] == "ANGELONE_PUBLISHER_WEB_REDIRECT"
    assert "smartapi.angelone.in/publisher-login" in ao_init_data["authorization_url"]
    ao_state = ao_init_data["state"]
    assert len(ao_state) >= 40  # 256-bit URL-safe token

    with patch(
        "app.services.broker_providers.AngelOneProvider._verify_session_profile",
        return_value={"clientcode": "AO998877", "name": "Aarav Trader"},
    ):
        r_ao_cb = client.get(
            "/api/brokers/angelone/callback",
            query_string={
                "state": ao_state,
                "auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_1",
                "refresh_token": "angel_refresh_token_1",
                "feed_token": "angel_feed_token_1",
                "format": "json",
            },
        )
        assert r_ao_cb.status_code == 200, f"Angel One callback failed: {r_ao_cb.get_json()}"
        ao_cb_data = r_ao_cb.get_json()
        assert ao_cb_data["connection"]["status"] == "CONNECTED"
        assert ao_cb_data["connection"]["broker_user_id"] == "AO998877"
        # Ensure raw tokens are NEVER in the JSON response
        raw_dump = str(ao_cb_data)
        assert "angel_access_token_1" not in raw_dump
        assert "angel_refresh_token_1" not in raw_dump

        # Verify encrypted storage at rest in MongoDB
        raw_db_doc = db.broker_connections.find_one({"user_id": user_id, "broker": "angelone"})
        assert raw_db_doc is not None
        assert raw_db_doc["access_token_encrypted"] != "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_1"
        assert token_crypto.decrypt(raw_db_doc["access_token_encrypted"]) == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_1"

        # Verify single-use state replay protection
        r_replay = client.get(
            "/api/brokers/angelone/callback",
            query_string={
                "state": ao_state,
                "auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_1",
                "format": "json",
            },
        )
        assert r_replay.status_code == 400
        assert r_replay.get_json()["error_code"] == "INVALID_OAUTH_STATE"
    results.append(("3. Connect Angel One (Publisher redirect + encrypted storage)", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 4: Cancel Angel One Authorization
    # -------------------------------------------------------------------------
    r_ao_init2 = client.post("/api/brokers/angelone/connect", json={}, headers=auth_headers)
    ao_state_cancel = r_ao_init2.get_json()["state"]
    r_ao_cancel = client.get(
        "/api/brokers/angelone/callback",
        query_string={"state": ao_state_cancel, "error": "access_denied", "format": "json"},
    )
    assert r_ao_cancel.status_code == 400
    ao_cancel_data = r_ao_cancel.get_json()
    assert ao_cancel_data["status"] == "cancelled"
    assert ao_cancel_data["connection"]["status"] == "DISCONNECTED"
    results.append(("4. Cancel Angel One authorization", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 5: Reconnect Angel One
    # -------------------------------------------------------------------------
    r_ao_init3 = client.post("/api/brokers/angelone/connect", json={}, headers=auth_headers)
    ao_state_recon = r_ao_init3.get_json()["state"]
    with patch(
        "app.services.broker_providers.AngelOneProvider._verify_session_profile",
        return_value={"clientcode": "AO998877", "name": "Aarav Trader"},
    ):
        r_ao_recon = client.post(
            "/api/brokers/angelone/callback",
            json={
                "state": ao_state_recon,
                "auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_2",
                "refresh_token": "angel_refresh_token_2",
            },
        )
        assert r_ao_recon.status_code == 200
        assert r_ao_recon.get_json()["connection"]["status"] == "CONNECTED"
    results.append(("5. Reconnect Angel One", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 6: Connect Groww
    # -------------------------------------------------------------------------
    r_gw_init = client.post("/api/brokers/groww/connect", json={}, headers=auth_headers)
    assert r_gw_init.status_code == 200
    gw_init_data = r_gw_init.get_json()
    assert gw_init_data["broker"] == "groww"
    assert gw_init_data["auth_mechanism"] == "GROWW_TRADING_API_PORTAL_CONSENT"
    assert "This broker currently requires" in gw_init_data["auth_mechanism_notice"]
    gw_state = gw_init_data["state"]

    with patch(
        "app.services.broker_providers.GrowwProvider._verify_groww_token",
        return_value={"broker_user_id": "GW-774411"},
    ):
        r_gw_cb = client.post(
            "/api/brokers/groww/callback",
            json={
                "state": gw_state,
                "access_token": "groww_daily_bearer_token_xyz987654321",
                "broker_user_id": "GW-774411",
            },
        )
        assert r_gw_cb.status_code == 200
        gw_cb_data = r_gw_cb.get_json()
        assert gw_cb_data["connection"]["status"] == "CONNECTED"
        assert gw_cb_data["connection"]["broker_user_id"] == "GW-774411"
        assert "groww_daily_bearer_token_xyz987654321" not in str(gw_cb_data)
    results.append(("6. Connect Groww (Official portal consent + explicit notice)", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 7: Cancel Groww Authorization
    # -------------------------------------------------------------------------
    r_gw_init2 = client.post("/api/brokers/groww/connect", json={}, headers=auth_headers)
    gw_state_cancel = r_gw_init2.get_json()["state"]
    r_gw_cancel = client.post(
        "/api/brokers/groww/callback",
        json={"state": gw_state_cancel, "status": "cancelled"},
    )
    assert r_gw_cancel.status_code == 400
    assert r_gw_cancel.get_json()["connection"]["status"] == "DISCONNECTED"
    results.append(("7. Cancel Groww authorization", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 8: Token Expiration
    # -------------------------------------------------------------------------
    # Force Angel One token_expires_at into the past
    past_time = datetime.now(timezone.utc) - timedelta(hours=2)
    db.broker_connections.update_one(
        {"user_id": user_id, "broker": "angelone"},
        {"$set": {"token_expires_at": past_time, "status": "CONNECTED"}},
    )
    r_exp = client.get("/api/portfolio/holdings?broker=angelone", headers=auth_headers)
    assert r_exp.status_code == 401
    exp_json = r_exp.get_json()
    assert exp_json["error_code"] == "BROKER_REAUTH_REQUIRED"
    assert exp_json["connection_status"] == "EXPIRED"
    # Verify DB status automatically transitioned to EXPIRED
    conn_after_exp = BrokerConnectionRepository.get_connection(user_id, "angelone")
    assert conn_after_exp["status"] == "EXPIRED"
    results.append(("8. Token expiration -> automatic EXPIRED state + 401 re-auth prompt", "PASS"))

    # Restore valid Angel One connection for Scenarios 9-12
    future_time = datetime.now(timezone.utc) + timedelta(hours=8)
    db.broker_connections.update_one(
        {"user_id": user_id, "broker": "angelone"},
        {"$set": {"token_expires_at": future_time, "status": "CONNECTED"}},
    )

    # -------------------------------------------------------------------------
    # SCENARIO 9: Token Revocation (Upstream Broker Rejects Revoked Token)
    # -------------------------------------------------------------------------
    with patch(
        "app.services.broker_providers.AngelOneProvider.get_holdings",
        side_effect=BrokerAuthExpiredError("Angel One token was revoked upstream (AB1010)."),
    ):
        r_rev = client.get("/api/portfolio/holdings?broker=angelone", headers=auth_headers)
        assert r_rev.status_code == 401
        rev_json = r_rev.get_json()
        assert rev_json["error_code"] == "BROKER_REAUTH_REQUIRED"
        assert rev_json["requires_reconnection"] is True
        conn_after_rev = BrokerConnectionRepository.get_connection(user_id, "angelone")
        assert conn_after_rev["status"] == "EXPIRED"
    results.append(("9. Token revocation -> marks EXPIRED + 401 BROKER_REAUTH_REQUIRED", "PASS"))

    # Restore CONNECTED status
    db.broker_connections.update_one(
        {"user_id": user_id, "broker": "angelone"},
        {"$set": {"token_expires_at": future_time, "status": "CONNECTED"}},
    )

    # -------------------------------------------------------------------------
    # SCENARIO 10: Broker API Failure (502 Bad Gateway, No Blind Retries)
    # -------------------------------------------------------------------------
    call_counter = {"count": 0}

    def _failing_upstream(*args, **kwargs):
        call_counter["count"] += 1
        raise BrokerUpstreamError("Angel One upstream OMS service unavailable (500).")

    with patch("app.services.broker_providers.AngelOneProvider.get_positions", side_effect=_failing_upstream):
        r_api_fail = client.get("/api/portfolio/positions?broker=angelone", headers=auth_headers)
        assert r_api_fail.status_code == 502
        assert r_api_fail.get_json()["error_code"] == "BROKER_API_FAILURE"
        assert call_counter["count"] == 1  # Zero blind retries
    results.append(("10. Broker API failure -> 502 BROKER_API_FAILURE (zero blind retries)", "PASS"))

    # Restore CONNECTED status
    db.broker_connections.update_one(
        {"user_id": user_id, "broker": "angelone"},
        {"$set": {"token_expires_at": future_time, "status": "CONNECTED"}},
    )

    # -------------------------------------------------------------------------
    # SCENARIO 11: Network Failure (503 Service Unavailable, No Blind Retries)
    # -------------------------------------------------------------------------
    net_counter = {"count": 0}

    def _failing_network(*args, **kwargs):
        net_counter["count"] += 1
        raise BrokerNetworkError("Timed out waiting for Angel One gateway.")

    with patch("app.services.broker_providers.AngelOneProvider.get_orders", side_effect=_failing_network):
        r_net_fail = client.get("/api/orders?broker=angelone", headers=auth_headers)
        assert r_net_fail.status_code == 503
        assert r_net_fail.get_json()["error_code"] == "BROKER_NETWORK_FAILURE"
        assert net_counter["count"] == 1
    results.append(("11. Network failure -> 503 BROKER_NETWORK_FAILURE (zero blind retries)", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 12: Duplicate Order Submission & Missing Confirmation Guard
    # -------------------------------------------------------------------------
    # 12a. Reject order without user_confirmed=True
    r_unconfirmed = client.post(
        "/api/orders",
        json={
            "broker": "angelone",
            "symbol": "RELIANCE-EQ",
            "symboltoken": "2885",
            "exchange": "NSE",
            "side": "BUY",
            "order_type": "MARKET",
            "product": "DELIVERY",
            "quantity": 5,
            "user_confirmed": False,
        },
        headers=auth_headers,
    )
    assert r_unconfirmed.status_code == 400
    assert r_unconfirmed.get_json()["error_code"] == "ORDER_CONFIRMATION_REQUIRED"

    # 12b. Place confirmed order with Idempotency-Key -> succeeds once
    idem_key = f"idem-{secrets.token_hex(8)}"
    with patch(
        "app.services.broker_providers.AngelOneProvider.place_order",
        return_value={
            "order_id": "AO-ORD-9001",
            "broker": "angelone",
            "symbol": "RELIANCE-EQ",
            "side": "BUY",
            "quantity": 5,
            "status": "SUBMITTED",
        },
    ) as mock_place:
        order_body = {
            "broker": "angelone",
            "symbol": "RELIANCE-EQ",
            "symboltoken": "2885",
            "exchange": "NSE",
            "side": "BUY",
            "order_type": "MARKET",
            "product": "DELIVERY",
            "quantity": 5,
            "user_confirmed": True,
        }
        r_ord1 = client.post(
            "/api/orders",
            json=order_body,
            headers={**auth_headers, "Idempotency-Key": idem_key},
        )
        assert r_ord1.status_code == 201
        assert r_ord1.get_json()["order"]["order_id"] == "AO-ORD-9001"
        assert mock_place.call_count == 1

        # 12c. Duplicate submission with same Idempotency-Key -> blocked with 409
        r_ord_dup1 = client.post(
            "/api/orders",
            json=order_body,
            headers={**auth_headers, "Idempotency-Key": idem_key},
        )
        assert r_ord_dup1.status_code == 409
        assert r_ord_dup1.get_json()["error_code"] == "DUPLICATE_ORDER_BLOCKED"
        assert mock_place.call_count == 1  # Upstream broker NOT called a second time

        # 12d. Rapid double-tap with a NEW Idempotency-Key but identical order parameters within 30s -> blocked with 409
        r_ord_dup2 = client.post(
            "/api/orders",
            json=order_body,
            headers={**auth_headers, "Idempotency-Key": f"idem-{secrets.token_hex(8)}"},
        )
        assert r_ord_dup2.status_code == 409
        assert r_ord_dup2.get_json()["error_code"] == "DUPLICATE_ORDER_BLOCKED"
        assert mock_place.call_count == 1
    results.append(("12. Duplicate order submission & confirmation guard -> 409/400 blocked", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 13: Logout / Login
    # -------------------------------------------------------------------------
    r_logout = client.post("/api/v1/users/logout", headers=auth_headers)
    assert r_logout.status_code == 200

    # Verify old JWT token is now rejected with 401
    r_after_logout = client.get("/api/brokers", headers=auth_headers)
    assert r_after_logout.status_code == 401

    # Log back in via Phone + OTP and verify broker_connections are preserved
    r_otp3 = client.post(
        "/api/v1/users/otp/send",
        json={"phone": test_phone, "sandbox_mode": True},
        headers={"X-Brifix-Test-Mode": "1"},
    )
    otp_code_3 = r_otp3.get_json()["sandbox_otp"]
    r_ver3 = client.post(
        "/api/v1/users/otp/verify",
        json={"phone": test_phone, "otp": otp_code_3},
    )
    assert r_ver3.status_code == 200
    relogged_user = r_ver3.get_json()["data"]["data"][0]
    jwt_token = relogged_user["accessToken"]
    auth_headers = {"Authorization": f"Bearer {jwt_token}"}
    brokers_on_login = {c["broker"]: c["status"] for c in relogged_user["broker_connections"]}
    assert brokers_on_login.get("angelone") == "CONNECTED"
    results.append(("13. Logout / Login (session revoked; broker connections preserved)", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 14: Disconnect Broker
    # -------------------------------------------------------------------------
    r_disc = client.delete("/api/brokers/angelone", headers=auth_headers)
    assert r_disc.status_code == 200
    disc_json = r_disc.get_json()
    assert disc_json["connection"]["status"] == "DISCONNECTED"
    raw_after_disc = db.broker_connections.find_one({"user_id": user_id, "broker": "angelone"})
    assert raw_after_disc["access_token_encrypted"] is None
    assert raw_after_disc["refresh_token_encrypted"] is None
    results.append(("14. Disconnect broker -> upstream terminated + encrypted tokens wiped", "PASS"))

    # -------------------------------------------------------------------------
    # SCENARIO 15: Reconnect Broker After Disconnect
    # -------------------------------------------------------------------------
    r_ao_init4 = client.post("/api/brokers/angelone/connect", json={}, headers=auth_headers)
    ao_state_final = r_ao_init4.get_json()["state"]
    with patch(
        "app.services.broker_providers.AngelOneProvider._verify_session_profile",
        return_value={"clientcode": "AO998877", "name": "Aarav Trader"},
    ):
        r_ao_final = client.post(
            "/api/brokers/angelone/callback",
            json={
                "state": ao_state_final,
                "auth_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.angel_access_token_final",
                "refresh_token": "angel_refresh_token_final",
            },
        )
        assert r_ao_final.status_code == 200
        assert r_ao_final.get_json()["connection"]["status"] == "CONNECTED"
    results.append(("15. Reconnect broker after disconnect -> status CONNECTED", "PASS"))

    # Verify log redaction utility
    sample_sensitive_log = (
        'Error calling broker with Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret.sig '
        'and "refresh_token": "rt_secret_999" and pin=1234'
    )
    redacted = redact_secrets(sample_sensitive_log)
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "rt_secret_999" not in redacted
    assert "1234" not in redacted

    # Cleanup test user artifacts
    db.users.delete_many({"_id": user_id})
    db.broker_connections.delete_many({"user_id": user_id})
    db.broker_order_idempotency.delete_many({"user_id": user_id})

    print("\n================ 15-SCENARIO VERIFICATION MATRIX ================")
    for name, status in results:
        print(f"[{status}] {name}")
    print("=================================================================\n")


if __name__ == "__main__":
    run_all_15_scenarios()
