import time
import threading
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from app.utils.logger import get_logger
from app.models.user import db
from app.services.broker_service import get_broker_for_user

logger = get_logger(__name__)


def get_ist_time():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


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

        clean_sym = symbol.replace(".NS", "").upper()
        # Find all open positions for this symbol
        open_positions = list(db.autotrade_positions.find({
            "symbol": clean_sym,
            "status": "OPEN"
        }))

        if not open_positions:
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
            if trailing:
                gain_pct = ((ltp - entry) / entry) * 100
                if gain_pct >= 2.0:
                    # Lock in +1.0% profit
                    new_sl = max(sl, round(entry * 1.01, 2))
                elif gain_pct >= 1.0:
                    # Lock in break-even (+0.2%)
                    new_sl = max(sl, round(entry * 1.002, 2))

            # Persist highest and updated trailing SL
            if highest > pos.get("highestPrice", 0) or new_sl > sl:
                db.autotrade_positions.update_one(
                    {"_id": pos_id},
                    {"$set": {"highestPrice": highest, "stopLossPrice": new_sl}}
                )

            # 2. Check Target Hit (Take Profit)
            if ltp >= target:
                self._close_position(pos, ltp, "TARGET_HIT")
                continue

            # 3. Check Stop Loss Hit (Cut Loss)
            if ltp <= new_sl:
                reason = "TRAILING_SL_HIT" if new_sl > entry else "STOP_LOSS_HIT"
                self._close_position(pos, ltp, reason)
                continue

    def _close_position(self, pos: dict, exit_price: float, reason: str):
        """Execute exit order and record trade result."""
        pos_id = pos["_id"]
        user_id = pos["userId"]
        symbol = pos["symbol"]
        qty = int(pos["quantity"])
        entry = float(pos["entryPrice"])

        user_doc = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
        broker = get_broker_for_user(user_doc)

        # Place SELL order
        sell_res = broker.place_order(symbol, "SELL", qty, exit_price)
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
        while self.running:
            try:
                # Check intraday square-off (3:15 PM IST)
                ist_now = get_ist_time()
                is_square_off_time = (ist_now.hour == 15 and ist_now.minute >= 15) or (ist_now.hour > 15)

                if is_square_off_time:
                    open_intraday = list(db.autotrade_positions.find({"status": "OPEN"}))
                    if open_intraday:
                        from app.socket.indexes import _shared_quotes
                        for pos in open_intraday:
                            sym = pos["symbol"]
                            q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
                            ltp = float(q.get("ltp", pos.get("entryPrice", 100.0)))
                            self._close_position(pos, ltp, "INTRADAY_SQUAREOFF")

                # Scan active enabled users for new high-probability trade opportunities
                enabled_users = list(db.autotrade_configs.find({"enabled": True}))
                if enabled_users and not is_square_off_time:
                    self._scan_and_execute_signals(enabled_users)

            except Exception as e:
                logger.error(f"[AutoTradeEngine] Error in main loop: {e}")

            time.sleep(5.0)

    def _scan_and_execute_signals(self, enabled_users: list):
        """Evaluate AI recommendations against active user capital and risk parameters."""
        from app.socket.indexes import _shared_quotes

        # Load daily picks from prediction service
        try:
            from app.services.stock_prediction_service import StockPredictionService
            daily_picks = StockPredictionService.get_daily_picks()
        except Exception:
            daily_picks = []

        if not daily_picks:
            return

        for u_cfg in enabled_users:
            user_id = u_cfg["userId"]
            max_open = int(u_cfg.get("maxOpenTrades", 3))

            # Count current open positions
            open_count = db.autotrade_positions.count_documents({"userId": user_id, "status": "OPEN"})
            if open_count >= max_open:
                continue

            user_doc = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
            broker = get_broker_for_user(user_doc)
            margin = broker.get_margin()
            avail_cash = float(margin.get("available_cash", 0.0))

            for pick in daily_picks:
                if open_count >= max_open:
                    break

                sym = pick.get("symbol", "").upper()
                rating = pick.get("action", "").upper() or pick.get("signal", "").upper()
                conf = float(pick.get("confidence", 0) or 0)

                # High probability criteria: confidence >= 75% and BUY recommendation
                if "BUY" not in rating or conf < 75:
                    continue

                # Check if position already exists for this symbol
                existing = db.autotrade_positions.find_one({"userId": user_id, "symbol": sym, "status": "OPEN"})
                if existing:
                    continue

                q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
                ltp = float(q.get("ltp", pick.get("currentPrice", 0)))
                if ltp <= 0:
                    continue

                max_alloc = min(float(u_cfg.get("maxCapitalPerTrade", 10000.0)), avail_cash)
                qty = int(max_alloc / ltp)
                if qty <= 0:
                    continue

                # Calculate Risk-Reward parameters
                sl_pct = float(u_cfg.get("stopLossPct", 1.5))
                rr = float(u_cfg.get("riskRewardRatio", 2.0))
                tp_pct = sl_pct * rr

                sl_price = round(ltp * (1 - sl_pct / 100), 2)
                target_price = round(ltp * (1 + tp_pct / 100), 2)

                # Place BUY order
                buy_res = broker.place_order(sym, "BUY", qty, ltp)
                if buy_res.get("status") == "success":
                    db.autotrade_positions.insert_one({
                        "userId": user_id,
                        "symbol": sym,
                        "quantity": qty,
                        "entryPrice": ltp,
                        "stopLossPrice": sl_price,
                        "targetPrice": target_price,
                        "highestPrice": ltp,
                        "status": "OPEN",
                        "tradeMode": u_cfg.get("tradeMode", "paper"),
                        "aiConfidence": conf,
                        "entryTime": datetime.utcnow()
                    })
                    open_count += 1
                    avail_cash -= (ltp * qty)
                    logger.info(f"[AutoTradeEngine] Executed Auto BUY: {qty}x {sym} @ ₹{ltp} | SL: ₹{sl_price} (-{sl_pct}%) | TP: ₹{target_price} (+{tp_pct}%)")


# Global singleton instance
autotrade_engine = AutoTradeEngine()
