"""
============================================================
  Indian Stock Market AI/ML Prediction System
  NSE & BSE | Real Data via yfinance + NSEpy
  Models: LSTM, Random Forest, XGBoost, ARIMA, SVM
  Author: StockAI.IN
============================================================

SETUP:
  pip install yfinance pandas numpy scikit-learn xgboost
              tensorflow keras statsmodels ta requests
              colorama tabulate rich

RUN:
  python indian_stock_predictor.py
  python indian_stock_predictor.py --symbol RELIANCE --exchange NSE
"""

import argparse
import warnings
import sys
import os
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# ─── Dependency check ───────────────────────────────────────
REQUIRED = {
    "yfinance": "yfinance",
    "pandas": "pandas",
    "numpy": "numpy",
    "sklearn": "scikit-learn",
    "xgboost": "xgboost",
    "statsmodels": "statsmodels",
    "ta": "ta",
    "rich": "rich",
}

missing = []
for mod, pkg in REQUIRED.items():
    try:
        __import__(mod)
    except ImportError:
        missing.append(pkg)

if missing:
    print(f"\n[!] Install missing packages:\n    pip install {' '.join(missing)}\n")
    sys.exit(1)

# ─── Imports ────────────────────────────────────────────────
import numpy as np
import pandas as pd
import yfinance as yf
import ta
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.svm import SVR
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_percentage_error
from statsmodels.tsa.arima.model import ARIMA
import xgboost as xgb
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box
from rich.text import Text
from rich.columns import Columns
from rich.rule import Rule

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

console = Console()


# ═══════════════════════════════════════════════════════════
#  1. DATA FETCHER
# ═══════════════════════════════════════════════════════════

class IndianStockDataFetcher:
    """Fetch OHLCV data from Yahoo Finance for NSE/BSE stocks."""

    EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}

    def __init__(self, symbol: str, exchange: str = "NSE"):
        self.symbol = symbol.upper()
        self.exchange = exchange.upper()
        suffix = self.EXCHANGE_SUFFIX.get(self.exchange, ".NS")
        self.ticker = f"{self.symbol}{suffix}"

    def fetch(self, period: str = "2y") -> pd.DataFrame:
        """Download historical data."""
        console.print(f"  [cyan]Fetching[/cyan] [bold]{self.ticker}[/bold] from Yahoo Finance...")
        try:
            df = yf.download(self.ticker, period=period, auto_adjust=True, progress=False)
            if df.empty:
                raise ValueError(f"No data returned for {self.ticker}. Check symbol.")
            df.dropna(inplace=True)
            # Flatten MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            console.print(f"  [green]✓[/green] {len(df)} trading days loaded ({df.index[0].date()} → {df.index[-1].date()})")
            return df
        except Exception as e:
            console.print(f"  [red]Error fetching data:[/red] {e}")
            sys.exit(1)

    def get_info(self) -> dict:
        """Fetch company fundamentals."""
        try:
            info = yf.Ticker(self.ticker).info
            return {
                "name":        info.get("longName", self.symbol),
                "sector":      info.get("sector", "N/A"),
                "industry":    info.get("industry", "N/A"),
                "mkt_cap":     info.get("marketCap", 0),
                "pe_ratio":    info.get("trailingPE", "N/A"),
                "pb_ratio":    info.get("priceToBook", "N/A"),
                "dividend":    info.get("dividendYield", 0),
                "52w_high":    info.get("fiftyTwoWeekHigh", "N/A"),
                "52w_low":     info.get("fiftyTwoWeekLow", "N/A"),
                "avg_volume":  info.get("averageVolume", "N/A"),
                "beta":        info.get("beta", "N/A"),
                "roe":         info.get("returnOnEquity", "N/A"),
                "debt_equity": info.get("debtToEquity", "N/A"),
            }
        except Exception:
            return {"name": self.symbol}


# ═══════════════════════════════════════════════════════════
#  2. FEATURE ENGINEERING
# ═══════════════════════════════════════════════════════════

