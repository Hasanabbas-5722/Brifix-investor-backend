from flask import Blueprint, request, jsonify
from bson import ObjectId
from datetime import datetime
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.models.user import db
from app.services.autotrade_engine import autotrade_engine, check_market_session
from app.utils.market_calendar import (
    get_ist_time,
    format_ist_datetime,
    format_ist_date_key,
    format_ist_display_date,
)

logger = get_logger(__name__)
autotrade_bp = Blueprint("autotrade", __name__, url_prefix="/api/v1/autotrade")


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


@autotrade_bp.route("/toggle", methods=["POST"])
@validate_access_token
def toggle_auto_trade():
    """Toggle master automated trading switch."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enabled", False))
    try:
        cfg = autotrade_engine.toggle_engine(uid, enable)
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 400

    # Ensure background worker thread is running
    autotrade_engine.start()

    eval_result = {}
    is_open, status_code, status_msg = check_market_session()

    if enable:
        if is_open:
            try:
                eval_result = autotrade_engine.evaluate_user(uid, force_scan=True)
            except Exception as e:
                logger.error(f"Error during on-demand autotrade evaluation: {e}")

            new_positions = eval_result.get("new_positions", []) if eval_result else []
            new_count = len(new_positions)
            if new_count > 0:
                msg = f"Automated trading ACTIVATED. AI scanned Indian stocks and opened {new_count} positions with >80% confidence."
            else:
                msg = "Automated trading ACTIVATED. Engine scanning for setups with >80% AI confidence."
        else:
            msg = f"Automated trading ACTIVATED in Standby Mode. {status_msg}"
    else:
        msg = "Automated trading STOPPED."

    return jsonify({
        "status": "success",
        "enabled": cfg.get("enabled", False),
        "config": cfg,
        "market_session": {"is_open": is_open, "status": status_code, "message": status_msg},
        "eval_result": eval_result,
        "message": msg
    })


@autotrade_bp.route("/scan", methods=["POST"])
@validate_access_token
def scan_and_trade():
    """Immediately scan top Indian stocks, run AI predictions, and execute trades for >80% confidence picks."""
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
        eval_result = autotrade_engine.evaluate_user(uid, force_scan=True)
    except Exception as e:
        logger.error(f"Error during manual scan: {e}")
        return jsonify({"status": "failed", "error": str(e)}), 500

    new_count = len(eval_result.get("new_positions", [])) if eval_result else 0
    qual_count = eval_result.get("qualified_count", 0) if eval_result else 0
    scanned = eval_result.get("scanned_count", 0) if eval_result else 0

    return jsonify({
        "status": "success",
        "result": eval_result,
        "message": f"AI Scan complete: Analyzed {scanned} Indian stocks, found {qual_count} picks with >80% confidence. Opened {new_count} new trades."
    })


@autotrade_bp.route("/config", methods=["GET"])
@validate_access_token
def get_config():
    """Fetch user auto-trade risk configuration and daily metrics."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    is_open, status_code, status_msg = check_market_session()
    cfg = autotrade_engine.get_user_config(uid)
    return jsonify({
        "status": "success",
        "config": cfg,
        "market_session": {"is_open": is_open, "status": status_code, "message": status_msg}
    })


@autotrade_bp.route("/config", methods=["POST"])
@validate_access_token
def update_config():
    """Update risk management parameters."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    try:
        updated = autotrade_engine.update_user_config(uid, data)
    except Exception as e:
        return jsonify({"status": "failed", "error": str(e)}), 400

    return jsonify({
        "status": "success",
        "message": "Risk parameters updated successfully",
        "config": updated
    })


@autotrade_bp.route("/positions", methods=["GET"])
@validate_access_token
def get_open_positions():
    """Fetch currently active open auto-traded positions with live P&L."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    docs = list(db.autotrade_positions.find({"userId": uid, "status": "OPEN"}).sort("entryTime", -1))
    from app.socket.indexes import _shared_quotes

    positions = []
    total_unrealized_pnl = 0.0

    for d in docs:
        sym = d["symbol"]
        qty = int(d["quantity"])
        entry = float(d["entryPrice"])
        q = _shared_quotes.get(f"{sym}.NS") or _shared_quotes.get(sym) or {}
        ltp = float(q.get("ltp", entry))

        prev_high = float(d.get("highestPrice", entry))
        prev_curr = float(d.get("currentPrice", entry))
        highest = max(prev_high, ltp)
        initial_sl = float(d.get("initialStopLossPrice", round(entry * 0.985, 2)))
        stored_sl = float(d.get("stopLossPrice", initial_sl))

        raw_gap = entry - initial_sl
        risk_gap = max(raw_gap, 1.0) if raw_gap > 0 else round(entry * 0.015, 2)
        live_sl = stored_sl

        if highest > entry:
            sl_from_entry = round(initial_sl + (highest - entry), 2)
            live_sl = max(live_sl, sl_from_entry)

        if highest > prev_high:
            sl_from_high_step = round(stored_sl + (highest - prev_high), 2)
            live_sl = max(live_sl, sl_from_high_step)

        if ltp > prev_curr or ltp > entry:
            sl_from_live_gap = round(ltp - risk_gap, 2)
            if sl_from_live_gap > live_sl:
                live_sl = sl_from_live_gap

        if (ltp != prev_curr) or (highest > prev_high) or (live_sl > stored_sl):
            try:
                db.autotrade_positions.update_one(
                    {"_id": d["_id"]},
                    {"$set": {
                        "currentPrice": ltp,
                        "highestPrice": highest,
                        "stopLossPrice": live_sl,
                        "initialStopLossPrice": initial_sl,
                    }}
                )
            except Exception:
                pass

        pnl = round((ltp - entry) * qty, 2)
        pnl_pct = round(((ltp - entry) / entry * 100), 2) if entry > 0 else 0
        total_unrealized_pnl += pnl

        entry_time_ist = d.get("entryTimeIST") or format_ist_datetime(d.get("entryTime"))
        entry_date_ist = d.get("entryDateIST") or format_ist_date_key(d.get("entryTime"))
        display_date_ist = d.get("displayDateIST") or format_ist_display_date(entry_date_ist)

        positions.append({
            "id": str(d["_id"]),
            "symbol": sym,
            "quantity": qty,
            "entry_price": entry,
            "current_price": ltp,
            "initial_stop_loss_price": initial_sl,
            "stop_loss_price": live_sl,
            "target_price": float(d.get("targetPrice", entry * 1.045)),
            "highest_price": highest,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "trade_mode": d.get("tradeMode", "paper"),
            "ai_confidence": d.get("aiConfidence", 80),
            "entry_time": entry_time_ist,
            "entry_date_ist": entry_date_ist,
            "display_date_ist": display_date_ist,
        })

    return jsonify({
        "status": "success",
        "positions": positions,
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "open_count": len(positions)
    })


