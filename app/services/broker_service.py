import os
import base64
import hashlib
import time
from datetime import datetime
from bson import ObjectId
import pyotp
from cryptography.fernet import Fernet

from app.utils.logger import get_logger
from app.models.user import db

logger = get_logger(__name__)

# ── Credential Encryption Helper ────────────────────────────
_ENCRYPTION_SECRET = os.environ.get(
    "BROKER_ENCRYPTION_KEY",
    "brifix-investor-broker-secure-encryption-key-2026-auth"
)
_DERIVED_KEY = base64.urlsafe_b64encode(hashlib.sha256(_ENCRYPTION_SECRET.encode()).digest())
_CIPHER = Fernet(_DERIVED_KEY)


def encrypt_val(plain_text: str) -> str:
    """Encrypt sensitive broker credentials."""
    if not plain_text:
        return ""
    try:
        return _CIPHER.encrypt(plain_text.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption error: {e}")
        return plain_text


def decrypt_val(cipher_text: str) -> str:
    """Decrypt sensitive broker credentials."""
    if not cipher_text:
        return ""
    try:
        return _CIPHER.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except Exception:
        return cipher_text


_ACTIVE_ANGEL_SESSIONS = {}
_ACTIVE_GROWW_SESSIONS = {}


class BaseBroker:
    broker_name = "base"

    def get_profile(self) -> dict:
        raise NotImplementedError

    def get_margin(self) -> dict:
        raise NotImplementedError

    def place_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0, order_type: str = "MARKET") -> dict:
        raise NotImplementedError

    def get_orders(self) -> list:
        raise NotImplementedError

    def get_positions(self) -> list:
        raise NotImplementedError


class PaperTradingBroker(BaseBroker):
    """Zero-risk simulated broker with virtual ₹10,00,000 balance for safe testing."""
    broker_name = "paper"

    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        self._ensure_account()

    def _ensure_account(self):
        acc = db.paper_accounts.find_one({"userId": self.user_id})
        if not acc:
            db.paper_accounts.insert_one({
                "userId": self.user_id,
                "initialBalance": 1000000.0,
                "availableCash": 1000000.0,
                "usedMargin": 0.0,
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            })

    def get_profile(self) -> dict:
        return {
            "broker": "paper",
            "name": "Paper Trading Sandbox",
            "client_code": f"PAPER-{self.user_id[-6:].upper()}",
            "status": "connected",
            "is_paper": True,
            "message": "Virtual sandbox active with real-time market prices"
        }

    def get_margin(self) -> dict:
        acc = db.paper_accounts.find_one({"userId": self.user_id}) or {}
        cash = float(acc.get("availableCash", 1000000.0))
        used = float(acc.get("usedMargin", 0.0))
        return {
            "available_cash": round(cash, 2),
            "used_margin": round(used, 2),
            "total_balance": round(cash + used, 2),
            "is_paper": True
        }

    def place_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0, order_type: str = "MARKET") -> dict:
        clean_sym = symbol.replace(".NS", "").upper()
        fill_price = float(price)
        if fill_price <= 0:
            from app.socket.indexes import _shared_quotes
            q = _shared_quotes.get(f"{clean_sym}.NS") or _shared_quotes.get(clean_sym) or {}
            fill_price = float(q.get("ltp", 100.0))

        order_val = round(fill_price * quantity, 2)
        acc = db.paper_accounts.find_one({"userId": self.user_id}) or {}
        cash = float(acc.get("availableCash", 1000000.0))

        if transaction_type.upper() == "BUY":
            if cash < order_val:
                return {"status": "failed", "error": f"Insufficient paper margin: Required ₹{order_val}, Available ₹{cash}"}
            db.paper_accounts.update_one(
                {"userId": self.user_id},
                {"$inc": {"availableCash": -order_val, "usedMargin": order_val}, "$set": {"updatedAt": datetime.utcnow()}}
            )
        elif transaction_type.upper() == "SELL":
            db.paper_accounts.update_one(
                {"userId": self.user_id},
                {"$inc": {"availableCash": order_val, "usedMargin": -min(order_val, float(acc.get("usedMargin", order_val)))}, "$set": {"updatedAt": datetime.utcnow()}}
            )

        order_doc = {
            "userId": self.user_id,
            "broker": "paper",
            "orderId": f"PAPER-{int(time.time()*1000)}",
            "symbol": clean_sym,
            "transactionType": transaction_type.upper(),
            "quantity": quantity,
            "price": fill_price,
            "orderValue": order_val,
            "orderType": order_type,
            "status": "COMPLETE",
            "timestamp": datetime.utcnow()
        }
        db.paper_orders.insert_one(order_doc)
        logger.info(f"[PaperBroker] Executed {transaction_type} {quantity}x {clean_sym} @ ₹{fill_price}")

        return {
            "status": "success",
            "order_id": order_doc["orderId"],
            "fill_price": fill_price,
            "quantity": quantity,
            "order_value": order_val,
            "message": f"Paper order executed for {quantity}x {clean_sym} @ ₹{fill_price}"
        }

    def get_orders(self) -> list:
        docs = list(db.paper_orders.find({"userId": self.user_id}).sort("timestamp", -1).limit(50))
        for d in docs:
            d["_id"] = str(d["_id"])
            if isinstance(d.get("timestamp"), datetime):
                d["timestamp"] = d["timestamp"].isoformat()
        return docs

    def get_positions(self) -> list:
        orders = list(db.paper_orders.find({"userId": self.user_id}))
        pos_map = {}
        for o in orders:
            sym = o["symbol"]
            qty = o["quantity"]
            price = o["price"]
            if sym not in pos_map:
                pos_map[sym] = {"qty": 0, "total_cost": 0.0}
            if o["transactionType"] == "BUY":
                pos_map[sym]["qty"] += qty
                pos_map[sym]["total_cost"] += (price * qty)
            else:
                pos_map[sym]["qty"] -= qty
                pos_map[sym]["total_cost"] -= (price * qty)

        from app.socket.indexes import _shared_quotes
        positions = []
        for sym, data in pos_map.items():
            if data["qty"] > 0:
                avg = data["total_cost"] / data["qty"]
                q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
                ltp = float(q.get("ltp", avg))
                pnl = (ltp - avg) * data["qty"]
                pnl_pct = ((ltp - avg) / avg * 100) if avg > 0 else 0
                positions.append({
                    "symbol": sym,
                    "quantity": data["qty"],
                    "average_price": round(avg, 2),
                    "ltp": round(ltp, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2)
                })
        return positions


