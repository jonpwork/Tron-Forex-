import streamlit as st
import ccxt
import pandas as pd
import numpy as np
import requests
import time

# ================== CONFIG ==================
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
CHAT_ID = st.secrets["CHAT_ID"]

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"
PERIOD = 100
ENVELOPE_PCT = 0.003  # 0.3%
# ============================================

st.set_page_config(page_title="Tron Forex – Envelope Bot", layout="centered")
st.title("📈 Tron Forex – Envelope Strategy (M1)")

# ---------- TELEGRAM ----------
def send_telegram(signal):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": signal
    }
    requests.post(url, data=data, timeout=10)

# ---------- BINANCE (DADOS) ----------
exchange = ccxt.binance({
    "enableRateLimit": True
})

@st.cache_data(ttl=60)
def get_data():
    ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=PERIOD + 5)
    df = pd.DataFrame(
        ohlcv,
        columns=["time", "open", "high", "low", "close", "volume"]
    )
    return df

# ---------- LÓGICA ENVELOPE ----------
def envelope_strategy(df):
    df["ma"] = df["close"].rolling(PERIOD).mean()
    df["upper"] = df["ma"] * (1 + ENVELOPE_PCT)
    df["lower"] = df["ma"] * (1 - ENVELOPE_PCT)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # COMPRA:
    # rompe superior → retesta inferior
    if prev["close"] > prev["upper"] and last["low"] <= last["lower"]:
        return "BUY"

    # VENDA:
    # rompe inferior → retesta superior
    if prev["close"] < prev["lower"] and last["high"] >= last["upper"]:
        return "SELL"

    return None

# ---------- INTERFACE ----------
df = get_data()
signal = envelope_strategy(df)

st.subheader("Último candle")
st.write(df.tail(3))

st.subheader("Sinal atual")
if signal:
    st.success(signal)
else:
    st.info("Sem sinal")

# ---------- ENVIO MANUAL ----------
st.divider()
st.subheader("⚡ Envio manual (teste)")

col1, col2 = st.columns(2)

with col1:
    if st.button("Enviar BUY"):
        send_telegram("BUY")
        st.success("BUY enviado")

with col2:
    if st.button("Enviar SELL"):
        send_telegram("SELL")
        st.success("SELL enviado")

# ---------- ENVIO AUTOMÁTICO ----------
st.divider()
st.subheader("🤖 Automação")

auto = st.toggle("Ativar envio automático")

if auto and signal:
    send_telegram(signal)
    st.success(f"Sinal {signal} enviado automaticamente")
    time.sleep(2)
