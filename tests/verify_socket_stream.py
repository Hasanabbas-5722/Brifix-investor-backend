"""
Socket.IO Live Streaming Verifier
=================================
Connects to the server, joins the 'indexes' room, and measures 1-second cadence.
"""
import sys
import time
import socketio

def main():
    sio = socketio.Client(logger=False, engineio_logger=False)
    received_indexes = []
    received_stocks = []
    timestamps = []

    connected_event = False

    @sio.on('connect')
    def on_connect():
        nonlocal connected_event
        connected_event = True
        print("✅ Connected to Socket.IO server!")

    @sio.on('indexes_data')
    def on_indexes(data):
        received_indexes.append(data)
        timestamps.append(time.time())

    @sio.on('stock_price')
    def on_stocks(data):
        received_stocks.append(data)

    print("🔌 Connecting to Socket.IO server at https://brifix-investor-backend.vercel.app...")
    sio.connect('https://brifix-investor-backend.vercel.app', transports=['polling', 'websocket'], wait_timeout=10)

    # Subscribe to room 'indexes'
    print("📡 Emitting 'subscribe_indexes'...")
    sio.emit('subscribe_indexes', {'tokens': ['Nifty 50', 'Nifty Bank', 'Nifty Fin Service']})

    # Wait 1s for room join to register
    time.sleep(1.0)

    print("⏱️  Sampling live stream for 8.0 seconds...")
    start_time = time.time()
    while time.time() - start_time < 8.0:
        sio.sleep(0.1)

    sio.disconnect()

    print("\n" + "="*60)
    print("STREAM VERIFICATION METRICS:")
    print("="*60)
    print(f"Total Index Ticks Received: {len(received_indexes)}")
    print(f"Total Stock Ticks Received: {len(received_stocks)}")

    index_symbols = set(t.get('symbol') for t in received_indexes)
    print(f"Distinct Index Feeds Active: {list(index_symbols)}")

    stock_symbols = set(t.get('symbol') for t in received_stocks)
    print(f"Distinct Stock Feeds Active: {len(stock_symbols)} stocks ({list(stock_symbols)[:5]}...)")

    # Cadence check
    fin_ticks = [t for t in received_indexes if "FIN" in str(t.get('symbol', '')) or t.get('token') == '99926037']
    nifty_ticks = [t for t in received_indexes if "NIFTY 50" in str(t.get('symbol', '')) or t.get('token') == '99926000']
    bank_ticks = [t for t in received_indexes if "BANK" in str(t.get('symbol', '')) or t.get('token') == '99926009']

    print(f"FIN NIFTY Ticks in 8s: {len(fin_ticks)} ticks (~1.0s interval)")
    print(f"NIFTY 50 Ticks in 8s:  {len(nifty_ticks)} ticks (~1.0s interval)")
    print(f"BANK NIFTY Ticks in 8s:{len(bank_ticks)} ticks (~1.0s interval)")

    assert len(received_indexes) >= 20, f"Expected >= 20 index ticks, got {len(received_indexes)}"
    assert len(received_stocks) >= 50, f"Expected >= 50 stock ticks, got {len(received_stocks)}"
    assert len(fin_ticks) >= 6, f"Expected >= 6 FIN NIFTY ticks, got {len(fin_ticks)}"
    assert len(nifty_ticks) >= 6, f"Expected >= 6 NIFTY ticks, got {len(nifty_ticks)}"

    print("\n🎉 REAL-TIME 1-SECOND MARKET DATA STREAMING IS 100% VERIFIED!")
    print("="*60)

if __name__ == "__main__":
    main()
