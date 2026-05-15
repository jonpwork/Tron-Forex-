"""
╔══════════════════════════════════════════════════════════════╗
║  TRON FOREX BOT — Elliott Wave + SMC + Fibonacci + Multi-TF ║
║                                                              ║
║  Elliott Wave em TODOS os TFs (H4, H1, M15, M5, M1):       ║
║    • Detecta ondas 1-2-3-4-5 e correc. ABC                  ║
║    • Melhor entrada: Onda 2 (50-61.8%) e Onda 4 (38-50%)    ║
║    • Alvo: Onda 3 (161.8%) e Onda 5 (= Onda 1)             ║
║  SMC: Order Block, FVG, Liquidity Sweep, BOS/CHoCH          ║
║  Fibonacci OTE 61.8–79% + retracoes 38.2/50/61.8            ║
║  Score de confluencia 0-10                                   ║
╚══════════════════════════════════════════════════════════════╝
"""
import requests, time, os, json, base64, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ══════════ CONFIG ══════════
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID         = os.environ.get("CHAT_ID", "")
GROQ_KEY        = os.environ.get("GROQ_API_KEY", "")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "")
GITHUB_FILE     = "memory.json"
SYMBOL          = "BTCUSDT"
CHECK_INTERVAL  = 60
SIGNAL_COOLDOWN = 1800
PORT            = int(os.environ.get("PORT", 8080))
MIN_SCORE       = 4      # score minimo para disparar sinal (0-10)
SWING_LARGE     = 5      # pivots TFs maiores
SWING_SMALL     = 3      # pivots M1
# ════════════════════════════

last_signal_time    = {}   # cooldown sinal multi-TF (chave: direcao)
last_ew_signal_time = {}   # cooldown sinal Elliott por TF (chave: "tf_direcao")
last_update_id      = 0
memory = {
    "analyses":[], "signals":[],
    "zone_tol":0.08, "min_wave_usd":30,
    "total_prints":0, "last_update":""
}

# Cooldowns separados por tipo de sinal
EW_COOLDOWN  = 3600   # sinal Elliott puro: 1h entre sinais do mesmo TF+direcao
MTF_COOLDOWN = 1800   # sinal multi-TF confluente: 30min

# ── HTTP keep-alive ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"TRON FOREX Bot OK")
    def log_message(self, *a): pass
def run_server(): HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ── TELEGRAM ────────────────────────────────────────────────
def send_telegram(msg, chat_id=None):
    cid = chat_id or CHAT_ID
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id":cid,"text":msg,"parse_mode":"HTML"}, timeout=10)
        print(f"[TG] {msg[:80].strip()}")
    except Exception as e: print(f"TG err:{e}")

def get_updates():
    global last_update_id
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset":last_update_id+1,"timeout":2}, timeout=8)
        u = r.json().get("result",[])
        if u: last_update_id = u[-1]["update_id"]
        return u
    except: return []

def download_photo(file_id):
    r  = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                      params={"file_id":file_id}, timeout=10)
    fp = r.json()["result"]["file_path"]
    return requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fp}", timeout=20).content

# ── KRAKEN DATA ──────────────────────────────────────────────
TF_MAP = {"1m":1,"5m":5,"15m":15,"1h":60,"4h":240}
KRAKEN_PAIR = "XBTUSDT"

