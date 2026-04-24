from flask import Flask, jsonify
from flask_cors import CORS
import os
import ccxt
import pandas as pd

app = Flask(__name__)
CORS(app)

exchange = ccxt.binance({
    "enableRateLimit": True,
    "options": {
        "defaultType": "spot"
    }
})

@app.route("/")
def home():
    return "TRON FOREX API ONLINE"

@app.route("/data/<tf>")
def data(tf):
    try:
        timeframe_map = {
            "1m": "1m",
            "3m": "3m",
            "5m": "5m",
            "15m": "15m",
            "30m": "30m",
            "1h": "1h"
        }

        if tf not in timeframe_map:
            return jsonify({"error": "invalid timeframe"}), 400

        ohlcv = exchange.fetch_ohlcv(
            symbol="BTC/USDT",
            timeframe=timeframe_map[tf],
            limit=100
        )

        df = pd.DataFrame(ohlcv, columns=["t","o","h","l","c","v"])

        ma = df["c"].rolling(14).mean()
        upper = ma * 1.003
        lower = ma * 0.997
        price = df["c"].iloc[-1]

        return jsonify({
            "price": float(price),
            "ma": float(ma.iloc[-1]),
            "upper": float(upper.iloc[-1]),
            "lower": float(lower.iloc[-1]),
            "rsi": 50.0
        })

    except Exception as e:
        return jsonify({
            "error": "internal",
            "message": str(e)
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
