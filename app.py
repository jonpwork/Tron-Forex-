"""
Multi-Timeframe Bitcoin Signal Bot
- Fonte de dados: Kraken (API publica, sem bloqueio geo)
- Aprendizado via Claude Vision
- Memoria no GitHub
"""

import requests
import time
import os
import json
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ================== CONFIG ==================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "")
GROQ_KEY         = os.environ.get("GROQ_API_KEY", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")
GITHUB_FILE      = "memory.json"
SYMBOL           = "BTCUSDT"
CHECK_INTERVAL   = 60
SIGNAL_COOLDOWN  = 1800
PORT             = int(os.environ.get("PORT", 8080))
SWING_N          = 5
SWING_M1         = 3
# ============================================

last_signal_time = {}
last_update_id   = 0

memory = {
    "analyses":     [],
    "signals":      [],   # historico de sinais com resultado
    "zone_tol":     0.08,
    "min_wave_usd": 15,
    "total_prints": 0,
    "last_update":  ""
}

# ─── HTTP keep-alive ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"BTC Bot running")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
def send_telegram(msg, chat_id=None):
    cid = chat_id or CHAT_ID
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        print(f"[TG] {msg[:80].strip()}")
    except Exception as e:
        print(f"Erro Telegram: {e}")

def get_updates():
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 2},
            timeout=8
        )
        updates = r.json().get("result", [])
        if updates:
            last_update_id = updates[-1]["update_id"]
        return updates
    except:
        return []

def download_telegram_photo(file_id):
    r   = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
        params={"file_id": file_id}, timeout=10
    )
    fp  = r.json()["result"]["file_path"]
    img = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fp}",
        timeout=20
    )
    return img.content

# ─── KRAKEN DATA (API publica, sem bloqueio geo em servidores cloud) ─────────
# Mapa de timeframes: nosso formato -> Kraken (em minutos)
TF_MAP = {
    "1m": 1, "5m": 5, "15m": 15,
    "1h": 60, "4h": 240
}
KRAKEN_PAIR = "XBTUSDT"   # BTC/USDT na Kraken

def get_candles(tf, limit=120):
    """Busca candles da Kraken — API publica, sem restricao geografica."""
    interval = TF_MAP.get(tf, 60)
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": KRAKEN_PAIR, "interval": interval},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise Exception(f"Kraken erro: {data['error']}")
    # A Kraken retorna lista com nome dinamico da pair — pegar primeiro resultado
    result_key = [k for k in data["result"] if k != "last"][0]
    rows = data["result"][result_key]
    # rows = [time, open, high, low, close, vwap, volume, count] — ja em ordem crescente
    candles = []
    for k in rows[-limit:]:
        candles.append({
            "open":  float(k[1]),
            "high":  float(k[2]),
            "low":   float(k[3]),
            "close": float(k[4])
        })
    return candles

# ─── GITHUB MEMORIA ──────────────────────────────────────────────────────────
def gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"}

def load_memory_from_github():
    global memory
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[MEM] GitHub nao configurado"); return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = requests.get(url, headers=gh_headers(), timeout=10)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            memory  = json.loads(content)
            print(f"[MEM] Carregada: {memory['total_prints']} prints")
        else:
            print("[MEM] Arquivo novo, usando padrao")
    except Exception as e:
        print(f"[MEM] Erro ao carregar: {e}")

def save_memory_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    try:
        url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        content = base64.b64encode(
            json.dumps(memory, indent=2, ensure_ascii=False).encode()
        ).decode()
        r       = requests.get(url, headers=gh_headers(), timeout=10)
        payload = {
            "message": f"memoria: {memory['total_prints']} prints",
            "content": content
        }
        if r.status_code == 200:
            payload["sha"] = r.json()["sha"]
        requests.put(url, headers=gh_headers(), json=payload, timeout=15)
        print("[MEM] Salva no GitHub")
    except Exception as e:
        print(f"[MEM] Erro ao salvar: {e}")

# ─── GROQ VISION (gratis, llama-4-scout com visao) ──────────────────────────
VISION_PROMPT = """Voce e um especialista em analise tecnica de trading.
Analise este grafico do MetaTrader 5 com as marcacoes do trader.

Retorne APENAS um JSON valido, sem texto extra, sem markdown:
{
  "timeframe": "M1/M5/M15/H1/H4",
  "tendencia": "up/down/neutral",
  "tipo_onda": "impulso/correcao/lateral",
  "nivel_entrada": 0.0,
  "nivel_stop": 0.0,
  "nivel_alvo": 0.0,
  "correcao_pct": 0.0,
  "observacoes": "descricao curta",
  "padroes": ["padrao1", "padrao2"],
  "qualidade_setup": "alta/media/baixa"
}
Se nao identificar um campo use null."""

def analyze_image_with_claude(image_bytes):
    if not GROQ_KEY:
        raise Exception("GROQ_API_KEY nao configurada no Render!")

    img_b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {GROQ_KEY}",
            "Content-Type":  "application/json"
        },
        json={
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",      "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/jpeg;base64,{img_b64}"
                    }}
                ]
            }],
            "max_tokens": 1000,
            "temperature": 0.1
        },
        timeout=30
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = text.replace("```json","").replace("```","").strip()
    return json.loads(text)

# ─── CALIBRAR PARAMETROS ─────────────────────────────────────────────────────
def calibrate_from_memory():
    bons = [a for a in memory["analyses"]
            if a.get("qualidade_setup") == "alta"
            and a.get("correcao_pct") and float(a["correcao_pct"]) > 0]
    if len(bons) >= 3:
        media = sum(float(a["correcao_pct"]) for a in bons) / len(bons)
        nova  = round(abs(media - 0.5) + 0.10, 3)
        nova  = max(0.05, min(0.20, nova))
        memory["zone_tol"] = nova
        print(f"[CAL] Tolerancia: {nova:.0%} ({len(bons)} setups)")

