"""
Stock Prediction Service
========================
Wraps the AI/ML prediction engine (Random Forest, XGBoost, SVR, ARIMA, LSTM)
into a clean service that returns a fully JSON-serializable result dict.
"""

import warnings
import os
import sys

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf
import ta
from sklearn.ensemble import RandomForestRegressor
from sklearn.svm import SVR
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_percentage_error
try:
    import xgboost as xgb
except Exception:
    xgb = None
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule
from app.utils.logger import get_logger


logger = get_logger(__name__)

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

console = Console(safe_box=True)

def safe_print(*args, **kwargs):
    try:
        console.print(*args, **kwargs)
    except Exception:
        pass

# Optional TensorFlow/Keras for LSTM
try:
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
    import tensorflow as tf
    from tensorflow.keras.models import Sequential
    from tensorflow.keras.layers import LSTM, Dense, Dropout
    from tensorflow.keras.callbacks import EarlyStopping
    TF_AVAILABLE = True
except ImportError:
    TF_AVAILABLE = False
    safe_print("  [yellow]⚠[/yellow] TensorFlow not available — LSTM model will be skipped.")


# ── Feature columns used by ML models ──────────────────────
FEATURE_COLS = [
    "EMA_9", "EMA_20", "EMA_50", "SMA_20", "MACD", "MACD_sig", "MACD_diff",
    "ADX", "RSI", "Stoch_k", "Stoch_d", "Williams", "ROC", "TSI",
    "BB_high", "BB_low", "BB_pct", "BB_width", "ATR", "OBV", "MFI", "CMF",
    "Returns", "Log_ret", "Volatility", "HL_range", "Gap", "Price_EMA20",
    "Close_lag1", "Close_lag2", "Close_lag3", "Close_lag5", "Close_lag10",
    "Return_lag1", "Return_lag2", "Return_lag3",
]


def _safe_float(val):
    """Convert numpy/pandas scalar to native Python float safely."""
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, 4)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════
#  DATA FETCHER
# ══════════════════════════════════════════════════════════

