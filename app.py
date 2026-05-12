"""
Multi-Timeframe Bitcoin Signal Bot
Sem dependencia de pandas/numpy - compativel com qualquer versao Python
"""

import requests
import time
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ================== CONFIG ==================
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "8762172696:AAHP3CSVO5KDI9PBjzxvTI_yQUVHt1B4UzM")
CHAT_ID         = os.environ.get("CHAT_ID", "8085416549")
SYMBOL          = "BTCUSDT"
CHECK_INTERVAL  = 60        # segundos entre cada analise
SIGNAL_COOLDOWN = 1800      # 30 min sem repetir mesmo sinal
PORT            = int(os.environ.get("PORT", 8080))
SWING_N         = 5         # candles para confirmar pivot nos TFs maiores
SWING_M1        = 3         # candles para confirmar pivot no M1
ZONE_TOL        = 0.06      # +-6% = zona de 50%
MIN_WAVE_USD    = 50        # tamanho minimo da onda M1 em dolares
# ============================================

last_signal_time = {}

# ─── HTTP keep-alive ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"BTC Multi-TF Bot running")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
        print(f"[TG] {msg[:100].strip()}")
    except Exception as e:
        print(f"Erro Telegram: {e}")

# ─── BINANCE DATA ─────────────────────────────────────────────────────────────
# Retorna lista de dicts: [{open, high, low, close}, ...]
def get_candles(tf: str, limit: int = 120) -> list:
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": SYMBOL, "interval": tf,
                                   "limit": limit}, timeout=10)
    r.raise_for_status()
    candles = []
    for k in r.json():
        candles.append({
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4]),
        })
    return candles

# ─── PIVOT HIGHS / LOWS ───────────────────────────────────────────────────────
def find_pivots(candles: list, n: int = SWING_N):
    highs, lows = [], []
    length = len(candles)
    for i in range(n, length - n):
        window_h = [candles[j]["high"]  for j in range(i-n, i+n+1)]
        window_l = [candles[j]["low"]   for j in range(i-n, i+n+1)]
        if candles[i]["high"] == max(window_h):
            highs.append((i, candles[i]["high"]))
        if candles[i]["low"] == min(window_l):
            lows.append((i, candles[i]["low"]))
    return highs, lows

# ─── TENDENCIA ────────────────────────────────────────────────────────────────
def get_trend(candles: list, n: int = SWING_N) -> str:
    highs, lows = find_pivots(candles, n)
    if len(highs) < 3 or len(lows) < 3:
        return "neutral"
    rh = highs[-3:]; rl = lows[-3:]
    hh = rh[2][1] > rh[1][1] > rh[0][1]
    hl = rl[2][1] > rl[1][1] > rl[0][1]
    lh = rh[2][1] < rh[1][1] < rh[0][1]
    ll = rl[2][1] < rl[1][1] < rl[0][1]
    if hh and hl: return "up"
    if lh and ll: return "down"
    if hh or hl:  return "up"
    if lh or ll:  return "down"
    return "neutral"

# ─── ULTIMA ONDA DE IMPULSO ───────────────────────────────────────────────────
def last_wave(candles: list, direction: str, n: int = SWING_N):
    """
    up   -> do ultimo pivot low ate o pivot high seguinte
    down -> do ultimo pivot high ate o pivot low seguinte
    Retorna (preco_inicio, preco_fim, idx_fim) ou None
    """
    highs, lows = find_pivots(candles, n)
    if not highs or not lows:
        return None
    if direction == "up":
        base = lows[-1]
        tops = [(i, p) for i, p in highs if i > base[0]]
        if not tops: return None
        peak = max(tops, key=lambda x: x[1])
        return (base[1], peak[1], peak[0])
    if direction == "down":
        base = highs[-1]
        troughs = [(i, p) for i, p in lows if i > base[0]]
        if not troughs: return None
        trough = min(troughs, key=lambda x: x[1])
        return (base[1], trough[1], trough[0])
    return None

# ─── ZONA DE 50% ──────────────────────────────────────────────────────────────
def in_50_zone(candles: list, wave, tol: float = ZONE_TOL):
    if wave is None:
        return False, 0.0
    start, end, _ = wave
    size = abs(end - start)
    if size == 0:
        return False, 0.0
    fifty   = (start + end) / 2
    current = candles[-1]["close"]
    dist    = abs(current - fifty) / size
    retrace = abs(current - end) / size
    return (dist <= tol), round(retrace, 3)

# ─── ENTRADA NO M1 ───────────────────────────────────────────────────────────
def m1_entry(candles_m1: list, direction: str):
    """
    1. M1 forma mini-onda de impulso na direcao do fluxo maior
    2. Essa onda e corrigida ~50%
    3. Preco nao rompeu o extremo oposto (e correcao, nao reversao)
    -> SINAL com stop no extremo da mini-onda M1
    """
    wave = last_wave(candles_m1, direction, n=SWING_M1)
    if wave is None:
        return None

    start, end, _ = wave
    size = abs(end - start)

    if size < MIN_WAVE_USD:
        return None

    in_zone, retrace = in_50_zone(candles_m1, wave, tol=ZONE_TOL)
    if not in_zone:
        return None

    current = candles_m1[-1]["close"]

    if direction == "up"   and current < start: return None
    if direction == "down" and current > start: return None

    return {
        "entry":       current,
        "stop":        start,
        "wave_start":  start,
        "wave_end":    end,
        "wave_size":   size,
        "retrace_pct": retrace,
    }