# ─── PROCESSAR PRINT ─────────────────────────────────────────────────────────
def process_chart_image(image_bytes, chat_id, caption=""):
    send_telegram("🔍 Analisando seu grafico com IA...", chat_id)
    try:
        analise = analyze_image_with_claude(image_bytes)
    except Exception as e:
        send_telegram(f"❌ Erro na analise: {e}", chat_id)
        return

    analise["data"]    = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
    analise["caption"] = caption

    memory["analyses"].append(analise)
    if len(memory["analyses"]) > 100:
        memory["analyses"] = memory["analyses"][-100:]
    memory["total_prints"] += 1
    memory["last_update"]   = analise["data"]

    calibrate_from_memory()
    save_memory_to_github()

    tf   = analise.get("timeframe","—")
    tend = analise.get("tendencia","—")
    tipo = analise.get("tipo_onda","—")
    corr = analise.get("correcao_pct")
    ep   = analise.get("nivel_entrada")
    sp   = analise.get("nivel_stop")
    alvo = analise.get("nivel_alvo")
    qual = analise.get("qualidade_setup","—")
    obs  = analise.get("observacoes","—")
    pads = analise.get("padroes",[]) or []

    te = "📈" if tend=="up" else ("📉" if tend=="down" else "↔️")
    qe = "🟢" if qual=="alta" else ("🟡" if qual=="media" else "🔴")

    msg = (
        f"✅ <b>Grafico analisado!</b>\n"
        f"________________________\n"
        f"📊 Timeframe: <b>{tf}</b>\n"
        f"📈 Tendencia: <b>{tend.upper()}</b> {te}\n"
        f"🌊 Tipo onda: <b>{tipo}</b>\n"
        f"📐 Correcao:  <b>{f'{float(corr):.0%}' if corr else '—'}</b>\n"
        f"________________________\n"
    )
    if ep:  msg += f"💰 Entrada: ${float(ep):,.2f}\n"
    if sp:  msg += f"🛑 Stop:    ${float(sp):,.2f}\n"
    if alvo: msg += f"🎯 Alvo:    ${float(alvo):,.2f}\n"
    msg += (
        f"________________________\n"
        f"{qe} Qualidade: <b>{qual.upper()}</b>\n"
        f"💡 {obs}\n"
    )
    if pads:
        msg += f"🔍 Padroes: {', '.join(pads)}\n"
    msg += (
        f"________________________\n"
        f"🧠 Total aprendido: <b>{memory['total_prints']} prints</b>\n"
        f"⚙️ Tolerancia 50%: <b>{memory['zone_tol']:.0%}</b>\n"
        f"📅 {analise['data']} UTC"
    )
    send_telegram(msg, chat_id)


# ═══════════════════════════════════════════════════════════════════════════════
# TRON-SMC/EWF ENGINE — Order Blocks · FVG · Fibonacci · Liquidity Sweep
# ═══════════════════════════════════════════════════════════════════════════════

def swing_points(candles, left=5, right=5):
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i]["high"]
        l = candles[i]["low"]
        if all(h >= candles[i-j]["high"] for j in range(1,left+1)) and            all(h >= candles[i+j]["high"] for j in range(1,right+1)):
            highs.append((i, h))
        if all(l <= candles[i-j]["low"] for j in range(1,left+1)) and            all(l <= candles[i+j]["low"] for j in range(1,right+1)):
            lows.append((i, l))
    return highs, lows

def detect_order_block(candles, direction):
    n = len(candles)
    if n < 10:
        return None
    recent = candles[-5:]
    if direction == "up":
        impulse = recent[-1]["close"] - recent[-3]["open"]
        if impulse < 0:
            return None
        for i in range(n-4, max(n-20,0), -1):
            c = candles[i]
            if c["close"] < c["open"]:
                return (c["low"], c["high"])
    else:
        impulse = recent[-3]["open"] - recent[-1]["close"]
        if impulse < 0:
            return None
        for i in range(n-4, max(n-20,0), -1):
            c = candles[i]
            if c["close"] > c["open"]:
                return (c["low"], c["high"])
    return None

def detect_fvg(candles, direction):
    fvgs = []
    n = len(candles)
    price_now = candles[-1]["close"]
    for i in range(2, min(n-2, 30)):
        c0 = candles[n-1-i]
        c2 = candles[n-1-i+2]
        if direction == "up":
            if c2["low"] > c0["high"]:
                flo, fhi = c0["high"], c2["low"]
                if price_now <= fhi * 1.005:
                    fvgs.append((flo, fhi))
        else:
            if c2["high"] < c0["low"]:
                flo, fhi = c2["high"], c0["low"]
                if price_now >= flo * 0.995:
                    fvgs.append((flo, fhi))
    return fvgs[:3]

def detect_liquidity_sweep(candles, direction, lookback=20):
    n = len(candles)
    if n < lookback + 5:
        return False
    window = candles[n-lookback-5 : n-5]
    recent = candles[n-5:]
    if direction == "up":
        window_low   = min(c["low"]  for c in window)
        recent_low   = min(c["low"]  for c in recent)
        recent_close = recent[-1]["close"]
        recent_open  = recent[-5]["open"]
        return recent_low < window_low and recent_close > recent_open
    else:
        window_high  = max(c["high"] for c in window)
        recent_high  = max(c["high"] for c in recent)
        recent_close = recent[-1]["close"]
        recent_open  = recent[-5]["open"]
        return recent_high > window_high and recent_close < recent_open