class AngelOneBroker(BaseBroker):
    """Live broker interface for Angel One SmartAPI."""
    broker_name = "angelone"

    def __init__(self, client_code: str, pin: str, totp_secret: str, api_key: str):
        self.client_code = client_code
        self.pin = pin
        self.totp_secret = totp_secret
        self.api_key = api_key
        self.smart_connect = None
        self._authenticate()

    def _authenticate(self):
        from SmartApi import SmartConnect
        global _ACTIVE_ANGEL_SESSIONS

        if self.client_code in _ACTIVE_ANGEL_SESSIONS:
            sess_info = _ACTIVE_ANGEL_SESSIONS[self.client_code]
            if time.time() - sess_info.get("timestamp", 0) < 18 * 3600:
                self.smart_connect = sess_info["obj"]
                return

        try:
            totp = pyotp.TOTP(self.totp_secret).now()
            obj = SmartConnect(api_key=self.api_key)
            session = obj.generateSession(self.client_code, self.pin, totp)
            if session and session.get("status") and session.get("data"):
                jwt = session["data"]["jwtToken"]
                if " " in jwt:
                    jwt = jwt.split(" ")[1]
                obj.setAccessToken(jwt)
                obj.setRefreshToken(session["data"].get("refreshToken", ""))
                obj.api_key = self.api_key
                self.smart_connect = obj
                _ACTIVE_ANGEL_SESSIONS[self.client_code] = {
                    "obj": obj,
                    "session": session,
                    "timestamp": time.time()
                }
                logger.info(f"[AngelOneBroker] Authenticated successfully: {self.client_code}")
            else:
                raise ValueError(session.get("message", "Angel One login failed"))
        except Exception as e:
            logger.error(f"[AngelOneBroker] Connection error for {self.client_code}: {e}")
            raise

    def get_profile(self) -> dict:
        if not self.smart_connect:
            return {"status": "error", "message": "Angel One session not established"}
        try:
            prof = self.smart_connect.getProfile(self.smart_connect.refresh_token)
            pdata = prof.get("data", {}) if isinstance(prof, dict) else {}
            return {
                "broker": "angelone",
                "client_code": self.client_code,
                "name": pdata.get("name", self.client_code),
                "email": pdata.get("email", ""),
                "status": "connected",
                "is_paper": False
            }
        except Exception:
            return {"broker": "angelone", "client_code": self.client_code, "status": "connected", "is_paper": False}

    def get_margin(self) -> dict:
        if not self.smart_connect:
            return {"available_cash": 0, "used_margin": 0, "is_paper": False}
        try:
            rms = self.smart_connect.rmsLimit()
            data = rms.get("data", {}) if isinstance(rms, dict) else {}
            net = float(data.get("net", 0.0) or 0.0)
            avail = float(data.get("availablecash", net) or net)
            return {
                "available_cash": round(avail, 2),
                "used_margin": round(float(data.get("utilisedmargin", 0.0) or 0.0), 2),
                "total_balance": round(net, 2),
                "is_paper": False
            }
        except Exception as e:
            logger.warning(f"Error fetching Angel One RMS: {e}")
            return {"available_cash": 0, "used_margin": 0, "is_paper": False}

    def place_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0, order_type: str = "MARKET") -> dict:
        from app.socket.indexes import resolve_symbol_to_token
        clean_sym = symbol.replace(".NS", "").upper()
        token_id, _ = resolve_symbol_to_token(clean_sym)
        if not token_id:
            token_id = "3045"

        params = {
            "variety": "NORMAL",
            "tradingsymbol": f"{clean_sym}-EQ",
            "symboltoken": str(token_id),
            "transactiontype": transaction_type.upper(),
            "exchange": "NSE",
            "ordertype": "MARKET" if order_type == "MARKET" else "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(price) if order_type != "MARKET" else "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity)
        }
        try:
            res = self.smart_connect.placeOrder(params)
            order_id = res.get("data", {}).get("orderid") if isinstance(res, dict) else str(res)
            logger.info(f"[AngelOneBroker] Placed order: {order_id}")
            return {
                "status": "success",
                "order_id": order_id,
                "symbol": clean_sym,
                "quantity": quantity,
                "transaction_type": transaction_type.upper(),
                "message": f"Order submitted to Angel One: {order_id}"
            }
        except Exception as e:
            logger.error(f"[AngelOneBroker] Order placement failed: {e}")
            return {"status": "failed", "error": str(e)}

    def get_orders(self) -> list:
        try:
            book = self.smart_connect.orderBook()
            data = book.get("data", []) if isinstance(book, dict) else []
            return data or []
        except Exception as e:
            logger.warning(f"Error getting Angel One orderbook: {e}")
            return []

    def get_positions(self) -> list:
        try:
            pos = self.smart_connect.position()
            data = pos.get("data", []) if isinstance(pos, dict) else []
            return data or []
        except Exception as e:
            logger.warning(f"Error getting Angel One positions: {e}")
            return []


