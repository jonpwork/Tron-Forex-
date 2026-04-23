import streamlit as st
import pandas as pd
import numpy as np
import ccxt
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG STREAMLIT
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Envelope Bybit Bot",
    page_icon="📈",
    layout="wide"
)

# ─────────────────────────────────────────────
# TELEGRAM (opcional)
# ─────────────────────────────────────────────
def send_telegram(token, chat_id, msg):
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": msg}, timeout=5)
    except Exception:
        pass

# ─────────────────────────────────────────────
# BYBIT — DADOS PÚBLICOS
# ─────────────────────────────────────────────
def fetch_bybit_ohlcv(symbol, limit=250):
    try:
        exchange = ccxt.bybit({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"}
        })

        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1m", limit=limit)

        df = pd.DataFrame(
            ohlcv,
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    except Exception as e:
        st.warning(f"Erro Bybit: {e}")
        return pd.DataFrame()

# ─────────────────────────────────────────────
# ENVELOPE
# ─────────────────────────────────────────────
def calc_envelope(df, period=100, pct=0.5):
    df = df.copy()
    df["ma"] = df["close"].rolling(period).mean()
    df["upper"] = df["ma"] * (1 + pct / 100)
    df["lower"] = df["ma"] * (1 - pct / 100)
    return df

# ─────────────────────────────────────────────
# ESTRATÉGIA (CORRETA)
# Compra: rompe superior → retesta inferior
# Venda : rompe inferior → retesta superior
# ─────────────────────────────────────────────
def detect_signal(df):
    if len(df) < 120:
        return "WAIT"

    breakout_up = False
    breakout_down = False

    # procura rompimento recente
    for i in range(5, 30):
        c = df.iloc[-i]
        if c["close"] > c["upper"]:
            breakout_up = True
        if c["close"] < c["lower"]:
            breakout_down = True

    last = df.iloc[-1]

    # BUY
    if (
        breakout_up and
        last["low"] <= last["lower"] and
        last["close"] > last["ma"]
    ):
        return "BUY"

    # SELL
    if (
        breakout_down and
        last["high"] >= last["upper"] and
        last["close"] < last["ma"]
    ):
        return "SELL"

    return "WAIT"

# ─────────────────────────────────────────────
# GRÁFICO
# ─────────────────────────────────────────────
def build_chart(df, symbol):
    fig = make_subplots(rows=1, cols=1)

    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=symbol
    ))

    fig.add_trace(go.Scatter(
        x=df.index, y=df["upper"],
        line=dict(color="blue", dash="dot"),
        name="Upper"
    ))

    fig.add_trace(go.Scatter(
        x=df.index, y=df["lower"],
        line=dict(color="red", dash="dot"),
        name="Lower"
    ))

    fig.add_trace(go.Scatter(
        x=df.index, y=df["ma"],
        line=dict(color="gray"),
        name="MA 100"
    ))

    fig.update_layout(
        height=420,
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=30, b=10)
    )
    return fig

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.title("📈 Envelope Bybit")

    symbol = st.selectbox(
        "Par",
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        index=0
    )

    env_period = st.number_input("Período MA", value=100, step=5)
    env_pct = st.number_input("Envelope %", value=0.5, step=0.1)

    refresh = st.slider("Atualização (seg)", 10, 120, 30)

    st.markdown("---")
    st.subheader("📩 Telegram (opcional)")
    tg_token = st.text_input("Bot Token", type="password")
    tg_chat = st.text_input("Chat ID")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
st.header(f"Envelope Strategy — {symbol} (M1)")

df = fetch_bybit_ohlcv(symbol)

if df.empty:
    st.stop()

df = calc_envelope(df, env_period, env_pct)
df.dropna(inplace=True)

signal = detect_signal(df)
price = df["close"].iloc[-1]

# ── STATUS
col1, col2, col3 = st.columns(3)
col1.metric("Preço", f"{price:,.2f}")
col2.metric("Sinal", signal)
col3.metric("Hora", datetime.now().strftime("%H:%M:%S"))

# ── ALERTA TELEGRAM
if signal in ["BUY", "SELL"]:
    msg = f"{'🟢' if signal=='BUY' else '🔴'} {signal} {symbol}\nPreço: {price:,.2f}\nEnvelope {env_period} / {env_pct}%"
    send_telegram(tg_token, tg_chat, msg)

# ── GRÁFICO
st.plotly_chart(build_chart(df, symbol), use_container_width=True)

# ── AUTO REFRESH
time.sleep(refresh)
st.rerun()