def fibonacci_levels(swing_low, swing_high, direction):
    diff = swing_high - swing_low
    if direction == "up":
        ret = {
            "0.382": swing_high - diff * 0.382,
            "0.500": swing_high - diff * 0.500,
            "0.618": swing_high - diff * 0.618,
            "0.786": swing_high - diff * 0.786,
        }
        ext = {"1.618": swing_low + diff * 1.618}
    else:
        ret = {
            "0.382": swing_low  + diff * 0.382,
            "0.500": swing_low  + diff * 0.500,
            "0.618": swing_low  + diff * 0.618,
            "0.786": swing_low  + diff * 0.786,
        }
        ext = {"1.618": swing_high - diff * 1.618}
    return ret, ext

def in_golden_zone(price, fib_ret, tol=0.015):
    lo = min(fib_ret["0.500"], fib_ret["0.618"]) * (1 - tol)
    hi = max(fib_ret["0.500"], fib_ret["0.618"]) * (1 + tol)
    return lo <= price <= hi

def smc_confluence_score(candles_m5, candles_m15, candles_h1, direction, price):
    score    = 0
    detalhes = []
    tp_fib   = None
    ob_result= None
    all_fvgs = []

    # 1. ORDER BLOCK (30 pts)
    ob_h1  = detect_order_block(candles_h1,  direction)
    ob_m15 = detect_order_block(candles_m15, direction)
    ob_result = ob_h1 or ob_m15
    if ob_result:
        ob_lo, ob_hi = ob_result
        if ob_lo <= price <= ob_hi:
            score += 30
            tf_ob = "H1" if ob_h1 else "M15"
            detalhes.append(f"✅ Order Block {tf_ob} (${ob_lo:,.0f}–${ob_hi:,.0f})")
        else:
            score += 10
            detalhes.append(f"⚠️ Order Block detectado fora do range")
    else:
        detalhes.append("❌ Sem Order Block")

    # 2. FAIR VALUE GAP (20 pts)
    fvgs_m5  = detect_fvg(candles_m5,  direction)
    fvgs_m15 = detect_fvg(candles_m15, direction)
    all_fvgs = fvgs_m5 + fvgs_m15
    if all_fvgs:
        fvg_c = min(all_fvgs, key=lambda f: abs((f[0]+f[1])/2 - price))
        dist_pct = abs((fvg_c[0]+fvg_c[1])/2 - price) / price * 100
        if dist_pct < 0.4:
            score += 20
            detalhes.append(f"✅ FVG ativo ${fvg_c[0]:,.0f}–${fvg_c[1]:,.0f}")
        elif dist_pct < 1.5:
            score += 10
            detalhes.append(f"⚠️ FVG proximo {dist_pct:.1f}% dist")
        else:
            detalhes.append(f"❌ FVG distante {dist_pct:.1f}%")
    else:
        detalhes.append("❌ Sem Fair Value Gap")

    # 3. FIBONACCI GOLDEN ZONE (25 pts)
    highs_h1, lows_h1 = swing_points(candles_h1, left=3, right=3)
    if highs_h1 and lows_h1:
        if direction == "up":
            sw_lo = min(lows_h1,  key=lambda x: x[1])[1]
            sw_hi = max(highs_h1, key=lambda x: x[1])[1]
        else:
            sw_hi = max(highs_h1, key=lambda x: x[1])[1]
            sw_lo = min(lows_h1,  key=lambda x: x[1])[1]
        if sw_hi > sw_lo:
            fib_ret, fib_ext = fibonacci_levels(sw_lo, sw_hi, direction)
            tp_fib = fib_ext["1.618"]
            if in_golden_zone(price, fib_ret):
                score += 25
                detalhes.append(f"✅ Golden Zone 50–61.8% (${fib_ret['0.500']:,.0f}–${fib_ret['0.618']:,.0f})")
            elif abs(price - fib_ret["0.382"]) / price < 0.01:
                score += 12
                detalhes.append(f"⚠️ Fib 38.2% em ${fib_ret['0.382']:,.0f}")
            elif abs(price - fib_ret["0.786"]) / price < 0.01:
                score += 15
                detalhes.append(f"⚠️ Fib 78.6% em ${fib_ret['0.786']:,.0f}")
            else:
                detalhes.append("❌ Fora das zonas Fibonacci")
    else:
        detalhes.append("⚠️ Swings insuficientes para Fibonacci")

    # 4. LIQUIDITY SWEEP / CHoCH (25 pts)
    sweep_h1  = detect_liquidity_sweep(candles_h1,  direction, lookback=10)
    sweep_m15 = detect_liquidity_sweep(candles_m15, direction, lookback=15)
    if sweep_h1:
        score += 25
        detalhes.append("✅ Liquidity Sweep H1 + CHoCH")
    elif sweep_m15:
        score += 15
        detalhes.append("✅ Liquidity Sweep M15")
    else:
        detalhes.append("❌ Sem Liquidity Sweep")

    return score, detalhes, ob_result, all_fvgs, tp_fib

# ─── ANALISE TECNICA ─────────────────────────────────────────────────────────
def find_pivots(candles, n):
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        wh = [candles[j]["high"] for j in range(i-n, i+n+1)]
        wl = [candles[j]["low"]  for j in range(i-n, i+n+1)]
        if candles[i]["high"] == max(wh): highs.append((i, candles[i]["high"]))
        if candles[i]["low"]  == min(wl): lows.append((i, candles[i]["low"]))
    return highs, lows