class GrowwBroker(BaseBroker):
    """Live broker interface for Groww."""
    broker_name = "groww"

    def __init__(self, api_key: str, totp_secret: str):
        self.api_key = api_key
        self.totp_secret = totp_secret
        self.client = None
        self._authenticate()

    def _authenticate(self):
        from growwapi import GrowwAPI
        global _ACTIVE_GROWW_SESSIONS
        sess_key = self.api_key[:20] if self.api_key else "default"

        if sess_key in _ACTIVE_GROWW_SESSIONS:
            self.client = _ACTIVE_GROWW_SESSIONS[sess_key]
            return

        try:
            totp = pyotp.TOTP(self.totp_secret).now()
            access_token = GrowwAPI.get_access_token(api_key=self.api_key, totp=totp)
            groww_obj = GrowwAPI(access_token)
            self.client = groww_obj
            _ACTIVE_GROWW_SESSIONS[sess_key] = groww_obj
            logger.info("[GrowwBroker] Authenticated successfully")
        except Exception as e:
            logger.error(f"[GrowwBroker] Authentication error: {e}")
            raise

    def get_profile(self) -> dict:
        if not self.client:
            return {"status": "error", "message": "Groww session not initialized"}
        try:
            prof = self.client.get_user_profile()
            return {
                "broker": "groww",
                "name": prof.get("name", "Groww User"),
                "email": prof.get("email", ""),
                "status": "connected",
                "is_paper": False
            }
        except Exception:
            return {"broker": "groww", "status": "connected", "is_paper": False}

    def get_margin(self) -> dict:
        if not self.client:
            return {"available_cash": 0, "used_margin": 0, "is_paper": False}
        try:
            margins = self.client.get_available_margin_details()
            avail = float(margins.get("net", 0.0) or 0.0)
            return {
                "available_cash": round(avail, 2),
                "used_margin": round(float(margins.get("utilised", 0.0) or 0.0), 2),
                "total_balance": round(avail, 2),
                "is_paper": False
            }
        except Exception:
            return {"available_cash": 0, "used_margin": 0, "is_paper": False}

    def place_order(self, symbol: str, transaction_type: str, quantity: int, price: float = 0, order_type: str = "MARKET") -> dict:
        clean_sym = symbol.replace(".NS", "").upper()
        try:
            res = self.client.place_order(
                exchange=self.client.EXCHANGE_NSE,
                segment=self.client.SEGMENT_CASH,
                trading_symbol=clean_sym,
                order_type=self.client.ORDER_TYPE_MARKET if order_type == "MARKET" else self.client.ORDER_TYPE_LIMIT,
                transaction_type=self.client.TRANSACTION_TYPE_BUY if transaction_type.upper() == "BUY" else self.client.TRANSACTION_TYPE_SELL,
                product=self.client.PRODUCT_MIS,
                quantity=quantity,
                price=price if order_type != "MARKET" else 0
            )
            order_id = res.get("order_id") or str(res)
            return {"status": "success", "order_id": order_id, "symbol": clean_sym, "quantity": quantity}
        except Exception as e:
            logger.error(f"[GrowwBroker] Order failed: {e}")
            return {"status": "failed", "error": str(e)}

    def get_orders(self) -> list:
        try:
            return self.client.get_order_list() or []
        except Exception:
            return []

    def get_positions(self) -> list:
        try:
            return self.client.get_positions_for_user() or []
        except Exception:
            return []