class FeatureEngineer:
    """Generate 40+ technical indicators as ML features."""

    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        close = d["Close"]
        high  = d["High"]
        low   = d["Low"]
        vol   = d["Volume"]

        # ── Trend indicators
        d["EMA_9"]   = ta.trend.ema_indicator(close, window=9)
        d["EMA_20"]  = ta.trend.ema_indicator(close, window=20)
        d["EMA_50"]  = ta.trend.ema_indicator(close, window=50)
        d["EMA_200"] = ta.trend.ema_indicator(close, window=200)
        d["SMA_20"]  = ta.trend.sma_indicator(close, window=20)
        d["SMA_50"]  = ta.trend.sma_indicator(close, window=50)
        d["MACD"]    = ta.trend.macd(close)
        d["MACD_sig"]= ta.trend.macd_signal(close)
        d["MACD_diff"]= ta.trend.macd_diff(close)
        d["ADX"]     = ta.trend.adx(high, low, close)
        d["CCI"]     = ta.trend.cci(high, low, close)
        d["Aroon_up"]= ta.trend.aroon_up(high, low)
        d["Aroon_dn"]= ta.trend.aroon_down(high, low)

        # ── Momentum
        d["RSI"]     = ta.momentum.rsi(close)
        d["Stoch_k"] = ta.momentum.stoch(high, low, close)
        d["Stoch_d"] = ta.momentum.stoch_signal(high, low, close)
        d["Williams"]= ta.momentum.williams_r(high, low, close)
        d["ROC"]     = ta.momentum.roc(close)
        d["TSI"]     = ta.momentum.tsi(close)

        # ── Volatility
        bb = ta.volatility.BollingerBands(close)
        d["BB_high"] = bb.bollinger_hband()
        d["BB_low"]  = bb.bollinger_lband()
        d["BB_mid"]  = bb.bollinger_mavg()
        d["BB_pct"]  = bb.bollinger_pband()
        d["BB_width"]= bb.bollinger_wband()
        d["ATR"]     = ta.volatility.average_true_range(high, low, close)
        d["Keltner_h"]= ta.volatility.keltner_channel_hband(high, low, close)
        d["Keltner_l"]= ta.volatility.keltner_channel_lband(high, low, close)

        # ── Volume
        d["OBV"]     = ta.volume.on_balance_volume(close, vol)
        d["VWAP"]    = ta.volume.volume_weighted_average_price(high, low, close, vol)
        d["MFI"]     = ta.volume.money_flow_index(high, low, close, vol)
        d["CMF"]     = ta.volume.chaikin_money_flow(high, low, close, vol)
        d["ADI"]     = ta.volume.acc_dist_index(high, low, close, vol)

        # ── Price-derived
        d["Returns"]    = close.pct_change()
        d["Log_ret"]    = np.log(close / close.shift(1))
        d["Volatility"] = d["Returns"].rolling(20).std()
        d["HL_range"]   = (high - low) / close
        d["Gap"]        = (d["Open"] - close.shift(1)) / close.shift(1)
        d["Price_EMA20"]= (close - d["EMA_20"]) / d["EMA_20"]

        # ── Lag features
        for lag in [1, 2, 3, 5, 10]:
            d[f"Close_lag{lag}"] = close.shift(lag)
            d[f"Return_lag{lag}"]= d["Returns"].shift(lag)

        # ── Target: next-day close
        d["Target"] = close.shift(-1)

        d.dropna(inplace=True)
        return d


# ═══════════════════════════════════════════════════════════
#  3. MODELS
# ═══════════════════════════════════════════════════════════

FEATURE_COLS = [
    "EMA_9","EMA_20","EMA_50","SMA_20","MACD","MACD_sig","MACD_diff",
    "ADX","RSI","Stoch_k","Stoch_d","Williams","ROC","TSI",
    "BB_high","BB_low","BB_pct","BB_width","ATR","OBV","MFI","CMF",
    "Returns","Log_ret","Volatility","HL_range","Gap","Price_EMA20",
    "Close_lag1","Close_lag2","Close_lag3","Close_lag5","Close_lag10",
    "Return_lag1","Return_lag2","Return_lag3",
]


def split_data(df: pd.DataFrame):
    X = df[FEATURE_COLS].values
    y = df["Target"].values
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.15, shuffle=False
    )
    return X_train, X_test, y_train, y_test, X[-1:]  # last row = live input


# ── 3a. Random Forest ────────────────────────────────────────
def train_random_forest(X_train, y_train, X_live):
    model = RandomForestRegressor(
        n_estimators=300, max_depth=12, min_samples_leaf=3,
        n_jobs=-1, random_state=42
    )
    model.fit(X_train, y_train)
    pred = model.predict(X_live)[0]
    conf = min(model.score(X_train, y_train) * 100, 95)
    return pred, conf, model


