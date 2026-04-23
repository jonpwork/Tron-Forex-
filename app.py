import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import time

# ================== CONFIG ==================
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
CHAT_ID = st.secrets["CHAT_ID"]

SYMBOL = "BTC-USD"
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

# ---------- YFINANCE (DADOS) ----------
@st.cache_data(ttl=60)
def get_data():
    ticker = yf.Ticker(SYMBOL)
    df = ticker.history(period="1d", interval="1m")
    df = df.reset_index()
    df = df.rename(columns={
        "Datetime": "time",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume"
    })
    df = df[["time", "open", "high", "low", "close", "volume"]].tail(PERIOD + 5)
    df = df.reset_index(drop=True)
    return df

# ---------- LÓGICA ENVELOPE (mean reversion) ----------
def envelope_strategy(df):
    df["ma"] = df["close"].rolling(PERIOD).mean()
    df["upper"] = df["ma"] * (1 + ENVELOPE_PCT)
    df["lower"] = df["ma"] * (1 - ENVELOPE_PCT)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # SELL: preço toca/ultrapassa banda superior → reversão para baixo
    if last["high"] >= last["upper"] and prev["high"] < prev["upper"]:
        return "SELL"

    # BUY: preço toca/ultrapassa banda inferior → reversão para cima
    if last["low"] <= last["lower"] and prev["low"] > prev["lower"]:
        return "BUY"

    return None

# ---------- INTERFACE ----------
df = get_data()

# Calcular bandas para exibir no dashboard
df["ma"] = df["close"].rolling(PERIOD).mean()
df["upper"] = df["ma"] * (1 + ENVELOPE_PCT)
df["lower"] = df["ma"] * (1 - ENVELOPE_PCT)

signal = envelope_strategy(df)

last = df.iloc[-1]

st.subheader("📊 Bandas atuais")
col1, col2, col3 = st.columns(3)
col1.metric("Banda Superior", f"{last['upper']:.2f}" if not pd.isna(last['upper']) else "—")
col2.metric("Média (MA)", f"{last['ma']:.2f}" if not pd.isna(last['ma']) else "—")
col3.metric("Banda Inferior", f"{last['lower']:.2f}" if not pd.isna(last['lower']) else "—")

st.subheader("Último candle")
st.write(df[["time", "open", "high", "low", "close", "upper", "lower"]].tail(3))

st.subheader("Sinal atual")
if signal == "BUY":
    st.success("✅ BUY — preço tocou banda inferior")
elif signal == "SELL":
    st.error("🔴 SELL — preço tocou banda superior")
else:
    st.info("Sem sinal — preço dentro das bandas")

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