def get_candles(tf, limit=150):
    r = requests.get("https://api.kraken.com/0/public/OHLC",
        params={"pair":KRAKEN_PAIR,"interval":TF_MAP.get(tf,60)}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("error"): raise Exception(f"Kraken:{d['error']}")
    key  = [k for k in d["result"] if k!="last"][0]
    rows = d["result"][key][-limit:]
    return [{"open":float(k[1]),"high":float(k[2]),
              "low":float(k[3]),"close":float(k[4])} for k in rows]

# ── GITHUB MEMORIA ───────────────────────────────────────────
def gh_h():
    return {"Authorization":f"token {GITHUB_TOKEN}",
            "Accept":"application/vnd.github.v3+json"}

def load_memory():
    global memory
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = requests.get(url, headers=gh_h(), timeout=10)
        if r.status_code == 200:
            memory = json.loads(base64.b64decode(r.json()["content"]).decode())
            print(f"[MEM] {memory['total_prints']} prints")
    except Exception as e: print(f"[MEM load] {e}")

def save_memory():
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        cnt = base64.b64encode(
            json.dumps(memory,indent=2,ensure_ascii=False).encode()).decode()
        r   = requests.get(url, headers=gh_h(), timeout=10)
        pl  = {"message":f"mem:{memory['total_prints']}","content":cnt}
        if r.status_code==200: pl["sha"]=r.json()["sha"]
        requests.put(url, headers=gh_h(), json=pl, timeout=15)
    except Exception as e: print(f"[MEM save] {e}")

# ══════════════════════════════════════════════════════════════
#   MOTOR ELLIOTT WAVE — DETECTA ONDAS 1-2-3-4-5 e ABC
# ══════════════════════════════════════════════════════════════

def find_pivots(c, n):
    """Encontra pivot highs e lows com janela n."""
    H, L = [], []
    for i in range(n, len(c)-n):
        wh = [c[j]["high"] for j in range(i-n, i+n+1)]
        wl = [c[j]["low"]  for j in range(i-n, i+n+1)]
        if c[i]["high"] == max(wh): H.append((i, c[i]["high"]))
        if c[i]["low"]  == min(wl): L.append((i, c[i]["low"]))
    return H, L

def fib_ret(start, end, level):
    """Nivel de retracement Fibonacci."""
    return end - (end - start) * level

def fib_ext(start, end, level):
    """Nivel de extensao Fibonacci."""
    return end + abs(end - start) * level

class ElliottResult:
    """Resultado da analise de Elliott Wave num timeframe."""
    def __init__(self):
        self.wave_position = None   # "2","4","3","5","C","entre_1_2" etc
        self.wave_label    = "—"
        self.entry_valid   = False  # entrada valida neste TF
        self.retrace_pct   = 0.0
        self.stop_level    = 0.0
        self.tp1           = 0.0    # alvo 1 (100% da onda anterior)
        self.tp2           = 0.0    # alvo 2 (161.8%)
        self.score_contrib = 0      # quanto contribui para o score
        self.emoji         = "⚪"
        self.detail        = ""

def detect_elliott(c, direction, n=SWING_LARGE):
    """
    Detecta a posicao atual na sequencia de Elliott Wave.

    Regras de Elliott (impulsiva bullish):
      Wave 1 : primeiro impulso de alta a partir do fundo
      Wave 2 : retracao de W1 — tipicamente 50-61.8% (nunca > 100%)
      Wave 3 : maior impulso — 161.8% ou mais de W1
      Wave 4 : retracao de W3 — tipicamente 38.2-50% (nao overlap W1)
      Wave 5 : ultimo impulso — aprox. igual a W1

    Corrective ABC (pos impulso):
      A : impulso contrario
      B : retracao de A
      C : impulso contrario, geralmente = A

    Retorna ElliottResult
    """
    res = ElliottResult()
    H, L = find_pivots(c, n)
    if len(H) < 2 or len(L) < 2:
        res.detail = "pivots insuficientes"
        return res

    price = c[-1]["close"]

    if direction == "up":
        # Precisamos de pelo menos: L1(fundo) H1(topo1) L2(fundo2) H2(topo2)
        if len(L) < 2 or len(H) < 2:
            return res

        # Candidatos a W1, W2, W3, W4
        L1 = L[-2]; H1 = H[-2]; L2 = L[-1]; H2 = H[-1]

        w1_start = L1[1]; w1_end = H1[1]
        w1_size  = w1_end - w1_start
        if w1_size <= 0: return res

        w2_low   = L2[1]
        w2_ret   = (w1_end - w2_low) / w1_size  # retracao de W2 sobre W1

        # ─── POSICAO ONDA 2 (correcao apos W1) ───────────────────────────────
        # Preco atual perto do fundo de W2 (zona de entrada para W3)
        if 0.382 <= w2_ret <= 0.786:
            at_w2_zone = abs(price - w2_low) / w1_size <= 0.08
            if at_w2_zone or (w2_low <= price <= w1_end * 0.98):
                res.wave_position = "2"
                res.retrace_pct   = round(w2_ret, 3)
                res.stop_level    = w1_start           # stop abaixo de W1
                res.tp1           = w1_end + w1_size * 1.0    # 100% de W1
                res.tp2           = w1_end + w1_size * 1.618  # extensao W3
                res.entry_valid   = True
                res.score_contrib = 3  # Onda 2 e a melhor entrada!
                res.emoji         = "🌊"
                res.wave_label    = f"Onda 2 ({w2_ret:.0%} retrac)"
                res.detail        = f"W1=${w1_start:,.0f}→${w1_end:,.0f} | W2 retrac {w2_ret:.0%}"
                return res

        # ─── POSICAO ONDA 4 (correcao apos W3) ───────────────────────────────
        if len(H) >= 2 and len(L) >= 2:
            # W3 do ultimo topo ao segundo ultimo fundo
            w3_end   = H2[1]
            w3_start = H1[1]  # W3 comeca no fim de W2 (aprox topo anterior)
            w3_size  = w3_end - w3_start
            if w3_size > 0 and w3_end > w1_end:  # W3 deve ser maior que W1
                w4_ret = (w3_end - price) / w3_size
                # W4 nao pode overlap W1 (preco nao vai abaixo do topo de W1)
                if 0.236 <= w4_ret <= 0.618 and price > w1_end:
                    res.wave_position = "4"
                    res.retrace_pct   = round(w4_ret, 3)
                    res.stop_level    = w1_end              # stop no topo de W1
                    res.tp1           = w3_end              # retorna ao topo W3
                    res.tp2           = w3_end + w1_size    # extensao W5 = W1
                    res.entry_valid   = True
                    res.score_contrib = 2  # Onda 4 e boa entrada
                    res.emoji         = "🌊"
                    res.wave_label    = f"Onda 4 ({w4_ret:.0%} retrac)"
                    res.detail        = f"W3=${w3_start:,.0f}→${w3_end:,.0f} | W4 retrac {w4_ret:.0%}"
                    return res

        # ─── ONDA 3 EM DESENVOLVIMENTO ────────────────────────────────────────
        if price > w1_end:
            rally = (price - w2_low) / w1_size
            res.wave_position = "3"
            res.emoji         = "🚀"
            res.wave_label    = f"Onda 3 em desenvolvimento ({rally:.0%} de W1)"
            res.score_contrib = 1
            res.detail        = f"Impulso atual = {rally:.0%} de W1"
            return res

        # ─── ONDA 1 EM DESENVOLVIMENTO ────────────────────────────────────────
        if price > w1_start and price <= w1_end:
            p = (price - w1_start) / w1_size
            res.wave_position = "1"
            res.emoji         = "1️⃣"
            res.wave_label    = f"Onda 1 ({p:.0%})"
            res.detail        = "Primeiro impulso — aguardar W2 para entrar"
            return res

    else:  # direction == "down" — espelho bearish
        if len(H) < 2 or len(L) < 2: return res
        H1=H[-2]; L1=L[-2]; H2=H[-1]; L2=L[-1]
        w1_start=H1[1]; w1_end=L1[1]; w1_size=w1_start-w1_end
        if w1_size <= 0: return res
        w2_high = H2[1]
        w2_ret  = (w2_high - w1_end) / w1_size

        if 0.382 <= w2_ret <= 0.786:
            at_w2 = abs(price - w2_high) / w1_size <= 0.08
            if at_w2 or (w1_end * 1.02 <= price <= w2_high):
                res.wave_position = "2"
                res.retrace_pct   = round(w2_ret, 3)
                res.stop_level    = w1_start
                res.tp1           = w1_end - w1_size * 1.0
                res.tp2           = w1_end - w1_size * 1.618
                res.entry_valid   = True
                res.score_contrib = 3
                res.emoji         = "🌊"
                res.wave_label    = f"Onda 2 ({w2_ret:.0%} retrac)"
                res.detail        = f"W1=${w1_start:,.0f}→${w1_end:,.0f} | W2 retrac {w2_ret:.0%}"
                return res

        if len(L) >= 2:
            w3_end = L2[1]; w3_start = L1[1]; w3_size = w3_start - w3_end
            if w3_size > 0 and w3_end < w1_end:
                w4_ret = (price - w3_end) / w3_size
                if 0.236 <= w4_ret <= 0.618 and price < w1_end:
                    res.wave_position = "4"
                    res.retrace_pct   = round(w4_ret, 3)
                    res.stop_level    = w1_end
                    res.tp1           = w3_end
                    res.tp2           = w3_end - w1_size
                    res.entry_valid   = True
                    res.score_contrib = 2
                    res.emoji         = "🌊"
                    res.wave_label    = f"Onda 4 ({w4_ret:.0%} retrac)"
                    res.detail        = f"W3=${w3_start:,.0f}→${w3_end:,.0f} | W4 retrac {w4_ret:.0%}"
                    return res

        if price < w1_end:
            rally = (w2_high - price) / w1_size
            res.wave_position = "3"
            res.emoji         = "🚀"
            res.wave_label    = f"Onda 3 em desenvolvimento ({rally:.0%})"
            res.score_contrib = 1
            return res

    return res

# ══════════════════════════════════════════════════════════════
#   SMC — Order Block, FVG, Liquidity Sweep, BOS
# ══════════════════════════════════════════════════════════════

def detect_order_block(c, direction, n=3):
    price = c[-1]["close"]
    H, L  = find_pivots(c, n)
    if not H or not L: return None
    if direction == "up":
        if not L: return None
        base_idx = L[-1][0]
        for i in range(base_idx, max(0, base_idx-15), -1):
            if c[i]["close"] < c[i]["open"]:
                ob_h=c[i]["high"]; ob_l=c[i]["low"]
                if ob_l <= price <= ob_h * 1.005:
                    return {"high":ob_h,"low":ob_l,"idx":i}
    else:
        if not H: return None
        base_idx = H[-1][0]
        for i in range(base_idx, max(0, base_idx-15), -1):
            if c[i]["close"] > c[i]["open"]:
                ob_h=c[i]["high"]; ob_l=c[i]["low"]
                if ob_l*0.995 <= price <= ob_h:
                    return {"high":ob_h,"low":ob_l,"idx":i}
    return None

def detect_fvg(c, direction, lookback=20):
    price = c[-1]["close"]
    for i in range(len(c)-3, max(0, len(c)-3-lookback), -1):
        if direction == "up":
            if c[i+2]["low"] > c[i]["high"]:
                bot=c[i]["high"]; top=c[i+2]["low"]
                if bot <= price <= top*1.002:
                    return {"top":top,"bot":bot,"idx":i}
        else:
            if c[i+2]["high"] < c[i]["low"]:
                bot=c[i+2]["high"]; top=c[i]["low"]
                if bot*0.998 <= price <= top:
                    return {"top":top,"bot":bot,"idx":i}
    return None

def detect_sweep(c, direction, lookback=15):
    if len(c) < lookback+3: return False
    n = len(c)
    recent = c[n-4:n-1]
    for candle in recent:
        if direction == "up":
            prior_lows=[c[i]["low"] for i in range(n-lookback-3, n-4)]
            if prior_lows and candle["low"]<min(prior_lows) and candle["close"]>min(prior_lows):
                return True
        else:
            prior_highs=[c[i]["high"] for i in range(n-lookback-3, n-4)]
            if prior_highs and candle["high"]>max(prior_highs) and candle["close"]<max(prior_highs):
                return True
    return False

def detect_bos(c, direction, n=SWING_LARGE):
    H,L = find_pivots(c,n)
    if not H or not L: return None
    price = c[-1]["close"]
    if direction=="up":
        return "BOS" if price>H[-1][1] else ("CHoCH" if price<L[-1][1] else None)
    return "BOS" if price<L[-1][1] else ("CHoCH" if price>H[-1][1] else None)

# ══════════════════════════════════════════════════════════════
#   FIBONACCI — OTE + zonas clássicas
# ══════════════════════════════════════════════════════════════
FIB_ZONES = [
    (0.382, 0.06, "38.2%"),
    (0.500, 0.06, "50%"),
    (0.618, 0.06, "61.8%"),
    (0.705, 0.05, "70.5% OTE"),
    (0.786, 0.05, "78.6% OTE"),
]

def fib_zone_hit(price, wave):
    if not wave: return False, "—"
    start,end,_ = wave
    size = abs(end-start)
    if size==0: return False,"—"
    ret = abs(price-end)/size
    for lvl,tol,lbl in FIB_ZONES:
        if abs(ret-lvl)<=tol:
            return True, lbl
    return False, f"{ret:.0%}"

def last_wave(c, direction, n=SWING_LARGE):
    H,L = find_pivots(c,n)
    if not H or not L: return None
    if direction=="up":
        base=L[-1]; tops=[(i,p) for i,p in H if i>base[0]]
        if not tops: return None
        pk=max(tops,key=lambda x:x[1]); return (base[1],pk[1],pk[0])
    base=H[-1]; ts=[(i,p) for i,p in L if i>base[0]]
    if not ts: return None
    t=min(ts,key=lambda x:x[1]); return (base[1],t[1],t[0])

# ══════════════════════════════════════════════════════════════
#   TENDÊNCIA
# ══════════════════════════════════════════════════════════════
def get_trend(c, n=SWING_LARGE):
    H,L=find_pivots(c,n)
    if len(H)<3 or len(L)<3: return "neutral"
    rh=H[-3:]; rl=L[-3:]
    hh=rh[2][1]>rh[1][1]>rh[0][1]; hl=rl[2][1]>rl[1][1]>rl[0][1]
    lh=rh[2][1]<rh[1][1]<rh[0][1]; ll=rl[2][1]<rl[1][1]<rl[0][1]
    if hh and hl: return "up"
    if lh and ll: return "down"
    if hh or hl:  return "up"
    if lh or ll:  return "down"
    return "neutral"

# ══════════════════════════════════════════════════════════════
#   ENTRADA NO M1 — mini-onda + 50%
# ══════════════════════════════════════════════════════════════
def m1_entry(c, direction):
    wave = last_wave(c, direction, n=SWING_SMALL)
    if not wave: return None
    start,end,_ = wave
    size = abs(end-start)
    if size < memory["min_wave_usd"]: return None
    price = c[-1]["close"]
    fifty = (start+end)/2
    if abs(price-fifty)/size > memory["zone_tol"]: return None
    if direction=="up"   and price < start: return None
    if direction=="down" and price > start: return None
    return {"entry":price,"stop":start,"wave_start":start,
            "wave_end":end,"wave_size":size,"retrace_pct":round(abs(price-end)/size,3)}

# ══════════════════════════════════════════════════════════════
#   SCORE DE CONFLUENCIA — TODOS OS FATORES
# ══════════════════════════════════════════════════════════════
def score_all(c_h4, c_h1, c_m15, c_m5, c_m1, h4_trend):
    """
    Calcula score de confluencia (0-10) somando:
      +1  H4 tendencia definida
      +3  Elliott Onda 2 (melhor entrada)
      +2  Elliott Onda 4
      +1  Elliott Onda 2 ou 4 em H1
      +1  Elliott em M15 ou M5
      +2  M1 mini-onda 50% (gatilho)
      +1  Fibonacci OTE (61.8-79%)
      +1  Order Block
      +1  Fair Value Gap
      +1  Liquidity Sweep
      +1  BOS confirmado
    """
    price  = c_m1[-1]["close"]
    sc     = 0
    det    = {}

    # ── H4 tendencia ─────────────────────────────────────────
    det["h4_trend"] = h4_trend != "neutral"
    if det["h4_trend"]: sc += 1

    # ── Elliott em cada TF ───────────────────────────────────
    ew = {}
    for label, candles, n in [
        ("h4",  c_h4,  SWING_LARGE),
        ("h1",  c_h1,  SWING_LARGE),
        ("m15", c_m15, SWING_LARGE),
        ("m5",  c_m5,  SWING_LARGE),
        ("m1",  c_m1,  SWING_SMALL),
    ]:
        ew[label] = detect_elliott(candles, h4_trend, n=n)

    det["ew"] = ew

    # H4 Elliott — contexto macro
    if ew["h4"].wave_position in ("2","4"):
        sc += ew["h4"].score_contrib  # +3 se onda 2, +2 se onda 4
        det["ew_h4_signal"] = True
    else:
        det["ew_h4_signal"] = False

    # H1 Elliott — confirmacao
    if ew["h1"].wave_position in ("2","4"):
        sc += 1
        det["ew_h1_signal"] = True
    else:
        det["ew_h1_signal"] = False

    # M15 ou M5 Elliott — refinamento
    m15_ew_ok = ew["m15"].wave_position in ("2","4")
    m5_ew_ok  = ew["m5"].wave_position  in ("2","4")
    det["ew_m15_signal"] = m15_ew_ok
    det["ew_m5_signal"]  = m5_ew_ok
    if m15_ew_ok or m5_ew_ok: sc += 1

    # M1 Elliott
    det["ew_m1_signal"] = ew["m1"].wave_position in ("2","4")

    # ── M1 mini-onda + 50% (gatilho central) ─────────────────
    m1_e = m1_entry(c_m1, h4_trend)
    det["m1_entry"] = m1_e
    if m1_e: sc += 2

    # ── Fibonacci OTE 61.8–79% ───────────────────────────────
    ote_hit = False; ote_lbl = "—"
    for c_tf, n in [(c_m5,SWING_LARGE),(c_m15,SWING_LARGE)]:
        wv = last_wave(c_tf, h4_trend, n=n)
        ok, lbl = fib_zone_hit(price, wv)
        if ok: ote_hit=True; ote_lbl=lbl; break
    det["ote"] = ote_hit; det["ote_lbl"] = ote_lbl
    if ote_hit: sc += 1

    # ── SMC: Order Block ─────────────────────────────────────
    ob = detect_order_block(c_m5, h4_trend) or detect_order_block(c_m15, h4_trend, n=4)
    det["ob"] = ob is not None
    if ob: sc += 1

    # ── SMC: Fair Value Gap ──────────────────────────────────
    fvg = detect_fvg(c_m5, h4_trend) or detect_fvg(c_m1, h4_trend, lookback=10)
    det["fvg"] = fvg is not None
    if fvg: sc += 1

    # ── SMC: Liquidity Sweep ─────────────────────────────────
    sweep = detect_sweep(c_m15, h4_trend) or detect_sweep(c_m5, h4_trend)
    det["sweep"] = sweep
    if sweep: sc += 1

    # ── SMC: BOS ─────────────────────────────────────────────
    bos = detect_bos(c_h1, h4_trend) or detect_bos(c_m15, h4_trend)
    det["bos"] = bos == "BOS"
    if det["bos"]: sc += 1

    return min(sc, 10), det, m1_e

# ══════════════════════════════════════════════════════════════
#   ANALISE COMPLETA
# ══════════════════════════════════════════════════════════════
def full_analyze():
    c_h4  = get_candles("4h", 150)
    c_h1  = get_candles("1h", 150)
    c_m15 = get_candles("15m",150)
    c_m5  = get_candles("5m", 100)
    c_m1  = get_candles("1m",  80)

    h4_trend = get_trend(c_h4)
    price    = c_h4[-1]["close"]

    sc, det, m1_e = score_all(c_h4, c_h1, c_m15, c_m5, c_m1, h4_trend)

    ew = det["ew"]
    barra = "█"*sc + "░"*(10-sc)

    def ew_line(tf_key, label):
        e = ew[tf_key]
        if e.wave_position:
            return f"{label} → {e.emoji} {e.wave_label}\n"
        return f"{label} → ⚪ sem onda clara\n"

    debug = (
        f"📊 <b>Analise BTCUSDT</b>\n"
        f"💰 <b>${price:,.2f}</b>\n"
        f"________________________\n"
        f"🎯 Score: <b>{sc}/10</b> [{barra}]\n"
        f"________________________\n"
        f"📊 <b>Elliott Wave por TF:</b>\n"
        + ew_line("h4","H4 ")
        + ew_line("h1","H1 ")
        + ew_line("m15","M15")
        + ew_line("m5","M5 ")
        + ew_line("m1","M1 ")
        + f"________________________\n"
        f"<b>SMC + Fibonacci:</b>\n"
        f"OB:     {'✅' if det['ob'] else '❌'}\n"
        f"FVG:    {'✅' if det['fvg'] else '❌'}\n"
        f"Sweep:  {'✅' if det['sweep'] else '❌'}\n"
        f"BOS:    {'✅' if det['bos'] else '❌'}\n"
        f"OTE Fib:{'✅' if det['ote'] else '❌'} {det['ote_lbl']}\n"
        f"M1 50%: {'✅ ENTRADA OK' if m1_e else '❌ aguardando'}\n"
        f"________________________\n"
        f"🧠 {memory['total_prints']} prints | min:{MIN_SCORE}/10\n"
    )
    if sc >= MIN_SCORE and m1_e and h4_trend != "neutral":
        debug += f"🚀 <b>SINAL PRONTO! ({sc}/10)</b>"
    else:
        if   h4_trend=="neutral": motivo="H4 sem tendencia"
        elif not m1_e:            motivo="M1 aguardando onda 50%"
        else:                     motivo=f"Score {sc}/{MIN_SCORE} insuficiente"
        debug += f"⏳ <i>{motivo}</i>"

    return debug, sc, det, m1_e, h4_trend, price

# ══════════════════════════════════════════════════════════════
#   DISPARAR SINAL
# ══════════════════════════════════════════════════════════════
def fire_signal(sc, det, m1_e, h4_trend, price):
    ep=m1_e["entry"]; sp=m1_e["stop"]; risk=abs(ep-sp)
    ew=det["ew"]

    # Alvo baseado em Elliott: preferencia pelo TP2 (161.8%) se onda 2
    h4_ew = ew.get("h4", ElliottResult())
    if h4_ew.tp2 and h4_ew.tp2 > 0:
        tp = h4_ew.tp2 if sc >= 6 else h4_ew.tp1
    else:
        ext = 1.618 if sc >= 6 else 1.0
        tp  = (ep + m1_e["wave_size"]*ext) if h4_trend=="up" else (ep - m1_e["wave_size"]*ext)

    if h4_trend=="up":
        emoji="✅"; action="COMPRA BUY"; direcao="ALTA 📈"; sl_lbl="Fundo M1"
    else:
        emoji="🔴"; action="VENDA SELL"; direcao="BAIXA 📉"; sl_lbl="Topo M1"

    rr    = round(abs(tp-ep)/risk,1) if risk>0 else 0
    barra = "█"*sc + "░"*(10-sc)

    # Linha Elliott resumida por TF
    def ew_mini(key):
        e = ew.get(key, ElliottResult())
        return f"{e.emoji}{e.wave_position or '—'}" if e.wave_position else "⚪"

    # Fatores SMC
    smc_ativos=[]
    if det["ob"]:    smc_ativos.append("Order Block")
    if det["fvg"]:   smc_ativos.append("FVG")
    if det["sweep"]: smc_ativos.append("Liq.Sweep")
    if det["bos"]:   smc_ativos.append("BOS")
    if det["ote"]:   smc_ativos.append(f"OTE {det['ote_lbl']}")
    smc_str=(" | ".join(smc_ativos)) if smc_ativos else "—"

    note=(f"\n🧠 <i>Calibrado com {memory['total_prints']} prints</i>"
          if memory["total_prints"]>0 else "")
    ts=datetime.utcnow().strftime("%d/%m/%Y %H:%M")

    # Salva sinal
    sinal={"id":len(memory["signals"])+1,"direcao":h4_trend,
           "entrada":ep,"stop":sp,"alvo":tp,"risco":risk,"rr":rr,
           "score":sc,"data":ts,"status":"aberto","resultado":None}
    memory["signals"].append(sinal)
    if len(memory["signals"])>200: memory["signals"]=memory["signals"][-200:]
    save_memory()

    send_telegram(
        f"{emoji} <b>SINAL {action}</b> — BTCUSDT\n"
        f"________________________\n"
        f"💰 Entrada: <b>${ep:,.2f}</b>\n"
        f"🛑 Stop:    <b>${sp:,.2f}</b> ({sl_lbl})\n"
        f"🎯 Alvo:    <b>${tp:,.2f}</b>\n"
        f"📐 Risco:   ${risk:,.2f} | R:R ≈ 1:{rr}\n"
        f"🎯 Score:   <b>{sc}/10</b> [{barra}]\n"
        f"________________________\n"
        f"🌊 <b>Elliott Wave:</b>\n"
        f"  H4 {ew_mini('h4')} H1 {ew_mini('h1')} "
        f"M15 {ew_mini('m15')} M5 {ew_mini('m5')} M1 {ew_mini('m1')}\n"
        f"  {ew['h4'].wave_label if ew['h4'].wave_position else ew['h1'].wave_label}\n"
        f"________________________\n"
        f"H4 → {direcao}\n"
        f"SMC: {smc_str}\n"
        f"M1  → onda 50% ✅ → <b>ENTRADA LIBERADA</b>{note}\n"
        f"________________________\n"
        f"⏰ {ts} UTC\n"
        f"⚠️ <i>Gerencie o risco!</i>"
    )

# ── Monitorar sinais abertos ─────────────────────────────────
def check_open(price):
    changed=False
    for s in [x for x in memory.get("signals",[]) if x["status"]=="aberto"]:
        ep=s["entrada"]; tp=s["alvo"]; sp=s["stop"]
        hit_tp=(s["direcao"]=="up" and price>=tp)or(s["direcao"]=="down" and price<=tp)
        hit_sl=(s["direcao"]=="up" and price<=sp)or(s["direcao"]=="down" and price>=sp)
        if hit_tp:
            s["status"]="win"; s["resultado"]=f"+{s['rr']}R"
            s["fechamento"]=datetime.utcnow().strftime("%d/%m/%Y %H:%M"); changed=True
            send_telegram(f"🏆 <b>TAKE PROFIT!</b> #{s['id']}\n"
                          f"${ep:,.0f}→${tp:,.0f} ✅ <b>+{s['rr']}R</b>")
        elif hit_sl:
            s["status"]="loss"; s["resultado"]="-1R"
            s["fechamento"]=datetime.utcnow().strftime("%d/%m/%Y %H:%M"); changed=True
            send_telegram(f"🛑 <b>STOP LOSS</b> #{s['id']}\n"
                          f"${ep:,.0f} ❌ <b>-1R</b>")
    if changed: save_memory()

# ══════════════════════════════════════════════════════════════
#   SINAIS AUTONOMOS DE ELLIOTT WAVE POR TIMEFRAME
#
#   Lógica:
#   • Toda vez que uma Onda 2 ou Onda 4 é detectada num TF,
#     o bot dispara um sinal próprio daquele TF.
#   • Sinal de TF maior (H4/H1) = SWING — stop maior, alvo maior
#   • Sinal de TF menor (M15/M5/M1) = SCALP — stop menor, alvo menor
#   • Cada TF tem seu próprio cooldown para não repetir
# ══════════════════════════════════════════════════════════════

TF_CONFIG = {
    # tf_label : (tipo_operacao, peso_qualidade)
    "H4" : ("🏦 SWING",  "⭐⭐⭐"),
    "H1" : ("📊 SWING",  "⭐⭐⭐"),
    "M15": ("⚡ INTRADAY","⭐⭐"),
    "M5" : ("⚡ INTRADAY","⭐⭐"),
    "M1" : ("🎯 SCALP",  "⭐"),
}

def fire_elliott_signal(tf_label, ew_result, direction, price, h4_trend):
    """
    Dispara sinal de compra ou venda baseado puramente em Elliott Wave.
    Onda 2 → entrada para pegar a Onda 3 (maior movimento)
    Onda 4 → entrada para pegar a Onda 5
    """
    global last_ew_signal_time

    # Cooldown por TF + direcao
    ew_key = f"{tf_label}_{direction}"
    now_ts = time.time()
    if now_ts - last_ew_signal_time.get(ew_key, 0) < EW_COOLDOWN:
        return  # ainda em cooldown

    ep  = price
    sp  = ew_result.stop_level
    tp1 = ew_result.tp1
    tp2 = ew_result.tp2

    if sp == 0 or tp1 == 0:
        return  # dados insuficientes

    risk = abs(ep - sp)
    if risk < 10:
        return  # risco muito pequeno, provavelmente erro

    rr1 = round(abs(tp1-ep)/risk, 1) if risk>0 else 0
    rr2 = round(abs(tp2-ep)/risk, 1) if risk>0 else 0

    if direction == "up":
        emoji  = "✅"
        action = "COMPRA BUY"
        direcao= "ALTA 📈"
        sl_lbl = "Abaixo do início da onda"
    else:
        emoji  = "🔴"
        action = "VENDA SELL"
        direcao= "BAIXA 📉"
        sl_lbl = "Acima do início da onda"

    tipo_op, qualidade = TF_CONFIG.get(tf_label, ("📊","⭐"))

    # Contexto da onda
    if ew_result.wave_position == "2":
        onda_ctx = (
            f"🌊 <b>Onda 2 confirmada</b> — entrada para pegar a <b>Onda 3</b>\n"
            f"   Onda 3 costuma ser o maior movimento (161.8% de W1)\n"
            f"   Retração atual: {ew_result.retrace_pct:.0%}"
        )
        confianca = "ALTA 🟢"
    else:  # wave 4
        onda_ctx = (
            f"🌊 <b>Onda 4 confirmada</b> — entrada para pegar a <b>Onda 5</b>\n"
            f"   Onda 5 tipicamente = tamanho da Onda 1\n"
            f"   Retração atual: {ew_result.retrace_pct:.0%}"
        )
        confianca = "MEDIA 🟡"

    # H4 divergindo da tendencia = alerta
    alerta = ""
    if h4_trend != "neutral" and h4_trend != direction:
        alerta = "\n⚠️ <i>Contra tendencia H4 — use lote reduzido!</i>"

    ts = datetime.utcnow().strftime("%d/%m/%Y %H:%M")

    # Salva no historico
    sinal = {
        "id":       len(memory["signals"])+1,
        "direcao":  direction,
        "entrada":  ep,
        "stop":     sp,
        "alvo":     tp2,
        "risco":    risk,
        "rr":       rr2,
        "score":    f"Elliott {tf_label}",
        "data":     ts,
        "status":   "aberto",
        "resultado":None,
        "tipo":     "elliott"
    }
    memory["signals"].append(sinal)
    if len(memory["signals"])>200: memory["signals"]=memory["signals"][-200:]
    save_memory()

    last_ew_signal_time[ew_key] = now_ts

    send_telegram(
        f"{emoji} <b>SINAL ELLIOTT — {action}</b>\n"
        f"{tipo_op} | TF: <b>{tf_label}</b> | {qualidade}\n"
        f"________________________\n"
        f"💰 Entrada: <b>${ep:,.2f}</b>\n"
        f"🛑 Stop:    <b>${sp:,.2f}</b>\n"
        f"   ({sl_lbl})\n"
        f"🎯 TP1:     <b>${tp1:,.2f}</b>  R:R ≈ 1:{rr1}\n"
        f"🎯 TP2:     <b>${tp2:,.2f}</b>  R:R ≈ 1:{rr2}\n"
        f"📐 Risco:   ${risk:,.2f}\n"
        f"________________________\n"
        f"{onda_ctx}\n"
        f"________________________\n"
        f"🎯 Confiança: <b>{confianca}</b>\n"
        f"H4 Tendência: {'📈 ALTA' if h4_trend=='up' else '📉 BAIXA' if h4_trend=='down' else '⚪ NEUTRO'}\n"
        f"{ew_result.detail}{alerta}\n"
        f"________________________\n"
        f"⏰ {ts} UTC\n"
        f"⚠️ <i>Gerencie o risco!</i>"
    )

def check_elliott_signals(c_h4, c_h1, c_m15, c_m5, c_m1, h4_trend, price):
    """
    Varre todos os TFs procurando Onda 2 ou Onda 4.
    Dispara sinal autônomo de Elliott para cada TF que confirmar.
    """
    tfs = [
        ("H4",  c_h4,  SWING_LARGE),
        ("H1",  c_h1,  SWING_LARGE),
        ("M15", c_m15, SWING_LARGE),
        ("M5",  c_m5,  SWING_LARGE),
        ("M1",  c_m1,  SWING_SMALL),
    ]

    for tf_label, candles, n in tfs:
        # Analisa na direção da tendência H4
        if h4_trend != "neutral":
            ew = detect_elliott(candles, h4_trend, n=n)
            if ew.entry_valid and ew.wave_position in ("2","4"):
                fire_elliott_signal(tf_label, ew, h4_trend, price, h4_trend)

        # Também analisa na direção contrária (reversões)
        # Mas só se for H4 ou H1 (evita ruído em TFs pequenos)
        if tf_label in ("H4","H1"):
            opp = "down" if h4_trend=="up" else "up"
            ew_opp = detect_elliott(candles, opp, n=n)
            if ew_opp.entry_valid and ew_opp.wave_position in ("2","4"):
                fire_elliott_signal(tf_label, ew_opp, opp, price, h4_trend)

# ══════════════════════════════════════════════════════════════
#   VISAO IA — GROQ (analise de prints MT5)
# ══════════════════════════════════════════════════════════════
VISION_PROMPT = """Voce e especialista em Elliott Wave, SMC e Fibonacci.
Analise este grafico MT5 com as marcacoes do trader.
Identifique: em qual onda de Elliott o preco esta? (1,2,3,4,5,A,B,C)
Retorne SOMENTE JSON valido sem markdown:
{
  "timeframe":"M1/M5/M15/H1/H4",
  "tendencia":"up/down/neutral",
  "onda_atual":"1/2/3/4/5/A/B/C/null",
  "tipo_onda":"impulso/correcao_w2/correcao_w4/correcao_abc",
  "nivel_entrada":0.0,
  "nivel_stop":0.0,
  "nivel_alvo":0.0,
  "correcao_pct":0.0,
  "fib_level":"38.2/50/61.8/OTE/null",
  "smc_padroes":["Order Block","FVG","Liquidity Sweep","BOS"],
  "observacoes":"descricao curta",
  "qualidade_setup":"alta/media/baixa"
}
Se nao identificar use null."""

def analyze_image(img_bytes):
    if not GROQ_KEY: raise Exception("GROQ_API_KEY nao configurada!")
    b64=base64.b64encode(img_bytes).decode()
    r=requests.post("https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
        json={"model":"meta-llama/llama-4-scout-17b-16e-instruct",
              "messages":[{"role":"user","content":[
                  {"type":"text","text":VISION_PROMPT},
                  {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
              ]}],"max_tokens":1000,"temperature":0.1},
        timeout=30)
    r.raise_for_status()
    txt=r.json()["choices"][0]["message"]["content"].strip()
    return json.loads(txt.replace("```json","").replace("```","").strip())

def calibrate():
    bons=[a for a in memory["analyses"]
          if a.get("qualidade_setup")=="alta" and a.get("correcao_pct") and float(a["correcao_pct"])>0]
    if len(bons)>=3:
        m=sum(float(a["correcao_pct"]) for a in bons)/len(bons)
        nova=max(0.05,min(0.20,round(abs(m-0.5)+0.10,3)))
        memory["zone_tol"]=nova; print(f"[CAL] tol={nova:.0%}")

def process_chart_image(img_bytes, chat_id, caption=""):
    send_telegram("🔍 Analisando Elliott + SMC + Fibonacci...", chat_id)
    try:
        a=analyze_image(img_bytes)
    except Exception as e:
        send_telegram(f"❌ Erro: {e}", chat_id); return

    a["data"]=datetime.utcnow().strftime("%d/%m/%Y %H:%M"); a["caption"]=caption
    memory["analyses"].append(a)
    if len(memory["analyses"])>100: memory["analyses"]=memory["analyses"][-100:]
    memory["total_prints"]+=1; memory["last_update"]=a["data"]
    calibrate(); save_memory()

    onda=a.get("onda_atual","—"); tf=a.get("timeframe","—")
    tend=a.get("tendencia","—"); tipo=a.get("tipo_onda","—")
    corr=a.get("correcao_pct"); ep=a.get("nivel_entrada")
    sp=a.get("nivel_stop"); alvo=a.get("nivel_alvo")
    fib=a.get("fib_level","—"); qual=a.get("qualidade_setup","—")
    obs=a.get("observacoes","—"); smcp=a.get("smc_padroes",[]) or []
    te="📈" if tend=="up" else("📉" if tend=="down" else"↔️")
    qe="🟢" if qual=="alta" else("🟡" if qual=="media" else"🔴")
    onda_emoji={"2":"🌊","4":"🌊","3":"🚀","5":"🏁","A":"↩️","B":"↪️","C":"⬇️"}.get(str(onda),"⚪")

    msg=(f"✅ <b>Grafico analisado!</b>\n"
         f"________________________\n"
         f"📊 TF: <b>{tf}</b> | {te} {tend.upper()}\n"
         f"🌊 Onda atual: <b>{onda_emoji} Onda {onda}</b>\n"
         f"📐 Tipo: <b>{tipo}</b>\n"
         f"📐 Retracao: <b>{f'{float(corr):.0%}' if corr else '—'}</b> | Fib: <b>{fib}</b>\n"
         f"________________________\n")
    if ep:   msg+=f"💰 Entrada: ${float(ep):,.2f}\n"
    if sp:   msg+=f"🛑 Stop:    ${float(sp):,.2f}\n"
    if alvo: msg+=f"🎯 Alvo:    ${float(alvo):,.2f}\n"
    msg+=(f"________________________\n"
          f"{qe} Qualidade: <b>{qual.upper()}</b>\n💡 {obs}\n")
    if smcp: msg+=f"🔍 SMC: {', '.join(smcp)}\n"
    msg+=(f"________________________\n"
          f"🧠 {memory['total_prints']} prints aprendidos\n📅 {a['data']} UTC")
    send_telegram(msg, chat_id)

# ══════════════════════════════════════════════════════════════
#   COMANDOS TELEGRAM
# ══════════════════════════════════════════════════════════════
def handle_command(text, chat_id):
    cmd=text.strip().lower().split()[0]

    if cmd in ("/help","/commands","/start","/tools","/skill"):
        send_telegram(
            "🤖 <b>TRON FOREX BOT</b>\n\n"
            "📋 <b>Comandos:</b>\n"
            "/status    — Preco + Elliott H4\n"
            "/analise   — Score completo todos TFs\n"
            "/elliott   — Elliott Wave em todos TFs\n"
            "/relatorio — Historico de sinais\n"
            "/hoje      — Desempenho hoje\n"
            "/memoria   — O que aprendi\n"
            "/help      — Ajuda\n\n"
            "📸 <b>Envie print MT5</b> para treinar!\n\n"
            "🔭 <b>Estrategias:</b>\n"
            "  🌊 Elliott Wave 1-2-3-4-5 (todos TFs)\n"
            "  🏦 SMC: OB + FVG + Sweep + BOS\n"
            "  📐 Fibonacci OTE 61.8–79%\n"
            "  🎯 Score confluencia (min 4/10)", chat_id)

    elif cmd == "/status":
        try:
            c=get_candles("4h",150); t=get_trend(c); p=c[-1]["close"]
            ew=detect_elliott(c,t)
            em="🟢" if t=="up" else("🔴" if t=="down" else"⚪")
            send_telegram(
                f"📡 <b>Status</b>\nBTC: <b>${p:,.2f}</b>\n"
                f"H4: {em} {t.upper()}\n"
                f"🌊 Elliott H4: {ew.emoji} {ew.wave_label}\n"
                f"🧠 {memory['total_prints']} prints | Score min: {MIN_SCORE}/10\n"
                f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC", chat_id)
        except Exception as e: send_telegram(f"Erro: {e}", chat_id)

    elif cmd == "/elliott":
        try:
            c_h4 =get_candles("4h",150); t=get_trend(c_h4); p=c_h4[-1]["close"]
            c_h1 =get_candles("1h",150)
            c_m15=get_candles("15m",150)
            c_m5 =get_candles("5m",100)
            c_m1 =get_candles("1m",80)
            tfs={"H4":(c_h4,SWING_LARGE),"H1":(c_h1,SWING_LARGE),
                 "M15":(c_m15,SWING_LARGE),"M5":(c_m5,SWING_LARGE),"M1":(c_m1,SWING_SMALL)}
            msg=f"🌊 <b>Elliott Wave — BTCUSDT ${p:,.2f}</b>\n"
            msg+=f"H4 Tendência: {'📈 ALTA' if t=='up' else '📉 BAIXA' if t=='down' else '⚪ NEUTRO'}\n"
            msg+="________________________\n"
            for tf_lbl,(cc,nn) in tfs.items():
                e=detect_elliott(cc,t,n=nn)
                if e.wave_position:
                    msg+=f"{tf_lbl} {e.emoji} <b>{e.wave_label}</b>\n"
                    if e.entry_valid:
                        msg+=(f"   → Entrada valida!\n"
                              f"   Stop: ${e.stop_level:,.0f}\n"
                              f"   TP1:  ${e.tp1:,.0f} | TP2: ${e.tp2:,.0f}\n")
                    if e.detail: msg+=f"   {e.detail}\n"
                else:
                    msg+=f"{tf_lbl} ⚪ sem onda clara\n"
            send_telegram(msg, chat_id)
        except Exception as e: send_telegram(f"Erro: {e}", chat_id)

    elif cmd == "/analise":
        try:
            debug,*_=full_analyze(); send_telegram(debug, chat_id)
        except Exception as e: send_telegram(f"Erro: {e}", chat_id)

    elif cmd == "/memoria":
        total=memory["total_prints"]
        if total==0:
            send_telegram("🧠 Vazio. Envie prints MT5!", chat_id); return
        alta=sum(1 for a in memory["analyses"] if a.get("qualidade_setup")=="alta")
        ondas={};
        for a in memory["analyses"]:
            o=str(a.get("onda_atual","—"))
            ondas[o]=ondas.get(o,0)+1
        ond_str=", ".join(f"W{k}({v})" for k,v in sorted(ondas.items()) if k!="None")
        send_telegram(
            f"🧠 <b>Memoria</b>\n📸 {total} prints | 🟢 Alta: {alta}\n"
            f"🌊 Ondas vistas: {ond_str or '—'}\n"
            f"⚙️ Tol: {memory['zone_tol']:.0%} | Score min: {MIN_SCORE}/10\n"
            f"📅 {memory['last_update']}", chat_id)

    elif cmd == "/relatorio":
        sinais=memory.get("signals",[])
        if not sinais:
            send_telegram("📊 Nenhum sinal ainda.", chat_id); return
        wins=[s for s in sinais if s["status"]=="win"]
        losses=[s for s in sinais if s["status"]=="loss"]
        ab=[s for s in sinais if s["status"]=="aberto"]
        tf=len(wins)+len(losses); wr=(len(wins)/tf*100) if tf>0 else 0
        rnet=sum(float(s["rr"]) for s in wins)-len(losses)
        send_telegram(
            f"📊 <b>Relatorio</b>\n"
            f"Total: {len(sinais)} | ✅{len(wins)} ❌{len(losses)} ⏳{len(ab)}\n"
            f"🎯 Win Rate: <b>{wr:.0f}%</b>\n"
            f"💰 Resultado: <b>{rnet:+.1f}R</b>\n"
            f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC", chat_id)

    elif cmd == "/hoje":
        hoje=datetime.utcnow().strftime("%d/%m/%Y")
        hs=[s for s in memory.get("signals",[]) if s.get("data","").startswith(hoje)]
        if not hs:
            send_telegram(f"📅 Sem sinais hoje ({hoje}).", chat_id); return
        wh=[s for s in hs if s["status"]=="win"]; lh=[s for s in hs if s["status"]=="loss"]
        tf=len(wh)+len(lh); wr=(len(wh)/tf*100) if tf>0 else 0
        rn=sum(float(s["rr"]) for s in wh)-len(lh)
        send_telegram(
            f"📅 <b>Hoje ({hoje})</b>\n"
            f"Sinais: {len(hs)} ✅{len(wh)} ❌{len(lh)}\n"
            f"🎯 WR: <b>{wr:.0f}%</b> | 💰 <b>{rn:+.1f}R</b>", chat_id)

    else: send_telegram("Comando nao reconhecido. /help", chat_id)

# ── Loop comandos + fotos ────────────────────────────────────
def commands_loop():
    print("Ouvindo...")
    while True:
        try:
            for upd in get_updates():
                msg=upd.get("message") or upd.get("edited_message")
                if not msg: continue
                cid=str(msg["chat"]["id"]); txt=msg.get("text","")
                if txt.startswith("/"):
                    print(f"[CMD] {txt}"); handle_command(txt,cid)
                elif msg.get("photo"):
                    photo=msg["photo"][-1]; cap=msg.get("caption","")
                    try:
                        img=download_photo(photo["file_id"])
                        threading.Thread(target=process_chart_image,
                            args=(img,cid,cap),daemon=True).start()
                    except Exception as e: send_telegram(f"Erro foto:{e}",cid)
        except Exception as e: print(f"[CMD] {e}")
        time.sleep(2)

# ── Loop principal ───────────────────────────────────────────
_loop_n=0
STATUS_EVERY=max(1,int(4*3600/CHECK_INTERVAL))

def main_loop():
    global _loop_n
    while True:
        try:
            _loop_n+=1
            if _loop_n%STATUS_EVERY==0:
                c=get_candles("4h",150); t=get_trend(c); p=c[-1]["close"]
                ew=detect_elliott(c,t)
                em="🟢" if t=="up" else("🔴" if t=="down" else"⚪")
                send_telegram(f"📡 BTC <b>${p:,.2f}</b> H4:{em}{t.upper()}\n"
                              f"🌊 Elliott: {ew.emoji} {ew.wave_label}\n"
                              f"⏰ {datetime.utcnow().strftime('%d/%m %H:%M')} UTC")

            # ── Busca candles uma só vez para todos os usos ──────────
            c_h4  = get_candles("4h", 150)
            c_h1  = get_candles("1h", 150)
            c_m15 = get_candles("15m",150)
            c_m5  = get_candles("5m", 100)
            c_m1  = get_candles("1m",  80)
            h4_trend = get_trend(c_h4)
            price    = c_h4[-1]["close"]

            # ── Score multi-TF + sinal confluente ─────────────────
            sc, det, m1_e = score_all(c_h4,c_h1,c_m15,c_m5,c_m1,h4_trend)
            check_open(price)

            ew=det["ew"]
            print(f"[{datetime.utcnow().strftime('%H:%M')}] "
                  f"${price:,.0f} {h4_trend} sc:{sc}/10 "
                  f"EW H4:{ew['h4'].wave_position or '—'} "
                  f"H1:{ew['h1'].wave_position or '—'} "
                  f"M5:{ew['m5'].wave_position or '—'} "
                  f"M1:{'OK' if m1_e else '—'}")

            # ── Sinais autônomos de Elliott por TF ────────────────
            if h4_trend != "neutral":
                check_elliott_signals(c_h4,c_h1,c_m15,c_m5,c_m1,h4_trend,price)

            # ── Sinal confluente multi-TF (score + M1) ────────────
            if sc>=MIN_SCORE and m1_e and h4_trend!="neutral":
                now_ts=time.time()
                if now_ts-last_signal_time.get(h4_trend,0)>=MTF_COOLDOWN:
                    fire_signal(sc,det,m1_e,h4_trend,price)
                    last_signal_time[h4_trend]=now_ts
                else: print("  [cooldown multi-TF]")

        except Exception as e:
            print(f"[ERRO] {e}")
            import traceback; traceback.print_exc()
        time.sleep(CHECK_INTERVAL)

# ── START ────────────────────────────────────────────────────
print("TRON FOREX Bot iniciado...")
threading.Thread(target=run_server,    daemon=True).start()
load_memory()
threading.Thread(target=commands_loop, daemon=True).start()

send_telegram(
    "🤖 <b>TRON FOREX Bot v3</b>\n\n"
    "🌊 <b>Elliott Wave ativo em TODOS os TFs:</b>\n"
    "  H4 → H1 → M15 → M5 → M1\n"
    "  Detecta ondas 1-2-3-4-5 e ABC\n"
    "  Melhor entrada: Onda 2 e Onda 4\n\n"
    "🏦 SMC: Order Block + FVG + Sweep + BOS\n"
    "📐 Fibonacci OTE 61.8–79%\n"
    "🎯 Score confluencia (min 4/10)\n\n"
    "📋 /elliott — ver ondas em todos TFs\n"
    "📸 Envie prints MT5 para treinar\n\n"
    f"🧠 {memory['total_prints']} prints na memoria\n"
    f"⏰ {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC"
)

main_loop()
