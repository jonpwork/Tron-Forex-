# ================= TRON FOREX – BACKEND =================
# Repo: https://github.com/jonpwork/Tron-Forex-
# Envelope + MA + RSI | Multi-Timeframe | Telegram + API

import requests
import pandas as pd
import time
from flask import Flask, jsonify
import threading

# ================= CONFIG =================
SYMBOL = "BTCUSDT"
PERIOD = 14

TIMEFRAMES = {
    "1m":  0.003,
    "3m":  0.004,
    "5m":  0.006,
    "15m": 0.010,
    "30m": 0.020,
    "1h":  0.030,
    "4h":  0.070,
    "1d":  0.100,
}

# 🔴 TELEGRAM (dados fornecidos)
TELEGRAM_TOKEN = "8762172696:AAHP3CSVO5KDI9PBjzxvTI_yQUVHt1B4UzM"
CHAT_ID = "8085416549"
# =========================================

app = Flask(__name__)
last_sent = {}

# ================= UTILS =================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
        print("Telegram enviado:", msg)
    except Exception as e:
        print("Erro Telegram:", e)

def get_data(tf, limit=200):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": SYMBOL, "interval": tf, "limit": limit}
    data = requests.get(url, params=params, timeout=10).json()

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","vol",
        "_","_","_","_","_","_"
    ])
    df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
    df["time"] = df["time"] // 1000
    return df

def apply_indicators(df, env):
    df["ma"] = df["close"].rolling(PERIOD).mean()
    df["upper"] = df["ma"] * (1 + env)
    df["lower"] = df["ma"] * (1 - env)

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    return df

def check_signal(df, tf):
    last = df.iloc[-1]

    if last["close"] <= last["lower"] and last["rsi"] < 30:
        return f"🟢 BUY BTC ({tf})\nEnvelope + RSI"

    if last["close"] >= last["upper"] and last["rsi"] > 70:
        return f"🔴 SELL BTC ({tf})\nEnvelope + RSI"

    return None

# ================= API =================
@app.route("/data/<tf>")
def api_data(tf):
    if tf not in TIMEFRAMES:
        return jsonify({"error": "invalid timeframe"})

    env = TIMEFRAMES[tf]
    df = apply_indicators(get_data(tf), env)
    last = df.iloc[-1]

    return jsonify({
        "timeframe": tf,
        "price": last["close"],
        "ma": last["ma"],
        "upper": last["upper"],
        "lower": last["lower"],
        "rsi": last["rsi"]
    })

# ================= BOT LOOP =================
def bot_loop():
    send_telegram("🤖 Tron Forex Bot iniciado")
    while True:
        for tf, env in TIMEFRAMES.items():
            try:
                df = apply_indicators(get_data(tf), env)
                signal = check_signal(df, tf)

                if signal and last_sent.get(tf) != signal:
                    send_telegram(signal)
                    last_sent[tf] = signal

            except Exception as e:
                print("Erro:", e)

        time.sleep(60)

# ================= START =================
if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