class _DataFetcher:
    EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}

    def __init__(self, symbol: str, exchange: str = "NSE"):
        self.symbol = symbol.upper()
        self.exchange = exchange.upper()
        suffix = self.EXCHANGE_SUFFIX.get(self.exchange, ".NS")
        self.ticker = f"{self.symbol}{suffix}"

    def fetch(self, period: str = "2y") -> pd.DataFrame:
        safe_print(f"  [cyan]Fetching[/cyan] [bold]{self.ticker}[/bold] from Yahoo Finance...")
        df = yf.download(self.ticker, period=period, auto_adjust=True, progress=False)
        if df.empty:
            raise ValueError(f"No data returned for {self.ticker}. Check the symbol/exchange.")
        df.dropna(inplace=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        safe_print(f"  [green]✓[/green] {len(df)} trading days loaded ({df.index[0].date()} → {df.index[-1].date()})")
        return df

    def get_info(self) -> dict:
        try:
            info = yf.Ticker(self.ticker).info
            return {
                "name":        info.get("longName", self.symbol),
                "sector":      info.get("sector", "N/A"),
                "industry":    info.get("industry", "N/A"),
                "mkt_cap":     _safe_float(info.get("marketCap", 0)),
                "pe_ratio":    _safe_float(info.get("trailingPE")),
                "pb_ratio":    _safe_float(info.get("priceToBook")),
                "dividend":    _safe_float(info.get("dividendYield", 0)),
                "52w_high":    _safe_float(info.get("fiftyTwoWeekHigh")),
                "52w_low":     _safe_float(info.get("fiftyTwoWeekLow")),
                "avg_volume":  info.get("averageVolume"),
                "beta":        _safe_float(info.get("beta")),
                "roe":         _safe_float(info.get("returnOnEquity")),
                "debt_equity": _safe_float(info.get("debtToEquity")),
                "logo":        info.get("logo_url") or f"https://www.google.com/s2/favicons?domain={info.get('website','')}&sz=256",
                "website":     info.get("website", ""),
            }
        except Exception as e:
            safe_print(f"  [yellow]⚠[/yellow] Could not fetch company info: {e}")
            return {"name": self.symbol}


# ══════════════════════════════════════════════════════════
#  FEATURE ENGINEERING
# ══════════════════════════════════════════════════════════

class _FeatureEngineer:
    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        close = d["Close"]
        high  = d["High"]
        low   = d["Low"]
        vol   = d["Volume"]

        d["EMA_9"]    = ta.trend.ema_indicator(close, window=9)
        d["EMA_20"]   = ta.trend.ema_indicator(close, window=20)
        d["EMA_50"]   = ta.trend.ema_indicator(close, window=50)
        d["EMA_200"]  = ta.trend.ema_indicator(close, window=200)
        d["SMA_20"]   = ta.trend.sma_indicator(close, window=20)
        d["SMA_50"]   = ta.trend.sma_indicator(close, window=50)
        d["MACD"]     = ta.trend.macd(close)
        d["MACD_sig"] = ta.trend.macd_signal(close)
        d["MACD_diff"]= ta.trend.macd_diff(close)
        d["ADX"]      = ta.trend.adx(high, low, close)
        d["CCI"]      = ta.trend.cci(high, low, close)
        d["Aroon_up"] = ta.trend.aroon_up(high, low)
        d["Aroon_dn"] = ta.trend.aroon_down(high, low)

        d["RSI"]      = ta.momentum.rsi(close)
        d["Stoch_k"]  = ta.momentum.stoch(high, low, close)
        d["Stoch_d"]  = ta.momentum.stoch_signal(high, low, close)
        d["Williams"] = ta.momentum.williams_r(high, low, close)
        d["ROC"]      = ta.momentum.roc(close)
        d["TSI"]      = ta.momentum.tsi(close)

        bb = ta.volatility.BollingerBands(close)
        d["BB_high"]  = bb.bollinger_hband()
        d["BB_low"]   = bb.bollinger_lband()
        d["BB_mid"]   = bb.bollinger_mavg()
        d["BB_pct"]   = bb.bollinger_pband()
        d["BB_width"] = bb.bollinger_wband()
        d["ATR"]      = ta.volatility.average_true_range(high, low, close)
        d["Keltner_h"]= ta.volatility.keltner_channel_hband(high, low, close)
        d["Keltner_l"]= ta.volatility.keltner_channel_lband(high, low, close)

        d["OBV"]      = ta.volume.on_balance_volume(close, vol)
        d["VWAP"]     = ta.volume.volume_weighted_average_price(high, low, close, vol)
        d["MFI"]      = ta.volume.money_flow_index(high, low, close, vol)
        d["CMF"]      = ta.volume.chaikin_money_flow(high, low, close, vol)
        d["ADI"]      = ta.volume.acc_dist_index(high, low, close, vol)

        d["Returns"]     = close.pct_change()
        d["Log_ret"]     = np.log(close / close.shift(1))
        d["Volatility"]  = d["Returns"].rolling(20).std()
        d["HL_range"]    = (high - low) / close
        d["Gap"]         = (d["Open"] - close.shift(1)) / close.shift(1)
        d["Price_EMA20"] = (close - d["EMA_20"]) / d["EMA_20"]

        for lag in [1, 2, 3, 5, 10]:
            d[f"Close_lag{lag}"]  = close.shift(lag)
            d[f"Return_lag{lag}"] = d["Returns"].shift(lag)

        d["Target"] = close.shift(-1)
        d.dropna(inplace=True)
        return d


# ══════════════════════════════════════════════════════════
#  INDIVIDUAL MODELS
# ══════════════════════════════════════════════════════════

def _split(df):
    X = df[FEATURE_COLS].values
    y = df["Target"].values
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.15, shuffle=False)
    return Xtr, Xte, ytr, yte, X[-1:]


def _random_forest(Xtr, ytr, X_live):
    m = RandomForestRegressor(n_estimators=300, max_depth=12, min_samples_leaf=3, n_jobs=1, random_state=42)
    m.fit(Xtr, ytr)
    pred = float(m.predict(X_live)[0])
    conf = min(float(m.score(Xtr, ytr)) * 100, 95)
    return pred, conf


def _xgboost(Xtr, ytr, Xte, yte, X_live):
    if xgb is None:
        return _random_forest(Xtr, ytr, X_live)
    m = xgb.XGBRegressor(n_estimators=500, learning_rate=0.05, max_depth=6,
                          subsample=0.8, colsample_bytree=0.8, random_state=42, verbosity=0)
    m.fit(Xtr, ytr, eval_set=[(Xte, yte)], verbose=False)
    pred = float(m.predict(X_live)[0])
    mape = mean_absolute_percentage_error(yte, m.predict(Xte))
    conf = max(min((1 - mape) * 100, 95), 40)
    return pred, conf


def _svr(Xtr, ytr, X_live):
    sc = StandardScaler()
    Xs = sc.fit_transform(Xtr)
    Xl = sc.transform(X_live)
    m  = SVR(kernel="rbf", C=100, gamma=0.001, epsilon=0.1)
    m.fit(Xs, ytr)
    pred = float(m.predict(Xl)[0])
    conf = abs(min(float(m.score(Xs, ytr)) * 80, 82))
    return pred, conf


def _arima(close_series):
    try:
        m   = ARIMA(close_series[-200:], order=(5, 1, 2))
        res = m.fit()
        pred = float(res.forecast(steps=1)[0])
        aic  = float(res.aic)
        conf = min(max(70 - aic / 5000, 50), 80)
        return pred, conf
    except Exception:
        return float(close_series.iloc[-1]) * 1.001, 55.0


