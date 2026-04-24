import streamlit as st
import pandas as pd
import requests
import time

# ================== CONFIG ==================
TELEGRAM_TOKEN = st.secrets["TELEGRAM_TOKEN"]
CHAT_ID = st.secrets["CHAT_ID"]

SYMBOL = "BTCUSDT"
TIMEFRAME = "1m"
PERIOD = 100
ENVELOPE_PCT = 0.003  # 0.3%
# ============================================

st.set_page_config(page_title="Tron Forex – Envelope Bot", layout="centered")
st.title("📈 Tron Forex – Envelope Strategy (M1)")

# ---------- TELEGRAM ----------
def send_telegram(signal):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": signal}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        st.warning(f"Erro ao enviar Telegram: {e}")

# ---------- BINANCE REST API DIRETA ----------
@st.cache_data(ttl=60)
def get_data():
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": SYMBOL,
        "interval": TIMEFRAME,
        "limit": PERIOD + 5
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()
    df = pd.DataFrame(raw, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"
    ])
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df

# ---------- LÓGICA ENVELOPE (mean reversion) ----------
def envelope_strategy(df):
    df = df.copy()
    df["ma"] = df["close"].rolling(PERIOD).mean()
    df["upper"] = df["ma"] * (1 + ENVELOPE_PCT)
    df["lower"] = df["ma"] * (1 - ENVELOPE_PCT)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # SELL: preço toca banda superior pela primeira vez
    if last["high"] >= last["upper"] and prev["high"] < prev["upper"]:
        return "SELL", df

    # BUY: preço toca banda inferior pela primeira vez
    if last["low"] <= last["lower"] and prev["low"] > prev["lower"]:
        return "BUY", df

    return None, df

# ---------- INTERFACE ----------
try:
    df = get_data()
except Exception as e:
    st.error(f"Erro ao buscar dados: {e}")
    st.stop()

signal, df = envelope_strategy(df)

last = df.iloc[-1]

st.subheader("📊 Bandas atuais")
col1, col2, col3 = st.columns(3)
col1.metric("Banda Superior", f"{last['upper']:.2f}" if not pd.isna(last['upper']) else "—")
col2.metric("Média (MA100)", f"{last['ma']:.2f}" if not pd.isna(last['ma']) else "—")
col3.metric("Banda Inferior", f"{last['lower']:.2f}" if not pd.isna(last['lower']) else "—")

st.subheader("Último candle")
st.write(df[["time", "open", "high", "low", "close", "upper", "lower"]].tail(3).to_string(index=False))

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