# ─── ANALISE PRINCIPAL ────────────────────────────────────────────────────────
def analyze():
    try:
        ts = datetime.utcnow().strftime("%H:%M")

        # 1. H4 - direcao do mercado
        c_h4     = get_candles("4h", 120)
        h4_trend = get_trend(c_h4)
        price    = c_h4[-1]["close"]

        print(f"[{ts} UTC] BTC ${price:,.0f} | H4: {h4_trend.upper()}")

        if h4_trend == "neutral":
            return

        # 2. H1 - confirma tendencia
        c_h1     = get_candles("1h", 120)
        h1_wave  = last_wave(c_h1, h4_trend)
        h1_in50, h1_ret = in_50_zone(c_h1, h1_wave)

        # 3. M15 - refinamento
        c_m15    = get_candles("15m", 120)
        m15_wave = last_wave(c_m15, h4_trend)
        m15_in50, m15_ret = in_50_zone(c_m15, m15_wave)

        # 4. M5 - aproximacao
        c_m5     = get_candles("5m", 100)
        m5_wave  = last_wave(c_m5, h4_trend)
        m5_in50, m5_ret = in_50_zone(c_m5, m5_wave)

        print(f"  H1 50%:{h1_in50}({h1_ret:.0%}) | "
              f"M15 50%:{m15_in50}({m15_ret:.0%}) | "
              f"M5 50%:{m5_in50}({m5_ret:.0%})")

        # Pelo menos 1 TF intermediario em correcao
        if not (h1_in50 or m15_in50 or m5_in50):
            return

        # 5. M1 - mini-onda + correcao 50% = ENTRADA
        c_m1  = get_candles("1m", 80)
        entry = m1_entry(c_m1, h4_trend)

        print(f"  M1: {entry}")

        if entry is None:
            return

        # Anti-flood
        now_ts = time.time()
        if now_ts - last_signal_time.get(h4_trend, 0) < SIGNAL_COOLDOWN:
            print("  [cooldown] bloqueado")
            return

        # Calcular alvo e R:R
        ep   = entry["entry"]
        sp   = entry["stop"]
        risk = abs(ep - sp)

        if h4_trend == "up":
            emoji   = "✅"
            action  = "COMPRA  BUY"
            direcao = "ALTA 📈"
            tp      = ep + entry["wave_size"]
            sl_lbl  = "Fundo da mini-onda M1"
        else:
            emoji   = "🔴"
            action  = "VENDA  SELL"
            direcao = "BAIXA 📉"
            tp      = ep - entry["wave_size"]
            sl_lbl  = "Topo da mini-onda M1"

        rr = round(abs(tp - ep) / risk, 1) if risk > 0 else 0

        h1_ws = (f"${h1_wave[0]:,.0f}→${h1_wave[1]:,.0f}" if h1_wave else "—")
        m1_ws = f"${entry['wave_start']:,.0f}→${entry['wave_end']:,.0f}"

        msg = (
            f"{emoji} <b>SINAL {action}</b> — BTCUSDT\n"
            f"________________________\n"
            f"💰 Entrada: <b>${ep:,.2f}</b>\n"
            f"🛑 Stop:    <b>${sp:,.2f}</b>\n"
            f"   ({sl_lbl})\n"
            f"🎯 Alvo:    <b>${tp:,.2f}</b>\n"
            f"📐 Risco: ${risk:,.2f}  |  R:R ≈ 1:{rr}\n"
            f"________________________\n"
            f"🔭 <b>Fluxo de confirmacao</b>\n"
            f"  H4  → {direcao}\n"
            f"  H1  → Onda {h1_ws}\n"
            f"        Correcao {h1_ret:.0%} {'✅' if h1_in50 else '—'}\n"
            f"  M15 → Correcao {m15_ret:.0%} {'✅' if m15_in50 else '—'}\n"
            f"  M5  → Correcao {m5_ret:.0%} {'✅' if m5_in50 else '—'}\n"
            f"  M1  → Onda {m1_ws}\n"
            f"        Correcao {entry['retrace_pct']:.0%} ✅\n"
            f"        → <b>ENTRADA LIBERADA</b>\n"
            f"________________________\n"
            f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC\n"
            f"⚠️ <i>Gerencie sempre o risco!</i>"
        )

        send_telegram(msg)
        last_signal_time[h4_trend] = now_ts

    except Exception as e:
        print(f"[ERRO] {e}")
        import traceback; traceback.print_exc()

# ─── STATUS A CADA 4H ─────────────────────────────────────────────────────────
_loop_n      = 0
STATUS_EVERY = max(1, int(4 * 3600 / CHECK_INTERVAL))

def maybe_status():
    global _loop_n
    _loop_n += 1
    if _loop_n % STATUS_EVERY != 0:
        return
    try:
        c     = get_candles("4h", 120)
        trend = get_trend(c)
        price = c[-1]["close"]
        em    = "🟢" if trend == "up" else ("🔴" if trend == "down" else "⚪")
        send_telegram(
            f"📡 <b>Status do Bot</b>\n"
            f"BTC: <b>${price:,.2f}</b>\n"
            f"H4: {em} {trend.upper()}\n"
            f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC\n"
            f"✅ Rodando normalmente"
        )
    except Exception as e:
        print(f"Erro status: {e}")

# ─── START ────────────────────────────────────────────────────────────────────
print("🤖 Multi-TF Bitcoin Bot iniciado...")
threading.Thread(target=run_server, daemon=True).start()

send_telegram(
    "🤖 <b>Bot Multi-Timeframe BTC iniciado!</b>\n\n"
    "⚙️ <b>Logica de entrada:</b>\n"
    "  H4  → define o fluxo direcional\n"
    "  H1 + M15 + M5 → confirmam correcao 50%\n"
    "  M1  → mini-onda corrige 50%\n"
    "        → <b>ENTRADA + stop no extremo M1</b>\n\n"
    f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"
)

while True:
    maybe_status()
    analyze()
    time.sleep(CHECK_INTERVAL)