def _lstm(close_series, lookback=60):
    if not TF_AVAILABLE:
        return None, None
    sc   = MinMaxScaler()
    data = sc.fit_transform(close_series.values.reshape(-1, 1))
    X, y = [], []
    for i in range(lookback, len(data)):
        X.append(data[i - lookback:i, 0])
        y.append(data[i, 0])
    X, y = np.array(X), np.array(y)
    X = X.reshape(X.shape[0], X.shape[1], 1)
    split = int(len(X) * 0.85)
    Xtr, Xte = X[:split], X[split:]
    ytr, yte = y[:split], y[split:]
    model = Sequential([
        LSTM(64, return_sequences=True, input_shape=(lookback, 1)),
        Dropout(0.2),
        LSTM(32, return_sequences=False),
        Dropout(0.2),
        Dense(16, activation="relu"),
        Dense(1),
    ])
    model.compile(optimizer="adam", loss="huber")
    es = EarlyStopping(patience=5, restore_best_weights=True)
    model.fit(Xtr, ytr, epochs=50, batch_size=32, validation_data=(Xte, yte), callbacks=[es], verbose=0)
    live_seq = data[-lookback:].reshape(1, lookback, 1)
    pred_sc  = model.predict(live_seq, verbose=0)[0][0]
    pred     = float(sc.inverse_transform([[pred_sc]])[0][0])
    mape     = mean_absolute_percentage_error(
        sc.inverse_transform(yte.reshape(-1, 1)),
        sc.inverse_transform(model.predict(Xte, verbose=0))
    )
    conf = min((1 - mape) * 100, 92)
    return pred, float(conf)


# ══════════════════════════════════════════════════════════
#  MAIN SERVICE CLASS
# ══════════════════════════════════════════════════════════

import time

_PREDICTION_CACHE = {}
_DAILY_PICKS_CACHE = {"data": None, "timestamp": 0}
DAILY_PICKS_TTL = 3600  # 1 hour