def get_trend(candles, n=SWING_N):
    highs, lows = find_pivots(candles, n)
    if not highs or not lows: return "neutral"

    # Pega últimos pivots disponíveis (min 2)
    rh = highs[-3:] if len(highs) >= 3 else highs[-2:] if len(highs) >= 2 else highs
    rl = lows[-3:]  if len(lows)  >= 3 else lows[-2:]  if len(lows)  >= 2 else lows

    # Pontuação: +1 para cada HH/HL (up) ou LH/LL (down)
    score_up = score_dn = 0
    for i in range(1, len(rh)):
        if rh[i][1] > rh[i-1][1]: score_up += 1
        else: score_dn += 1
    for i in range(1, len(rl)):
        if rl[i][1] > rl[i-1][1]: score_up += 1
        else: score_dn += 1

    # Estrutura atual do preço vs média dos pivots
    price_now = candles[-1]["close"]
    avg_high = sum(h[1] for h in rh) / len(rh)
    avg_low  = sum(l[1] for l in rl)  / len(rl)
    mid = (avg_high + avg_low) / 2
    if price_now > mid: score_up += 1
    else: score_dn += 1

    # Último swing: direção do movimento mais recente
    last_h_idx = highs[-1][0] if highs else 0
    last_l_idx = lows[-1][0]  if lows  else 0
    if last_h_idx > last_l_idx: score_dn += 1  # último pivot foi topo → possível queda
    else: score_up += 1                          # último pivot foi fundo → possível subida

    if score_up > score_dn: return "up"
    if score_dn > score_up: return "down"
    return "neutral"

def last_wave(candles, direction, n=SWING_N):
    highs, lows = find_pivots(candles, n)
    if not highs or not lows: return None
    if direction == "up":
        base = lows[-1]
        tops = [(i,p) for i,p in highs if i > base[0]]
        if not tops: return None
        peak = max(tops, key=lambda x: x[1])
        return (base[1], peak[1], peak[0])
    base = highs[-1]
    ts   = [(i,p) for i,p in lows if i > base[0]]
    if not ts: return None
    t    = min(ts, key=lambda x: x[1])
    return (base[1], t[1], t[0])

def in_50_zone(candles, wave, tol=None):
    tol = tol or memory["zone_tol"]
    if wave is None: return False, 0.0
    start, end, _ = wave
    size = abs(end - start)
    if size == 0: return False, 0.0
    fifty   = (start + end) / 2
    current = candles[-1]["close"]
    return (abs(current - fifty) / size <= tol), round(abs(current - end) / size, 3)

def in_ote_zone(price, swing_lo, swing_hi, direction):
    """ICT Optimal Trade Entry: 0.618 a 0.786 de retração — zona de maior probabilidade."""
    diff = swing_hi - swing_lo
    if diff == 0: return False, 0.0
    if direction == "up":
        ote_hi = swing_hi - diff * 0.500   # aceita de 50% também (golden zone)
        ote_lo = swing_hi - diff * 0.786
        retrace = (swing_hi - price) / diff
    else:
        ote_lo = swing_lo + diff * 0.500
        ote_hi = swing_lo + diff * 0.786
        retrace = (price - swing_lo) / diff
    in_zone = ote_lo <= price <= ote_hi
    return in_zone, round(retrace, 3)

def m1_entry(candles_m1, direction):
    wave = last_wave(candles_m1, direction, n=SWING_M1)
    if wave is None: return None
    start, end, _ = wave
    size = abs(end - start)
    if size < memory["min_wave_usd"]: return None
    cur = candles_m1[-1]["close"]
    if direction == "up":
        sw_lo, sw_hi = start, end
        if cur < start: return None
    else:
        sw_lo, sw_hi = end, start
        if cur > start: return None

    # Tenta OTE primeiro (0.50–0.786), fallback para tolerância calibrada
    in_ote, retrace = in_ote_zone(cur, sw_lo, sw_hi, direction)
    if not in_ote:
        in_50, retrace = in_50_zone(candles_m1, wave)
        if not in_50: return None

    return {"entry": cur, "stop": start, "wave_start": start,
            "wave_end": end, "wave_size": size, "retrace_pct": retrace,
            "in_ote": in_ote}

def full_analyze():
    c_h4 = get_candles("4h", 120); h4_trend = get_trend(c_h4); price = c_h4[-1]["close"]
    c_h1 = get_candles("1h", 120); h1_wave  = last_wave(c_h1, h4_trend)
    h1_in50,  h1_ret  = in_50_zone(c_h1,  h1_wave)
    c_m15= get_candles("15m",120); m15_wave = last_wave(c_m15, h4_trend)
    m15_in50, m15_ret = in_50_zone(c_m15, m15_wave)
    c_m5 = get_candles("5m", 100); m5_wave  = last_wave(c_m5, h4_trend)
    m5_in50,  m5_ret  = in_50_zone(c_m5,  m5_wave)
    c_m1 = get_candles("1m",  80)
    entry = None
    entry_tf = ""
    if h4_trend != "neutral":
        entry = m1_entry(c_m1, h4_trend)
        if entry: entry_tf = "M1"
        else:
            # Fallback: tenta M5 como entrada quando M1 sem onda clara
            entry5 = m1_entry(c_m5, h4_trend)
            if entry5:
                entry = entry5
                entry_tf = "M5"

    # SMC Confluence Score — roda sempre para enriquecer debug
    smc_score, smc_detalhes, ob_zone, fvgs, tp_fib = (0, [], None, [], None)
    if h4_trend != "neutral":
        smc_score, smc_detalhes, ob_zone, fvgs, tp_fib = smc_confluence_score(
            c_m5, c_m15, c_h1, h4_trend, price
        )

    # Qualidade SMC: Alta ≥60, Média ≥35, Baixa <35
    smc_qual = "🟢 ALTA" if smc_score >= 60 else ("🟡 MEDIA" if smc_score >= 35 else "🔴 BAIXA")

    # Bloqueio inteligente — exige score mínimo de 35 para disparar
    if h4_trend == "neutral":
        bloqueio = "H4 sem tendencia"
    elif not(h1_in50 or m15_in50 or m5_in50):
        bloqueio = "TFs aguardando correcao 50%"
    elif entry is None:
        bloqueio = "M1/M5 aguardando OTE (50–78.6%)"
    elif smc_score < 35:
        bloqueio = f"SMC score insuficiente ({smc_score}/100) — sem confluencia"
        entry = None  # bloqueia sinal de baixa qualidade
    else:
        bloqueio = ""

    ote_lbl = "OTE ✅" if (entry and entry.get("in_ote")) else "50% zone"
    debug = (
        f"📊 <b>Analise BTCUSDT</b>\n"
        f"💰 <b>${price:,.2f}</b>\n"
        f"________________________\n"
        f"H4  → {'ALTA 📈' if h4_trend=='up' else 'BAIXA 📉' if h4_trend=='down' else 'NEUTRO ⚪'}\n"
        f"H1  → {h1_ret:.0%} {'✅' if h1_in50 else '❌'}\n"
        f"M15 → {m15_ret:.0%} {'✅' if m15_in50 else '❌'}\n"
        f"M5  → {m5_ret:.0%} {'✅' if m5_in50 else '❌'}\n"
        f"{entry_tf or 'M1'}  → {'✅ ' + ote_lbl if entry else '❌ Aguardando'}\n"
        f"________________________\n"
        f"🏦 SMC Score: <b>{smc_score}/100</b> {smc_qual}\n"
        + "\n".join(smc_detalhes) + "\n"
        f"________________________\n"
        f"🧠 Prints: {memory['total_prints']} | Tol: {memory['zone_tol']:.0%}\n"
        + (f"⏳ <i>{bloqueio}</i>" if bloqueio else "🚀 <b>SINAL PRONTO!</b>")
    )
    return (debug, entry, h4_trend, price,
            h1_wave, h1_in50, h1_ret,
            m15_in50, m15_ret, m5_in50, m5_ret,
            smc_score, smc_detalhes, ob_zone, fvgs, tp_fib)

