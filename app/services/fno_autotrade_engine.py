import time
import threading
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.utils.logger import get_logger
from app.models.user import db
from app.services.broker_service import get_broker_for_user, LiveBrokerError
from app.services.fno_prediction_service import FNOPredictionService, INDEX_SPECS
from app.utils.market_calendar import check_market_session, get_ist_time, is_market_holiday

logger = get_logger(__name__)

# Concurrency locks to prevent duplicate F&O orders for the same user and underlying
_fno_order_locks = set()
_fno_order_locks_mutex = threading.Lock()


def resolve_underlying_key(index_symbol: str) -> str:
    """
    Robustly maps any ticker, display name, token, or symbol to its canonical underlying key:
    'NIFTY', 'BANKNIFTY', or 'FINNIFTY'.
    Prevents false substring matches (e.g. 'NIFTY' matching 'FIN NIFTY' or 'BANK NIFTY').
    """
    if not index_symbol:
        return None
    
    clean = str(index_symbol).strip().upper()
    compact = clean.replace(" ", "").replace("_", "").replace("-", "")

    # 1. Exact canonical key match
    if clean in INDEX_SPECS:
        return clean

    # 2. Check FIN NIFTY first (must precede NIFTY to avoid substring collision)
    if "FINNIFTY" in compact or "FINSERVICE" in compact or clean == "NIFTY_FIN_SERVICE.NS" or clean == "99926037" or "FIN NIFTY" in clean:
        return "FINNIFTY"

    # 3. Check BANK NIFTY second (must precede NIFTY to avoid substring collision)
    if "BANKNIFTY" in compact or "NSEBANK" in compact or "BANK" in compact or clean == "^NSEBANK" or clean == "99926009" or "BANK NIFTY" in clean:
        return "BANKNIFTY"

    # 4. Check NIFTY 50
    if "NIFTY50" in compact or "NSEI" in compact or clean == "^NSEI" or clean == "99926000" or clean == "NIFTY" or "NIFTY 50" in clean:
        return "NIFTY"

    return None


