from flask import Blueprint, request, jsonify
from bson import ObjectId
from datetime import datetime
from app.utils.logger import get_logger
from app.utils.access_token_validate import validate_access_token
from app.models.user import db
from app.services.autotrade_engine import autotrade_engine

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
    cfg = autotrade_engine.toggle_engine(uid, enable)

    # Make sure engine loop is running
    autotrade_engine.start()

    # If enabled, evaluate immediately so user gets trades executed without delay
    if enable:
        try:
            autotrade_engine.evaluate_user(uid)
        except Exception as e:
            logger.error(f"Error during on-demand autotrade evaluation: {e}")

    return jsonify({
        "status": "success",
        "enabled": cfg.get("enabled", False),
        "config": cfg,
        "message": f"Automated trading {'ACTIVATED' if enable else 'STOPPED'}"
    })


@autotrade_bp.route("/config", methods=["GET"])
@validate_access_token
def get_config():
    """Fetch user auto-trade risk configuration and daily metrics."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    cfg = autotrade_engine.get_user_config(uid)
    return jsonify({"status": "success", "config": cfg})


@autotrade_bp.route("/config", methods=["POST"])
@validate_access_token
def update_config():
    """Update risk management parameters."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    updated = autotrade_engine.update_user_config(uid, data)

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

    # If automation is active for this user, evaluate breakout recommendations & position status
    cfg = autotrade_engine.get_user_config(uid)
    if cfg.get("enabled", False):
        try:
            autotrade_engine.evaluate_user(uid)
        except Exception as eval_err:
            logger.debug(f"Position sync evaluation: {eval_err}")

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

        pnl = round((ltp - entry) * qty, 2)
        pnl_pct = round(((ltp - entry) / entry * 100), 2) if entry > 0 else 0
        total_unrealized_pnl += pnl

        entry_time_str = d["entryTime"].isoformat() if isinstance(d.get("entryTime"), datetime) else str(d.get("entryTime", ""))

        positions.append({
            "id": str(d["_id"]),
            "symbol": sym,
            "quantity": qty,
            "entry_price": entry,
            "current_price": ltp,
            "stop_loss_price": float(d.get("stopLossPrice", entry * 0.985)),
            "target_price": float(d.get("targetPrice", entry * 1.03)),
            "highest_price": float(d.get("highestPrice", entry)),
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "trade_mode": d.get("tradeMode", "paper"),
            "ai_confidence": d.get("aiConfidence", 80),
            "entry_time": entry_time_str
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
    """Fetch completed automated trade history."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    docs = list(db.autotrade_positions.find({"userId": uid, "status": "CLOSED"}).sort("exitTime", -1).limit(50))
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
            "quantity": d.get("quantity"),
            "entry_price": d.get("entryPrice"),
            "exit_price": d.get("exitPrice"),
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


@autotrade_bp.route("/emergency_exit", methods=["POST"])
@validate_access_token
def emergency_exit():
    """Instant panic kill switch: square off all open positions and disable automation."""
    uid = _get_uid()
    if not uid:
        return jsonify({"status": "failed", "error": "Unauthorized"}), 401

    res = autotrade_engine.emergency_exit_all(uid)
    return jsonify(res)