def fire_signal(entry, h4_trend, h1_wave, h1_in50, h1_ret,
                m15_in50, m15_ret, m5_in50, m5_ret,
                smc_score=0, smc_detalhes=None, ob_zone=None, fvgs=None, tp_fib=None):
    ep=entry["entry"]; sp=entry["stop"]; risk=abs(ep-sp)
    if h4_trend=="up":
        emoji="✅"; action="COMPRA BUY"; direcao="ALTA 📈"
        # TP: Fibonacci 1.618 se disponível, senão tamanho da onda
        tp = tp_fib if (tp_fib and tp_fib > ep) else ep + entry["wave_size"]
        sl_lbl="Fundo onda M1 / OB"
    else:
        emoji="🔴"; action="VENDA SELL"; direcao="BAIXA 📉"
        tp = tp_fib if (tp_fib and tp_fib < ep) else ep - entry["wave_size"]
        sl_lbl="Topo onda M1 / OB"
    rr    = round(abs(tp-ep)/risk,1) if risk>0 else 0
    h1_ws = (f"${h1_wave[0]:,.0f}→${h1_wave[1]:,.0f}" if h1_wave else "—")
    m1_ws = f"${entry['wave_start']:,.0f}→${entry['wave_end']:,.0f}"
    ote_lbl = "OTE 61.8–78.6%" if entry.get("in_ote") else "Golden Zone 50%"
    smc_sum = "\n".join(smc_detalhes or [])
    note  = (f"\n🧠 <i>Calibrado com {memory['total_prints']} prints</i>"
             if memory["total_prints"] > 0 else "")
    ts = datetime.utcnow().strftime('%d/%m/%Y %H:%M')
    # Salva sinal aberto na memoria
    sinal = {
        "id":        len(memory["signals"]) + 1,
        "direcao":   h4_trend,
        "entrada":   ep,
        "stop":      sp,
        "alvo":      tp,
        "risco":     risk,
        "rr":        rr,
        "data":      ts,
        "status":    "aberto",
        "resultado": None
    }
    memory["signals"].append(sinal)
    if len(memory["signals"]) > 200:
        memory["signals"] = memory["signals"][-200:]
    save_memory_to_github()

    send_telegram(
        f"{emoji} <b>SINAL {action}</b> — BTCUSDT\n"
        f"________________________\n"
        f"💰 Entrada: <b>${ep:,.2f}</b>\n"
        f"🛑 Stop:    <b>${sp:,.2f}</b> ({sl_lbl})\n"
        f"🎯 Alvo:    <b>${tp:,.2f}</b>  {'(Fib 1.618)' if tp_fib else '(Onda M1)'}\n"
        f"📐 Risco: ${risk:,.2f}  |  R:R ≈ 1:{rr}\n"
        f"________________________\n"
        f"📡 <b>Multi-TF:</b>\n"
        f"H4 → {direcao}\n"
        f"H1  → {h1_ws} | {h1_ret:.0%} {'✅' if h1_in50 else '—'}\n"
        f"M15 → {m15_ret:.0%} {'✅' if m15_in50 else '—'}\n"
        f"M5  → {m5_ret:.0%} {'✅' if m5_in50 else '—'}\n"
        f"M1  → {m1_ws} | {entry['retrace_pct']:.0%} ✅ {ote_lbl}\n"
        f"________________________\n"
        f"🏦 <b>SMC Score: {smc_score}/100</b>\n"
        f"{smc_sum}\n"
        f"________________________\n"
        f"     → <b>ENTRADA LIBERADA</b>{note}\n"
        f"⏰ {ts} UTC\n"
        f"⚠️ <i>Gerencie sempre o risco!</i>"
    )

