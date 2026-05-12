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
GEMINI_KEY       = os.environ.get("GEMINI_KEY", "")
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
    "zone_tol":     0.08,
    "min_wave_usd": 30,
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

# ─── GEMINI VISION (gratuito) ────────────────────────────────────────────────
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
    if not GEMINI_KEY:
        raise Exception("GEMINI_KEY nao configurada no Render!")

    img_b64 = base64.b64encode(image_bytes).decode()
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}",
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{
                "parts": [
                    {"text": VISION_PROMPT},
                    {"inline_data": {
                        "mime_type": "image/jpeg",
                        "data": img_b64
                    }}
                ]
            }],
            "generationConfig": {"temperature": 0.1}
        },
        timeout=30
    )
    r.raise_for_status()
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
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
    if len(highs) < 3 or len(lows) < 3: return "neutral"
    rh=highs[-3:]; rl=lows[-3:]
    hh=rh[2][1]>rh[1][1]>rh[0][1]; hl=rl[2][1]>rl[1][1]>rl[0][1]
    lh=rh[2][1]<rh[1][1]<rh[0][1]; ll=rl[2][1]<rl[1][1]<rl[0][1]
    if hh and hl: return "up"
    if lh and ll: return "down"
    if hh or hl:  return "up"
    if lh or ll:  return "down"
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

def m1_entry(candles_m1, direction):
    wave = last_wave(candles_m1, direction, n=SWING_M1)
    if wave is None: return None
    start, end, _ = wave
    size = abs(end - start)
    if size < memory["min_wave_usd"]: return None
    in_zone, retrace = in_50_zone(candles_m1, wave)
    if not in_zone: return None
    cur = candles_m1[-1]["close"]
    if direction == "up"   and cur < start: return None
    if direction == "down" and cur > start: return None
    return {"entry": cur, "stop": start, "wave_start": start,
            "wave_end": end, "wave_size": size, "retrace_pct": retrace}

def full_analyze():
    c_h4 = get_candles("4h", 120); h4_trend = get_trend(c_h4); price = c_h4[-1]["close"]
    c_h1 = get_candles("1h", 120); h1_wave  = last_wave(c_h1, h4_trend)
    h1_in50,  h1_ret  = in_50_zone(c_h1,  h1_wave)
    c_m15= get_candles("15m",120); m15_wave = last_wave(c_m15, h4_trend)
    m15_in50, m15_ret = in_50_zone(c_m15, m15_wave)
    c_m5 = get_candles("5m", 100); m5_wave  = last_wave(c_m5, h4_trend)
    m5_in50,  m5_ret  = in_50_zone(c_m5,  m5_wave)
    c_m1 = get_candles("1m",  80)
    entry = m1_entry(c_m1, h4_trend) if h4_trend != "neutral" else None

    if h4_trend == "neutral":               bloqueio = "H4 sem tendencia"
    elif not(h1_in50 or m15_in50 or m5_in50): bloqueio = "TFs aguardando correcao 50%"
    elif entry is None:                     bloqueio = "M1 aguardando onda + 50%"
    else:                                   bloqueio = ""

    debug = (
        f"📊 <b>Analise BTCUSDT</b>\n"
        f"💰 <b>${price:,.2f}</b>\n"
        f"________________________\n"
        f"H4  → {'ALTA 📈' if h4_trend=='up' else 'BAIXA 📉' if h4_trend=='down' else 'NEUTRO ⚪'}\n"
        f"H1  → {h1_ret:.0%} {'✅' if h1_in50 else '❌'}\n"
        f"M15 → {m15_ret:.0%} {'✅' if m15_in50 else '❌'}\n"
        f"M5  → {m5_ret:.0%} {'✅' if m5_in50 else '❌'}\n"
        f"M1  → {'✅ Setup pronto!' if entry else '❌ Aguardando'}\n"
        f"________________________\n"
        f"🧠 Prints: {memory['total_prints']} | Tol: {memory['zone_tol']:.0%}\n"
        + (f"⏳ <i>{bloqueio}</i>" if bloqueio else "🚀 <b>SINAL PRONTO!</b>")
    )
    return (debug, entry, h4_trend, price,
            h1_wave, h1_in50, h1_ret,
            m15_in50, m15_ret, m5_in50, m5_ret)

def fire_signal(entry, h4_trend, h1_wave, h1_in50, h1_ret,
                m15_in50, m15_ret, m5_in50, m5_ret):
    ep=entry["entry"]; sp=entry["stop"]; risk=abs(ep-sp)
    if h4_trend=="up":
        emoji="✅"; action="COMPRA BUY"; direcao="ALTA 📈"
        tp=ep+entry["wave_size"]; sl_lbl="Fundo mini-onda M1"
    else:
        emoji="🔴"; action="VENDA SELL"; direcao="BAIXA 📉"
        tp=ep-entry["wave_size"]; sl_lbl="Topo mini-onda M1"
    rr    = round(abs(tp-ep)/risk,1) if risk>0 else 0
    h1_ws = (f"${h1_wave[0]:,.0f}→${h1_wave[1]:,.0f}" if h1_wave else "—")
    m1_ws = f"${entry['wave_start']:,.0f}→${entry['wave_end']:,.0f}"
    note  = (f"\n🧠 <i>Calibrado com {memory['total_prints']} prints</i>"
             if memory["total_prints"] > 0 else "")
    send_telegram(
        f"{emoji} <b>SINAL {action}</b> — BTCUSDT\n"
        f"________________________\n"
        f"💰 Entrada: <b>${ep:,.2f}</b>\n"
        f"🛑 Stop:    <b>${sp:,.2f}</b> ({sl_lbl})\n"
        f"🎯 Alvo:    <b>${tp:,.2f}</b>\n"
        f"📐 Risco: ${risk:,.2f}  |  R:R ≈ 1:{rr}\n"
        f"________________________\n"
        f"H4 → {direcao}\n"
        f"H1  → {h1_ws} | {h1_ret:.0%} {'✅' if h1_in50 else '—'}\n"
        f"M15 → {m15_ret:.0%} {'✅' if m15_in50 else '—'}\n"
        f"M5  → {m5_ret:.0%} {'✅' if m5_in50 else '—'}\n"
        f"M1  → {m1_ws} | {entry['retrace_pct']:.0%} ✅\n"
        f"     → <b>ENTRADA LIBERADA</b>{note}\n"
        f"________________________\n"
        f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"⚠️ <i>Gerencie sempre o risco!</i>"
    )

# ─── COMANDOS ────────────────────────────────────────────────────────────────
def handle_command(text, chat_id):
    cmd = text.strip().lower().split()[0]

    if cmd in ("/help","/commands","/start","/tools","/skill"):
        send_telegram(
            "🤖 <b>Comandos:</b>\n\n"
            "/status  — Preco e tendencia H4\n"
            "/analise — Analise completa\n"
            "/memoria — O que aprendi\n"
            "/help    — Esta mensagem\n\n"
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
            m15_in50, m15_ret, m5_in50, m5_ret = full_analyze()

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