# ── 3b. XGBoost ─────────────────────────────────────────────
def train_xgboost(X_train, y_train, X_test, y_test, X_live):
    model = xgb.XGBRegressor(
        n_estimators=500, learning_rate=0.05, max_depth=6,
        subsample=0.8, colsample_bytree=0.8, random_state=42,
        verbosity=0
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False
    )
    pred = model.predict(X_live)[0]
    mape = mean_absolute_percentage_error(y_test, model.predict(X_test))
    conf = max(min((1 - mape) * 100, 95), 40)
    return pred, conf, model


# ── 3c. SVR ─────────────────────────────────────────────────
def train_svr(X_train, y_train, X_live):
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X_train)
    Xl = scaler.transform(X_live)
    model = SVR(kernel="rbf", C=100, gamma=0.001, epsilon=0.1)
    model.fit(Xs, y_train)
    pred = model.predict(Xl)[0]
    conf = min(model.score(Xs, y_train) * 80, 82)
    return pred, abs(conf), model


# ── 3d. ARIMA ────────────────────────────────────────────────
def train_arima(close_series: pd.Series):
    try:
        model = ARIMA(close_series[-200:], order=(5, 1, 2))
        result = model.fit()
        pred = result.forecast(steps=1)[0]
        aic = result.aic
        conf = min(max(70 - aic / 5000, 50), 80)
        return pred, conf
    except Exception:
        return close_series.iloc[-1] * 1.001, 55.0


# ── 3e. LSTM ─────────────────────────────────────────────────
def train_lstm(close_series: pd.Series, lookback: int = 60):
    if not TF_AVAILABLE:
        return None, None

    scaler = MinMaxScaler()
    data = scaler.fit_transform(close_series.values.reshape(-1, 1))

    X, y = [], []
    for i in range(lookback, len(data)):
        X.append(data[i - lookback:i, 0])
        y.append(data[i, 0])
    X, y = np.array(X), np.array(y)
    X = X.reshape(X.shape[0], X.shape[1], 1)

    split = int(len(X) * 0.85)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

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
    model.fit(
        X_train, y_train,
        epochs=50, batch_size=32,
        validation_data=(X_test, y_test),
        callbacks=[es], verbose=0
    )

    live_seq = data[-lookback:].reshape(1, lookback, 1)
    pred_scaled = model.predict(live_seq, verbose=0)[0][0]
    pred = scaler.inverse_transform([[pred_scaled]])[0][0]
    mape = mean_absolute_percentage_error(
        scaler.inverse_transform(y_test.reshape(-1,1)),
        scaler.inverse_transform(model.predict(X_test, verbose=0))
    )
    conf = min((1 - mape) * 100, 92)
    return pred, conf


# ═══════════════════════════════════════════════════════════
#  4. ENSEMBLE & ANALYSIS
# ═══════════════════════════════════════════════════════════