@autotrade_bp.route("/history", methods=["GET"])
@validate_access_token
def get_trade_history():
    """Fetch completed automated trade history with all-day date-wise breakdown in IST."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    docs = list(db.autotrade_positions.find({"userId": uid, "status": "CLOSED"}).sort("exitTime", -1).limit(500))
    trades = []
    total_realized_pnl = 0.0
    today_realized_pnl = 0.0
    today_str = get_ist_time().strftime("%Y-%m-%d")
    daily_groups = {}

    for d in docs:
        pnl = round(float(d.get("realizedPnL", 0.0)), 2)
        total_realized_pnl += pnl

        entry_dt = d.get("entryTime")
        exit_dt = d.get("exitTime") or entry_dt
        entry_time_ist = d.get("entryTimeIST") or format_ist_datetime(entry_dt)
        exit_time_ist = d.get("exitTimeIST") or format_ist_datetime(exit_dt)
        date_ist = d.get("exitDateIST") or format_ist_date_key(exit_dt)
        display_date_ist = format_ist_display_date(date_ist)

        if date_ist == today_str:
            today_realized_pnl += pnl

        trade_item = {
            "id": str(d["_id"]),
            "symbol": d.get("symbol"),
            "quantity": d.get("quantity"),
            "entry_price": d.get("entryPrice"),
            "exit_price": d.get("exitPrice"),
            "exit_reason": d.get("exitReason", "CLOSED"),
            "pnl": pnl,
            "pnl_pct": float(d.get("realizedPnLPct", 0.0)),
            "trade_mode": d.get("tradeMode", "paper"),
            "entry_time": entry_time_ist,
            "exit_time": exit_time_ist,
            "date_ist": date_ist,
            "display_date_ist": display_date_ist,
        }
        trades.append(trade_item)

        if date_ist not in daily_groups:
            daily_groups[date_ist] = {
                "date_ist": date_ist,
                "display_date_ist": display_date_ist,
                "is_today": date_ist == today_str,
                "total_pnl": 0.0,
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "trades": [],
            }
        grp = daily_groups[date_ist]
        grp["total_pnl"] = round(grp["total_pnl"] + pnl, 2)
        grp["total_trades"] += 1
        if pnl >= 0:
            grp["wins"] += 1
        else:
            grp["losses"] += 1
        grp["trades"].append(trade_item)

    daily_history = []
    for date_key in sorted(daily_groups.keys(), reverse=True):
        grp = daily_groups[date_key]
        grp["is_loss_day"] = grp["total_pnl"] < 0
        grp["status_label"] = (
            "LOSS DAY" if grp["total_pnl"] < 0 else ("PROFIT DAY" if grp["total_pnl"] > 0 else "BREAKEVEN")
        )
        grp["win_rate"] = round((grp["wins"] / grp["total_trades"]) * 100.0, 1) if grp["total_trades"] > 0 else 0.0
        daily_history.append(grp)

    return jsonify({
        "status": "success",
        "history": trades,
        "daily_history": daily_history,
        "today_date_ist": today_str,
        "today_realized_pnl": round(today_realized_pnl, 2),
        "total_realized_pnl": round(today_realized_pnl, 2),
        "all_time_realized_pnl": round(total_realized_pnl, 2),
        "total_trades": len(trades)
    })


@autotrade_bp.route("/positions/<pos_id>/exit", methods=["POST"])
@validate_access_token
def exit_single_position(pos_id):
    """Square off a single open equity position immediately with zero slippage."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    exit_price = data.get("exit_price")
    res = autotrade_engine.close_single_position(uid, pos_id, exit_price=exit_price)
    if res.get("status") == "failed":
        return jsonify(res), 400
    return jsonify(res)


@autotrade_bp.route("/emergency_exit", methods=["POST"])
@validate_access_token
def emergency_exit():
    """Instant panic kill switch: square off all open positions and disable automation."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    res = autotrade_engine.emergency_exit_all(uid)
    return jsonify(res)