# ─── MONITORAR SINAIS ABERTOS ────────────────────────────────────────────────
def check_open_signals(price):
    """Verifica se algum sinal aberto bateu TP ou SL."""
    abertos = [s for s in memory.get("signals", []) if s["status"] == "aberto"]
    if not abertos:
        return
    alterado = False
    for s in abertos:
        ep = s["entrada"]; tp = s["alvo"]; sp = s["stop"]
        hit_tp = (s["direcao"] == "up"   and price >= tp) or                  (s["direcao"] == "down" and price <= tp)
        hit_sl = (s["direcao"] == "up"   and price <= sp) or                  (s["direcao"] == "down" and price >= sp)
        if hit_tp:
            s["status"]    = "win"
            s["resultado"] = f"+{s['rr']}R"
            s["fechamento"] = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
            alterado = True
            send_telegram(
                f"🏆 <b>TAKE PROFIT ATINGIDO!</b>\n"
                f"________________________\n"
                f"Sinal #{s['id']} — {'COMPRA' if s['direcao']=='up' else 'VENDA'}\n"
                f"💰 Entrada:  ${ep:,.2f}\n"
                f"🎯 Alvo:     ${tp:,.2f}\n"
                f"💵 Preco:    ${price:,.2f}\n"
                f"________________________\n"
                f"✅ Resultado: <b>{s['resultado']}</b>\n"
                f"⏰ {s['fechamento']} UTC"
            )
        elif hit_sl:
            s["status"]    = "loss"
            s["resultado"] = "-1R"
            s["fechamento"] = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
            alterado = True
            send_telegram(
                f"🛑 <b>STOP LOSS ATINGIDO</b>\n"
                f"________________________\n"
                f"Sinal #{s['id']} — {'COMPRA' if s['direcao']=='up' else 'VENDA'}\n"
                f"💰 Entrada:  ${ep:,.2f}\n"
                f"🛑 Stop:     ${sp:,.2f}\n"
                f"💵 Preco:    ${price:,.2f}\n"
                f"________________________\n"
                f"❌ Resultado: <b>{s['resultado']}</b>\n"
                f"⏰ {s['fechamento']} UTC"
            )
    if alterado:
        save_memory_to_github()