def get_broker_for_user(user_doc: dict = None, user_id: str = None, trade_mode: str = None) -> BaseBroker:
    """Instantiate appropriate broker instance based on user's saved credentials and configuration."""
    uid = str(user_id or (user_doc.get("_id") if user_doc else None) or (user_doc.get("id") if user_doc else None) or "demo")

    # If explicitly in paper mode or trade_mode is paper, always return PaperTradingBroker
    if trade_mode == "paper":
        return PaperTradingBroker(uid)

    if not user_doc:
        return PaperTradingBroker(uid)

    active_broker = user_doc.get("activeBroker", "paper").lower()

    if active_broker == "angelone":
        code = user_doc.get("angleClientCode")
        pin = decrypt_val(user_doc.get("angleClientPin", ""))
        totp = decrypt_val(user_doc.get("angleTotpSecret", ""))
        api_key = decrypt_val(user_doc.get("angleApiKey", ""))

        if code and pin and totp and api_key:
            try:
                return AngelOneBroker(code, pin, totp, api_key)
            except Exception as e:
                logger.warning(f"Angel One session failed for {uid}, falling back to Paper: {e}")

    elif active_broker == "groww":
        api_key = decrypt_val(user_doc.get("growwApiKey", ""))
        totp = decrypt_val(user_doc.get("growwTotpSecret", ""))

        if api_key and totp:
            try:
                return GrowwBroker(api_key, totp)
            except Exception as e:
                logger.warning(f"Groww session failed for {uid}, falling back to Paper: {e}")

    return PaperTradingBroker(uid)