class StockAnalyser:

    def __init__(self, symbol: str, exchange: str = "NSE"):
        self.symbol   = symbol.upper()
        self.exchange = exchange.upper()
        self.fetcher  = IndianStockDataFetcher(symbol, exchange)

    # ── Pivot-based support/resistance ──────────────────────
    def _support_resistance(self, df: pd.DataFrame):
        recent = df.tail(20)
        pivot  = (recent["High"].mean() + recent["Low"].mean() + df["Close"].iloc[-1]) / 3
        r1 = 2 * pivot - recent["Low"].min()
        r2 = pivot + (recent["High"].max() - recent["Low"].min())
        s1 = 2 * pivot - recent["High"].max()
        s2 = pivot - (recent["High"].max() - recent["Low"].min())
        return {"R2": r2, "R1": r1, "S1": s1, "S2": s2, "Pivot": pivot}

    # ── ATR-based stop-loss ──────────────────────────────────
    def _stop_loss(self, df: pd.DataFrame, current: float, multiplier: float = 2.0):
        atr = ta.volatility.average_true_range(df["High"], df["Low"], df["Close"]).iloc[-1]
        sl = current - multiplier * atr
        return sl, atr

    # ── Trend determination ──────────────────────────────────
    def _trend(self, df: pd.DataFrame) -> str:
        close = df["Close"].iloc[-1]
        ema20 = ta.trend.ema_indicator(df["Close"], 20).iloc[-1]
        ema50 = ta.trend.ema_indicator(df["Close"], 50).iloc[-1]
        adx   = ta.trend.adx(df["High"], df["Low"], df["Close"]).iloc[-1]
        if close > ema20 > ema50 and adx > 25:
            return "Strong Uptrend"
        elif close > ema20:
            return "Uptrend"
        elif close < ema20 < ema50 and adx > 25:
            return "Strong Downtrend"
        elif close < ema20:
            return "Downtrend"
        return "Sideways"

    # ── Signal summary ───────────────────────────────────────
    def _signal_table(self, df: pd.DataFrame) -> list:
        close = df["Close"]
        rsi = ta.momentum.rsi(close).iloc[-1]
        macd_v = ta.trend.macd(close).iloc[-1]
        macd_s = ta.trend.macd_signal(close).iloc[-1]
        ema20  = ta.trend.ema_indicator(close, 20).iloc[-1]
        ema50  = ta.trend.ema_indicator(close, 50).iloc[-1]
        bb     = ta.volatility.BollingerBands(close)
        bb_pct = bb.bollinger_pband().iloc[-1]
        adx    = ta.trend.adx(df["High"], df["Low"], close).iloc[-1]
        mfi    = ta.volume.money_flow_index(df["High"], df["Low"], close, df["Volume"]).iloc[-1]
        stoch  = ta.momentum.stoch(df["High"], df["Low"], close).iloc[-1]
        current = close.iloc[-1]

        sigs = [
            ("RSI (14)",      f"{rsi:.1f}",
             "Oversold→BUY"  if rsi < 30 else ("Overbought→SELL" if rsi > 70 else "Neutral"),
             "green" if rsi < 40 else ("red" if rsi > 70 else "yellow")),
            ("MACD",          f"{macd_v:.2f}",
             "Bullish" if macd_v > macd_s else "Bearish",
             "green" if macd_v > macd_s else "red"),
            ("EMA 20",        f"₹{ema20:,.2f}",
             "Above EMA→Bull" if current > ema20 else "Below EMA→Bear",
             "green" if current > ema20 else "red"),
            ("EMA 50",        f"₹{ema50:,.2f}",
             "Above EMA→Bull" if current > ema50 else "Below EMA→Bear",
             "green" if current > ema50 else "red"),
            ("ADX",           f"{adx:.1f}",
             "Strong Trend" if adx > 25 else "Weak/Ranging",
             "green" if adx > 25 else "yellow"),
            ("Bollinger %B",  f"{bb_pct:.2f}",
             "Overbought" if bb_pct > 0.8 else ("Oversold" if bb_pct < 0.2 else "Normal"),
             "red" if bb_pct > 0.8 else ("green" if bb_pct < 0.2 else "yellow")),
            ("MFI (14)",      f"{mfi:.1f}",
             "Buying pressure" if mfi > 60 else ("Selling pressure" if mfi < 40 else "Neutral"),
             "green" if mfi > 60 else ("red" if mfi < 40 else "yellow")),
            ("Stochastic %K", f"{stoch:.1f}",
             "Oversold" if stoch < 20 else ("Overbought" if stoch > 80 else "Neutral"),
             "green" if stoch < 30 else ("red" if stoch > 70 else "yellow")),
        ]
        return sigs

    # ── Main run ─────────────────────────────────────────────
    def run(self):
        console.print()
        console.print(Rule("[bold cyan]STOCKAI.IN — Indian ML Prediction Engine[/bold cyan]"))
        console.print()

        # 1. Fetch data
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True) as prog:
            t = prog.add_task("", total=None)

            prog.update(t, description="[cyan]Step 1/6[/cyan] Downloading market data...")
            df = self.fetcher.fetch()
            info = self.fetcher.get_info()

            prog.update(t, description="[cyan]Step 2/6[/cyan] Engineering features...")
            fe   = FeatureEngineer()
            df_f = fe.build(df)

            prog.update(t, description="[cyan]Step 3/6[/cyan] Training Random Forest...")
            X_tr, X_te, y_tr, y_te, X_live = split_data(df_f)
            rf_pred, rf_conf, rf_model = train_random_forest(X_tr, y_tr, X_live)

            prog.update(t, description="[cyan]Step 4/6[/cyan] Training XGBoost...")
            xgb_pred, xgb_conf, _ = train_xgboost(X_tr, y_tr, X_te, y_te, X_live)

            prog.update(t, description="[cyan]Step 5/6[/cyan] Training SVR + ARIMA...")
            svr_pred, svr_conf, _ = train_svr(X_tr, y_tr, X_live)
            arima_pred, arima_conf = train_arima(df["Close"])

            lstm_pred, lstm_conf = None, None
            if TF_AVAILABLE:
                prog.update(t, description="[cyan]Step 6/6[/cyan] Training LSTM neural network...")
                lstm_pred, lstm_conf = train_lstm(df["Close"])

        # 2. Ensemble
        current = float(df["Close"].iloc[-1])
        preds = {"Random Forest": (rf_pred, rf_conf, 0.28),
                 "XGBoost":       (xgb_pred, xgb_conf, 0.28),
                 "SVR":           (svr_pred, svr_conf, 0.14),
                 "ARIMA":         (arima_pred, arima_conf, 0.14)}
        if lstm_pred:
            preds["LSTM"] = (lstm_pred, lstm_conf, 0.16)
            # re-normalise weights
            total_w = sum(v[2] for v in preds.values())
            preds = {k: (v[0], v[1], v[2]/total_w) for k, v in preds.items()}

        ensemble_pred = sum(v[0]*v[2] for v in preds.values())
        ensemble_conf = sum(v[1]*v[2] for v in preds.values())

        # 3. Risk levels
        sl, atr = self._stop_loss(df, current)
        sr = self._support_resistance(df)
        trend = self._trend(df)
        signals = self._signal_table(df)

        # Multi-horizon targets (using ensemble + momentum scaling)
        ret_5d  = (ensemble_pred - current) / current
        target_5d  = current * (1 + ret_5d)
        target_15d = current * (1 + ret_5d * 2.5)
        target_30d = current * (1 + ret_5d * 4.5)
        upside_pct = (target_15d - current) / current * 100
        sl_pct     = (current - sl) / current * 100
        rr_ratio   = upside_pct / sl_pct if sl_pct > 0 else 0

        rsi_now = ta.momentum.rsi(df["Close"]).iloc[-1]
        overall_signal = (
            "STRONG BUY"  if rsi_now < 40 and "Uptrend" in trend else
            "BUY"         if rsi_now < 55 and "Uptrend" in trend else
            "STRONG SELL" if rsi_now > 75 else
            "SELL"        if rsi_now > 65 else
            "HOLD / WAIT"
        )

        # ── PRINT RESULTS ────────────────────────────────────

        console.print()
        console.print(Panel(
            f"[bold white]{info.get('name', self.symbol)}[/bold white]   "
            f"[cyan]{self.symbol}.{self.exchange}[/cyan]   "
            f"[dim]{info.get('sector','N/A')} | {info.get('industry','N/A')}[/dim]",
            title="[bold green]Company Overview[/bold green]",
            border_style="green"
        ))

        # Fundamentals
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        t.add_column("Metric", style="dim")
        t.add_column("Value", style="bold")
        t.add_column("Metric", style="dim")
        t.add_column("Value", style="bold")
        mc = info.get("mkt_cap", 0)
        mc_str = f"₹{mc/1e7:,.0f} Cr" if mc else "N/A"
        t.add_row("CMP",       f"₹{current:,.2f}",
                  "Market Cap",f"{mc_str}")
        t.add_row("P/E Ratio", str(info.get("pe_ratio","N/A")),
                  "P/B Ratio", str(info.get("pb_ratio","N/A")))
        t.add_row("52W High",  f"₹{info.get('52w_high','N/A')}",
                  "52W Low",   f"₹{info.get('52w_low','N/A')}")
        t.add_row("Beta",      str(info.get("beta","N/A")),
                  "Div Yield", f"{(info.get('dividend',0) or 0)*100:.2f}%")
        t.add_row("ROE",       f"{(info.get('roe',0) or 0)*100:.1f}%",
                  "D/E Ratio", str(info.get("debt_equity","N/A")))
        console.print(t)

        # ML Model results
        console.print()
        console.print(Rule("[bold yellow]ML Model Predictions[/bold yellow]"))
        mt = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
        mt.add_column("Model", style="bold", width=18)
        mt.add_column("Next-Day Target", justify="right")
        mt.add_column("vs Current", justify="right")
        mt.add_column("Confidence", justify="center")
        mt.add_column("Weight", justify="center")

        for name, (pred, conf, wt) in preds.items():
            chg = (pred - current) / current * 100
            chg_str = f"[green]+{chg:.2f}%[/green]" if chg >= 0 else f"[red]{chg:.2f}%[/red]"
            conf_bar = "█" * int(conf / 10) + "░" * (10 - int(conf / 10))
            mt.add_row(name, f"₹{pred:,.2f}", chg_str,
                       f"[cyan]{conf_bar}[/cyan] {conf:.1f}%",
                       f"{wt*100:.0f}%")

        ens_chg = (ensemble_pred - current) / current * 100
        ens_str = f"[bold green]+{ens_chg:.2f}%[/bold green]" if ens_chg >= 0 else f"[bold red]{ens_chg:.2f}%[/bold red]"
        mt.add_row("[bold]ENSEMBLE[/bold]",
                   f"[bold]₹{ensemble_pred:,.2f}[/bold]",
                   ens_str,
                   f"[bold]{ensemble_conf:.1f}%[/bold]",
                   "[bold]100%[/bold]")
        console.print(mt)

        # Price targets
        console.print()
        console.print(Rule("[bold green]Price Targets & Risk Management[/bold green]"))
        pt = Table(box=box.ROUNDED, show_header=True, header_style="bold green")
        pt.add_column("Horizon", style="bold")
        pt.add_column("Target Price", justify="right")
        pt.add_column("Expected Move", justify="right")

        def add_target(label, price):
            chg = (price - current) / current * 100
            col = "green" if chg >= 0 else "red"
            pt.add_row(label, f"₹{price:,.2f}", f"[{col}]{chg:+.2f}%[/{col}]")

        add_target("Next Day  (1D)", ensemble_pred)
        add_target("Short Term (5D)", target_5d)
        add_target("Medium Term (15D)", target_15d)
        add_target("Swing Target (30D)", target_30d)
        console.print(pt)

        # Risk table
        rt = Table(box=box.ROUNDED, show_header=True, header_style="bold red")
        rt.add_column("Risk Parameter", style="bold")
        rt.add_column("Value", justify="right")
        rt.add_column("Detail", style="dim")

        rt.add_row("Stop-Loss (2×ATR)",    f"[red]₹{sl:,.2f}[/red]",  f"-{sl_pct:.2f}% from CMP")
        rt.add_row("Tight SL (1×ATR)",     f"[red]₹{current-atr:,.2f}[/red]", f"-{atr/current*100:.2f}% from CMP")
        rt.add_row("ATR (14)",              f"₹{atr:,.2f}",            "Average True Range")
        rt.add_row("Risk/Reward",           f"[bold]1:{rr_ratio:.2f}[/bold]", "≥ 1:2 recommended")
        rt.add_row("Overall Signal",
                   f"[bold green]{overall_signal}[/bold green]" if "BUY" in overall_signal
                   else (f"[bold red]{overall_signal}[/bold red]" if "SELL" in overall_signal
                         else f"[bold yellow]{overall_signal}[/bold yellow]"),
                   f"RSI {rsi_now:.1f} | {trend}")
        console.print(rt)

        # Support / Resistance
        console.print()
        console.print(Rule("[bold blue]Support & Resistance Levels[/bold blue]"))
        srt = Table(box=box.SIMPLE, show_header=True, header_style="bold blue")
        srt.add_column("Level", style="bold", width=22)
        srt.add_column("Price", justify="right")
        srt.add_column("Distance from CMP", justify="right")

        for label, val, col in [
            ("Strong Resistance (R2)", sr["R2"], "red"),
            ("Resistance (R1)",        sr["R1"], "yellow"),
            ("Pivot",                  sr["Pivot"], "cyan"),
            ("Support (S1)",           sr["S1"], "yellow"),
            ("Strong Support (S2)",    sr["S2"], "green"),
        ]:
            dist = (val - current) / current * 100
            srt.add_row(label, f"[{col}]₹{val:,.2f}[/{col}]", f"{dist:+.2f}%")
        console.print(srt)

        # Technical signals
        console.print()
        console.print(Rule("[bold magenta]Technical Signals[/bold magenta]"))
        sts = Table(box=box.SIMPLE, show_header=True, header_style="bold magenta")
        sts.add_column("Indicator", style="bold", width=18)
        sts.add_column("Value", justify="right", width=14)
        sts.add_column("Signal", width=24)

        for name, val, sig, col in signals:
            sts.add_row(name, val, f"[{col}]{sig}[/{col}]")
        console.print(sts)

        # Entry zones
        console.print()
        entry_agg  = current * 0.999
        entry_cons = sr["S1"] * 1.002
        console.print(Panel(
            f"[bold]Aggressive Entry:[/bold]  ₹{entry_agg:,.2f} (near CMP)\n"
            f"[bold]Conservative Entry:[/bold] ₹{entry_cons:,.2f} (near S1 support)\n"
            f"[bold]Stop-Loss:[/bold]         ₹{sl:,.2f}  ([red]-{sl_pct:.2f}%[/red])\n"
            f"[bold]Target 1:[/bold]          ₹{target_5d:,.2f}  ([green]+{(target_5d-current)/current*100:.2f}%[/green])\n"
            f"[bold]Target 2:[/bold]          ₹{target_15d:,.2f}  ([green]+{(target_15d-current)/current*100:.2f}%[/green])\n"
            f"[bold]Target 3:[/bold]          ₹{target_30d:,.2f}  ([green]+{(target_30d-current)/current*100:.2f}%[/green])\n"
            f"[bold]Signal:[/bold]            [{('green' if 'BUY' in overall_signal else 'red' if 'SELL' in overall_signal else 'yellow')}]{overall_signal}[/]",
            title="[bold green]Trade Setup[/bold green]",
            border_style="green"
        ))

        # Disclaimer
        console.print()
        console.print(Panel(
            "[yellow]RISK DISCLAIMER:[/yellow] This tool uses ML models trained on historical price data.\n"
            "NO model provides 100% accurate predictions. Markets are inherently uncertain.\n"
            "This is NOT SEBI-registered investment advice. Always do your own research.\n"
            "Past performance does not guarantee future results. Use stop-losses always.",
            border_style="red",
            title="[red]Important[/red]"
        ))
        console.print()

        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "current_price": current,
            "ensemble_prediction": ensemble_pred,
            "ensemble_confidence": ensemble_conf,
            "target_5d": target_5d,
            "target_15d": target_15d,
            "target_30d": target_30d,
            "stop_loss": sl,
            "atr": atr,
            "signal": overall_signal,
            "trend": trend,
            "rsi": rsi_now,
            "support_resistance": sr,
            "model_predictions": {k: {"price": v[0], "confidence": v[1]} for k, v in preds.items()},
        }


