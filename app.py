import requests
import pandas as pd
import time
import os

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SYMBOL = "BTCUSDT"
TIMEFRAME = "1m"
PERIOD = 100
ENVELOPE_PCT = 0.003
INTERVAL = 60  # segundos entre cada verificação
# ============================================

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        print(f"Telegram enviado: {msg}")
    except Exception as e:
        print(f"Erro Telegram: {e}")

def get_data():
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": TIMEFRAME, "limit": PERIOD + 5}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()
    df = pd.DataFrame(raw, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"
    ])
    df = df[["time", "open", "high", "low", "close"]].copy()
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    return df

def check_signal(df):
    df = df.copy()
    df["ma"] = df["close"].rolling(PERIOD).mean()
    df["upper"] = df["ma"] * (1 + ENVELOPE_PCT)
    df["lower"] = df["ma"] * (1 - ENVELOPE_PCT)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if last["high"] >= last["upper"] and prev["high"] < prev["upper"]:
        return "🔴 SELL — BTC tocou banda superior"

    if last["low"] <= last["lower"] and prev["low"] > prev["lower"]:
        return "✅ BUY — BTC tocou banda inferior"

    return None

print("🤖 Tron Forex Bot iniciado...")
send_telegram("🤖 Tron Forex Bot iniciado!")

while True:
    try:
        df = get_data()
        signal = check_signal(df)
        last_close = df.iloc[-1]["close"]
        print(f"Preço: {last_close:.2f} | Sinal: {signal or 'Nenhum'}")

        if signal:
            send_telegram(signal)

    except Exception as e:
        print(f"Erro: {e}")

    time.sleep(INTERVAL)
