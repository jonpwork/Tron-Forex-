import requests
import pandas as pd
import time
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ================== CONFIG ==================
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "8762172696:AAHP3CSVO5KDI9PBjzxvTI_yQUVHt1B4UzM")
CHAT_ID         = os.environ.get("CHAT_ID", "8085416549")
SYMBOL          = "BTCUSDT"
CHECK_INTERVAL  = 60            # Verificar a cada 1 minuto (M1 precisa de agilidade)
SIGNAL_COOLDOWN = 1800          # Não repetir mesmo sinal por 30 min
PORT            = int(os.environ.get("PORT", 8080))
SWING_LOOKBACK  = 5             # Candles para confirmar pivot nos TFs maiores
SWING_M1        = 3             # Candles para confirmar pivot no M1 (mais rápido)
ZONE_TOLERANCE  = 0.06          # +-6% da onda = zona de 50%
# ============================================

last_signal_time = {}

# --- HTTP keep-alive (Render) -----------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Multi-TF Bitcoin Bot running")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# --- TELEGRAM ----------------------------------------------------------------
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
        print(f"[TG] {msg[:120].strip()}")
    except Exception as e:
        print(f"Erro Telegram: {e}")

# --- BINANCE DATA ------------------------------------------------------------
def get_candles(timeframe: str, limit: int = 120) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol": SYMBOL, "interval": timeframe,
                                   "limit": limit}, timeout=10)
    r.raise_for_status()
    cols = ["time","open","high","low","close","volume",
            "close_time","qv","trades","tb","tq","ignore"]
    df = pd.DataFrame(r.json(), columns=cols)[["time","open","high","low","close"]].copy()
    df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    return df.reset_index(drop=True)

# --- PIVOT HIGHS / LOWS ------------------------------------------------------
def find_pivots(df: pd.DataFrame, n: int = SWING_LOOKBACK):
    highs, lows = [], []
    for i in range(n, len(df) - n):
        if df["high"].iloc[i] == df["high"].iloc[i-n:i+n+1].max():
            highs.append((i, df["high"].iloc[i]))
        if df["low"].iloc[i] == df["low"].iloc[i-n:i+n+1].min():
            lows.append((i, df["low"].iloc[i]))
    return highs, lows

# --- TENDENCIA (estrutura de topos e fundos) ---------------------------------
def get_trend(df: pd.DataFrame, n: int = SWING_LOOKBACK) -> str:
    highs, lows = find_pivots(df, n)
    if len(highs) < 3 or len(lows) < 3:
        return "neutral"
    rh = highs[-3:]; rl = lows[-3:]
    hh = rh[-1][1] > rh[-2][1] > rh[-3][1]
    hl = rl[-1][1] > rl[-2][1] > rl[-3][1]
    lh = rh[-1][1] < rh[-2][1] < rh[-3][1]
    ll = rl[-1][1] < rl[-2][1] < rl[-3][1]
    if hh and hl:  return "up"
    if lh and ll:  return "down"
    if hh or hl:   return "up"
    if lh or ll:   return "down"
    return "neutral"

# --- ULTIMA ONDA DE IMPULSO --------------------------------------------------
def last_impulse_wave(df: pd.DataFrame, direction: str, n: int = SWING_LOOKBACK):
    """
    up   -> do ultimo pivot low ate o pivot high seguinte mais alto
    down -> do ultimo pivot high ate o pivot low seguinte mais baixo
    Retorna (preco_inicio, preco_fim, idx_fim) ou None.
    """
    highs, lows = find_pivots(df, n)
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

# --- ZONA DE 50% -------------------------------------------------------------
def in_50_zone(df: pd.DataFrame, wave, tol: float = ZONE_TOLERANCE):
    """
    Retorna (bool_na_zona, pct_retracao_atual).
    Retracao medida a partir do FIM da onda (onde o impulso terminou).
    """
    if wave is None:
        return False, 0.0
    start, end, _ = wave
    size = abs(end - start)
    if size == 0:
        return False, 0.0

    fifty   = (start + end) / 2
    current = df["close"].iloc[-1]
    dist    = abs(current - fifty) / size
    retrace = abs(current - end) / size
    return (dist <= tol), round(retrace, 3)

# --- ENTRADA NO M1: onda de impulso + correcao 50% --------------------------
def m1_entry(df_m1: pd.DataFrame, direction: str):
    """
    Logica de entrada no M1:

    Passo 1: M1 forma uma mini-onda de impulso na DIRECAO do fluxo maior.
             Ex (buy): M1 sobe de A ate B.

    Passo 2: Essa mini-onda e corrigida ~50% (preco esta entre A e B, perto de (A+B)/2).

    Passo 3: O preco NAO rompeu o extremo oposto da onda
             (confirmando que e correcao, nao reversao).

    SINAL -> Entrada no preco atual.
    STOP  -> Fundo da mini-onda (buy) ou Topo da mini-onda (sell).

    Retorna dict com detalhes ou None.
    """
    wave = last_impulse_wave(df_m1, direction, n=SWING_M1)
    if wave is None:
        return None

    start, end, end_idx = wave
    size = abs(end - start)
    if size == 0:
        return None

    # A onda precisa de tamanho minimo (~$50 no BTC para evitar ruido)
    if size < 50:
        return None

    in_zone, retrace = in_50_zone(df_m1, wave, tol=ZONE_TOLERANCE)
    if not in_zone:
        return None

    current = df_m1["close"].iloc[-1]

    # Preco nao pode ter rompido o inicio da onda (seria reversao)
    if direction == "up" and current < start:
        return None
    if direction == "down" and current > start:
        return None

    # Stop = extremo de inicio da onda (fundo para buy, topo para sell)
    stop = start

    return {
        "signal":      True,
        "entry":       current,
        "stop":        stop,
        "wave_start":  start,
        "wave_end":    end,
        "wave_size":   size,
        "retrace_pct": retrace,
    }

