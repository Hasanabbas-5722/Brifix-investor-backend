from flask import Blueprint, request, jsonify
from bson import ObjectId
from datetime import datetime
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.models.user import db
from app.services.fno_autotrade_engine import fno_autotrade_engine, check_market_session
from app.services.fno_prediction_service import FNOPredictionService

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


@fno_bp.route("/positions", methods=["GET"])
@validate_access_token
def get_open_fno_positions():
    """Fetch currently active open F&O option positions with real-time premium & P&L."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    # Strictly READ-ONLY: Never evaluate or execute new trades inside a GET request!
    from app.socket.indexes import _shared_quotes
    from app.services.fno_prediction_service import INDEX_SPECS

    docs = list(db.fno_autotrade_positions.find({"userId": uid, "status": "OPEN"}).sort("entryTime", -1))
    positions = []
    total_unrealized_pnl = 0.0

    for d in docs:
        qty = int(d["quantity"])
        entry_prem = float(d["entryPremium"])
        entry_idx = float(d.get("entryIndexPrice", 0.0))
        opt_type = str(d.get("optionType", "CE")).upper()
        delta = float(d.get("delta", 0.50))
        highest_prem = float(d.get("highestPremium", entry_prem))

        # Real-time repricing from live shared quotes
        und = d.get("underlying")
        spec = INDEX_SPECS.get(und, {})
        sym = spec.get("symbol")
        live_quote = _shared_quotes.get(sym, {}) if sym else {}
        live_ltp = float(live_quote.get("ltp", 0.0))

        if live_ltp > 0 and entry_idx > 0:
            idx_diff = live_ltp - entry_idx
            prem_diff = delta * idx_diff if opt_type == "CE" else -delta * idx_diff
            curr_prem = max(round(entry_prem + prem_diff, 2), 1.0)
        else:
            curr_prem = float(d.get("currentPremium", entry_prem))

        highest_prem = max(highest_prem, curr_prem)

        pnl = round((curr_prem - entry_prem) * qty, 2)
        pnl_pct = round(((curr_prem - entry_prem) / entry_prem * 100.0), 2) if entry_prem > 0 else 0.0
        total_unrealized_pnl += pnl

        entry_time_str = d["entryTime"].isoformat() if isinstance(d.get("entryTime"), datetime) else str(d.get("entryTime", ""))

        positions.append({
            "id": str(d["_id"]),
            "symbol": d["symbol"],
            "underlying": d["underlying"],
            "option_type": d["optionType"],
            "strike": float(d["strike"]),
            "lots": int(d.get("lots", 1)),
            "lot_size": int(d.get("lotSize", 25)),
            "quantity": qty,
            "entry_premium": entry_prem,
            "current_premium": curr_prem,
            "stop_loss_premium": float(d.get("stopLossPremium", entry_prem * 0.80)),
            "target_premium": float(d.get("targetPremium", entry_prem * 1.40)),
            "highest_premium": highest_prem,
            "entry_index_price": entry_idx if entry_idx > 0 else live_ltp,
            "delta": delta,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "trade_mode": d.get("tradeMode", "paper"),
            "ai_confidence": d.get("aiConfidence", 75),
            "signal": d.get("signal", "CALL_BUY"),
            "entry_time": entry_time_str
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
    """Fetch completed F&O trade history."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    docs = list(db.fno_autotrade_positions.find({"userId": uid, "status": "CLOSED"}).sort("exitTime", -1).limit(50))
    trades = []
    total_realized_pnl = 0.0

    for d in docs:
        pnl = float(d.get("realizedPnL", 0.0))
        total_realized_pnl += pnl
        entry_time_str = d["entryTime"].isoformat() if isinstance(d.get("entryTime"), datetime) else str(d.get("entryTime", ""))
        exit_time_str = d["exitTime"].isoformat() if isinstance(d.get("exitTime"), datetime) else str(d.get("exitTime", ""))

        trades.append({
            "id": str(d["_id"]),
            "symbol": d.get("symbol"),
            "underlying": d.get("underlying"),
            "option_type": d.get("optionType"),
            "strike": d.get("strike"),
            "lots": d.get("lots", 1),
            "quantity": d.get("quantity"),
            "entry_premium": d.get("entryPremium"),
            "exit_premium": d.get("exitPremium"),
            "exit_reason": d.get("exitReason", "CLOSED"),
            "pnl": pnl,
            "pnl_pct": float(d.get("realizedPnLPct", 0.0)),
            "trade_mode": d.get("tradeMode", "paper"),
            "entry_time": entry_time_str,
            "exit_time": exit_time_str
        })

    return jsonify({
        "status": "success",
        "history": trades,
        "total_realized_pnl": round(total_realized_pnl, 2),
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
    """Square off a specific open F&O option position manually."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    res = fno_autotrade_engine.close_single_fno_position(uid, pos_id)
    code = 200 if res.get("status") == "success" else 400
    return jsonify(res), code

