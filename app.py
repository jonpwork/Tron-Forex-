import time
import ccxt
import pandas as pd
import requests

# =========================
# TELEGRAM (FIXO)
# =========================
TELEGRAM_TOKEN = "8762172696:AAHP3CSVO5KDI9PBjzxvTI_yQUVHt1B4UzM"
TELEGRAM_CHAT_ID = "8085416549"

# =========================
# CONFIGURAÇÃO DA ESTRATÉGIA
# =========================
SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"        # pode mudar: 5m, 15m, etc
PERIODO = 14
DESVIO = 0.001          # 0.10% (estratégia original)
CHECK_INTERVAL = 30     # segundos

# =========================
# EXCHANGE
# =========================
exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot"
    }
})

last_candle_time = None
last_signal = None

# =========================
# FUNÇÃO TELEGRAM
# =========================
def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload, timeout=10)

# =========================
# LOOP PRINCIPAL
# =========================
print("📡 TRON FOREX Telegram Bot iniciado...")

while True:
    try:
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
        df = pd.DataFrame(
            ohlcv,
            columns=["time", "open", "high", "low", "close", "volume"]
        )

        candle_time = df["time"].iloc[-1]
        price = df["close"].iloc[-1]

        ma = df["close"].rolling(PERIODO).mean()
        upper = ma * (1 + DESVIO)
        lower = ma * (1 - DESVIO)

        signal = None
        if price >= upper.iloc[-1]:
            signal = "SELL"
        elif price <= lower.iloc[-1]:
            signal = "BUY"

        # =========================
        # ANTI-SPAM (1 sinal por candle)
        # =========================
        if signal and candle_time != last_candle_time:
            if signal != last_signal:
                message = (
                    f"🚨 *SINAL {signal}*\n\n"
                    f"📊 Par: BTCUSDT\n"
                    f"⏱ Timeframe: {TIMEFRAME}\n"
                    f"💰 Preço: {price:.2f}\n"
                    f"📐 Envelope SMA(14) ±0.10%"
                )
                send_message(message)
                last_signal = signal
                last_candle_time = candle_time

        time.sleep(CHECK_INTERVAL)

    except Exception as e:
        print("Erro:", e)
        time.sleep(10)