# --- ANALISE PRINCIPAL -------------------------------------------------------
def analyze():
    try:
        now_str = datetime.utcnow().strftime("%H:%M")

        # 1. H4: direcao do mercado
        df_h4    = get_candles("4h", 120)
        h4_trend = get_trend(df_h4)
        price    = df_h4["close"].iloc[-1]

        print(f"[{now_str} UTC] BTC ${price:,.0f} | H4: {h4_trend.upper()}")

        if h4_trend == "neutral":
            return

        # 2. H1: confirma tendencia e correcao
        df_h1    = get_candles("1h", 120)
        h1_trend = get_trend(df_h1)
        h1_wave  = last_impulse_wave(df_h1, h4_trend)
        h1_in50, h1_ret = in_50_zone(df_h1, h1_wave)

        # 3. M15: refinamento
        df_m15   = get_candles("15m", 120)
        m15_wave = last_impulse_wave(df_m15, h4_trend)
        m15_in50, m15_ret = in_50_zone(df_m15, m15_wave)

        # 4. M5: aproximacao
        df_m5    = get_candles("5m", 100)
        m5_wave  = last_impulse_wave(df_m5, h4_trend)
        m5_in50, m5_ret = in_50_zone(df_m5, m5_wave)

        print(f"  H1({h1_trend}) 50%:{h1_in50}({h1_ret:.0%}) | "
              f"M15 50%:{m15_in50}({m15_ret:.0%}) | "
              f"M5 50%:{m5_in50}({m5_ret:.0%})")

        # Precisa de pelo menos 1 TF intermediario em correcao
        if not (h1_in50 or m15_in50 or m5_in50):
            return

        # 5. M1: mini-onda de impulso + correcao 50% = ENTRADA
        df_m1  = get_candles("1m", 80)
        entry  = m1_entry(df_m1, h4_trend)

        print(f"  M1 entrada: {entry}")

        if entry is None:
            return

        # 6. Anti-flood
        now_ts = time.time()
        key    = h4_trend
        if now_ts - last_signal_time.get(key, 0) < SIGNAL_COOLDOWN:
            print(f"  [cooldown] sinal {key} bloqueado")
            return

        # 7. Calcular alvo e R:R
        entry_price = entry["entry"]
        stop_price  = entry["stop"]
        risk        = abs(entry_price - stop_price)

        if h4_trend == "up":
            emoji    = "✅"
            action   = "COMPRA  BUY"
            direcao  = "ALTA 📈"
            tp_price = entry_price + entry["wave_size"]   # projecao da onda M1
            sl_label = "Fundo da mini-onda M1"
        else:
            emoji    = "🔴"
            action   = "VENDA  SELL"
            direcao  = "BAIXA 📉"
            tp_price = entry_price - entry["wave_size"]
            sl_label = "Topo da mini-onda M1"

        rr = round(abs(tp_price - entry_price) / risk, 1) if risk > 0 else 0

        h1_ws  = (f"${h1_wave[0]:,.0f} -> ${h1_wave[1]:,.0f}" if h1_wave else "-")
        m1_ws  = f"${entry['wave_start']:,.0f} -> ${entry['wave_end']:,.0f}"

        msg = (
            f"{emoji} <b>SINAL {action}</b> — BTCUSDT\n"
            f"________________________\n"
            f"💰 Entrada: <b>${entry_price:,.2f}</b>\n"
            f"🛑 Stop:    <b>${stop_price:,.2f}</b>\n"
            f"   ({sl_label})\n"
            f"🎯 Alvo:    <b>${tp_price:,.2f}</b>\n"
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
        last_signal_time[key] = now_ts

    except Exception as e:
        print(f"[ERRO] {e}")
        import traceback; traceback.print_exc()

# --- STATUS A CADA 4H --------------------------------------------------------
_loop_n = 0
STATUS_EVERY = max(1, int(4 * 3600 / CHECK_INTERVAL))

def maybe_status():
    global _loop_n
    _loop_n += 1
    if _loop_n % STATUS_EVERY != 0:
        return
    try:
        df    = get_candles("4h", 120)
        trend = get_trend(df)
        price = df["close"].iloc[-1]
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

# --- INICIALIZACAO -----------------------------------------------------------
print("🤖 Multi-TF Bitcoin Bot iniciado...")
threading.Thread(target=run_server, daemon=True).start()

send_telegram(
    "🤖 <b>Bot Multi-Timeframe BTC iniciado!</b>\n\n"
    "⚙️ <b>Logica de entrada:</b>\n"
    "  H4  → define o fluxo direcional\n"
    "  H1 + M15 + M5 → confirmam correcao 50%\n"
    "  M1  → mini-onda de impulso corrige 50%\n"
    "        → <b>ENTRADA + stop no extremo da onda</b>\n\n"
    f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"
)

while True:
    maybe_status()
    analyze()
    time.sleep(CHECK_INTERVAL)