# ─── COMANDOS ────────────────────────────────────────────────────────────────
def handle_command(text, chat_id):
    cmd = text.strip().lower().split()[0]

    if cmd in ("/help","/commands","/start","/tools","/skill"):
        send_telegram(
            "🤖 <b>Comandos:</b>\n\n"
            "/status    — Preco e tendencia H4\n"
            "/analise   — Analise completa multi-TF\n"
            "/m5        — Analise micro M5 detalhada\n"
            "/m1        — Analise micro M1 detalhada\n"
            "/relatorio — Historico completo de sinais\n"
            "/hoje      — Desempenho do dia\n"
            "/memoria   — O que aprendi\n"
            "/help      — Esta mensagem\n\n"
            "📸 <b>Envie um print do MT5</b> com suas\n"
            "marcacoes para eu aprender!\n\n"
            "📡 Sinais automaticos ativos (Kraken data)",
            chat_id
        )
    elif cmd == "/status":
        try:
            c=get_candles("4h",120); t=get_trend(c); p=c[-1]["close"]
            em="🟢" if t=="up" else ("🔴" if t=="down" else "⚪")
            send_telegram(
                f"📡 <b>Status</b>\n"
                f"BTC: <b>${p:,.2f}</b>\n"
                f"H4: {em} {t.upper()}\n"
                f"🧠 Prints aprendidos: {memory['total_prints']}\n"
                f"⚙️ Tolerancia 50%: {memory['zone_tol']:.0%}\n"
                f"📡 Fonte: Kraken\n"
                f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC",
                chat_id
            )
        except Exception as e:
            send_telegram(f"Erro: {e}", chat_id)
    elif cmd == "/analise":
        try:
            debug, *_ = full_analyze()
            send_telegram(debug, chat_id)
        except Exception as e:
            send_telegram(f"Erro: {e}", chat_id)
    elif cmd == "/memoria":
        total = memory["total_prints"]
        if total == 0:
            send_telegram(
                "🧠 <b>Memoria vazia</b>\n\n"
                "Envie prints do MT5 com marcacoes\n"
                "para comecar o aprendizado!", chat_id)
        else:
            alta  = sum(1 for a in memory["analyses"] if a.get("qualidade_setup")=="alta")
            media = sum(1 for a in memory["analyses"] if a.get("qualidade_setup")=="media")
            baixa = sum(1 for a in memory["analyses"] if a.get("qualidade_setup")=="baixa")
            ups   = sum(1 for a in memory["analyses"] if a.get("tendencia")=="up")
            downs = sum(1 for a in memory["analyses"] if a.get("tendencia")=="down")
            send_telegram(
                f"🧠 <b>Memoria do Bot</b>\n"
                f"________________________\n"
                f"📸 Total prints: <b>{total}</b>\n"
                f"🟢 Alta qualidade: {alta}\n"
                f"🟡 Media qualidade: {media}\n"
                f"🔴 Baixa qualidade: {baixa}\n"
                f"________________________\n"
                f"📈 Setups alta: {ups}\n"
                f"📉 Setups baixa: {downs}\n"
                f"________________________\n"
                f"⚙️ Tolerancia 50%: {memory['zone_tol']:.0%}\n"
                f"⚙️ Onda min M1: ${memory['min_wave_usd']}\n"
                f"📅 Ultimo: {memory['last_update']}", chat_id)
    elif cmd == "/relatorio":
        sinais = memory.get("signals", [])
        if not sinais:
            send_telegram("📊 Nenhum sinal registrado ainda.", chat_id)
        else:
            wins   = [s for s in sinais if s["status"] == "win"]
            losses = [s for s in sinais if s["status"] == "loss"]
            abertos= [s for s in sinais if s["status"] == "aberto"]
            total_f= len(wins) + len(losses)
            wr     = (len(wins)/total_f*100) if total_f > 0 else 0
            r_wins = sum(float(s["rr"]) for s in wins)
            r_loss = len(losses)
            r_net  = r_wins - r_loss
            # Ultimos 5 sinais
            ultimos = sinais[-5:]
            # Historico completo — quebra em blocos de 20 para nao exceder limite Telegram
            linhas = []
            for s in sinais:
                em = "✅" if s["status"]=="win" else ("❌" if s["status"]=="loss" else "⏳")
                dir_lbl = "BUY" if s["direcao"]=="up" else "SELL"
                res = s.get("resultado", "aberto")
                data = s.get("data","")[:10]
                linhas.append(f"{em} #{s['id']} {dir_lbl} ${s['entrada']:,.0f} → {res} {data}")

            resumo = (
                f"📊 <b>Relatorio Completo</b>\n"
                f"________________________\n"
                f"📈 Finalizados: {total_f} | ⏳ Abertos: {len(abertos)}\n"
                f"✅ Wins: {len(wins)}  ❌ Losses: {len(losses)}\n"
                f"🎯 Win Rate: <b>{wr:.0f}%</b>\n"
                f"💰 Resultado total: <b>{r_net:+.1f}R</b>\n"
                f"________________________\n"
                f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC"
            )
            send_telegram(resumo, chat_id)

            # Envia historico em blocos de 20 sinais
            BLOCO = 20
            for i in range(0, len(linhas), BLOCO):
                bloco = linhas[i:i+BLOCO]
                parte = i//BLOCO + 1
                total_partes = (len(linhas)-1)//BLOCO + 1
                msg = f"📋 <b>Historico {parte}/{total_partes}</b>\n" + "\n".join(bloco)
                send_telegram(msg, chat_id)
    elif cmd in ("/m5", "/m1"):
        tf_label = "M5" if cmd == "/m5" else "M1"
        tf_key   = "5m" if cmd == "/m5" else "1m"
        limite   = 100  if cmd == "/m5" else 80
        try:
            c_h4    = get_candles("4h", 60)
            h4_trend= get_trend(c_h4)
            price   = c_h4[-1]["close"]
            c_tf    = get_candles(tf_key, limite)

            # Pivots e onda
            wave = last_wave(c_tf, h4_trend) if h4_trend != "neutral" else None
            in50, ret50 = in_50_zone(c_tf, wave) if wave else (False, 0.0)

            # OTE
            in_ote_r, ote_ret = (False, 0.0)
            if wave:
                start, end, _ = wave
                sw_lo = min(start,end); sw_hi = max(start,end)
                in_ote_r, ote_ret = in_ote_zone(price, sw_lo, sw_hi, h4_trend)

            # Fibonacci da onda
            fib_str = ""
            tp_fib_local = None
            if wave:
                start, end, _ = wave
                sw_lo = min(start,end); sw_hi = max(start,end)
                fib_ret, fib_ext = fibonacci_levels(sw_lo, sw_hi, h4_trend)
                tp_fib_local = fib_ext["1.618"]
                p382 = f"${fib_ret['0.382']:,.0f}"
                p500 = f"${fib_ret['0.500']:,.0f}"
                p618 = f"${fib_ret['0.618']:,.0f}"
                p786 = f"${fib_ret['0.786']:,.0f}"
                ptp  = f"${tp_fib_local:,.0f}"
                fib_str = (
                    f"  38.2% → {p382}\n"
                    f"  50.0% → {p500} {'◀ PRECO' if in50 else ''}\n"
                    f"  61.8% → {p618} {'◀ PRECO' if in_ote_r else ''}\n"
                    f"  78.6% → {p786}\n"
                    f"  TP 1.618 → {ptp}\n"
                )

            # Order Block e FVG no TF
            ob = detect_order_block(c_tf, h4_trend) if h4_trend != "neutral" else None
            fvgs = detect_fvg(c_tf, h4_trend) if h4_trend != "neutral" else []

            # Liquidity sweep
            sweep = detect_liquidity_sweep(c_tf, h4_trend) if h4_trend != "neutral" else False

            # Entry check
            entry_micro = m1_entry(c_tf, h4_trend) if h4_trend != "neutral" else None

            ob_str   = f"${ob[0]:,.0f}–${ob[1]:,.0f}" if ob else "não detectado"
            fvg_str  = (", ".join(f"${f[0]:,.0f}–${f[1]:,.0f}" for f in fvgs[:2])) if fvgs else "não detectado"
            wave_str = (f"${wave[0]:,.0f} → ${wave[1]:,.0f}" if wave else "sem onda clara")
            trend_em = "📈 ALTA" if h4_trend=="up" else ("📉 BAIXA" if h4_trend=="down" else "⚪ NEUTRO")

            zona = "✅ OTE 61.8-78.6%" if in_ote_r else ("✅ Golden 50%" if in50 else "❌ Fora da zona")
            if entry_micro:
                ep_str  = "${:,.2f}".format(entry_micro["entry"])
                sp_str  = "${:,.2f}".format(entry_micro["stop"])
                ote_str = "OTE" if entry_micro.get("in_ote") else "50%"
                ret_str = "{:.1%}".format(entry_micro["retrace_pct"])
                entrada_str = "💰 {}  Stop: {}\n   Retracao: {}  {}".format(ep_str, sp_str, ret_str, ote_str)
            else:
                entrada_str = "❌ Sem setup confirmado"

            pr_str   = "${:,.2f}".format(price)
            msg_micro = (
                "🔬 <b>Analise Micro {}</b>\n".format(tf_label) +
                "💰 <b>{}</b>  |  H4: {}\n".format(pr_str, trend_em) +
                "________________________\n" +
                "🌊 Onda {}: {}\n".format(tf_label, wave_str) +
                "📍 Zona atual: {}  ({:.1%})\n".format(zona, ret50) +
                "________________________\n" +
                "<b>Fibonacci:</b>\n" + fib_str +
                "________________________\n" +
                "🏦 Order Block: {}\n".format(ob_str) +
                "🕳  FVG: {}\n".format(fvg_str) +
                "💧 Liquidity Sweep: {}\n".format("✅ SIM" if sweep else "❌ nao") +
                "________________________\n" +
                "<b>Setup entrada:</b>\n" + entrada_str + "\n" +
                "________________________\n" +
                "⏰ {} UTC".format(datetime.utcnow().strftime("%d/%m %H:%M"))
            )
            send_telegram(msg_micro, chat_id)
        except Exception as e:
            send_telegram(f"❌ Erro analise {tf_label}: {e}", chat_id)

    elif cmd == "/hoje":
        sinais = memory.get("signals", [])
        hoje = datetime.utcnow().strftime("%d/%m/%Y")
        hoje_sinais = [s for s in sinais if s.get("data","").startswith(hoje)]
        if not hoje_sinais:
            send_telegram(f"📅 Nenhum sinal hoje ({hoje}).", chat_id)
        else:
            wins_h   = [s for s in hoje_sinais if s["status"] == "win"]
            losses_h = [s for s in hoje_sinais if s["status"] == "loss"]
            abertos_h= [s for s in hoje_sinais if s["status"] == "aberto"]
            total_fh = len(wins_h) + len(losses_h)
            wr_h     = (len(wins_h)/total_fh*100) if total_fh > 0 else 0
            r_wins_h = sum(float(s["rr"]) for s in wins_h)
            r_net_h  = r_wins_h - len(losses_h)
            hist_h   = ""
            for s in hoje_sinais:
                em = "✅" if s["status"]=="win" else ("❌" if s["status"]=="loss" else "⏳")
                dir_lbl = "BUY" if s["direcao"]=="up" else "SELL"
                res = s.get("resultado", "aberto")
                hora = s.get("data","")[-5:]
                hist_h += f"{em} #{s['id']} {dir_lbl} ${s['entrada']:,.0f} → {res} {hora}\n"
            send_telegram(
                f"📅 <b>Desempenho de Hoje</b> ({hoje})\n"
                f"________________________\n"
                f"📊 Sinais: {len(hoje_sinais)} | ⏳ Abertos: {len(abertos_h)}\n"
                f"✅ Wins: {len(wins_h)}  ❌ Losses: {len(losses_h)}\n"
                f"🎯 Win Rate: <b>{wr_h:.0f}%</b>\n"
                f"💰 Resultado: <b>{r_net_h:+.1f}R</b>\n"
                f"________________________\n"
                f"{hist_h}"
                f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC",
                chat_id
            )
    else:
        send_telegram("Comando nao reconhecido. Use /help", chat_id)