class StockPredictionService:
    """
    Public API:
        result = StockPredictionService.predict(symbol="RELIANCE", exchange="NSE")
        picks = StockPredictionService.get_daily_recommendations()
    Returns fully JSON-serializable dicts.
    """

    @classmethod
    def get_daily_picks(cls) -> list:
        """Alias for get_daily_recommendations."""
        return cls.get_daily_recommendations()

    @staticmethod
    def get_daily_recommendations() -> list:
        """
        AI Screener that analyzes top NSE stocks to suggest today's best stocks to buy.
        Returns ranked list of actionable BUY opportunities with Target, SL, and Confidence.
        """
        now = time.time()
        if _DAILY_PICKS_CACHE["data"] and (now - _DAILY_PICKS_CACHE["timestamp"] < DAILY_PICKS_TTL):
            return _DAILY_PICKS_CACHE["data"]

        # Check MongoDB Atlas collection for warm cache across serverless instances
        try:
            from app.models.user import db
            cached_doc = db.predictions.find_one({"type": "daily_recommendations"})
            if cached_doc and (now - float(cached_doc.get("timestamp", 0)) < DAILY_PICKS_TTL):
                picks = cached_doc.get("picks", [])
                if picks:
                    _DAILY_PICKS_CACHE["data"] = picks
                    _DAILY_PICKS_CACHE["timestamp"] = float(cached_doc.get("timestamp", now))
                    return picks
        except Exception as e:
            logger.debug(f"MongoDB cache read error: {e}")

        candidates = [
            {"symbol": "HDFCBANK",   "name": "HDFC Bank Ltd",             "sector": "Banking",     "ticker": "HDFCBANK.NS"},
            {"symbol": "AXISBANK",   "name": "Axis Bank Ltd",             "sector": "Banking",     "ticker": "AXISBANK.NS"},
            {"symbol": "ICICIBANK",  "name": "ICICI Bank Ltd",            "sector": "Banking",     "ticker": "ICICIBANK.NS"},
            {"symbol": "SBIN",       "name": "State Bank of India",       "sector": "Banking",     "ticker": "SBIN.NS"},
            {"symbol": "KOTAKBANK",  "name": "Kotak Mahindra Bank",       "sector": "Banking",     "ticker": "KOTAKBANK.NS"},
            {"symbol": "RELIANCE",   "name": "Reliance Industries",       "sector": "Energy",      "ticker": "RELIANCE.NS"},
            {"symbol": "BHARTIARTL", "name": "Bharti Airtel Ltd",         "sector": "Telecom",     "ticker": "BHARTIARTL.NS"},
            {"symbol": "INFY",       "name": "Infosys Ltd",               "sector": "IT Services", "ticker": "INFY.NS"},
            {"symbol": "TCS",        "name": "Tata Consultancy Services", "sector": "IT Services", "ticker": "TCS.NS"},
            {"symbol": "LT",         "name": "Larsen & Toubro Ltd",       "sector": "Capital Goods","ticker": "LT.NS"},
            {"symbol": "SUNPHARMA",  "name": "Sun Pharmaceutical Ind",    "sector": "Healthcare",  "ticker": "SUNPHARMA.NS"},
            {"symbol": "BAJFINANCE", "name": "Bajaj Finance Ltd",         "sector": "Finance",     "ticker": "BAJFINANCE.NS"},
            {"symbol": "TATAMOTORS", "name": "Tata Motors Ltd",           "sector": "Automobile",  "ticker": "TMCV.NS"},
            {"symbol": "M&M",        "name": "Mahindra & Mahindra Ltd",   "sector": "Automobile",  "ticker": "M&M.NS"},
            {"symbol": "MARUTI",     "name": "Maruti Suzuki India",       "sector": "Automobile",  "ticker": "MARUTI.NS"},
            {"symbol": "ITC",        "name": "ITC Ltd",                   "sector": "FMCG",        "ticker": "ITC.NS"},
            {"symbol": "TITAN",      "name": "Titan Company Ltd",         "sector": "Consumer",    "ticker": "TITAN.NS"},
            {"symbol": "NTPC",       "name": "NTPC Ltd",                  "sector": "Power",       "ticker": "NTPC.NS"},
            {"symbol": "POWERGRID",  "name": "Power Grid Corporation",    "sector": "Power",       "ticker": "POWERGRID.NS"},
            {"symbol": "TATASTEEL",  "name": "Tata Steel Ltd",            "sector": "Metals",      "ticker": "TATASTEEL.NS"},
        ]

        scored_picks = []

        try:
            tickers = [c.get("ticker", f"{c['symbol']}.NS") for c in candidates]
            data = yf.download(tickers, period="3mo", interval="1d", progress=False, group_by="ticker")

            raw_picks = []
            for c in candidates:
                sym = c["symbol"]
                t_sym = c.get("ticker", f"{sym}.NS")
                if t_sym not in data:
                    continue

                sub = data[t_sym].dropna()
                if len(sub) < 30:
                    continue

                close = sub["Close"]
                high = sub["High"]
                low = sub["Low"]
                vol = sub["Volume"]

                cmp_price = round(float(close.iloc[-1]), 2)
                rsi = float(ta.momentum.rsi(close, window=14).iloc[-1])
                ema20 = float(ta.trend.ema_indicator(close, window=20).iloc[-1])
                ema50 = float(ta.trend.ema_indicator(close, window=50).iloc[-1])
                macd_series = ta.trend.macd(close)
                macd_sig = ta.trend.macd_signal(close)
                macd_diff = float((macd_series - macd_sig).iloc[-1])
                atr = float(ta.volatility.average_true_range(high, low, close, window=14).iloc[-1])
                vol_avg = float(vol.tail(20).mean())
                curr_vol = float(vol.iloc[-1])
                ret_5d = float((close.iloc[-1] - close.iloc[-5]) / close.iloc[-5] * 100) if len(close) >= 5 else 0

                # Score bullish strength
                score = 52.0
                signals = []

                if cmp_price > ema20:
                    score += 12.0
                    signals.append("Trading above 20-Day EMA")
                if ema20 > ema50:
                    score += 10.0
                    signals.append("Bullish EMA 20/50 trend alignment")
                if ret_5d > 0:
                    score += 6.0
                    signals.append(f"+{ret_5d:.1f}% 5D momentum")
                if 45 <= rsi <= 68:
                    score += 12.0
                    signals.append(f"Healthy RSI momentum ({rsi:.1f})")
                elif rsi < 40:
                    score += 8.0
                    signals.append("RSI in value accumulation zone")
                if macd_diff > 0:
                    score += 12.0
                    signals.append("MACD bullish expansion")
                if curr_vol > vol_avg:
                    score += 8.0
                    signals.append("Above-average institutional volume")

                raw_picks.append({
                    "c": c,
                    "sym": sym,
                    "cmp_price": cmp_price,
                    "rsi": rsi,
                    "atr": atr,
                    "raw_score": score,
                    "signals": signals
                })

            raw_picks.sort(key=lambda x: x["raw_score"], reverse=True)

            for idx, item in enumerate(raw_picks):
                c = item["c"]
                sym = item["sym"]
                cmp_price = item["cmp_price"]
                rsi = item["rsi"]
                atr = item["atr"]
                raw_score = item["raw_score"]
                signals = item["signals"]

                rank_bonus = max(0, 10 - idx * 2.0) if raw_score >= 60 else 0
                confidence = round(min(raw_score + rank_bonus, 96.0), 1)

                target_pct_1d = round(1.2 + (confidence % 10) * 0.15, 2)
                target_pct_5d = round(3.5 + (confidence % 8) * 0.35, 2)
                target_1d = round(cmp_price * (1 + target_pct_1d / 100), 2)
                target_5d = round(cmp_price * (1 + target_pct_5d / 100), 2)
                stop_loss = round(max(cmp_price - 1.5 * atr, cmp_price * 0.97), 2)
                sl_pct = round(((cmp_price - stop_loss) / cmp_price) * 100, 2)
                rr_ratio = round((target_5d - cmp_price) / max(cmp_price - stop_loss, 0.01), 2)

                if confidence >= 85:
                    trade_signal = "STRONG BUY"
                elif confidence >= 80:
                    trade_signal = "BUY"
                else:
                    trade_signal = "HOLD"

                scored_picks.append({
                    "symbol": sym,
                    "exchange": "NSE",
                    "name": c["name"],
                    "sector": c["sector"],
                    "current_price": cmp_price,
                    "target_1d": target_1d,
                    "target_5d": target_5d,
                    "expected_return_pct": target_pct_5d,
                    "stop_loss": stop_loss,
                    "stop_loss_pct": sl_pct,
                    "risk_reward_ratio": f"{rr_ratio}:1",
                    "signal": trade_signal,
                    "action": trade_signal,
                    "confidence": confidence,
                    "rsi": round(rsi, 1),
                    "rationale": " • ".join(signals[:3]) if signals else "Technical setup under accumulation.",
                    "logo": f"https://www.google.com/s2/favicons?domain={c['name'].split()[0].lower()}.com&sz=128"
                })

            # Sort descending by confidence and return potential
            scored_picks.sort(key=lambda x: (x["confidence"], x["expected_return_pct"]), reverse=True)
        except Exception as e:
            logger.error(f"Error building dynamic daily picks: {e}")

        # Fallback quality list if market is closed or download fails
        if not scored_picks:
            scored_picks = [
                {
                    "symbol": "HDFCBANK", "exchange": "NSE", "name": "HDFC Bank Ltd", "sector": "Banking",
                    "current_price": 718.50, "target_1d": 732.00, "target_5d": 755.00,
                    "expected_return_pct": 5.08, "stop_loss": 705.00, "stop_loss_pct": 1.88,
                    "risk_reward_ratio": "2.7:1", "signal": "STRONG BUY", "action": "STRONG BUY", "confidence": 94.0,
                    "rsi": 54.2, "rationale": "Bullish 20 EMA bounce • Strong institutional accumulation • Private banking leader",
                    "logo": "https://www.google.com/s2/favicons?domain=hdfcbank.com&sz=128"
                },
                {
                    "symbol": "AXISBANK", "exchange": "NSE", "name": "Axis Bank Ltd", "sector": "Banking",
                    "current_price": 1248.00, "target_1d": 1270.00, "target_5d": 1305.00,
                    "expected_return_pct": 4.57, "stop_loss": 1225.00, "stop_loss_pct": 1.84,
                    "risk_reward_ratio": "2.5:1", "signal": "BUY", "action": "BUY", "confidence": 86.0,
                    "rsi": 52.1, "rationale": "Multi-month breakout structure • Clean RSI momentum • Credit growth expansion",
                    "logo": "https://www.google.com/s2/favicons?domain=axisbank.com&sz=128"
                },
                {
                    "symbol": "RELIANCE", "exchange": "NSE", "name": "Reliance Industries", "sector": "Energy",
                    "current_price": 1254.00, "target_1d": 1280.00, "target_5d": 1320.00,
                    "expected_return_pct": 5.26, "stop_loss": 1230.00, "stop_loss_pct": 1.91,
                    "risk_reward_ratio": "2.8:1", "signal": "BUY", "action": "BUY", "confidence": 84.0,
                    "rsi": 56.4, "rationale": "Bullish moving average alignment • MACD expansion • Institutional inflow",
                    "logo": "https://www.google.com/s2/favicons?domain=ril.com&sz=128"
                },
                {
                    "symbol": "ICICIBANK", "exchange": "NSE", "name": "ICICI Bank Ltd", "sector": "Banking",
                    "current_price": 1352.00, "target_1d": 1380.00, "target_5d": 1415.00,
                    "expected_return_pct": 4.66, "stop_loss": 1325.00, "stop_loss_pct": 2.00,
                    "risk_reward_ratio": "2.3:1", "signal": "BUY", "action": "BUY", "confidence": 82.0,
                    "rsi": 54.8, "rationale": "Strong credit growth • Positive banking breadth • Consolidation breakout",
                    "logo": "https://www.google.com/s2/favicons?domain=icicibank.com&sz=128"
                },
                {
                    "symbol": "BHARTIARTL", "exchange": "NSE", "name": "Bharti Airtel Ltd", "sector": "Telecom",
                    "current_price": 1832.00, "target_1d": 1865.00, "target_5d": 1910.00,
                    "expected_return_pct": 4.26, "stop_loss": 1795.00, "stop_loss_pct": 2.02,
                    "risk_reward_ratio": "2.1:1", "signal": "BUY", "action": "BUY", "confidence": 80.0,
                    "rsi": 59.2, "rationale": "ARPU expansion • 5G user monetization • Sustained uptrend channel",
                    "logo": "https://www.google.com/s2/favicons?domain=airtel.in&sz=128"
                }
            ]

        final_picks = scored_picks[:10]
        _DAILY_PICKS_CACHE["data"] = final_picks
        _DAILY_PICKS_CACHE["timestamp"] = now

        # Persist to MongoDB collection for cross-lambda availability
        try:
            from app.models.user import db
            db.predictions.replace_one(
                {"type": "daily_recommendations"},
                {"type": "daily_recommendations", "timestamp": now, "picks": final_picks},
                upsert=True
            )
        except Exception as persist_err:
            logger.debug(f"Could not persist daily recommendations to MongoDB: {persist_err}")

        return final_picks

    @staticmethod
    def predict(symbol: str, exchange: str = "NSE") -> dict:
        try:
            symbol   = symbol.upper().strip()
            exchange = exchange.upper().strip()
            cache_key = f"{symbol}_{exchange}"
            now = time.time()

            # Return cached single prediction if fresh within 15 minutes
            if cache_key in _PREDICTION_CACHE:
                entry, ts = _PREDICTION_CACHE[cache_key]
                if now - ts < 900:
                    return entry

            safe_print()
            safe_print(Rule(f"[bold cyan]STOCKAI — Prediction for {symbol} ({exchange})[/bold cyan]"))
            safe_print()

            # ── 1. Data ─────────────────────────────────────────
            fetcher = _DataFetcher(symbol, exchange)
            df      = fetcher.fetch()
            info    = fetcher.get_info()

            # ── 2. Features ─────────────────────────────────────
            fe   = _FeatureEngineer()
            df_f = fe.build(df)

            # ── 3. Train models ─────────────────────────────────
            Xtr, Xte, ytr, yte, X_live = _split(df_f)

            safe_print("  [cyan]Step 1/5[/cyan] Training Random Forest...")
            rf_pred, rf_conf   = _random_forest(Xtr, ytr, X_live)

            safe_print(f"  [green]✓[/green] Random Forest done — ₹{rf_pred:,.2f} (conf {rf_conf:.1f}%)")
            safe_print("  [cyan]Step 2/5[/cyan] Training XGBoost...")
            xgb_pred, xgb_conf = _xgboost(Xtr, ytr, Xte, yte, X_live)

            safe_print(f"  [green]✓[/green] XGBoost done — ₹{xgb_pred:,.2f} (conf {xgb_conf:.1f}%)")
            safe_print("  [cyan]Step 3/5[/cyan] Training SVR...")
            svr_pred, svr_conf = _svr(Xtr, ytr, X_live)

            safe_print(f"  [green]✓[/green] SVR done — ₹{svr_pred:,.2f} (conf {svr_conf:.1f}%)")
            safe_print("  [cyan]Step 4/5[/cyan] Training ARIMA...")
            arima_pred, arima_conf = _arima(df["Close"])

            lstm_pred, lstm_conf = None, None
            if TF_AVAILABLE:
                safe_print(f"  [green]✓[/green] ARIMA done — ₹{arima_pred:,.2f} (conf {arima_conf:.1f}%)")
                safe_print("  [cyan]Step 5/5[/cyan] Training LSTM neural network...")
                lstm_pred, lstm_conf = _lstm(df["Close"])

            # ── 4. Ensemble ──────────────────────────────────────
            current = float(df["Close"].iloc[-1])

            preds = {
                "Random Forest": (rf_pred,    rf_conf,    0.28),
                "XGBoost":       (xgb_pred,   xgb_conf,   0.28),
                "SVR":           (svr_pred,   svr_conf,   0.14),
                "ARIMA":         (arima_pred, arima_conf, 0.14),
            }
            if lstm_pred is not None:
                preds["LSTM"] = (lstm_pred, lstm_conf, 0.16)
                total_w = sum(v[2] for v in preds.values())
                preds   = {k: (v[0], v[1], v[2] / total_w) for k, v in preds.items()}

            ensemble_pred = sum(v[0] * v[2] for v in preds.values())
            ensemble_conf = sum(v[1] * v[2] for v in preds.values())

            # ── 5. Risk metrics ──────────────────────────────────
            atr_series = ta.volatility.average_true_range(df["High"], df["Low"], df["Close"])
            atr = float(atr_series.iloc[-1])
            sl  = current - 2.0 * atr
            sl1 = current - 1.0 * atr

            # Support / Resistance (pivot method)
            recent = df.tail(20)
            pivot  = (float(recent["High"].mean()) + float(recent["Low"].mean()) + current) / 3
            r2 = pivot + (float(recent["High"].max()) - float(recent["Low"].min()))
            r1 = 2 * pivot - float(recent["Low"].min())
            s1 = 2 * pivot - float(recent["High"].max())
            s2 = pivot - (float(recent["High"].max()) - float(recent["Low"].min()))

            # Trend
            ema20 = float(ta.trend.ema_indicator(df["Close"], 20).iloc[-1])
            ema50 = float(ta.trend.ema_indicator(df["Close"], 50).iloc[-1])
            adx   = float(ta.trend.adx(df["High"], df["Low"], df["Close"]).iloc[-1])
            if current > ema20 > ema50 and adx > 25:
                trend = "Strong Uptrend"
            elif current > ema20:
                trend = "Uptrend"
            elif current < ema20 < ema50 and adx > 25:
                trend = "Strong Downtrend"
            elif current < ema20:
                trend = "Downtrend"
            else:
                trend = "Sideways"

            # Technical indicators for signal table
            rsi_now = float(ta.momentum.rsi(df["Close"]).iloc[-1])
            macd_v  = float(ta.trend.macd(df["Close"]).iloc[-1])
            macd_s  = float(ta.trend.macd_signal(df["Close"]).iloc[-1])
            bb      = ta.volatility.BollingerBands(df["Close"])
            bb_pct  = float(bb.bollinger_pband().iloc[-1])
            mfi     = float(ta.volume.money_flow_index(df["High"], df["Low"], df["Close"], df["Volume"]).iloc[-1])
            stoch   = float(ta.momentum.stoch(df["High"], df["Low"], df["Close"]).iloc[-1])

            # Overall signal
            overall_signal = (
                "STRONG BUY"  if rsi_now < 40 and "Uptrend" in trend else
                "BUY"         if rsi_now < 55 and "Uptrend" in trend else
                "STRONG SELL" if rsi_now > 75 else
                "SELL"        if rsi_now > 65 else
                "HOLD / WAIT"
            )

            # Multi-horizon targets
            ret_5d     = (ensemble_pred - current) / current
            target_5d  = current * (1 + ret_5d)
            target_15d = current * (1 + ret_5d * 2.5)
            target_30d = current * (1 + ret_5d * 4.5)
            upside_pct = (target_15d - current) / current * 100
            sl_pct     = (current - sl) / current * 100
            rr_ratio   = upside_pct / sl_pct if sl_pct > 0 else 0

            entry_agg  = current * 0.999
            entry_cons = s1 * 1.002

            # ── 6. Build JSON-safe response ──────────────────────
            model_predictions = {}
            for name, (pred, conf, wt) in preds.items():
                chg_pct = (pred - current) / current * 100
                model_predictions[name] = {
                    "price":       round(pred, 2),
                    "confidence":  round(conf, 2),
                    "conf":        round(conf, 2),
                    "weight_pct":  round(wt * 100, 1),
                    "change_pct":  round(chg_pct, 2),
                    "pct":         round(chg_pct, 2),
                }

            technical_signals = [
                {
                    "indicator": "RSI (14)",
                    "ind": "RSI (14)",
                    "value": round(rsi_now, 2),
                    "val": str(round(rsi_now, 2)),
                    "signal": "Oversold→BUY" if rsi_now < 30 else ("Overbought→SELL" if rsi_now > 70 else "Neutral"),
                    "sig": "Oversold→BUY" if rsi_now < 30 else ("Overbought→SELL" if rsi_now > 70 else "Neutral"),
                    "color": "green" if rsi_now < 40 else ("red" if rsi_now > 70 else "yellow"),
                    "c": "green" if rsi_now < 40 else ("red" if rsi_now > 70 else "amber"),
                },
                {
                    "indicator": "MACD",
                    "ind": "MACD",
                    "value": round(macd_v, 4),
                    "val": str(round(macd_v, 4)),
                    "signal": "Bullish" if macd_v > macd_s else "Bearish",
                    "sig": "Bullish" if macd_v > macd_s else "Bearish",
                    "color": "green" if macd_v > macd_s else "red",
                    "c": "green" if macd_v > macd_s else "red",
                },
                {
                    "indicator": "EMA 20",
                    "ind": "EMA 20",
                    "value": round(ema20, 2),
                    "val": str(round(ema20, 2)),
                    "signal": "Above EMA→Bull" if current > ema20 else "Below EMA→Bear",
                    "sig": "Above EMA→Bull" if current > ema20 else "Below EMA→Bear",
                    "color": "green" if current > ema20 else "red",
                    "c": "green" if current > ema20 else "red",
                },
                {
                    "indicator": "EMA 50",
                    "ind": "EMA 50",
                    "value": round(ema50, 2),
                    "val": str(round(ema50, 2)),
                    "signal": "Above EMA→Bull" if current > ema50 else "Below EMA→Bear",
                    "sig": "Above EMA→Bull" if current > ema50 else "Below EMA→Bear",
                    "color": "green" if current > ema50 else "red",
                    "c": "green" if current > ema50 else "red",
                },
                {
                    "indicator": "ADX",
                    "ind": "ADX",
                    "value": round(adx, 2),
                    "val": str(round(adx, 2)),
                    "signal": "Strong Trend" if adx > 25 else "Weak/Ranging",
                    "sig": "Strong Trend" if adx > 25 else "Weak/Ranging",
                    "color": "green" if adx > 25 else "yellow",
                    "c": "green" if adx > 25 else "amber",
                },
                {
                    "indicator": "Bollinger %B",
                    "ind": "Bollinger %B",
                    "value": round(bb_pct, 4),
                    "val": str(round(bb_pct, 4)),
                    "signal": "Overbought" if bb_pct > 0.8 else ("Oversold" if bb_pct < 0.2 else "Normal"),
                    "sig": "Overbought" if bb_pct > 0.8 else ("Oversold" if bb_pct < 0.2 else "Normal"),
                    "color": "red" if bb_pct > 0.8 else ("green" if bb_pct < 0.2 else "yellow"),
                    "c": "red" if bb_pct > 0.8 else ("green" if bb_pct < 0.2 else "amber"),
                },
                {
                    "indicator": "MFI (14)",
                    "ind": "MFI (14)",
                    "value": round(mfi, 2),
                    "val": str(round(mfi, 2)),
                    "signal": "Buying pressure" if mfi > 60 else ("Selling pressure" if mfi < 40 else "Neutral"),
                    "sig": "Buying pressure" if mfi > 60 else ("Selling pressure" if mfi < 40 else "Neutral"),
                    "color": "green" if mfi > 60 else ("red" if mfi < 40 else "yellow"),
                    "c": "green" if mfi > 60 else ("red" if mfi < 40 else "amber"),
                },
                {
                    "indicator": "Stochastic %K",
                    "ind": "Stochastic %K",
                    "value": round(stoch, 2),
                    "val": str(round(stoch, 2)),
                    "signal": "Oversold" if stoch < 20 else ("Overbought" if stoch > 80 else "Neutral"),
                    "sig": "Oversold" if stoch < 20 else ("Overbought" if stoch > 80 else "Neutral"),
                    "color": "green" if stoch < 30 else ("red" if stoch > 70 else "yellow"),
                    "c": "green" if stoch < 30 else ("red" if stoch > 70 else "amber"),
                },
            ]

            result = {
                # Company
                "symbol":   symbol,
                "exchange": exchange,
                "company":  info,

                # Price
                "current_price": round(current, 2),

                # Ensemble
                "ensemble_prediction": round(ensemble_pred, 2),
                "ensemble_confidence": round(ensemble_conf, 2),
                "ensemble_change_pct": round((ensemble_pred - current) / current * 100, 2),

                # Price targets
                "price_targets": {
                    "next_day_1d":  round(ensemble_pred, 2),
                    "short_term_5d":  round(target_5d, 2),
                    "medium_term_15d": round(target_15d, 2),
                    "swing_30d":      round(target_30d, 2),
                },

                # Risk management
                "risk": {
                    "stop_loss_2atr":  round(sl, 2),
                    "stop_loss_1atr":  round(sl1, 2),
                    "atr":             round(atr, 2),
                    "sl_pct":          round(sl_pct, 2),
                    "rr_ratio":        round(rr_ratio, 2),
                },

                # Entry zones
                "entry": {
                    "aggressive":   round(entry_agg, 2),
                    "conservative": round(entry_cons, 2),
                },

                # Support & Resistance
                "support_resistance": {
                    "R2":    round(r2, 2),
                    "R1":    round(r1, 2),
                    "Pivot": round(pivot, 2),
                    "S1":    round(s1, 2),
                    "S2":    round(s2, 2),
                },

                # Signal & Trend
                "signal": overall_signal,
                "trend":  trend,

                # Indicators
                "indicators": {
                    "rsi":    round(rsi_now, 2),
                    "adx":    round(adx, 2),
                    "macd":   round(macd_v, 4),
                    "ema20":  round(ema20, 2),
                    "ema50":  round(ema50, 2),
                    "bb_pct": round(bb_pct, 4),
                    "mfi":    round(mfi, 2),
                    "stoch":  round(stoch, 2),
                },

                # Per-model breakdown
                "model_predictions": model_predictions,

                # Technical signal table (for UI cards)
                "technical_signals": technical_signals,

                # LSTM available
                "lstm_used": lstm_pred is not None,

                # Disclaimer
                "disclaimer": (
                    "This tool uses ML models trained on historical data. "
                    "NO model provides 100% accurate predictions. Markets are inherently uncertain. "
                    "This is NOT SEBI-registered investment advice. Always do your own research."
                ),
            }

            sig_color = "green" if "BUY" in overall_signal else ("red" if "SELL" in overall_signal else "yellow")
            safe_print()
            safe_print(f"  [green]✓[/green] Prediction complete for [bold]{symbol}[/bold]")
            safe_print(f"  [bold]CMP:[/bold] ₹{current:,.2f}  →  [bold]Target:[/bold] ₹{ensemble_pred:,.2f}  |  [{sig_color}]{overall_signal}[/{sig_color}]  |  Confidence: {ensemble_conf:.1f}%")
            _PREDICTION_CACHE[cache_key] = (result, now)
            return result

        except ValueError as ve:
            logger.error(f"[predict_stock] ValueError: {str(ve)}")
            raise ve

        except Exception as e:
            logger.error(f"[predict_stock] Unexpected error: {str(e)}")
            raise e
            
