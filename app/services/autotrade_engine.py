import time
import threading
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from app.utils.logger import get_logger
from app.models.user import db
from app.services.broker_service import get_broker_for_user, LiveBrokerError
from app.utils.market_calendar import check_market_session, get_ist_time, is_market_holiday

logger = get_logger(__name__)

# Concurrency locks to prevent double-order placement on the same symbol for the same user
_order_locks = set()
_order_locks_mutex = threading.Lock()


class AutoTradeEngine:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(AutoTradeEngine, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.lock = threading.Lock()
        self.running = False
        self.worker_thread = None
        self._active_symbols = set()
        self._active_symbols_updated = 0

    def start(self):
        """Start background monitoring engine."""
        with self.lock:
            if self.running:
                return
            self.running = True
            self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
            self.worker_thread.start()
            logger.info("[AutoTradeEngine] Started background automation engine.")

    def get_user_config(self, user_id: str) -> dict:
        """Fetch or initialize user auto-trade configuration."""
        uid = str(user_id)
        cfg = db.autotrade_configs.find_one({"userId": uid})
        if not cfg:
            cfg = {
                "userId": uid,
                "enabled": False,
                "tradeMode": "paper",  # "paper" or "live"
                "maxCapitalPerTrade": 10000.0,
                "riskRewardRatio": 2.0,  # 1:2
                "stopLossPct": 1.5,      # 1.5%
                "takeProfitPct": 3.0,    # 1.5% * 2.0 = 3.0%
                "trailingStopLoss": True,
                "dailyMaxLoss": 5000.0,
                "dailyRealizedPnL": 0.0,
                "maxOpenTrades": 3,
                "lastResetDate": get_ist_time().strftime("%Y-%m-%d"),
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            }
            db.autotrade_configs.insert_one(cfg)

        # Check daily PnL reset at midnight IST
        today_str = get_ist_time().strftime("%Y-%m-%d")
        if cfg.get("lastResetDate") != today_str:
            db.autotrade_configs.update_one(
                {"userId": uid},
                {"$set": {"dailyRealizedPnL": 0.0, "lastResetDate": today_str}}
            )
            cfg["dailyRealizedPnL"] = 0.0
            cfg["lastResetDate"] = today_str

        cfg["_id"] = str(cfg["_id"])
        return cfg

    def update_user_config(self, user_id: str, updates: dict) -> dict:
        """Update user risk configuration."""
        uid = str(user_id)
        new_mode = updates.get("tradeMode")
        if new_mode == "live":
            user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
            # Validate broker can be initialized before saving live configuration
            get_broker_for_user(user_doc, user_id=uid, trade_mode="live")

        sl = float(updates.get("stopLossPct", 1.5))
        rr = float(updates.get("riskRewardRatio", 2.0))
        tp = round(sl * rr, 2)

        set_fields = {
            "tradeMode": updates.get("tradeMode", "paper"),
            "maxCapitalPerTrade": float(updates.get("maxCapitalPerTrade", 10000.0)),
            "riskRewardRatio": rr,
            "stopLossPct": sl,
            "takeProfitPct": tp,
            "trailingStopLoss": bool(updates.get("trailingStopLoss", True)),
            "dailyMaxLoss": float(updates.get("dailyMaxLoss", 5000.0)),
            "maxOpenTrades": int(updates.get("maxOpenTrades", 3)),
            "updatedAt": datetime.utcnow()
        }
        db.autotrade_configs.update_one({"userId": uid}, {"$set": set_fields}, upsert=True)
        return self.get_user_config(uid)

    def toggle_engine(self, user_id: str, enable: bool) -> dict:
        """Enable or disable auto-trading master switch for user."""
        uid = str(user_id)
        cfg = self.get_user_config(uid)
        if enable and cfg.get("tradeMode") == "live":
            user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
            get_broker_for_user(user_doc, user_id=uid, trade_mode="live")

        db.autotrade_configs.update_one(
            {"userId": uid},
            {"$set": {"enabled": bool(enable), "updatedAt": datetime.utcnow()}},
            upsert=True
        )
        status = "ENABLED" if enable else "DISABLED"
        logger.info(f"[AutoTradeEngine] User {uid} automation status: {status}")
        return self.get_user_config(uid)

    def on_tick(self, symbol: str, ltp: float):
        """Zero-latency tick evaluation for all active open positions on this symbol."""
        if not symbol or ltp <= 0:
            return

        now = time.time()
        if (now - self._active_symbols_updated) > 2.0:
            try:
                self._active_symbols = set(db.autotrade_positions.distinct("symbol", {"status": "OPEN"}))
                self._active_symbols_updated = now
            except Exception:
                pass

        clean_sym = symbol.replace(".NS", "").upper()
        if clean_sym not in self._active_symbols:
            return

        # Find all open positions for this symbol
        open_positions = list(db.autotrade_positions.find({
            "symbol": clean_sym,
            "status": "OPEN"
        }))

        if not open_positions:
            self._active_symbols.discard(clean_sym)
            return

        for pos in open_positions:
            pos_id = pos["_id"]
            user_id = pos["userId"]
            entry = float(pos.get("entryPrice", ltp))
            sl = float(pos.get("stopLossPrice", entry * 0.985))
            target = float(pos.get("targetPrice", entry * 1.03))
            highest = max(float(pos.get("highestPrice", entry)), ltp)
            qty = int(pos.get("quantity", 1))

            # Fetch user configuration
            cfg = self.get_user_config(user_id)
            trailing = cfg.get("trailingStopLoss", True)

            # 1. Update Trailing Stop Loss if price rises favorably
            new_sl = sl
            if trailing and ltp > entry:
                profit_margin = ltp - entry
                sl_distance = entry * (float(cfg.get("stopLossPct", 1.5)) / 100.0)
                tentative_sl = round(ltp - sl_distance, 2)
                if tentative_sl > sl:
                    new_sl = tentative_sl

            # Update DB with latest LTP, highest price & trailing SL
            db.autotrade_positions.update_one(
                {"_id": pos_id},
                {"$set": {
                    "currentPrice": ltp,
                    "highestPrice": highest,
                    "stopLossPrice": new_sl
                }}
            )

            # 2. Check Take Profit Hit
            if ltp >= target:
                self._close_position(pos, ltp, "TARGET_HIT")
                continue

            # Anti-jitter hysteresis: protect positions for the first 5 seconds after entry
            entry_time = pos.get("entryTime")
            if isinstance(entry_time, datetime):
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
                if age_seconds < 5.0:
                    continue

            # 3. Check Stop Loss Hit (Cut Loss)
            if ltp <= new_sl:
                is_trailing = new_sl > entry
                reason = "TRAILING_SL_HIT" if is_trailing else "STOP_LOSS_HIT"
                exit_price = max(ltp, new_sl) if is_trailing else ltp
                self._close_position(pos, exit_price, reason)
                continue

    def _close_position(self, pos: dict, exit_price: float, reason: str):
        """Execute exit order and record trade result."""
        pos_id = pos["_id"]
        user_id = pos["userId"]
        symbol = pos["symbol"]
        qty = int(pos["quantity"])
        entry = float(pos["entryPrice"])
        t_mode = pos.get("tradeMode", "paper")

        user_doc = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
        try:
            broker = get_broker_for_user(user_doc, user_id=str(user_id), trade_mode=t_mode)
            sell_res = broker.place_order(symbol, "SELL", qty, exit_price)
        except Exception as e:
            logger.error(f"[AutoTradeEngine] CRITICAL: Failed to execute SELL order for {user_id} {symbol}: {e}")
            sell_res = {"status": "failed", "error": str(e)}
        realized_pnl = round((exit_price - entry) * qty, 2)
        realized_pct = round(((exit_price - entry) / entry * 100), 2) if entry > 0 else 0

        # Update position record
        db.autotrade_positions.update_one(
            {"_id": pos_id},
            {"$set": {
                "status": "CLOSED",
                "exitPrice": exit_price,
                "exitReason": reason,
                "realizedPnL": realized_pnl,
                "realizedPnLPct": realized_pct,
                "exitTime": datetime.utcnow()
            }}
        )

        # Update cumulative daily PnL and check daily circuit breaker
        db.autotrade_configs.update_one(
            {"userId": user_id},
            {"$inc": {"dailyRealizedPnL": realized_pnl}}
        )

        cfg = self.get_user_config(user_id)
        if cfg.get("dailyRealizedPnL", 0) <= -float(cfg.get("dailyMaxLoss", 5000.0)):
            # Circuit breaker triggered!
            db.autotrade_configs.update_one(
                {"userId": user_id},
                {"$set": {"enabled": False}}
            )
            logger.warning(f"[AutoTradeEngine] Circuit breaker tripped for user {user_id}! Daily loss reached ₹{cfg.get('dailyRealizedPnL')}")

        logger.info(f"[AutoTradeEngine] Closed {symbol} ({reason}): {qty}x @ ₹{exit_price} | P&L: ₹{realized_pnl} ({realized_pct}%)")

    def emergency_exit_all(self, user_id: str) -> dict:
        """Panic kill switch: immediately disable automation and close all open positions at market price."""
        uid = str(user_id)
        # 1. Disable automation
        db.autotrade_configs.update_one({"userId": uid}, {"$set": {"enabled": False, "updatedAt": datetime.utcnow()}})

        # 2. Find all open positions
        open_pos = list(db.autotrade_positions.find({"userId": uid, "status": "OPEN"}))
        from app.socket.indexes import _shared_quotes

        closed_count = 0
        for pos in open_pos:
            sym = pos["symbol"]
            q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
            ltp = float(q.get("ltp", pos.get("entryPrice", 100.0)))
            self._close_position(pos, ltp, "EMERGENCY_EXIT")
            closed_count += 1

        logger.info(f"[AutoTradeEngine] Emergency exit executed for {uid}: closed {closed_count} positions.")
        return {
            "status": "success",
            "message": f"Emergency stop complete: {closed_count} open positions squared off at market price.",
            "closed_count": closed_count
        }

    def _run_loop(self):
        """Background loop scanning high-probability signals and enforcing session rules."""
        import time
        from app.models.user import _get_db
        while self.running:
            try:
                if _get_db() is None:
                    time.sleep(2.0)
                    continue

                is_open, status_code, status_msg = check_market_session()

                # Enforce intraday auto square-off at 15:15 IST cutoff for LIVE trading positions
                if not is_open:
                    open_intraday = list(db.autotrade_positions.find({"status": "OPEN", "tradeMode": {"$ne": "paper"}}))
                    if open_intraday:
                        from app.socket.indexes import _shared_quotes
                        for pos in open_intraday:
                            sym = pos["symbol"]
                            q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
                            ltp = float(q.get("ltp", pos.get("entryPrice", 100.0)))
                            self._close_position(pos, ltp, "INTRADAY_SQUAREOFF")
                            logger.info(f"[AutoTradeEngine] Live intraday auto square-off executed for {sym} (15:15 IST cutoff).")

                # Scan active enabled users for new trade opportunities ONLY during open market hours (09:15 - 15:15 IST)
                if is_open:
                    enabled_users = list(db.autotrade_configs.find({"enabled": True}))
                    for u in enabled_users:
                        try:
                            self.evaluate_user(u["userId"])
                        except Exception as e:
                            logger.error(f"[AutoTradeEngine] User evaluation error for {u.get('userId')}: {e}")

            except Exception as e:
                logger.error(f"[AutoTradeEngine] Error in main loop: {e}")

            time.sleep(5.0)

    def evaluate_user(self, user_id: str, force_scan: bool = False):
        """
        Evaluate positions and signals for a user immediately.
        Screens Indian stocks, applies AI predictions, checks confidence >= 80%,
        and executes auto-trades for qualified setups.
        """
        uid = str(user_id)
        cfg = self.get_user_config(uid)
        if not cfg.get("enabled", False) and not force_scan:
            return {"status": "disabled", "message": "Automated trading is disabled for this user."}

        trade_mode = cfg.get("tradeMode", "paper")

        # 1. Update/check open positions against latest market prices
        open_pos = list(db.autotrade_positions.find({"userId": uid, "status": "OPEN"}))
        from app.socket.indexes import _shared_quotes

        for pos in open_pos:
            sym = pos["symbol"]
            q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
            ltp = float(q.get("ltp", 0))
            if ltp > 0:
                self.on_tick(sym, ltp)

        # 2. Market session check - strictly enforce 09:15 - 15:15 IST window for ALL trades (live & paper)
        is_open, status_code, status_msg = check_market_session()
        if not is_open:
            return {
                "status": "market_closed",
                "session": status_code,
                "message": status_msg,
                "new_positions": []
            }

        # 3. Check if we have room for new positions
        max_open = int(cfg.get("maxOpenTrades", 3))
        open_count = db.autotrade_positions.count_documents({"userId": uid, "status": "OPEN"})
        if open_count >= max_open:
            return {
                "status": "max_positions_reached",
                "open_count": open_count,
                "max_open": max_open,
                "message": f"Maximum open positions ({max_open}) already active."
            }

        # 4. Fetch AI recommendations for Indian stocks
        try:
            from app.services.stock_prediction_service import StockPredictionService
            daily_picks = StockPredictionService.get_daily_recommendations()
        except Exception as e:
            logger.error(f"[AutoTradeEngine] Error fetching recommendations: {e}")
            daily_picks = []

        if not daily_picks:
            return {"status": "no_signals", "message": "No active signals returned from AI prediction model."}

        # Sort candidate picks descending by confidence
        daily_picks = sorted(daily_picks, key=lambda x: float(x.get("confidence", 0) or 0), reverse=True)

        user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
        try:
            broker = get_broker_for_user(user_doc, user_id=uid, trade_mode=trade_mode)
            margin = broker.get_margin()
            avail_cash = float(margin.get("available_cash", 0.0))
        except LiveBrokerError as e:
            logger.error(f"[AutoTradeEngine] Live broker error for {uid}: {e}")
            return {"status": "broker_error", "message": str(e), "new_positions": []}

        created_positions = []
        qualified_picks = []

        for pick in daily_picks:
            if open_count >= max_open:
                break

            sym = pick.get("symbol", "").upper()
            rating = str(pick.get("action", "") or pick.get("signal", "")).upper()
            conf = float(pick.get("confidence", 0) or 0)

            # STRICT USER RULE: Only pick stocks with confidence >= 80% and BUY signal
            if "BUY" not in rating or conf < 80.0:
                continue

            qualified_picks.append({"symbol": sym, "confidence": conf, "rating": rating})

            lock_key = f"{uid}:{sym}"
            with _order_locks_mutex:
                if lock_key in _order_locks:
                    continue
                _order_locks.add(lock_key)

            try:
                # Check if position already exists for this symbol
                existing = db.autotrade_positions.find_one({"userId": uid, "symbol": sym, "status": "OPEN"})
                if existing:
                    continue

                q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
                ltp = float(q.get("ltp") or pick.get("current_price") or pick.get("currentPrice") or 0)
                if ltp <= 0:
                    continue

                max_alloc = min(float(cfg.get("maxCapitalPerTrade", 10000.0)), avail_cash)
                qty = int(max_alloc / ltp)
                if qty <= 0 and avail_cash >= ltp:
                    qty = 1

                if qty <= 0:
                    continue

                order_cost = round(ltp * qty, 2)
                if trade_mode == "live" and avail_cash < order_cost:
                    logger.warning(f"[AutoTradeEngine] Insufficient live margin for {uid}: needed Rs.{order_cost}, had Rs.{avail_cash}")
                    continue

                # Calculate Risk-Reward parameters
                sl_pct = float(cfg.get("stopLossPct", 1.5))
                rr = float(cfg.get("riskRewardRatio", 2.0))
                tp_pct = round(sl_pct * rr, 2)

                sl_price = round(ltp * (1 - sl_pct / 100), 2)
                target_price = round(ltp * (1 + tp_pct / 100), 2)

                # Place BUY order through broker
                buy_res = broker.place_order(sym, "BUY", qty, ltp)
                if buy_res.get("status") == "success":
                    order_id = buy_res.get("order_id")
                    # In live mode, verify order status with broker RMS
                    if trade_mode == "live" and order_id:
                        v_stat = broker.verify_order_status(order_id)
                        if v_stat.get("status") in ("REJECTED", "CANCELLED"):
                            reason = v_stat.get("rejection_reason") or "Order rejected by broker RMS"
                            logger.error(f"[AutoTradeEngine] Live order {order_id} for {sym} rejected by broker: {reason}")
                            continue

                    new_pos = {
                        "userId": uid,
                        "symbol": sym,
                        "quantity": qty,
                        "entryPrice": ltp,
                        "stopLossPrice": sl_price,
                        "targetPrice": target_price,
                        "highestPrice": ltp,
                        "status": "OPEN",
                        "tradeMode": trade_mode,
                        "aiConfidence": conf,
                        "entryTime": datetime.utcnow()
                    }
                    res = db.autotrade_positions.insert_one(new_pos)
                    new_pos["id"] = str(res.inserted_id)
                    new_pos.pop("_id", None)
                    created_positions.append(new_pos)

                    open_count += 1
                    avail_cash -= order_cost
                    logger.info(
                        f"[AutoTradeEngine] Executed Auto BUY for {uid}: {qty}x {sym} @ Rs.{ltp} (AI Conf: {conf}%) | "
                        f"SL: Rs.{sl_price} (-{sl_pct}%) | TP: Rs.{target_price} (+{tp_pct}%)"
                    )
            finally:
                with _order_locks_mutex:
                    _order_locks.discard(lock_key)

        return {
            "status": "success",
            "open_count": open_count,
            "max_open": max_open,
            "scanned_count": len(daily_picks),
            "qualified_count": len(qualified_picks),
            "qualified_picks": qualified_picks,
            "new_positions": created_positions
        }


# Global singleton instance
autotrade_engine = AutoTradeEngine()