# ─── LOOP COMANDOS + FOTOS ───────────────────────────────────────────────────
def commands_loop():
    print("Ouvindo comandos...")
    while True:
        try:
            for upd in get_updates():
                msg  = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                cid  = str(msg["chat"]["id"])
                text = msg.get("text","")
                if text.startswith("/"):
                    print(f"[CMD] {text}")
                    handle_command(text, cid)
                elif msg.get("photo"):
                    print(f"[FOTO] Print de {cid}")
                    photo   = msg["photo"][-1]
                    caption = msg.get("caption","")
                    try:
                        img = download_telegram_photo(photo["file_id"])
                        threading.Thread(
                            target=process_chart_image,
                            args=(img, cid, caption), daemon=True
                        ).start()
                    except Exception as e:
                        send_telegram(f"Erro ao baixar foto: {e}", cid)
        except Exception as e:
            print(f"Erro commands_loop: {e}")
        time.sleep(2)

# ─── LOOP PRINCIPAL ──────────────────────────────────────────────────────────
_loop_n      = 0
STATUS_EVERY = max(1, int(4*3600/CHECK_INTERVAL))

def main_loop():
    global _loop_n
    while True:
        try:
            _loop_n += 1
            if _loop_n % STATUS_EVERY == 0:
                c=get_candles("4h",120); t=get_trend(c); p=c[-1]["close"]
                em="🟢" if t=="up" else ("🔴" if t=="down" else "⚪")
                send_telegram(f"📡 BTC <b>${p:,.2f}</b> | H4:{em}{t.upper()}\n"
                              f"🧠 {memory['total_prints']} prints\n"
                              f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC")

            debug, entry, h4_trend, price, h1_wave, h1_in50, h1_ret, \
            m15_in50, m15_ret, m5_in50, m5_ret, \
            smc_score, smc_detalhes, ob_zone, fvgs, tp_fib = full_analyze()

            # Verificar sinais abertos
            check_open_signals(price)

            print(f"[{datetime.utcnow().strftime('%H:%M')}] "
                  f"${price:,.0f} H4:{h4_trend} "
                  f"H1:{h1_in50} M15:{m15_in50} M5:{m5_in50} "
                  f"M1:{'SINAL' if entry else 'wait'} tol:{memory['zone_tol']:.0%}")

            if entry and h4_trend != "neutral":
                now_ts = time.time()
                if now_ts - last_signal_time.get(h4_trend, 0) >= SIGNAL_COOLDOWN:
                    fire_signal(entry, h4_trend, h1_wave, h1_in50, h1_ret,
                                m15_in50, m15_ret, m5_in50, m5_ret)
                    last_signal_time[h4_trend] = now_ts
                else:
                    print("  [cooldown]")

        except Exception as e:
            print(f"[ERRO] {e}")
            import traceback; traceback.print_exc()
        time.sleep(CHECK_INTERVAL)

# ─── START ───────────────────────────────────────────────────────────────────
print("Bot iniciado (Kraken)...")
threading.Thread(target=run_server,    daemon=True).start()
load_memory_from_github()
threading.Thread(target=commands_loop, daemon=True).start()

send_telegram(
    "🤖 <b>Bot Multi-TF BTC iniciado!</b>\n\n"
    "📡 Fonte de dados: <b>Kraken</b>\n"
    "📋 /status /analise /memoria /help\n"
    "📸 Envie prints do MT5 para aprender\n\n"
    f"🧠 Prints na memoria: {memory['total_prints']}\n"
    f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"
)

main_loop()
