import time
import threading
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.utils.logger import get_logger
from app.models.user import db
from app.services.broker_service import get_broker_for_user, LiveBrokerError
from app.services.fno_prediction_service import FNOPredictionService, INDEX_SPECS, estimate_option_premium, _compute_dte_days
from app.services.nse_market_service import nse_market_service
from app.utils.market_calendar import (
    check_market_session,
    format_ist_date_key,
    format_ist_datetime,
    format_ist_display_date,
    get_ist_time,
    is_market_holiday,
)

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


def resolve_live_option_premium(pos: dict, live_index_ltp: float = 0.0) -> tuple[float, float]:
    """
    Resolve the real-time option contract LTP for an open position using:
      1. Official NSE Option Chain v3 contract LTP (`nse_market_service.get_real_option_quote`)
      2. Sub-10s micro-tick adjustment from the option chain's underlying snapshot price,
         strictly guarded against stale/corrupt index jumps.
    Returns (current_premium, current_index_ltp).
    """
    und = pos.get("underlying")
    strike = float(pos.get("strike", 0.0))
    opt_type = str(pos.get("optionType", "CE")).upper()
    expiry = pos.get("expiryDate")
    entry_prem = float(pos.get("entryPremium", 100.0))
    prev_curr = float(pos.get("currentPremium", entry_prem))
    entry_idx = float(pos.get("entryIndexPrice", 0.0))
    delta = float(pos.get("delta", 0.50))

    # 1. Query Real NSE Option Chain v3
    try:
        opt_q = nse_market_service.get_real_option_quote(und, strike, opt_type, expiry=expiry)
        if opt_q and float(opt_q.get("ltp") or 0.0) > 0:
            real_opt_ltp = float(opt_q["ltp"])
            chain_und_ltp = float(opt_q.get("underlying_ltp") or live_index_ltp or entry_idx)
            eff_idx_ltp = live_index_ltp if live_index_ltp > 0 else chain_und_ltp

            # If live index tick has moved slightly since the 10s option chain snapshot,
            # apply delta on that small difference only (sanity-checked to < 1.5% index move)
            if eff_idx_ltp > 0 and chain_und_ltp > 0:
                micro_idx_diff = eff_idx_ltp - chain_und_ltp
                if abs(micro_idx_diff) <= (chain_und_ltp * 0.015):
                    prem_adj = (delta * micro_idx_diff) if opt_type == "CE" else (-delta * micro_idx_diff)
                    return max(round(real_opt_ltp + prem_adj, 2), 0.5), round(eff_idx_ltp, 2)

            return round(real_opt_ltp, 2), round(eff_idx_ltp, 2)
    except Exception as e:
        logger.debug(f"NSE option quote lookup fallback for {und} {strike} {opt_type}: {e}")

    # 2. Fallback: only apply delta if live_index_ltp is genuine and within realistic intraday range (<3%) of entry_idx
    if live_index_ltp > 0 and entry_idx > 0 and abs(live_index_ltp - entry_idx) <= (entry_idx * 0.03):
        idx_diff = live_index_ltp - entry_idx
        prem_diff = delta * idx_diff if opt_type == "CE" else -delta * idx_diff
        return max(round(entry_prem + prem_diff, 2), 0.5), round(live_index_ltp, 2)

    return round(prev_curr, 2), round(live_index_ltp if live_index_ltp > 0 else entry_idx, 2)


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
        today_str = get_ist_time().strftime("%Y-%m-%d")
        cfg = db.fno_autotrade_configs.find_one({"userId": uid})
        if not cfg:
            cfg = {
                "userId": uid,
                "enabled": False,
                "tradeMode": "paper",  # "paper" or "live"
                "targetIndices": ["NIFTY", "BANKNIFTY", "FINNIFTY"],
                "strikePreference": "ATM",  # "ATM" | "ITM" | "OTM"
                "lotsPerTrade": 1,
                "stopLossPct": 20.0,       # 20% SL on option premium
                "riskRewardRatio": 3.0,
                "takeProfitPct": 60.0,
                "profitTargetInr": 300.0,  # Auto Square-Off when Profit reaches ₹300+
                "maxLossPerTradeInr": 100.0,  # Auto Square-Off when Loss reaches 100 points / ₹100
                "strategyName": "VWAP + Supertrend(7,3) + CPR Breakout + NSE Option Chain OI",
                "trailingStopLoss": True,
                "dailyMaxLoss": 10000.0,
                "dailyRealizedPnL": 0.0,
                "maxOpenTrades": 3,
                "minConfidence": 85.0,
                "lastResetDate": today_str,
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow()
            }
            db.fno_autotrade_configs.insert_one(cfg)

        if "minConfidence" not in cfg or float(cfg.get("minConfidence", 0)) > 85.0:
            cfg["minConfidence"] = 85.0
            db.fno_autotrade_configs.update_one({"userId": uid}, {"$set": {"minConfidence": 85.0}})

        if "profitTargetInr" not in cfg:
            cfg["profitTargetInr"] = 300.0
        if "maxLossPerTradeInr" not in cfg:
            cfg["maxLossPerTradeInr"] = 100.0
        if "strategyName" not in cfg:
            cfg["strategyName"] = "VWAP + Supertrend(7,3) + CPR Breakout + NSE Option Chain OI"

        # Strictly compute today's realized P&L from closed trades on today's IST date
        today_closed = list(db.fno_autotrade_positions.find({"userId": uid, "status": "CLOSED"}))
        today_pnl = 0.0
        for d in today_closed:
            exit_date_key = d.get("exitDateIST") or format_ist_date_key(d.get("exitTime") or d.get("entryTime"))
            if exit_date_key == today_str:
                today_pnl += float(d.get("realizedPnL", 0.0))
        today_pnl = round(today_pnl, 2)

        # Check daily PnL reset at midnight IST (also reset enabled=False on new day)
        if cfg.get("lastResetDate") != today_str or round(float(cfg.get("dailyRealizedPnL", 0.0)), 2) != today_pnl:
            reset_updates = {"dailyRealizedPnL": today_pnl, "lastResetDate": today_str}
            if cfg.get("lastResetDate") != today_str:
                reset_updates["enabled"] = False
                cfg["enabled"] = False
            db.fno_autotrade_configs.update_one(
                {"userId": uid},
                {"$set": reset_updates}
            )
            cfg["dailyRealizedPnL"] = today_pnl
            cfg["lastResetDate"] = today_str

        cfg["_id"] = str(cfg["_id"])
        return cfg

    def update_user_config(self, user_id: str, updates: dict) -> dict:
        """Update F&O risk & lot parameters."""
        uid = str(user_id)
        new_mode = updates.get("tradeMode")
        if new_mode == "live":
            user_doc = db.users.find_one({"_id": ObjectId(uid)}) if ObjectId.is_valid(uid) else None
            get_broker_for_user(user_doc, user_id=uid, trade_mode="live")

        sl = float(updates.get("stopLossPct", 20.0))
        rr = float(updates.get("riskRewardRatio", 3.0))
        tp = round(sl * rr, 2)
        profit_target_inr = float(updates.get("profitTargetInr", 300.0))
        max_loss_inr = float(updates.get("maxLossPerTradeInr", 100.0))

        set_fields = {
            "tradeMode": updates.get("tradeMode", "paper"),
            "targetIndices": updates.get("targetIndices", ["NIFTY", "BANKNIFTY", "FINNIFTY"]),
            "strikePreference": updates.get("strikePreference", "ATM"),
            "lotsPerTrade": max(1, int(updates.get("lotsPerTrade", 1))),
            "riskRewardRatio": rr,
            "stopLossPct": sl,
            "takeProfitPct": tp,
            "profitTargetInr": profit_target_inr,
            "maxLossPerTradeInr": max_loss_inr,
            "strategyName": updates.get("strategyName", "VWAP + Supertrend(7,3) + CPR Breakout + NSE Option Chain OI"),
            "trailingStopLoss": bool(updates.get("trailingStopLoss", True)),
            "dailyMaxLoss": float(updates.get("dailyMaxLoss", 10000.0)),
            "maxOpenTrades": int(updates.get("maxOpenTrades", 3)),
            "minConfidence": min(float(updates.get("minConfidence", 85.0)), 85.0),
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
        Uses Real NSE Option Chain v3 contract LTP (`resolve_live_option_premium`)
        so option positions never experience synthetic jumps.
        """
        if not index_symbol or index_ltp <= 0:
            return

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
            qty = max(1, int(pos.get("quantity", 25)))
            entry_prem = float(pos.get("entryPremium", 100.0))
            cfg = self.get_user_config(user_id)
            max_loss_inr = float(pos.get("maxLossPerTradeInr", cfg.get("maxLossPerTradeInr", 100.0)))
            loss_pts_cap = round(min(max_loss_inr / qty, 100.0), 2)
            default_init_sl = round(max(0.05, entry_prem - loss_pts_cap), 2)
            sl_prem = float(pos.get("stopLossPremium", default_init_sl))
            initial_sl_prem = float(pos.get("initialStopLossPremium", min(sl_prem, default_init_sl)))
            prev_high = float(pos.get("highestPremium", entry_prem))
            prev_curr = float(pos.get("currentPremium", entry_prem))

            # Anti-jitter hysteresis check: protect position from early noise stop-out for first 5s
            in_anti_jitter = False
            entry_time = pos.get("entryTime")
            if isinstance(entry_time, datetime):
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()
                if age_seconds < 90.0:
                    in_anti_jitter = True

            # Resolve real NSE Option Chain premium
            current_prem, eff_index_ltp = resolve_live_option_premium(pos, live_index_ltp=index_ltp)
            highest_prem = max(prev_high, current_prem)

            # Point-for-Point 1:1 Smart Auto Trailing Stop Loss:
            # Whatever amount increases from the entry/average price, increase the exact same in SL.
            # Example: Entry=150, SL=140. If price rises to 160 (+10), SL increases to 150 (+10).
            new_sl = sl_prem
            if cfg.get("trailingStopLoss", True):
                gain_from_entry = max(0.0, highest_prem - entry_prem)
                point_for_point_sl = round(initial_sl_prem + gain_from_entry, 2)
                # Ratchet: Stop-Loss only moves UP, never downwards
                new_sl = max(sl_prem, point_for_point_sl, initial_sl_prem)

            profit_target_inr = float(pos.get("profitTargetInr", cfg.get("profitTargetInr", 300.0)))
            target_300_prem = round(entry_prem + (profit_target_inr / qty), 2)
            target_prem = float(pos.get("targetPremium", target_300_prem))
            current_pnl = round((current_prem - entry_prem) * qty, 2)
            points_loss = round(entry_prem - current_prem, 2)

            db.fno_autotrade_positions.update_one(
                {"_id": pos_id},
                {"$set": {
                    "currentPremium": current_prem,
                    "highestPremium": highest_prem,
                    "stopLossPremium": new_sl,
                    "initialStopLossPremium": initial_sl_prem,
                    "targetPremium": target_prem,
                    "profitTargetInr": profit_target_inr,
                    "maxLossPerTradeInr": max_loss_inr,
                    "currentIndexPrice": eff_index_ltp
                }}
            )

            # Auto Square-Off IMMEDIATELY when winning ₹300+ in this trade (or hitting target)
            if current_pnl >= 300.0 or current_pnl >= profit_target_inr or current_prem >= target_prem:
                self._close_fno_position(pos, current_prem, "PROFIT_300_TARGET_HIT")
                continue

            # Auto Square-Off IMMEDIATELY when loss reaches 100 points or ₹100 in this trade
            if current_pnl <= -100.0 or current_pnl <= -max_loss_inr or points_loss >= 100.0:
                reason = "LOSS_100_LIMIT_HIT" if (current_pnl <= -100.0 or current_pnl <= -max_loss_inr) else "LOSS_100_POINTS_HIT"
                self._close_fno_position(pos, current_prem, reason)
                continue

            if in_anti_jitter:
                continue

            if current_prem <= new_sl:
                is_trailing = new_sl > (initial_sl_prem + 0.01)
                reason = "TRAILING_SL_HIT" if is_trailing else "STOP_LOSS_HIT"
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

        now_utc = datetime.utcnow()
        exit_date_ist = format_ist_date_key(now_utc)
        exit_time_ist = format_ist_datetime(now_utc)
        display_date_ist = format_ist_display_date(now_utc)

        db.fno_autotrade_positions.update_one(
            {"_id": pos_id},
            {"$set": {
                "status": "CLOSED",
                "exitPremium": exit_prem,
                "exitReason": reason,
                "realizedPnL": realized_pnl,
                "realizedPnLPct": realized_pct,
                "exitTime": now_utc,
                "exitDateIST": exit_date_ist,
                "exitTimeIST": exit_time_ist,
                "displayDateIST": display_date_ist,
            }}
        )

        # Update cumulative daily P&L and check daily circuit breaker
        db.fno_autotrade_configs.update_one(
            {"userId": user_id},
            {"$inc": {"dailyRealizedPnL": realized_pnl}}
        )

        cfg = self.get_user_config(user_id)
        if cfg.get("dailyRealizedPnL", 0) <= -float(cfg.get("dailyMaxLoss", 10000.0)):
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
            curr_prem, _ = resolve_live_option_premium(pos)
            self._close_fno_position(pos, curr_prem, "EMERGENCY_EXIT")
            closed_count += 1

        logger.info(f"[FNOAutoTradeEngine] Emergency exit executed for {uid}: closed {closed_count} F&O positions.")
        return {
            "status": "success",
            "message": f"F&O Emergency Stop Complete: {closed_count} open contracts squared off at market price.",
            "closed_count": closed_count
        }

    def close_single_fno_position(self, user_id: str, pos_id_str: str, exit_price: float = None) -> dict:
        """Manually exit a specific open F&O option position at locked/live NSE market price."""
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

        if exit_price is not None and float(exit_price) > 0:
            curr_prem = round(float(exit_price), 2)
        else:
            live_ltp = 0.0
            try:
                from app.socket.indexes import _shared_quotes
                und = pos.get("underlying")
                spec = INDEX_SPECS.get(und, {})
                sym = spec.get("symbol")
                live_ltp = float((_shared_quotes.get(sym) or {}).get("ltp", 0.0)) if sym else 0.0
            except Exception:
                pass
            curr_prem, _ = resolve_live_option_premium(pos, live_index_ltp=live_ltp)

        self._close_fno_position(pos, curr_prem, "MANUAL_EXIT")
        return {
            "status": "success",
            "exit_premium": curr_prem,
            "message": f"Position {pos.get('symbol')} squared off instantly at Rs.{curr_prem}."
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
                            curr_prem, _ = resolve_live_option_premium(pos)
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
        and execute automated F&O options purchases using REAL NSE Option Chain LTPs.
        """
        uid = str(user_id)
        cfg = self.get_user_config(uid)
        if not cfg.get("enabled", False) and not force_scan:
            return {"status": "disabled", "message": "F&O automated trading is disabled for this user."}

        trade_mode = cfg.get("tradeMode", "paper")

        # 1. Update open positions with latest index ticks & NSE option chain quotes
        open_pos = list(db.fno_autotrade_positions.find({"userId": uid, "status": "OPEN"}))
        from app.socket.indexes import _shared_quotes

        for pos in open_pos:
            und = pos["underlying"]
            spec = INDEX_SPECS.get(und, {})
            sym = spec.get("symbol")
            ltp = float((_shared_quotes.get(sym) or {}).get("ltp", 0.0)) if sym else 0.0
            if ltp > 0:
                self.on_index_tick(sym, ltp)

        # 2. Market session check - strictly enforce 09:15 - 15:15 IST window for ALL trades
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
        min_conf = min(float(cfg.get("minConfidence", 85.0)), 85.0)

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

                # 60-second stabilization cooldown per underlying after last exit
                last_closed = db.fno_autotrade_positions.find_one(
                    {"userId": uid, "underlying": idx_key, "status": "CLOSED"},
                    sort=[("exitTime", -1)]
                )
                if last_closed and last_closed.get("exitTime"):
                    exit_time = last_closed["exitTime"]
                    if isinstance(exit_time, datetime) and exit_time.tzinfo is None:
                        exit_time = exit_time.replace(tzinfo=timezone.utc)
                    cooldown_elapsed = (datetime.now(timezone.utc) - exit_time).total_seconds()
                    if cooldown_elapsed < 60:  # 60 seconds stabilization cooldown
                        logger.debug(f"[FNOAutoTradeEngine] Cooldown active for {idx_key}: {int(60 - cooldown_elapsed)}s remaining")
                        continue

                # Max 20 trades per underlying per day
                today_str = get_ist_time().strftime("%Y-%m-%d")
                today_trades_count = db.fno_autotrade_positions.count_documents({
                    "userId": uid,
                    "underlying": idx_key,
                    "entryDateIST": today_str
                })
                if today_trades_count >= 20:
                    logger.debug(f"[FNOAutoTradeEngine] Max daily trades reached for {idx_key} ({today_trades_count} trades today)")
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

                opt_type = "CE" if sig == "CALL_BUY" else "PE"
                strike_pref = cfg.get("strikePreference", "ATM")
                strikes = analysis["strikes"]
                if strike_pref == "ITM":
                    strike = strikes["ce"]["itm"] if opt_type == "CE" else strikes["pe"]["itm"]
                elif strike_pref == "OTM":
                    strike = strikes["ce"]["otm"] if opt_type == "CE" else strikes["pe"]["otm"]
                else:
                    strike = strikes["atm"]

                # Strictly fetch REAL NSE Option Chain contract LTP for this exact strike & option type
                real_opt_q = nse_market_service.get_real_option_quote(
                    idx_key, strike, opt_type, expiry=analysis.get("expiry")
                )
                if not real_opt_q or float(real_opt_q.get("ltp") or 0.0) < 2.0:
                    logger.warning(
                        f"[FNOAutoTradeEngine] Skipping {idx_key} {strike} {opt_type}: "
                        f"real NSE Option Chain LTP unavailable or illiquid ({real_opt_q})"
                    )
                    continue

                est_premium = round(float(real_opt_q["ltp"]), 2)
                expiry_str = real_opt_q.get("expiry") or analysis.get("expiry") or ""
                live_idx_ltp = float(real_opt_q.get("underlying_ltp") or analysis.get("current_price") or 0.0)
                if live_idx_ltp <= 0:
                    continue

                raw_iv = float(real_opt_q.get("iv") or 0.0)
                iv_decimal = (raw_iv / 100.0) if raw_iv > 1.0 else INDEX_SPECS.get(idx_key, {}).get("base_iv", 0.14)

                live_pricing = estimate_option_premium(
                    live_idx_ltp,
                    strike,
                    opt_type,
                    dte_days=_compute_dte_days(expiry_str),
                    iv=iv_decimal,
                    real_ltp=est_premium,
                )
                delta = abs(live_pricing["delta"])

                qualified_signals.append({
                    "index": idx_key,
                    "signal": sig,
                    "confidence": conf,
                    "strike": strike,
                    "option_type": opt_type,
                    "real_nse_ltp": est_premium,
                    "expiry": expiry_str,
                })

                lots = max(1, int(cfg.get("lotsPerTrade", 1)))
                lot_size = analysis["lot_size"]
                quantity = lots * lot_size
                order_cost = round(est_premium * quantity, 2)

                # Check daily loss limit BEFORE entering new trade
                daily_pnl = float(cfg.get("dailyRealizedPnL", 0.0))
                daily_max = float(cfg.get("dailyMaxLoss", 10000.0))
                if daily_pnl <= -daily_max:
                    logger.info(f"[FNOAutoTradeEngine] Daily loss limit (-₹{daily_max}) already breached for {uid}. No new entries.")
                    continue

                if avail_cash < order_cost and trade_mode == "live":
                    logger.warning(f"[FNOAutoTradeEngine] Insufficient margin for {uid}: needed {order_cost}, had {avail_cash}")
                    continue

                profit_target_inr = float(cfg.get("profitTargetInr", 300.0))
                max_loss_inr = float(cfg.get("maxLossPerTradeInr", 100.0))

                # Maximum 100 Loss limit per trade (₹100 INR risk or 100 points maximum)
                risk_pts = round(min(max_loss_inr / quantity, 100.0), 2)
                sl_prem = round(max(0.05, est_premium - risk_pts), 2)

                # Profit target 300+ INR per trade
                target_pts = round(profit_target_inr / quantity, 2)
                tp_prem = round(est_premium + target_pts, 2)

                sig_atr = float(analysis.get('indicators', {}).get('atr', 85.0))
                sig_delta = abs(float(analysis.get('greeks', {}).get('delta', 0.50)))

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

                    now_utc = datetime.utcnow()
                    entry_date_ist = format_ist_date_key(now_utc)
                    entry_time_ist = format_ist_datetime(now_utc)
                    display_date_ist = format_ist_display_date(now_utc)

                    new_pos = {
                        "userId": uid,
                        "symbol": fno_symbol,
                        "underlying": idx_key,
                        "optionType": opt_type,
                        "strike": strike,
                        "expiryDate": expiry_str,
                        "isRealNseLtp": True,
                        "lots": lots,
                        "lotSize": lot_size,
                        "quantity": quantity,
                        "entryPremium": est_premium,
                        "currentPremium": est_premium,
                        "initialStopLossPremium": sl_prem,
                        "stopLossPremium": sl_prem,
                        "targetPremium": tp_prem,
                        "profitTargetInr": profit_target_inr,
                        "maxLossPerTradeInr": max_loss_inr,
                        "highestPremium": est_premium,
                        "entryIndexPrice": live_idx_ltp,
                        "currentIndexPrice": live_idx_ltp,
                        "delta": delta,
                        "atr": sig_atr,
                        "riskPoints": risk_pts,
                        "trailGap": max(risk_pts, est_premium * 0.30, 25.0),
                        "status": "OPEN",
                        "tradeMode": trade_mode,
                        "aiConfidence": conf,
                        "signal": sig,
                        "strategyName": analysis.get("strategy_name", "VWAP + Supertrend(7,3) + CPR Breakout + NSE Option Chain OI"),
                        "entryReasons": analysis.get("entry_reasons", []),
                        "entryTime": now_utc,
                        "entryDateIST": entry_date_ist,
                        "entryTimeIST": entry_time_ist,
                        "displayDateIST": display_date_ist,
                    }
                    res = db.fno_autotrade_positions.insert_one(new_pos)
                    new_pos["id"] = str(res.inserted_id)
                    new_pos.pop("_id", None)
                    created_positions.append(new_pos)
                    self._active_underlyings.add(idx_key)

                    open_count += 1
                    avail_cash -= order_cost
                    logger.info(
                        f"[FNOAutoTradeEngine] Executed Real NSE Auto F&O BUY for {uid}: {lots} lot(s) ({quantity} qty) "
                        f"{fno_symbol} ({expiry_str}) @ Real NSE LTP Rs.{est_premium} (AI Conf: {conf}%) | "
                        f"SL: Rs.{sl_prem} | Auto Target: Rs.{tp_prem} (+Rs.{profit_target_inr} Profit)"
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