class FNOAutoTradeEngine:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(FNOAutoTradeEngine, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.lock = threading.Lock()
        self.running = False
        self.worker_thread = None
        self._active_underlyings = set()
        self._active_underlyings_updated = 0

    def start(self):
        """Start background F&O monitoring engine."""
        with self.lock:
            if self.running:
                return
            self.running = True
            self.worker_thread = threading.Thread(target=self._run_loop, daemon=True)
            self.worker_thread.start()
            logger.info("[FNOAutoTradeEngine] Started background F&O automation engine.")

    def get_user_config(self, user_id: str) -> dict:
        """Fetch or initialize user F&O auto-trade configuration."""
        uid = str(user_id)
        cfg = db.fno_autotrade_configs.find_one({"userId": uid})
        if not cfg:
            cfg = {
                "userId": uid,
                "enabled": False,
                "tradeMode": "paper",  # "paper" or "live"
                "targetIndices": ["NIFTY", "BANKNIFTY", "FINNIFTY"],
                "strikePreference": "ATM",  # "ATM" | "ITM" | "OTM"
                "lotsPerTrade": 1,
                "stopLossPct": 20.0,       # 20% on option premium
                "riskRewardRatio": 2.0,    # 1:2 -> 40% TP on option premium
                "takeProfitPct": 40.0,
                "trailingStopLoss": True,
                "dailyMaxLoss": 10000.0,
                "dailyRealizedPnL": 0.0,
                "maxOpenTrades": 3,
                "minConfidence": 70.0,
                "lastResetDate": get_ist_time().strftime("%Y-%m-%d"),
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            }
            db.fno_autotrade_configs.insert_one(cfg)

        # Check daily PnL reset at midnight IST
        today_str = get_ist_time().strftime("%Y-%m-%d")
        if cfg.get("lastResetDate") != today_str:
            db.fno_autotrade_configs.update_one(
                {"userId": uid},
                {"$set": {"dailyRealizedPnL": 0.0, "lastResetDate": today_str}}
            )
            cfg["dailyRealizedPnL"] = 0.0
            cfg["lastResetDate"] = today_str

        cfg["_id"] = str(cfg["_id"])
        return cfg

    def update_user_config(self, user_id: str, updates: dict) -> dict:
        """Update F&O risk & lot parameters."""
        uid = str(user_id)
        new_mode = updates.get("tradeMode")
        if new_mode == "live":
            user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
            # Validate live broker can be initialized before saving live configuration
            get_broker_for_user(user_doc, user_id=uid, trade_mode="live")

        sl = float(updates.get("stopLossPct", 20.0))
        rr = float(updates.get("riskRewardRatio", 2.0))
        tp = round(sl * rr, 2)

        set_fields = {
            "tradeMode": updates.get("tradeMode", "paper"),
            "targetIndices": updates.get("targetIndices", ["NIFTY", "BANKNIFTY", "FINNIFTY"]),
            "strikePreference": updates.get("strikePreference", "ATM"),
            "lotsPerTrade": max(1, int(updates.get("lotsPerTrade", 1))),
            "riskRewardRatio": rr,
            "stopLossPct": sl,
            "takeProfitPct": tp,
            "trailingStopLoss": bool(updates.get("trailingStopLoss", True)),
            "dailyMaxLoss": float(updates.get("dailyMaxLoss", 10000.0)),
            "maxOpenTrades": int(updates.get("maxOpenTrades", 3)),
            "minConfidence": float(updates.get("minConfidence", 70.0)),
            "updatedAt": datetime.utcnow()
        }
        db.fno_autotrade_configs.update_one({"userId": uid}, {"$set": set_fields}, upsert=True)
        return self.get_user_config(uid)

    def toggle_engine(self, user_id: str, enable: bool) -> dict:
        """Enable or disable F&O auto-trading master switch."""
        uid = str(user_id)
        cfg = self.get_user_config(uid)
        if enable and cfg.get("tradeMode") == "live":
            user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
            get_broker_for_user(user_doc, user_id=uid, trade_mode="live")

        db.fno_autotrade_configs.update_one(
            {"userId": uid},
            {"$set": {"enabled": bool(enable), "updatedAt": datetime.utcnow()}},
            upsert=True
        )
        status = "ENABLED" if enable else "DISABLED"
        logger.info(f"[FNOAutoTradeEngine] User {uid} F&O status: {status}")
        return self.get_user_config(uid)

    def on_index_tick(self, index_symbol: str, index_ltp: float):
        """
        Evaluate all active open F&O positions when an underlying index ticks.
        Calculates option premium movement via Delta:
          CE: Delta * (Current Index - Entry Index)
          PE: -Delta * (Current Index - Entry Index)
        """
        if not index_symbol or index_ltp <= 0:
            return

        # Map ticker to underlying key
        underlying_key = resolve_underlying_key(index_symbol)
        if not underlying_key:
            return

        now = time.time()
        if (now - self._active_underlyings_updated) > 2.0:
            try:
                self._active_underlyings = set(db.fno_autotrade_positions.distinct("underlying", {"status": "OPEN"}))
                self._active_underlyings_updated = now
            except Exception:
                pass

        if underlying_key not in self._active_underlyings:
            return

        open_positions = list(db.fno_autotrade_positions.find({
            "underlying": underlying_key,
            "status": "OPEN"
        }))

        if not open_positions:
            self._active_underlyings.discard(underlying_key)
            return

        for pos in open_positions:
            pos_id = pos["_id"]
            user_id = pos["userId"]
            opt_type = pos.get("optionType", "CE").upper()
            entry_prem = float(pos.get("entryPremium", 100.0))
            entry_idx = float(pos.get("entryIndexPrice", index_ltp))
            delta = float(pos.get("delta", 0.50))
            sl_prem = float(pos.get("stopLossPremium", entry_prem * 0.80))
            target_prem = float(pos.get("targetPremium", entry_prem * 1.40))
            highest_prem = float(pos.get("highestPremium", entry_prem))

            # Anti-jitter hysteresis check: protect position from early noise stop-out for first 5s
            in_anti_jitter = False
            entry_time = pos.get("entryTime")
            if isinstance(entry_time, datetime):
                # Ensure entry_time is timezone-aware UTC
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
                if age_seconds < 5.0:
                    in_anti_jitter = True

            # Option price simulation based on index delta
            idx_diff = index_ltp - entry_idx
            if opt_type == "CE":
                prem_diff = delta * idx_diff
            else:
                prem_diff = -delta * idx_diff

            current_prem = max(round(entry_prem + prem_diff, 2), 1.0)
            highest_prem = max(highest_prem, current_prem)

            # Trailing stop-loss on option premium (ratchet based on peak gain achieved)
            cfg = self.get_user_config(user_id)
            new_sl = sl_prem
            if cfg.get("trailingStopLoss", True):
                peak_gain_pct = ((highest_prem - entry_prem) / entry_prem) * 100.0 if entry_prem > 0 else 0.0
                if peak_gain_pct >= 40.0:
                    # Lock in +25% profit floor
                    new_sl = max(sl_prem, round(entry_prem * 1.25, 2))
                elif peak_gain_pct >= 20.0:
                    # Lock in breakeven +5% floor
                    new_sl = max(sl_prem, round(entry_prem * 1.05, 2))

            # Update DB with latest premium & trailing SL immediately
            db.fno_autotrade_positions.update_one(
                {"_id": pos_id},
                {"$set": {
                    "currentPremium": current_prem,
                    "highestPremium": highest_prem,
                    "stopLossPremium": new_sl,
                    "currentIndexPrice": index_ltp
                }}
            )

            # If still within anti-jitter cooldown, allow position to settle (skip premature exit)
            if in_anti_jitter:
                continue

            # Check Take Profit
            if current_prem >= target_prem:
                self._close_fno_position(pos, current_prem, "TARGET_HIT")
                continue

            # Check Stop Loss Hit
            if current_prem <= new_sl:
                is_trailing = new_sl > entry_prem
                reason = "TRAILING_SL_HIT" if is_trailing else "STOP_LOSS_HIT"
                # A trailing stop loss order triggers at new_sl to lock in profit.
                # In paper trading or live stop orders, exit price is guaranteed at new_sl or higher.
                exit_prem = max(current_prem, new_sl) if is_trailing else current_prem
                self._close_fno_position(pos, exit_prem, reason)
                continue

    def _close_fno_position(self, pos: dict, exit_prem: float, reason: str):
        """Execute exit for F&O contract and update realized P&L."""
        pos_id = pos["_id"]
        user_id = pos["userId"]
        underlying = pos["underlying"]
        opt_type = pos["optionType"]
        strike = float(pos["strike"])
        qty = int(pos["quantity"])
        entry_prem = float(pos["entryPremium"])
        t_mode = pos.get("tradeMode", "paper")

        user_doc = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
        try:
            broker = get_broker_for_user(user_doc, user_id=str(user_id), trade_mode=t_mode)
            broker.place_fno_order(underlying, opt_type, strike, "SELL", qty, exit_prem)
        except Exception as e:
            logger.error(f"[FNOAutoTradeEngine] CRITICAL: Failed to execute SELL F&O order for {user_id} {pos.get('symbol')}: {e}")
        realized_pnl = round((exit_prem - entry_prem) * qty, 2)
        realized_pct = round(((exit_prem - entry_prem) / entry_prem * 100), 2) if entry_prem > 0 else 0.0

        db.fno_autotrade_positions.update_one(
            {"_id": pos_id},
            {"$set": {
                "status": "CLOSED",
                "exitPremium": exit_prem,
                "exitReason": reason,
                "realizedPnL": realized_pnl,
                "realizedPnLPct": realized_pct,
                "exitTime": datetime.utcnow()
            }}
        )

        # Update cumulative daily P&L and check daily circuit breaker
        db.fno_autotrade_configs.update_one(
            {"userId": user_id},
            {"$inc": {"dailyRealizedPnL": realized_pnl}}
        )

        cfg = self.get_user_config(user_id)
        if cfg.get("dailyRealizedPnL", 0) <= -float(cfg.get("dailyMaxLoss", 10000.0)):
            # Circuit breaker tripped!
            db.fno_autotrade_configs.update_one(
                {"userId": user_id},
                {"$set": {"enabled": False}}
            )
            logger.warning(f"[FNOAutoTradeEngine] Circuit breaker tripped for user {user_id}! Daily F&O loss reached ₹{cfg.get('dailyRealizedPnL')}")

        logger.info(f"[FNOAutoTradeEngine] Closed F&O {pos.get('symbol')} ({reason}): {qty}x @ Rs.{exit_prem} | P&L: Rs.{realized_pnl} ({realized_pct}%)")

    def emergency_exit_all_fno(self, user_id: str) -> dict:
        """Panic kill switch: immediately disable F&O automation and square off all open option positions."""
        uid = str(user_id)
        db.fno_autotrade_configs.update_one({"userId": uid}, {"$set": {"enabled": False, "updatedAt": datetime.utcnow()}})

        open_pos = list(db.fno_autotrade_positions.find({"userId": uid, "status": "OPEN"}))
        closed_count = 0
        for pos in open_pos:
            curr_prem = float(pos.get("currentPremium", pos.get("entryPremium", 100.0)))
            self._close_fno_position(pos, curr_prem, "EMERGENCY_EXIT")
            closed_count += 1

        logger.info(f"[FNOAutoTradeEngine] Emergency exit executed for {uid}: closed {closed_count} F&O positions.")
        return {
            "status": "success",
            "message": f"F&O Emergency Stop Complete: {closed_count} open contracts squared off at market price.",
            "closed_count": closed_count
        }

    def close_single_fno_position(self, user_id: str, pos_id_str: str) -> dict:
        """Manually exit a specific open F&O option position at current market price."""
        uid = str(user_id)
        if not ObjectId.is_valid(pos_id_str):
            return {"status": "failed", "error": "Invalid position ID"}

        pos = db.fno_autotrade_positions.find_one({
            "_id": ObjectId(pos_id_str),
            "userId": uid,
            "status": "OPEN"
        })
        if not pos:
            return {"status": "failed", "error": "Open position not found"}

        curr_prem = float(pos.get("currentPremium", pos.get("entryPremium", 100.0)))
        self._close_fno_position(pos, curr_prem, "MANUAL_EXIT")
        return {
            "status": "success",
            "message": f"Position {pos.get('symbol')} squared off successfully at Rs.{curr_prem}."
        }

    def _run_loop(self):
        """Background loop scanning enabled users and enforcing intraday 15:15 IST square-off."""
        from app.models.user import _get_db
        while self.running:
            try:
                if _get_db() is None:
                    time.sleep(2.0)
                    continue

                is_open, status_code, status_msg = check_market_session()

                # Enforce intraday auto square-off at 15:15 IST cutoff for LIVE trading positions
                if not is_open:
                    open_fno = list(db.fno_autotrade_positions.find({"status": "OPEN", "tradeMode": {"$ne": "paper"}}))
                    if open_fno:
                        for pos in open_fno:
                            curr_prem = float(pos.get("currentPremium") or pos.get("entryPremium", 100.0))
                            self._close_fno_position(pos, curr_prem, "INTRADAY_SQUAREOFF")
                            logger.info(f"[FNOAutoTradeEngine] Live intraday auto square-off executed for {pos.get('symbol')} (15:15 IST cutoff).")

                # Scan enabled users ONLY when market is open (09:15 - 15:15 IST Mon-Fri)
                if is_open:
                    enabled_users = list(db.fno_autotrade_configs.find({"enabled": True}))
                    for u in enabled_users:
                        try:
                            self.evaluate_user(u["userId"])
                        except Exception as e:
                            logger.error(f"[FNOAutoTradeEngine] User evaluation error for {u.get('userId')}: {e}")

            except Exception as e:
                logger.error(f"[FNOAutoTradeEngine] Error in main loop: {e}")

            time.sleep(5.0)

    def evaluate_user(self, user_id: str, force_scan: bool = False):
        """
        Evaluate technical signals for Nifty, Bank Nifty, and Fin Nifty,
        screen for high-conviction trades (Conf >= minConfidence),
        and execute automated F&O options purchases.
        """
        uid = str(user_id)
        cfg = self.get_user_config(uid)
        if not cfg.get("enabled", False) and not force_scan:
            return {"status": "disabled", "message": "F&O automated trading is disabled for this user."}

        trade_mode = cfg.get("tradeMode", "paper")

        # 1. Update open positions with latest index ticks
        open_pos = list(db.fno_autotrade_positions.find({"userId": uid, "status": "OPEN"}))
        from app.socket.indexes import _shared_quotes

        for pos in open_pos:
            und = pos["underlying"]
            spec = INDEX_SPECS.get(und, {})
            sym = spec.get("symbol")
            if sym and sym in _shared_quotes:
                ltp = float(_shared_quotes[sym].get("ltp", 0.0))
                if ltp > 0:
                    self.on_index_tick(sym, ltp)

        # 2. Market session check - strictly enforce 09:15 - 15:15 IST window for ALL trades (live & paper)
        is_open, status_code, status_msg = check_market_session()
        if not is_open:
            return {
                "status": "market_closed",
                "session": status_code,
                "message": status_msg,
                "new_positions": []
            }

        # 3. Check room for new positions
        max_open = int(cfg.get("maxOpenTrades", 3))
        open_count = db.fno_autotrade_positions.count_documents({"userId": uid, "status": "OPEN"})
        if open_count >= max_open:
            return {
                "status": "max_positions_reached",
                "open_count": open_count,
                "max_open": max_open,
                "message": f"Maximum F&O open positions ({max_open}) already active."
            }

        # 4. Fetch signals for selected target indices
        target_indices = cfg.get("targetIndices", ["NIFTY", "BANKNIFTY", "FINNIFTY"])
        min_conf = float(cfg.get("minConfidence", 70.0))

        user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
        try:
            broker = get_broker_for_user(user_doc, user_id=uid, trade_mode=trade_mode)
            margin = broker.get_margin()
            avail_cash = float(margin.get("available_cash", 0.0))
        except LiveBrokerError as e:
            logger.error(f"[FNOAutoTradeEngine] Live broker error for {uid}: {e}")
            return {"status": "broker_error", "message": str(e), "new_positions": []}

        created_positions = []
        qualified_signals = []

        for idx_key in target_indices:
            if open_count >= max_open:
                break

            if idx_key not in INDEX_SPECS:
                continue

            lock_key = f"{uid}:{idx_key}"
            with _fno_order_locks_mutex:
                if lock_key in _fno_order_locks:
                    continue
                _fno_order_locks.add(lock_key)

            try:
                # Check if user already has an open position for this underlying index
                existing = db.fno_autotrade_positions.find_one({
                    "userId": uid,
                    "underlying": idx_key,
                    "status": "OPEN"
                })
                if existing:
                    continue

                try:
                    analysis = FNOPredictionService.get_index_analysis(idx_key)
                except Exception as e:
                    logger.error(f"[FNOAutoTradeEngine] Error analyzing {idx_key}: {e}")
                    continue

                sig = analysis.get("signal", "WAIT")
                conf = float(analysis.get("confidence", 0.0))
                if sig not in ("CALL_BUY", "PUT_BUY") or conf < min_conf:
                    continue

                qualified_signals.append({
                    "index": idx_key,
                    "signal": sig,
                    "confidence": conf
                })

                opt_type = "CE" if sig == "CALL_BUY" else "PE"
                strike_pref = cfg.get("strikePreference", "ATM")
                strikes = analysis["strikes"]
                if strike_pref == "ITM":
                    strike = strikes["ce"]["itm"] if opt_type == "CE" else strikes["pe"]["itm"]
                elif strike_pref == "OTM":
                    strike = strikes["ce"]["otm"] if opt_type == "CE" else strikes["pe"]["otm"]
                else:
                    strike = strikes["atm"]

                # Estimate or lookup option premium
                pricing = analysis["option_pricing"]
                est_premium = pricing["ce_atm_premium"] if opt_type == "CE" else pricing["pe_atm_premium"]
                delta = abs(pricing["ce_delta"] if opt_type == "CE" else pricing["pe_delta"])

                lots = max(1, int(cfg.get("lotsPerTrade", 1)))
                lot_size = analysis["lot_size"]
                quantity = lots * lot_size
                order_cost = round(est_premium * quantity, 2)

                if avail_cash < order_cost and trade_mode == "live":
                    logger.warning(f"[FNOAutoTradeEngine] Insufficient margin for {uid}: needed {order_cost}, had {avail_cash}")
                    continue

                # Risk-Reward calculations on Option Premium
                sl_pct = float(cfg.get("stopLossPct", 20.0))
                rr = float(cfg.get("riskRewardRatio", 2.0))
                tp_pct = round(sl_pct * rr, 2)

                sl_prem = round(est_premium * (1.0 - sl_pct / 100.0), 2)
                tp_prem = round(est_premium * (1.0 + tp_pct / 100.0), 2)

                fno_symbol = f"{idx_key} {int(strike)} {opt_type}"

                # Execute order through broker
                buy_res = broker.place_fno_order(idx_key, opt_type, strike, "BUY", quantity, est_premium)
                if buy_res.get("status") == "success":
                    order_id = buy_res.get("order_id")
                    if trade_mode == "live" and order_id:
                        v_stat = broker.verify_order_status(order_id)
                        if v_stat.get("status") in ("REJECTED", "CANCELLED"):
                            reason = v_stat.get("rejection_reason") or "Broker RMS rejected F&O order"
                            logger.error(f"[FNOAutoTradeEngine] Live F&O order {order_id} rejected by broker: {reason}")
                            continue

                    live_idx_ltp = float(analysis.get("current_price", 0.0))
                    try:
                        from app.socket.indexes import _shared_quotes
                        spec = INDEX_SPECS.get(idx_key, {})
                        sym = spec.get("symbol")
                        if sym and sym in _shared_quotes:
                            q_ltp = float(_shared_quotes[sym].get("ltp", 0.0))
                            if q_ltp > 0:
                                live_idx_ltp = q_ltp
                    except Exception:
                        pass

                    new_pos = {
                        "userId": uid,
                        "symbol": fno_symbol,
                        "underlying": idx_key,
                        "optionType": opt_type,
                        "strike": strike,
                        "lots": lots,
                        "lotSize": lot_size,
                        "quantity": quantity,
                        "entryPremium": est_premium,
                        "currentPremium": est_premium,
                        "stopLossPremium": sl_prem,
                        "targetPremium": tp_prem,
                        "highestPremium": est_premium,
                        "entryIndexPrice": live_idx_ltp,
                        "currentIndexPrice": live_idx_ltp,
                        "delta": delta,
                        "status": "OPEN",
                        "tradeMode": trade_mode,
                        "aiConfidence": conf,
                        "signal": sig,
                        "entryTime": datetime.utcnow()
                    }
                    res = db.fno_autotrade_positions.insert_one(new_pos)
                    new_pos["id"] = str(res.inserted_id)
                    new_pos.pop("_id", None)
                    created_positions.append(new_pos)
                    self._active_underlyings.add(idx_key)

                    open_count += 1
                    avail_cash -= order_cost
                    logger.info(
                        f"[FNOAutoTradeEngine] Executed Auto F&O BUY for {uid}: {lots} lot(s) ({quantity} qty) "
                        f"{fno_symbol} @ Premium Rs.{est_premium} (AI Conf: {conf}%) | "
                        f"SL: Rs.{sl_prem} (-{sl_pct}%) | TP: Rs.{tp_prem} (+{tp_pct}%)"
                    )
            finally:
                with _fno_order_locks_mutex:
                    _fno_order_locks.discard(lock_key)

        return {
            "status": "success",
            "open_count": open_count,
            "max_open": max_open,
            "qualified_signals": qualified_signals,
            "new_positions": created_positions
        }


# Global singleton instance
fno_autotrade_engine = FNOAutoTradeEngine()
