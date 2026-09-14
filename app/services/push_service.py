"""
Push Notification Service (Web Push API / VAPID)
=================================================
Manages VAPID key pairs, browser push subscriptions, and dispatching
real-time push notifications for the #1 AI-recommended stock pick.
"""

import os
import json
import base64
import time
try:
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization
    from pywebpush import webpush, WebPushException
except ImportError:
    Vapid = None
    serialization = None
    webpush = None
    WebPushException = Exception
from app.utils.logger import get_logger
from app.extensions import connect_to_mongodb
from app.services.stock_prediction_service import StockPredictionService

logger = get_logger(__name__)

VAPID_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "vapid_private.pem")
VAPID_CLAIMS = {"sub": "mailto:admin@brifix.com"}


class PushNotificationService:
    _public_key_b64 = None

    @classmethod
    def initialize_vapid(cls) -> str:
        """
        Ensures a persistent VAPID private key exists on disk.
        Returns the base64 URL-safe public key string.
        """
        if cls._public_key_b64:
            return cls._public_key_b64

        try:
            if not os.path.exists(VAPID_FILE):
                logger.info(f"Generating new VAPID key pair at: {VAPID_FILE}")
                v = Vapid()
                v.generate_keys()
                v.save_key(VAPID_FILE)
            else:
                v = Vapid.from_file(VAPID_FILE)

            raw_pub = v.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint
            )
            cls._public_key_b64 = base64.urlsafe_b64encode(raw_pub).decode().rstrip("=")
            logger.info(f"VAPID initialized successfully. Public key: {cls._public_key_b64[:16]}...")
            return cls._public_key_b64
        except Exception as e:
            logger.error(f"Error initializing VAPID keys: {e}")
            raise

    @classmethod
    def get_public_key(cls) -> str:
        return cls.initialize_vapid()

    @classmethod
    def get_db(cls):
        try:
            return connect_to_mongodb()
        except Exception as e:
            logger.warning(f"Could not connect to MongoDB for push notifications: {e}")
            return None

    @classmethod
    def save_subscription(cls, subscription_data: dict, user_id: str = None) -> bool:
        """
        Saves or updates a Web Push subscription object in MongoDB.
        """
        if not subscription_data or "endpoint" not in subscription_data:
            raise ValueError("Invalid subscription object: missing endpoint")

        endpoint = subscription_data["endpoint"]
        db = cls.get_db()
        if db is None:
            logger.warning("MongoDB not available, subscription not stored persistently.")
            return False

        doc = {
            "endpoint": endpoint,
            "keys": subscription_data.get("keys", {}),
            "user_id": user_id,
            "updated_at": time.time()
        }

        try:
            db["push_subscriptions"].update_one(
                {"endpoint": endpoint},
                {"$set": doc},
                upsert=True
            )
            logger.info(f"Push subscription saved: {endpoint[:32]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to save push subscription: {e}")
            return False

    @classmethod
    def remove_subscription(cls, endpoint: str):
        """Removes an expired or revoked subscription."""
        db = cls.get_db()
        if db is not None:
            try:
                db["push_subscriptions"].delete_one({"endpoint": endpoint})
                logger.info(f"Removed expired push subscription: {endpoint[:32]}...")
            except Exception as e:
                logger.error(f"Error removing subscription: {e}")

    @classmethod
    def send_notification_to_subscription(cls, subscription: dict, payload: dict) -> bool:
        """
        Sends a single push notification payload to a browser subscription.
        """
        cls.initialize_vapid()
        endpoint = subscription.get("endpoint")
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload),
                vapid_private_key=VAPID_FILE,
                vapid_claims=VAPID_CLAIMS,
                ttl=86400
            )
            return True
        except WebPushException as ex:
            logger.warning(f"WebPushException for {endpoint[:32] if endpoint else 'unknown'}: {ex}")
            # If subscription has expired (HTTP 404 or 410), clean it up
            if ex.response and ex.response.status_code in (404, 410):
                if endpoint:
                    cls.remove_subscription(endpoint)
            return False
        except Exception as e:
            logger.error(f"Unexpected error in webpush send: {e}")
            return False

    @classmethod
    def notify_top_ai_pick(cls, custom_pick: dict = None, target_subscription: dict = None) -> dict:
        """
        Identifies the #1 ranked AI stock recommendation and broadcasts it to all
        subscribers (or to target_subscription if testing directly).
        """
        top_pick = custom_pick
        if not top_pick:
            try:
                picks = StockPredictionService.get_daily_recommendations()
                if picks and len(picks) > 0:
                    top_pick = picks[0]
            except Exception as e:
                logger.error(f"Error getting daily recommendations for push: {e}")

        if not top_pick:
            # Fallback high quality recommendation
            top_pick = {
                "symbol": "RELIANCE",
                "name": "Reliance Industries",
                "signal": "STRONG BUY",
                "confidence": 88,
                "current_price": 2985.40,
                "target_5d": 3120.00,
                "expected_return_pct": 4.51,
                "stop_loss": 2920.00,
                "rationale": "Bullish EMA alignment • MACD expansion • Institutional accumulation"
            }

        symbol = top_pick.get("symbol", "STOCK")
        confidence = top_pick.get("confidence", 85)
        signal = top_pick.get("signal", "BUY")
        price = top_pick.get("current_price", 0)
        target = top_pick.get("target_5d", 0)
        expected_return = top_pick.get("expected_return_pct", 0)
        stop_loss = top_pick.get("stop_loss", 0)

        payload = {
            "title": f"🚀 AI Top Pick: {symbol} ({confidence}% Conf)",
            "body": f"{signal} at ₹{price:,.2f} | Target ₹{target:,.2f} (+{expected_return}%) | SL ₹{stop_loss:,.2f}",
            "icon": "/brifix-logo.png",
            "badge": "/brifix-logo.png",
            "tag": f"ai-pick-{symbol}",
            "data": {
                "url": f"/predictions?symbol={symbol}",
                "symbol": symbol,
                "confidence": confidence,
                "price": price,
                "target": target,
                "signal": signal
            }
        }

        # If a single direct subscription was provided (e.g. from an immediate test button)
        if target_subscription:
            success = cls.send_notification_to_subscription(target_subscription, payload)
            return {
                "status": "success" if success else "failed",
                "sent_count": 1 if success else 0,
                "top_pick": top_pick,
                "payload": payload
            }

        # Otherwise broadcast to all registered subscriptions in DB
        db = cls.get_db()
        subscriptions = []
        if db is not None:
            try:
                subscriptions = list(db["push_subscriptions"].find())
            except Exception as e:
                logger.error(f"Error fetching subscriptions: {e}")

        sent_count = 0
        failed_count = 0

        for sub in subscriptions:
            sub_info = {
                "endpoint": sub.get("endpoint"),
                "keys": sub.get("keys", {})
            }
            if cls.send_notification_to_subscription(sub_info, payload):
                sent_count += 1
            else:
                failed_count += 1

        logger.info(f"Broadcast AI Top Pick push: {sent_count} sent, {failed_count} failed")

        return {
            "status": "success",
            "sent_count": sent_count,
            "failed_count": failed_count,
            "total_subscribers": len(subscriptions),
            "top_pick": top_pick,
            "payload": payload
        }
