import time
import threading
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Map string intervals to seconds
INTERVAL_SECONDS = {
    '1m': 60,
    '5m': 300,
    '15m': 900,
    '30m': 1800,
    '1h': 3600,
    '1d': 86400,
}

class CandleAggregator:
    """Aggregates tick-by-tick data into historical and active candles in O(1) time."""
    def __init__(self, token, interval):
        self.token = token
        self.interval = interval
        self.interval_sec = INTERVAL_SECONDS.get(interval, 60)
        self.lock = threading.Lock()
        self.candles = []  # List of historical candles: [{"time": ms, "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}]
        self.dirty = False  # Set to True when the active candle is updated

    def prime_history(self, historical_candles):
        """Seed the aggregator with historical candles fetched from the broker."""
        with self.lock:
            self.candles = list(historical_candles)
            self.dirty = False
            logger.info(f"Primed history for {self.token} ({self.interval}) with {len(self.candles)} candles.")

    def process_tick(self, price, volume, timestamp):
        """Update the active candle or start a new candle based on the tick timestamp."""
        ts_sec = int(timestamp / 1000) if timestamp > 1e11 else int(timestamp)
        # Exchange offset (IST = +19800) to align with lightweight-charts candles
        candle_start = int(ts_sec - (ts_sec % self.interval_sec)) + 19800

        with self.lock:
            if not self.candles:
                # No history yet, create the first candle
                new_candle = {
                    "time": candle_start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume
                }
                self.candles.append(new_candle)
                self.dirty = True
                return

            last_candle = self.candles[-1]

            is_same_candle = (candle_start == last_candle["time"]) or (
                self.interval == '1d' and abs(candle_start - last_candle["time"]) < 86400
            )

            if is_same_candle:
                # Tick belongs to the current active candle
                last_candle["high"] = round(max(last_candle["high"], price), 2)
                last_candle["low"] = round(min(last_candle["low"], price), 2)
                last_candle["close"] = round(price, 2)
                last_candle["volume"] = int(last_candle.get("volume", 0) or 0) + int(volume or 0)
                self.dirty = True
            elif candle_start > last_candle["time"]:
                # New candle interval has started
                new_candle = {
                    "time": candle_start,
                    "open": round(price, 2),
                    "high": round(price, 2),
                    "low": round(price, 2),
                    "close": round(price, 2),
                    "volume": int(volume or 0)
                }
                self.candles.append(new_candle)
                
                # Keep memory usage bounded - keep last 500 candles in memory
                if len(self.candles) > 500:
                    self.candles.pop(0)

                self.dirty = True
            else:
                # Historical timestamp slightly ahead, still update active candle close and bounds
                last_candle["high"] = round(max(last_candle["high"], price), 2)
                last_candle["low"] = round(min(last_candle["low"], price), 2)
                last_candle["close"] = round(price, 2)
                self.dirty = True

    def get_candles(self):
        """Return a copy of all candles."""
        with self.lock:
            return list(self.candles)

    def get_active_candle(self):
        """Return the current active (incomplete) candle."""
        with self.lock:
            if self.candles:
                return dict(self.candles[-1])
            return None


class RealtimeCandleManager:
    """Thread-safe centralized manager for real-time candle data and streaming broadcasts."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(RealtimeCandleManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.aggregators = {}  # key: (token, interval) -> CandleAggregator
        self.active_tokens = set()  # set of tokens currently requested by users
        self.lock = threading.Lock()
        self.socketio = None
        self._initialized = True
        self.broadcaster_running = False

    def init_socketio(self, socketio):
        """Bind SocketIO to this manager."""
        self.socketio = socketio
        self.start_broadcaster()

    def register_token_subscription(self, token, interval='1d'):
        """Mark a token as active when a client subscribes and initialize its aggregator."""
        if not token:
            return
        with self.lock:
            self.active_tokens.add(token)
            agg_key = (token, interval)
            if agg_key not in self.aggregators:
                self.aggregators[agg_key] = CandleAggregator(token, interval)

    def process_tick(self, token, price, volume, timestamp=None):
        """Process an incoming tick and feed it to all aggregators for this token."""
        self.start_broadcaster()

        if timestamp is None:
            timestamp = time.time()

        # Update aggregators for all active timeframes of this token
        with self.lock:
            for interval in list(INTERVAL_SECONDS.keys()):
                agg_key = (token, interval)
                agg = self.aggregators.get(agg_key)
                if agg:
                    agg.process_tick(price, volume, timestamp)
                elif token in self.active_tokens and interval == '1d':
                    agg = CandleAggregator(token, interval)
                    self.aggregators[agg_key] = agg
                    agg.process_tick(price, volume, timestamp)

    def get_aggregator(self, token, interval):
        """Get or create the CandleAggregator for a token and interval."""
        self.start_broadcaster()

        agg_key = (token, interval)
        with self.lock:
            if agg_key not in self.aggregators:
                self.aggregators[agg_key] = CandleAggregator(token, interval)
            return self.aggregators[agg_key]

    def start_broadcaster(self):
        """Start the background task to periodically broadcast updated candles to rooms."""
        with self.lock:
            if self.broadcaster_running or not self.socketio:
                return
            self.broadcaster_running = True
            
            try:
                if getattr(self.socketio, 'server', None):
                    self.socketio.start_background_task(self._broadcast_loop)
                    logger.info("Started real-time candle broadcast background task.")
                    return
            except Exception as e:
                logger.warning(f"Could not start via start_background_task: {e}")

            try:
                import eventlet
                eventlet.spawn(self._broadcast_loop)
                logger.info("Started real-time candle broadcast via eventlet.spawn.")
            except Exception:
                t = threading.Thread(target=self._broadcast_loop, daemon=True)
                t.start()
                logger.info("Started real-time candle broadcast via threading.Thread.")

    def _broadcast_loop(self):
        """Periodic broadcast loop checking for dirty candles and pushing updates (throttled)."""
        import eventlet
        while True:
            try:
                # Broadcast interval: 500ms for smooth UI updates under high load
                eventlet.sleep(0.5)

                # Collect all dirty candle updates
                updates = []
                with self.lock:
                    for agg_key, agg in list(self.aggregators.items()):
                        if agg.dirty:
                            active_candle = agg.get_active_candle()
                            if active_candle:
                                updates.append((agg.token, agg.interval, active_candle))
                            agg.dirty = False

                # Broadcast updates to their respective rooms
                for token, interval, candle in updates:
                    room = f"chart_{token}_{interval}"
                    
                    # Emit real-time candle update
                    self.socketio.emit('chart_data', {
                        'type': 'realtime',
                        'symbol': token,  # Symbol/token identification
                        'candle': candle,
                        'currentPrice': candle['close']
                    }, room=room)

            except Exception as e:
                logger.error(f"Error in candle broadcaster loop: {e}")

# Global singleton instance
realtime_candle_manager = RealtimeCandleManager()