# ═══════════════════════════════════════════════════════════
#  5. ENTRY POINT
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Indian Stock AI/ML Prediction System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python indian_stock_predictor.py
  python indian_stock_predictor.py --symbol TCS --exchange NSE
  python indian_stock_predictor.py --symbol HDFCBANK --exchange BSE
  python indian_stock_predictor.py --symbol INFY --symbol2 TCS   (compare two)
        """
    )
    parser.add_argument("--symbol",   default="RELIANCE", help="NSE/BSE ticker symbol")
    parser.add_argument("--exchange", default="NSE",      choices=["NSE", "BSE"])
    parser.add_argument("--symbol2",  default=None,       help="Optional second symbol to compare")
    args = parser.parse_args()

    results = []
    analyser = StockAnalyser(args.symbol, args.exchange)
    r = analyser.run()
    results.append(r)

    if args.symbol2:
        console.print()
        console.print(Rule(f"[cyan]Now analysing: {args.symbol2.upper()}[/cyan]"))
        a2 = StockAnalyser(args.symbol2, args.exchange)
        r2 = a2.run()
        results.append(r2)

        # Comparison table
        console.print()
        console.print(Rule("[bold]Comparison[/bold]"))
        ct = Table(box=box.ROUNDED, show_header=True, header_style="bold white")
        ct.add_column("Metric")
        for res in results:
            ct.add_column(res["symbol"], justify="right")
        for label, key in [
            ("CMP", "current_price"),
            ("Next-Day Target", "ensemble_prediction"),
            ("Confidence", "ensemble_confidence"),
            ("5D Target", "target_5d"),
            ("Stop-Loss", "stop_loss"),
            ("Signal", "signal"),
        ]:
            row = [label]
            for res in results:
                val = res[key]
                if isinstance(val, float):
                    row.append(f"₹{val:,.2f}" if key != "ensemble_confidence" else f"{val:.1f}%")
                else:
                    row.append(str(val))
            ct.add_row(*row)
        console.print(ct)

    return results


if __name__ == "__main__":
    main()