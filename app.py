# executor.py
import ccxt
import requests
import time

# ================= CONFIG =================
BYBIT_API_KEY = "SUA_API_KEY"
BYBIT_API_SECRET = "SEU_API_SECRET"

TELEGRAM_TOKEN = "SEU_BOT_TOKEN"
CHAT_ID = "SEU_CHAT_ID"

SYMBOL = "BTC/USDT"
USDT_PER_TRADE = 10
# ==========================================

exchange = ccxt.bybit({
    "apiKey": BYBIT_API_KEY,
    "secret": BYBIT_API_SECRET,
    "enableRateLimit": True,
    "options": {"defaultType": "spot"}
})

last_update_id = 0

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": msg}
    requests.post(url, data=data, timeout=10)

def buy():
    price = exchange.fetch_ticker(SYMBOL)["last"]
    amount = USDT_PER_TRADE / price
    order = exchange.create_market_buy_order(SYMBOL, amount)
    print("🟢 BUY executado")
    send_telegram(f"🟢 BUY {SYMBOL}\nPreço: {price}")

def sell():
    balance = exchange.fetch_balance()
    base = SYMBOL.split("/")[0]
    amount = balance["free"].get(base, 0)
    if amount > 0:
        exchange.create_market_sell_order(SYMBOL, amount)
        print("🔴 SELL executado")
        send_telegram(f"🔴 SELL {SYMBOL}")

print("🤖 Executor iniciado e escutando Telegram...")

while True:
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"offset": last_update_id + 1, "timeout": 30}
        resp = requests.get(url, params=params, timeout=35).json()

        for update in resp.get("result", []):
            last_update_id = update["update_id"]

            if "message" not in update:
                continue

            text = update["message"]["text"].upper()
            chat_id = update["message"]["chat"]["id"]

            print("📩 Mensagem recebida:", text)

            if chat_id != int(CHAT_ID):
                print("⚠️ Chat não autorizado")
                continue

            if text.startswith("BUY"):
                buy()

            elif text.startswith("SELL"):
                sell()

        time.sleep(1)

    except Exception as e:
        print("❌ Erro:", e)
        time.sleep(5)
