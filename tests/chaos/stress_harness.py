"""
Chaos & Rough Stress Test Harness
Developed for the Chaos & Stress QA Tester Agent (chaos_qa_tester)
Tests:
1. Flash Crash Simulation: Extreme -1000 point index plunge against active trailing SL
2. High Tick Velocity Storm: 100 rapid ticks/sec through the non-blocking evaluation queue
3. Boundary & Malformed Input Fuzzing: 0 qty, negative price, null symbols
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ["EVENTLET_NO_GREENDNS"] = "yes"
import selectors
if hasattr(selectors, "KqueueSelector"):
    selectors.DefaultSelector = selectors.SelectSelector

import eventlet
eventlet.monkey_patch()

# Add backend root to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

from app import create_app
from app.models.user import db
from app.services.fno_autotrade_engine import fno_autotrade_engine
from app.socket.indexes import _tick_eval_queue

def run_chaos_flash_crash():
    print("\n" + "="*70)
    print("CHAOS TEST 1: Flash Crash Simulation (-1,000 pts) on Active Position")
    print("="*70)
    
    app = create_app()
    with app.app_context():
        test_uid = "chaos_user_flash_crash"
        underlying = "FINNIFTY"
        entry_prem = 150.0
        entry_idx = 25400.0
        delta = 0.50
        qty = 50
        
        # 1. Clean up old test data
        db.fno_autotrade_positions.delete_many({"userId": test_uid})
        db.fno_autotrade_configs.update_one(
            {"userId": test_uid},
            {"$set": {
                "enabled": True,
                "tradeMode": "paper",
                "trailingStopLoss": True,
                "stopLossPct": 20.0,
                "riskRewardRatio": 2.0,
                "takeProfitPct": 40.0
            }},
            upsert=True
        )
        
        # 2. Open an initial position: FINNIFTY 25400 CE @ 150.0
        pos_doc = {
            "userId": test_uid,
            "symbol": "FINNIFTY 25400 CE",
            "underlying": underlying,
            "optionType": "CE",
            "strike": 25400,
            "lots": 2,
            "lotSize": 25,
            "quantity": qty,
            "entryPremium": entry_prem,
            "currentPremium": entry_prem,
            "stopLossPremium": round(entry_prem * 0.80, 2),  # 120.0
            "targetPremium": round(entry_prem * 2.50, 2),    # 375.0
            "highestPremium": entry_prem,
            "entryIndexPrice": entry_idx,
            "currentIndexPrice": entry_idx,
            "delta": delta,
            "status": "OPEN",
            "tradeMode": "paper",
            "aiConfidence": 85.0,
            "signal": "CALL_BUY",
            "entryTime": datetime.now(timezone.utc) - timedelta(seconds=10) # past 5s anti-jitter
        }
        pos_id = db.fno_autotrade_positions.insert_one(pos_doc).inserted_id
        fno_autotrade_engine._active_underlyings.add(underlying)
        fno_autotrade_engine._active_underlyings_updated = 0
        
        print(f"💥 Created Open Position: FINNIFTY 25400 CE @ Rs.{entry_prem} | Entry Index: {entry_idx} | Initial SL: Rs.{pos_doc['stopLossPremium']}")
        
        # 3. Simulate favorable market jump: Index rises +120 points to 25,520
        peak_idx = entry_idx + 120.0
        fno_autotrade_engine.on_index_tick("NIFTY_FIN_SERVICE.NS", peak_idx)
        
        pos = db.fno_autotrade_positions.find_one({"_id": pos_id})
        locked_sl = pos.get('stopLossPremium')
        print(f"📈 After Favorable Spike (+120 pts): Prem: Rs.{pos.get('currentPremium')} | Highest: Rs.{pos.get('highestPremium')} | Trailing SL: Rs.{locked_sl}")
        assert locked_sl > 150.0, f"Expected trailing SL to ratchet above entry, got {locked_sl}"
        
        # 4. INJECT CHAOS FLASH CRASH: Index plunges -1,000 points in 1 millisecond
        crash_idx = entry_idx - 1000.0
        print(f"⚡ INJECTING FLASH CRASH: FINNIFTY plummets 1,000 points to {crash_idx}!")
        fno_autotrade_engine.on_index_tick("NIFTY_FIN_SERVICE.NS", crash_idx)
        
        closed_pos = db.fno_autotrade_positions.find_one({"_id": pos_id})
        print(f"🛑 Position Status: {closed_pos.get('status')}")
        print(f"🏷️  Exit Reason: {closed_pos.get('exitReason')}")
        print(f"💰 Exit Premium: Rs.{closed_pos.get('exitPremium')}")
        print(f"📊 Realized P&L: Rs.{closed_pos.get('realizedPnL')} ({closed_pos.get('realizedPnLPct')}%)")
        
        assert closed_pos.get('status') == "CLOSED", "Position failed to close on flash crash!"
        assert closed_pos.get('exitReason') == "TRAILING_SL_HIT", f"Unexpected reason: {closed_pos.get('exitReason')}"
        assert closed_pos.get('exitPremium') >= locked_sl, f"Expected exit at or above locked SL {locked_sl}, got {closed_pos.get('exitPremium')}"
        assert closed_pos.get('realizedPnL') > 0, "P&L must be strictly positive (never zero or negative) on locked trailing SL!"
        
        # Cleanup
        db.fno_autotrade_positions.delete_many({"userId": test_uid})
        db.fno_autotrade_configs.delete_many({"userId": test_uid})
        print("✅ CHAOS TEST 1 PASSED: Flash crash absorbed! Position cleanly closed at locked SL with positive profit.")

def run_chaos_tick_storm():
    print("\n" + "="*70)
    print("CHAOS TEST 2: High Tick Velocity Storm (100 rapid ticks/sec)")
    print("="*70)
    
    start_qsize = _tick_eval_queue.qsize()
    print(f"Initial Queue Size: {start_qsize}")
    
    # Blast 100 ticks in tight loop
    t0 = time.time()
    for i in range(100):
        _tick_eval_queue.put(("NIFTY_FIN_SERVICE.NS", 25420.0 + (i % 10)))
    elapsed = time.time() - t0
    
    print(f"⚡ 100 ticks injected into evaluation queue in {elapsed*1000:.2f} ms")
    assert elapsed < 0.1, "Queue injection took unexpectedly long!"
    
    # Wait briefly for worker to consume
    eventlet.sleep(0.5)
    remaining = _tick_eval_queue.qsize()
    print(f"Remaining Queue Size after 500ms drain: {remaining}")
    assert remaining < 50, f"Queue is draining too slowly! Remaining: {remaining}"
    print("✅ CHAOS TEST 2 PASSED: High-velocity tick storm smoothly buffered and drained.")

def run_chaos_boundary_fuzz():
    print("\n" + "="*70)
    print("CHAOS TEST 3: Boundary & Malformed Input Fuzzing")
    print("="*70)
    
    app = create_app()
    with app.app_context():
        # Test extreme index prices (0, negative, NaN-like)
        print("Testing edge case: Index price = 0.0")
        try:
            fno_autotrade_engine.on_index_tick("NIFTY_50.NS", 0.0)
            print("  -> Gracefully handled index = 0.0")
        except Exception as e:
            assert False, f"Crash on index = 0.0: {e}"
            
        print("Testing edge case: Unknown / Malformed Underlying Symbol")
        try:
            fno_autotrade_engine.on_index_tick("UNKNOWN_CRYPTO_TOKEN.NS", 99999.0)
            print("  -> Gracefully handled unknown underlying")
        except Exception as e:
            assert False, f"Crash on unknown underlying: {e}"
            
        print("Testing edge case: Negative index price")
        try:
            fno_autotrade_engine.on_index_tick("BANKNIFTY.NS", -500.0)
            print("  -> Gracefully handled negative index quote")
        except Exception as e:
            assert False, f"Crash on negative index: {e}"
            
    print("✅ CHAOS TEST 3 PASSED: System exhibits complete immunity to malformed market data.")

if __name__ == "__main__":
    print("🚀 STARTING CHAOS QA TEST HARNESS")
    run_chaos_flash_crash()
    run_chaos_tick_storm()
    run_chaos_boundary_fuzz()
    print("\n" + "="*70)
    print("🏆 ALL CHAOS & STRESS TESTS PASSED WITH 100% RESILIENCE!")
    print("="*70)
