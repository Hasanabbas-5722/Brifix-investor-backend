from flask import Blueprint, request, jsonify
from bson import ObjectId
from datetime import datetime
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.models.user import db
from app.services.fno_autotrade_engine import fno_autotrade_engine, check_market_session
from app.services.fno_prediction_service import FNOPredictionService
from app.utils.market_calendar import (
    get_ist_time,
    format_ist_datetime,
    format_ist_date_key,
    format_ist_display_date,
)

logger = get_logger(__name__)
fno_bp = Blueprint("fno", __name__, url_prefix="/api/v1/fno")


def _get_uid():
    user = getattr(request, "user", None)
    if isinstance(user, dict):
        return str(user.get("_id") or user.get("id") or "")
    email = request.args.get("email") or (request.get_json(silent=True) or {}).get("email")
    if email:
        u = db.users.find_one({"email": email})
        if u:
            return str(u["_id"])
    return None


@fno_bp.route("/signals", methods=["GET"])
def get_fno_signals():
    """Returns live indicator analysis, radar, and trade signals for NIFTY, BANKNIFTY, FINNIFTY."""
    try:
        is_open, status_code, status_msg = check_market_session()
        signals = FNOPredictionService.get_all_index_signals()
        return jsonify({
            "status": "success",
            "signals": signals,
            "market_session": {"is_open": is_open, "status": status_code, "message": status_msg},
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        logger.error(f"[get_fno_signals] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@fno_bp.route("/toggle", methods=["POST"])
@validate_access_token
def toggle_fno():
    """Toggle master automated F&O options trading switch."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enabled", False))
    try:
        cfg = fno_autotrade_engine.toggle_engine(uid, enable)
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 400

    # Ensure engine loop is running
    fno_autotrade_engine.start()

    eval_result = {}
    is_open, status_code, status_msg = check_market_session()

    if enable:
        if is_open:
            try:
                eval_result = fno_autotrade_engine.evaluate_user(uid, force_scan=True)
            except Exception as e:
                logger.error(f"Error during on-demand F&O evaluation: {e}")

            new_positions = eval_result.get("new_positions", []) if eval_result else []
            new_count = len(new_positions)
            if new_count > 0:
                msg = f"F&O Automated Trading ACTIVATED. AI analyzed indices and entered {new_count} option contract(s)."
            else:
                msg = "F&O Automated Trading ACTIVATED. Engine scanning for high-conviction Index Option setups."
        else:
            msg = f"F&O Automated Trading ACTIVATED in Standby Mode. {status_msg}"
    else:
        msg = "F&O Automated Trading STOPPED."

    return jsonify({
        "status": "success",
        "enabled": cfg.get("enabled", False),
        "config": cfg,
        "market_session": {"is_open": is_open, "status": status_code, "message": status_msg},
        "eval_result": eval_result,
        "message": msg
    })


@fno_bp.route("/scan", methods=["POST"])
@validate_access_token
def scan_and_trade_fno():
    """Immediately evaluate indices, check indicator confluence, and execute options for qualified setups."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    is_open, status_code, status_msg = check_market_session()
    if not is_open:
        return jsonify({
            "status": "market_closed",
            "message": f"Market is currently closed: {status_msg}"
        }), 400

    try:
        eval_result = fno_autotrade_engine.evaluate_user(uid, force_scan=True)
    except Exception as e:
        logger.error(f"Error during manual F&O scan: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

    new_count = len(eval_result.get("new_positions", [])) if eval_result else 0
    qual_count = len(eval_result.get("qualified_signals", [])) if eval_result else 0

    return jsonify({
        "status": "success",
        "result": eval_result,
        "message": f"F&O Scan Complete: Evaluated Nifty, Bank Nifty, and Fin Nifty. Found {qual_count} high-confidence setup(s). Entered {new_count} option trade(s)."
    })


@fno_bp.route("/config", methods=["GET"])
@validate_access_token
def get_fno_config():
    """Fetch user F&O auto-trade configuration."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    is_open, status_code, status_msg = check_market_session()
    cfg = fno_autotrade_engine.get_user_config(uid)
    return jsonify({
        "status": "success",
        "config": cfg,
        "market_session": {"is_open": is_open, "status": status_code, "message": status_msg}
    })


@fno_bp.route("/config", methods=["POST"])
@validate_access_token
def update_fno_config():
    """Update F&O risk parameters and lot preferences."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        updated = fno_autotrade_engine.update_user_config(uid, data)
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 400

    return jsonify({
        "status": "success",
        "message": "F&O risk parameters updated successfully",
        "config": updated
    })


@fno_bp.route("/option-chain", methods=["GET"])
def get_fno_option_chain():
    """Returns real-time NSE Option Chain v3 (PCR, Max Pain, Expiry, Strikes CE/PE LTP, OI, IV)."""
    index_key = (request.args.get("symbol") or request.args.get("index") or "NIFTY").strip().upper()
    expiry = request.args.get("expiry")
    try:
        from app.services.nse_market_service import nse_market_service
        chain = nse_market_service.get_index_option_chain(index_key, expiry=expiry)
        if not chain:
            return jsonify({"status": "failed", "error": f"Option chain unavailable for {index_key}"}), 503
        # Convert integer strike keys to string for JSON serialization
        serialized_strikes = {str(k): v for k, v in (chain.get("strikes") or {}).items()}
        return jsonify({
            "status": "success",
            "index": chain.get("index"),
            "underlying_ltp": chain.get("underlying_ltp"),
            "expiry": chain.get("expiry"),
            "expiry_dates": chain.get("expiry_dates", []),
            "atm_strike": chain.get("atm_strike"),
            "strike_step": chain.get("strike_step"),
            "pcr": chain.get("pcr"),
            "max_pain": chain.get("max_pain"),
            "total_ce_oi": chain.get("total_ce_oi"),
            "total_pe_oi": chain.get("total_pe_oi"),
            "strikes": serialized_strikes,
            "updated_at": chain.get("updated_at"),
        })
    except Exception as e:
        logger.error(f"[get_fno_option_chain] Error: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500


@fno_bp.route("/positions", methods=["GET"])
@validate_access_token
def get_open_fno_positions():
    """Fetch currently active open F&O option positions with real-time NSE Option Chain premium & P&L."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    # Strictly READ-ONLY: Never evaluate or execute new trades inside a GET request!
    from app.socket.indexes import _shared_quotes
    from app.services.fno_prediction_service import INDEX_SPECS
    from app.services.fno_autotrade_engine import resolve_live_option_premium

    docs = list(db.fno_autotrade_positions.find({"userId": uid, "status": "OPEN"}).sort("entryTime", -1))
    positions = []
    total_unrealized_pnl = 0.0

    for d in docs:
        qty = int(d["quantity"])
        entry_prem = float(d["entryPremium"])
        entry_idx = float(d.get("entryIndexPrice", 0.0))
        opt_type = str(d.get("optionType", "CE")).upper()
        delta = float(d.get("delta", 0.50))
        prev_high = float(d.get("highestPremium", entry_prem))
        prev_curr = float(d.get("currentPremium", entry_prem))

        # Real-time repricing from live NSE Option Chain v3 & shared quotes
        und = d.get("underlying")
        spec = INDEX_SPECS.get(und, {})
        sym = spec.get("symbol")
        live_quote = _shared_quotes.get(sym, {}) if sym else {}
        live_ltp = float(live_quote.get("ltp", 0.0))

        curr_prem, eff_idx_ltp = resolve_live_option_premium(d, live_index_ltp=live_ltp)
        if eff_idx_ltp > 0:
            live_ltp = eff_idx_ltp

        highest_prem = max(prev_high, curr_prem)
        default_init_sl = round(max(entry_prem - 10.0, entry_prem * 0.80), 2)
        sl_prem = float(d.get("stopLossPremium", default_init_sl))
        initial_sl_prem = float(d.get("initialStopLossPremium", min(sl_prem, default_init_sl)))

        raw_gap = entry_prem - initial_sl_prem
        risk_gap = min(max(raw_gap, 1.0), 10.0) if raw_gap > 0 else 10.0

        if highest_prem > entry_prem:
            sl_from_entry = round(initial_sl_prem + (highest_prem - entry_prem), 2)
            sl_prem = max(sl_prem, sl_from_entry)

        if highest_prem > prev_high:
            sl_from_high_step = round(sl_prem + (highest_prem - prev_high), 2)
            sl_prem = max(sl_prem, sl_from_high_step)

        if curr_prem > prev_curr or curr_prem > entry_prem:
            sl_from_live_gap = round(curr_prem - risk_gap, 2)
            if sl_from_live_gap > sl_prem:
                sl_prem = sl_from_live_gap

        if (curr_prem != prev_curr) or (highest_prem > prev_high) or (sl_prem > float(d.get("stopLossPremium", 0))):
            try:
                db.fno_autotrade_positions.update_one(
                    {"_id": d["_id"]},
                    {"$set": {
                        "currentPremium": curr_prem,
                        "highestPremium": highest_prem,
                        "stopLossPremium": sl_prem,
                        "initialStopLossPremium": initial_sl_prem,
                    }}
                )
            except Exception:
                pass

        pnl = round((curr_prem - entry_prem) * qty, 2)
        pnl_pct = round(((curr_prem - entry_prem) / entry_prem * 100.0), 2) if entry_prem > 0 else 0.0
        total_unrealized_pnl += pnl

        entry_time_ist = d.get("entryTimeIST") or format_ist_datetime(d.get("entryTime"))
        entry_date_ist = d.get("entryDateIST") or format_ist_date_key(d.get("entryTime"))
        display_date_ist = d.get("displayDateIST") or format_ist_display_date(d.get("entryTime"))

        profit_target_inr = float(d.get("profitTargetInr", 300.0))
        default_tp_prem = round(entry_prem + (profit_target_inr / max(qty, 1)), 2)

        positions.append({
            "id": str(d["_id"]),
            "symbol": d["symbol"],
            "underlying": d["underlying"],
            "option_type": d["optionType"],
            "strike": float(d["strike"]),
            "expiry": d.get("expiryDate", ""),
            "lots": int(d.get("lots", 1)),
            "lot_size": int(d.get("lotSize", 25)),
            "quantity": qty,
            "entry_premium": entry_prem,
            "current_premium": curr_prem,
            "initial_stop_loss_premium": initial_sl_prem,
            "stop_loss_premium": sl_prem,
            "target_premium": float(d.get("targetPremium", default_tp_prem)),
            "profit_target_inr": profit_target_inr,
            "highest_premium": highest_prem,
            "entry_index_price": entry_idx if entry_idx > 0 else live_ltp,
            "delta": delta,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "trade_mode": d.get("tradeMode", "paper"),
            "ai_confidence": d.get("aiConfidence", 75),
            "signal": d.get("signal", "CALL_BUY"),
            "strategy_name": d.get("strategyName", "VWAP + Supertrend(7,3) + CPR Breakout + NSE Option Chain OI"),
            "entry_reasons": d.get("entryReasons", []),
            "entry_time": entry_time_ist,
            "date_ist": entry_date_ist,
            "display_date_ist": display_date_ist,
        })

    return jsonify({
        "status": "success",
        "positions": positions,
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "open_count": len(positions)
    })


@fno_bp.route("/history", methods=["GET"])
@validate_access_token
def get_fno_trade_history():
    """Fetch completed F&O trade history across all days with IST dates/times and daily P&L summary."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    today_str = get_ist_time().strftime("%Y-%m-%d")
    docs = list(db.fno_autotrade_positions.find({"userId": uid, "status": "CLOSED"}).sort("exitTime", -1).limit(500))
    trades = []
    today_realized_pnl = 0.0
    all_time_realized_pnl = 0.0
    daily_map = {}

    for d in docs:
        pnl = round(float(d.get("realizedPnL", 0.0)), 2)
        all_time_realized_pnl += pnl

        raw_entry = d.get("entryTime")
        raw_exit = d.get("exitTime") or raw_entry
        entry_time_ist = d.get("entryTimeIST") or format_ist_datetime(raw_entry)
        exit_time_ist = d.get("exitTimeIST") or format_ist_datetime(raw_exit)
        date_ist = d.get("exitDateIST") or format_ist_date_key(raw_exit)
        display_date_ist = format_ist_display_date(raw_exit)

        if date_ist == today_str:
            today_realized_pnl += pnl

        trade_item = {
            "id": str(d["_id"]),
            "symbol": d.get("symbol"),
            "underlying": d.get("underlying"),
            "option_type": d.get("optionType"),
            "strike": d.get("strike"),
            "expiry": d.get("expiryDate", ""),
            "lots": d.get("lots", 1),
            "quantity": d.get("quantity"),
            "entry_premium": d.get("entryPremium"),
            "exit_premium": d.get("exitPremium"),
            "exit_reason": d.get("exitReason", "CLOSED"),
            "pnl": pnl,
            "pnl_pct": round(float(d.get("realizedPnLPct", 0.0)), 2),
            "trade_mode": d.get("tradeMode", "paper"),
            "entry_time": entry_time_ist,
            "exit_time": exit_time_ist,
            "date_ist": date_ist,
            "display_date_ist": display_date_ist,
        }
        trades.append(trade_item)

        if date_ist not in daily_map:
            daily_map[date_ist] = {
                "date_ist": date_ist,
                "display_date": display_date_ist,
                "is_today": date_ist == today_str,
                "total_pnl": 0.0,
                "trades_count": 0,
                "wins": 0,
                "losses": 0,
                "trades": [],
            }
        bucket = daily_map[date_ist]
        bucket["total_pnl"] = round(bucket["total_pnl"] + pnl, 2)
        bucket["trades_count"] += 1
        if pnl >= 0:
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["trades"].append(trade_item)

    daily_history = []
    for k in sorted(daily_map.keys(), reverse=True):
        b = daily_map[k]
        cnt = b["trades_count"]
        b["win_rate"] = round((b["wins"] / cnt) * 100.0, 1) if cnt > 0 else 0.0
        b["is_loss_day"] = b["total_pnl"] < 0
        b["status_label"] = "LOSS DAY" if b["total_pnl"] < 0 else ("PROFIT DAY" if b["total_pnl"] > 0 else "BREAKEVEN")
        daily_history.append(b)

    return jsonify({
        "status": "success",
        "today_date_ist": today_str,
        "today_realized_pnl": round(today_realized_pnl, 2),
        "total_realized_pnl": round(today_realized_pnl, 2),
        "all_time_realized_pnl": round(all_time_realized_pnl, 2),
        "history": trades,
        "daily_history": daily_history,
        "total_trades": len(trades)
    })


@fno_bp.route("/emergency_exit", methods=["POST"])
@validate_access_token
def emergency_exit_fno():
    """Panic kill switch: square off all open F&O option positions and disable automation."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    res = fno_autotrade_engine.emergency_exit_all_fno(uid)
    return jsonify(res)


@fno_bp.route("/positions/<pos_id>/exit", methods=["POST"])
@validate_access_token
def exit_single_fno_position(pos_id):
    """Square off a specific open F&O option position manually at locked/instant price."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    exit_price = body.get("exit_price")
    res = fno_autotrade_engine.close_single_fno_position(uid, pos_id, exit_price=exit_price)
    code = 200 if res.get("status") == "success" else 400
    return jsonify(res), code

