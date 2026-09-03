"""
TRON FOREX BOT - Dev: Jon Padilha — Multi-Símbolo + Ordens Manuais + Performance
BTC/ETH/SOL/BNB/XRP + Bybit Spot + Futuros + Ordens Limitadas
"""

import requests, time, os, json, base64, threading, hmac, hashlib, subprocess, tempfile, sys, ast
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

# ── Fuso horário de Brasília (São Paulo) ───────────────────────
BR_TZ = timezone(timedelta(hours=-3))
def agora_br():
    """Hora atual em Brasília (usado só para exibição/relatórios, não afeta a lógica de sinais)."""
    return datetime.now(BR_TZ)

# ── Cotação USD/BRL (cacheada) ─────────────────────────────────
_usd_brl_cache = {"valor": 5.30, "ts": 0}
def get_usd_brl():
    """Retorna a cotação USD->BRL, com cache de 5 minutos e fallback seguro."""
    agora = time.time()
    if agora - _usd_brl_cache["ts"] < 300:
        return _usd_brl_cache["valor"]
    try:
        r = requests.get("https://economia.awesomeapi.com.br/last/USD-BRL", timeout=5)
        v = float(r.json()["USDBRL"]["bid"])
        if v > 0:
            _usd_brl_cache["valor"] = v
            _usd_brl_cache["ts"] = agora
    except Exception:
        pass
    return _usd_brl_cache["valor"]

def resultado_brl(s):
    """Calcula o valor REAL em BRL (lucro ou prejuízo) de um sinal já fechado.
    Prioridade: 1) PnL de verdade puxado da corretora (já líquido de taxa
    e slippage, gravado em resultado_usd quando o sinal fechou — ver
    pnl_real()); 2) preço de saída real (mais preciso que o estimado,
    mas SEM taxa/slippage); 3) estimado por risco x RR, só em registros
    antigos sem preço de saída salvo."""
    qty = s.get("qty_usada") or SYMBOLS.get(s.get("symbol", ""), {}).get("qty", 0)
    if s.get("resultado_usd") is not None:
        return s["resultado_usd"] * get_usd_brl()
    if s.get("preco_saida") is not None and s.get("entrada") is not None:
        mov = (s["preco_saida"]-s["entrada"]) if s.get("direcao")=="BUY" else (s["entrada"]-s["preco_saida"])
        return mov * qty * get_usd_brl()
    risco_usd = s.get("risco", 0) * qty
    if s.get("status") == "win":
        usd = risco_usd * float(s.get("rr", 0))
    elif s.get("status") == "loss":
        usd = -risco_usd
    else:
        return None
    return usd * get_usd_brl()

def fmt_brl(valor):
    if valor is None:
        return "aberto"
    sinal = "+" if valor >= 0 else "-"
    return f"{sinal}R$ {abs(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def fmt_num_brl(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def saldo_brl_txt(rotulo="🤖 Bot Crypto"):
    """Saldo TOTAL de ativos da conta (equity), já convertido pra BRL,
    formatado, pronto pra colar na mensagem — igual ao 'Total de ativos'
    que aparece no app da Bybit. Também soma o lucro manual
    (MANUAL_PROFITS_BRL, do .env) pra mostrar o total geral."""
    saldo = get_patrimonio_usdt()
    if not saldo:
        return ""
    saldo_brl = saldo * get_usd_brl()
    txt = f"\n{rotulo}: {fmt_num_brl(saldo_brl)}"
    total = saldo_brl + MANUAL_PROFITS_BRL
    txt += f"\n🖐️ Trades Manuais(Jon): {fmt_num_brl(total)}"
    return txt

# ── Lê .env ─────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                # BINGX_API_SECRET="abc..." (com aspas) é comum em quem
                # escreve .env no estilo bash — mas aqui as aspas viravam
                # PARTE do valor (não tem parser de verdade), o que
                # quebra silenciosamente qualquer assinatura HMAC que use
                # essa chave/secret. Tira só um par de aspas casadas nas
                # pontas, como todo parser de .env de verdade faz.
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ("'", '"'):
                    _v = _v[1:-1]
                os.environ.setdefault(_k.strip(), _v)
    print("[ENV] Variaveis carregadas do .env")

# ══════════ CONFIG ════════════════════════════════════════════
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID          = os.environ.get("CHAT_ID", "")
GROQ_KEY         = os.environ.get("GROQ_API_KEY", "")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")
GITHUB_FILE      = "memory.json"

BYBIT_API_KEY    = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BYBIT_MODE       = os.environ.get("BYBIT_MODE", "real").lower()
BYBIT_LEVERAGE   = int(os.environ.get("BYBIT_LEVERAGE", "5"))

BYBIT_URL = ("https://api-testnet.bybit.com"
             if BYBIT_MODE == "testnet"
             else "https://api.bybit.com")

# ── BingX ──────────────────────────────────────────────────────
# Troque a corretora com EXCHANGE=bingx no .env (padrão: bybit).
EXCHANGE          = os.environ.get("EXCHANGE", "bybit").strip().lower()
BINGX_API_KEY     = os.environ.get("BINGX_API_KEY", "")
BINGX_API_SECRET  = os.environ.get("BINGX_API_SECRET", "")
if BINGX_API_KEY or BINGX_API_SECRET:
    # nunca imprime o valor — só o tamanho, pra dar pra conferir por
    # aspas/espaço perdido no .env sem expor a chave em lugar nenhum.
    print(f"[ENV] BINGX_API_KEY: {len(BINGX_API_KEY)} chars | "
          f"BINGX_API_SECRET: {len(BINGX_API_SECRET)} chars")
BINGX_LEVERAGE    = int(os.environ.get("BINGX_LEVERAGE", os.environ.get("BYBIT_LEVERAGE", "5")))
# BINGX_MODE: real | demo   (demo = conta VST, dinheiro fictício da BingX)
BINGX_MODE        = os.environ.get("BINGX_MODE", "real").strip().lower()
BINGX_URL         = ("https://open-api-vst.bingx.com"
                     if BINGX_MODE == "demo"
                     else "https://open-api.bingx.com")
BINGX_SWAP        = "/openApi/swap/v2"
BINGX_SPOT        = "/openApi/spot/v1"

USANDO_BINGX = (EXCHANGE == "bingx")

# ── MULTI-CORRETORA ──────────────────────────────────────────────
# EXCHANGES_ATIVAS=bingx,bybit abre a ordem automática NAS DUAS ao
# mesmo tempo (cada uma com seu próprio saldo/quantidade/leverage) —
# sem isso, mantém o comportamento de sempre (só a EXCHANGE
# configurada). Comandos manuais (/long, /short, /comprar...)
# continuam só na corretora primária (EXCHANGE) — isso aqui afeta só
# os motores automáticos (fire_signal).
_exchanges_env = os.environ.get("EXCHANGES_ATIVAS", "").strip()
if _exchanges_env:
    EXCHANGES_ATIVAS = [e.strip().lower() for e in _exchanges_env.split(",") if e.strip()]
else:
    EXCHANGES_ATIVAS = [EXCHANGE]

# ── MODO SIMULAÇÃO (paper trading) ─────────────────────────────
# Com SIMULACAO=true o bot roda TODA a lógica (cascata de pernadas,
# gatilho de M1, stop técnico, projeção de 38.2%, travas de risco) e
# registra os trades normalmente pra você ver o desempenho — mas NÃO
# envia ordem nenhuma pra corretora. Nenhum centavo é movimentado.
# É o teste mais seguro: não depende nem de depósito, nem da conta demo.
SIMULACAO = os.environ.get("SIMULACAO", "false").strip().lower() in ("1","true","sim","yes")

def simulacao_de(exchange):
    """SIMULACAO pode ser ajustado por corretora — SIMULACAO_BINGX ou
    SIMULACAO_BYBIT no .env sobrescrevem o SIMULACAO global só pra essa
    corretora (ex: SIMULACAO=true + SIMULACAO_BYBIT=false deixa a Bybit
    operando REAL enquanto a BingX continua em papel). Sem override,
    cada corretora segue o SIMULACAO global."""
    override = os.environ.get(f"SIMULACAO_{exchange.upper()}", "").strip().lower()
    if override:
        return override in ("1", "true", "sim", "yes")
    return SIMULACAO

def modo_texto():
    """Rótulo do modo em que o bot está operando agora."""
    if SIMULACAO:
        return "SIMULAÇÃO 🧪"
    if USANDO_BINGX:
        return "DEMO 🟡 (VST)" if BINGX_MODE == "demo" else "REAL 🔴"
    return "TESTNET 🟡" if BYBIT_MODE == "testnet" else "REAL 🔴"

def modo_texto_ex(exchange):
    """Mesmo que modo_texto(), mas pra uma corretora específica —
    usado quando EXCHANGES_ATIVAS tem mais de uma."""
    if simulacao_de(exchange):
        return "SIMULAÇÃO 🧪"
    if exchange == "bingx":
        return "DEMO 🟡 (VST)" if BINGX_MODE == "demo" else "REAL 🔴"
    return "TESTNET 🟡" if BYBIT_MODE == "testnet" else "REAL 🔴"

def leverage_de(exchange):
    return BINGX_LEVERAGE if exchange == "bingx" else BYBIT_LEVERAGE

def nome_corretora(exchange):
    return "BingX" if exchange == "bingx" else "Bybit"

# Aviso genérico de conta real nas mensagens de sinal/resultado, sem
# identificar corretora nem modo (o bot fica em live no YouTube — não
# pode expor qual conta é real). Fixo em todas as mensagens até o Jon
# decidir uma forma definitiva de tratar isso.
TAG_CONTA_REAL = "\n💰 Conta REAL 🔴"

def ultima_atualizacao_texto():
    """Data/hora (Brasília) do último commit que mexeu no app.py — pra
    sempre saber quando o CÓDIGO rodando foi atualizado pela última
    vez. Filtra só por app.py (-- app.py) de propósito: o bot também
    empurra backups periódicos de memory.json pro GitHub via API, e
    esses não deveriam contar como "atualização de código"."""
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run(["git", "log", "-1", "--format=%ct", "--", "app.py"],
                           cwd=repo_dir, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            dt = datetime.fromtimestamp(int(r.stdout.strip()), tz=BR_TZ)
            return dt.strftime("%d/%m/%Y %H:%M")
    except Exception as e:
        print(f"[GIT] não consegui ler a data do último commit: {e}")
    return "desconhecida"

def bingx_symbol(symbol):
    """BingX usa hífen: BTCUSDT -> BTC-USDT. Fonte clássica de erro ao portar."""
    s = symbol.upper().replace("-", "")
    if s.endswith("USDT"): return f"{s[:-4]}-USDT"
    if s.endswith("USDC"): return f"{s[:-4]}-USDC"
    return s

SYMBOLS = {
    # Tier 1 — Alta liquidez
    "BTCUSDT":  {"qty": float(os.environ.get("QTY_BTC",   "0.001")),  "kraken": "XBTUSDT",  "min_wave": 30},
    "ETHUSDT":  {"qty": float(os.environ.get("QTY_ETH",   "0.01")),   "kraken": "ETHUSDT",  "min_wave": 2},
    "SOLUSDT":  {"qty": float(os.environ.get("QTY_SOL",   "0.1")),    "kraken": "SOLUSDT",  "min_wave": 1},
    "XRPUSDT":  {"qty": float(os.environ.get("QTY_XRP",   "10")),     "kraken": "XRPUSDT",  "min_wave": 0.05},
    "BNBUSDT":  {"qty": float(os.environ.get("QTY_BNB",   "0.01")),   "kraken": "BNBUSDT",  "min_wave": 1},
    # Tier 2 — Boa liquidez
    "DOGEUSDT": {"qty": float(os.environ.get("QTY_DOGE",  "100")),    "kraken": "XDGUSD",   "min_wave": 0.005},
    "ADAUSDT":  {"qty": float(os.environ.get("QTY_ADA",   "20")),     "kraken": "ADAUSDT",  "min_wave": 0.02},
    "AVAXUSDT": {"qty": float(os.environ.get("QTY_AVAX",  "0.1")),    "kraken": "AVAXUSDT", "min_wave": 0.5},
    "DOTUSDT":  {"qty": float(os.environ.get("QTY_DOT",   "1")),      "kraken": "DOTUSD",   "min_wave": 0.1},
    "LINKUSDT": {"qty": float(os.environ.get("QTY_LINK",  "1")),      "kraken": "LINKUSDT", "min_wave": 0.1},
    # Tier 3 — Volume sólido
    "LTCUSDT":  {"qty": float(os.environ.get("QTY_LTC",   "0.1")),    "kraken": "LTCUSDT",  "min_wave": 0.5},
    "ATOMUSDT": {"qty": float(os.environ.get("QTY_ATOM",  "1")),      "kraken": "ATOMUSDT", "min_wave": 0.1},
    "NEARUSDT": {"qty": float(os.environ.get("QTY_NEAR",  "2")),      "kraken": "NEARUSDT", "min_wave": 0.05},
    "APTUSDT":  {"qty": float(os.environ.get("QTY_APT",   "1")),      "kraken": "APTUSDT",  "min_wave": 0.1},
    "SUIUSDT":  {"qty": float(os.environ.get("QTY_SUI",   "5")),      "kraken": "SUIUSDT",  "min_wave": 0.02},
    "OPUSDT":   {"qty": float(os.environ.get("QTY_OP",    "2")),      "kraken": "OPUSDT",   "min_wave": 0.05},
    "ARBUSDT":  {"qty": float(os.environ.get("QTY_ARB",   "5")),      "kraken": "ARBUSD",   "min_wave": 0.02},
    "TRXUSDT":  {"qty": float(os.environ.get("QTY_TRX",   "50")),     "kraken": "TRXUSD",   "min_wave": 0.005},
    "TONUSDT":  {"qty": float(os.environ.get("QTY_TON",   "1")),      "kraken": "TONUSDT",  "min_wave": 0.05},
    "PEPEUSDT": {"qty": float(os.environ.get("QTY_PEPE",  "5000000")),"kraken": "PEPEUSD",  "min_wave": 0.000001},
}

# ── Filtro de pares ────────────────────────────────────────────
# PARES_ATIVOS no .env limita quais símbolos o bot opera, sem mexer
# na tabela SYMBOLS. Ex: PARES_ATIVOS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT
# Vazio = opera todos. Útil pra testar com amostra pequena e conferir
# cada sinal no gráfico.
_pares_env = os.environ.get("PARES_ATIVOS", "").strip()
if _pares_env:
    _escolhidos = [p.strip().upper().replace("-", "").replace("/", "")
                   for p in _pares_env.split(",") if p.strip()]
    # aceita "BTC", "btc", "BTC-USDT" ou "BTCUSDT" — tudo vira BTCUSDT
    _validos, _ignorados = [], []
    for _p in _escolhidos:
        if _p in SYMBOLS:
            _validos.append(_p)
        elif f"{_p}USDT" in SYMBOLS:
            _validos.append(f"{_p}USDT")
        else:
            _ignorados.append(_p)
    if _ignorados:
        print(f"[CONFIG] pares desconhecidos ignorados: {', '.join(_ignorados)}")
    if _validos:
        SYMBOLS = {k: v for k, v in SYMBOLS.items() if k in _validos}
        print(f"[CONFIG] operando só: {', '.join(SYMBOLS.keys())}")
    else:
        print("[CONFIG] PARES_ATIVOS não bateu com nenhum par conhecido — usando todos.")

CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "60"))    # ciclo de checagem (estratégia roda em M15/H1, não precisa ser tão rápido)
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "900"))
# ABC em construção: cooldown próprio, mais curto — as sub-pernas do M1
# se sucedem em minutos, não em quartos de hora.
SIGNAL_COOLDOWN_ABC = int(os.environ.get("SIGNAL_COOLDOWN_ABC", "180"))
ABC_CONSTRUCAO_ATIVO = os.environ.get("ABC_CONSTRUCAO_ATIVO", "true").strip().lower() in ("1","true","sim","yes")
# Filtro de zona: só opera a sub-perna quando o preço está na região de
# 50-100% de retração do impulso. ZONA_FIB_ATIVA=false desliga o filtro.
ZONA_FIB_ATIVA = os.environ.get("ZONA_FIB_ATIVA", "true").strip().lower() in ("1","true","sim","yes")

# ── FLUXO M1 PURO ───────────────────────────────────────────────
# Réplica do operacional manual no M1: perna + correção ~50% direto no
# M1 (sem esperar a âncora de H4/H1/M15), stop na origem da pernada,
# alvo no PRÓXIMO topo/fundo real do M1 — não uma projeção. Trades
# mais curtos e mais frequentes, pra girar volume — roda em paralelo
# ao M1-TECNICO/M1-ABC (que buscam alvo de M15/H4).
FLUXO_M1_ATIVO = os.environ.get("FLUXO_M1_ATIVO", "true").strip().lower() in ("1","true","sim","yes")
SIGNAL_COOLDOWN_FLUXO = int(os.environ.get("SIGNAL_COOLDOWN_FLUXO", "120"))

# ── MOTOR ÂNCORA (posições de longo prazo) ──────────────────────
# Cascata H4 → H1 → M15 → M5 (contexto_maior) pra achar a pernada maior
# corrigindo 38-65% — isso só define DIREÇÃO e ALVO (escala âncora,
# posição fica dias/semanas buscando o movimento de fundo). Quem ACIONA
# a entrada e define o STOP é sempre o M1, esperando ele confirmar a
# estrutura na mesma direção (é o bloco M1-TECNICO, mais abaixo em
# main_loop) — nunca abre ordem direto no tf âncora com gatilho/stop do
# próprio tf âncora (testado e corrigido: o rompimento tem que ser
# validado pelo M1 em correspondência, senão entra cedo demais).
ANCORA_ATIVO = os.environ.get("ANCORA_ATIVO", "true").strip().lower() in ("1","true","sim","yes")
SIGNAL_COOLDOWN_ANCORA = int(os.environ.get("SIGNAL_COOLDOWN_ANCORA", "1800"))

# ── ARBITRAGEM DE FLUXO ────────────────────────────────────────
# Permite compra e venda ABERTAS AO MESMO TEMPO no mesmo par — mas
# nunca no mesmo ponto: cada uma nasce de um gatilho próprio, em
# preço e momento diferentes, com stop e alvo técnicos próprios.
# Exige HEDGE MODE ligado na corretora (senão a segunda ordem só
# fecharia a primeira).
ARBITRAGEM_ATIVA = os.environ.get("ARBITRAGEM_ATIVA", "false").strip().lower() in ("1","true","sim","yes")
# distância mínima entre as duas entradas, em % do preço — impede que
# vire hedge no mesmo ponto por dois gatilhos quase simultâneos. Solto
# de propósito (0,02% ~ uns poucos dólares no BTC): no operacional
# manual do Jon os dois lados nascem bem próximos um do outro — só
# precisa não ser o MESMO ponto exato.
ARB_DIST_MIN_PCT = float(os.environ.get("ARB_DIST_MIN_PCT", "0.0002"))
# intervalo mínimo entre a entrada de um lado e a do outro (segundos) —
# também solto, só pra não deixar os dois nascerem no mesmo instante.
ARB_INTERVALO_MIN = int(os.environ.get("ARB_INTERVALO_MIN", "30"))

# ── Estratégia: tendência (EMA) + pullback (RSI) + risco por volatilidade (ATR) ──
EMA_RAPIDA   = int(os.environ.get("EMA_RAPIDA", "21"))     # EMA rápida no H1 (tendência)
EMA_LENTA    = int(os.environ.get("EMA_LENTA",  "55"))     # EMA lenta no H1 (tendência)
RSI_PERIODO  = int(os.environ.get("RSI_PERIODO", "14"))
RSI_PULLBACK = float(os.environ.get("RSI_PULLBACK", "45")) # compra quando RSI volta a cruzar essa linha subindo (venda: 100-45=55 caindo)
ATR_PERIODO  = int(os.environ.get("ATR_PERIODO", "14"))
ATR_STOP_MULT = float(os.environ.get("ATR_STOP_MULT", "1.5"))  # stop = 1.5x ATR (adapta à volatilidade real do par)
RR_ALVO       = float(os.environ.get("RR_ALVO", "2.0"))        # alvo = 2x o risco (RR 1:2)
ATR_PICO_MULT = float(os.environ.get("ATR_PICO_MULT", "3.0"))  # vela com range > 3x o ATR médio = possível notícia/evento, pula a entrada

# ── Sentimento de mercado (Fear & Greed Index — API pública gratuita) ──
FNG_FILTRO_ATIVO = os.environ.get("FNG_FILTRO_ATIVO", "true").strip().lower() in ("1","true","sim","yes")
FNG_EXTREMO_BAIXO = int(os.environ.get("FNG_EXTREMO_BAIXO", "15"))  # <= isso = pânico extremo
FNG_EXTREMO_ALTO  = int(os.environ.get("FNG_EXTREMO_ALTO", "85"))   # >= isso = euforia extrema

# ── Alvo fixo (sem trailing) — o alvo do sinal é fixo (RR_ALVO) e, se o
# lucro potencial em BRL ficar abaixo do mínimo aceitável, o RR é ampliado
# pra buscar um alvo maior em vez de fechar cedo. ──
ALVO_MINIMO_BRL = float(os.environ.get("ALVO_MINIMO_BRL", "1.87"))


# ── Gestão de risco e proteção de capital ──────────────────────
RISCO_PCT              = float(os.environ.get("RISCO_PCT", "0.02"))     # risco por trade = 2% do saldo disponível
MAX_TRADES_SIMULTANEOS = int(os.environ.get("MAX_TRADES_SIMULTANEOS", "0"))  # 0 = sem limite
PERDA_DIARIA_MAX_PCT   = float(os.environ.get("PERDA_DIARIA_MAX_PCT", "0.06"))  # freio: pausa auto-trade se cair X% no dia
FREIO_DIARIO_ATIVO     = os.environ.get("FREIO_DIARIO_ATIVO", "false").strip().lower() in ("1","true","sim","yes")
PORT = int(os.environ.get("PORT", "0"))
DEPOSITO_TOTAL_BRL = float(os.environ.get("DEPOSITO_TOTAL_BRL", "0"))   # quanto você já depositou, no total
MANUAL_PROFITS_BRL = float(os.environ.get("MANUAL_PROFITS_BRL", "0"))   # lucro já realizado/sacado fora do robô (trades manuais)

last_signal_time = {}
_setups_executados = set()   # trava anti-reentrada no mesmo gatilho
_ultimo_gatilho  = {}       # qual dos 3 gatilhos de M1 disparou, por símbolo
_ts_entrada      = {}       # timestamp da última entrada por "SIMBOLO|DIRECAO" (arbitragem)
# hedge mode / leverage já configurados nesta sessão — por corretora,
# senão configurar na BingX marcava a Bybit como "já feito" também
_hedge_verificado = {"bingx": False, "bybit": False}
last_update_id   = 0
_leverage_set    = {"bingx": set(), "bybit": set()}
_min_qty_cache   = {}       # (exchange, symbol) -> mínimo real de qty consultado na corretora
_freio_diario    = {"data": None, "saldo_inicial": None, "pausado": False, "ativo": FREIO_DIARIO_ATIVO}

memory = {
    "analyses":     [],
    "signals":      [],
    "zone_tol":     0.08,
    "total_prints": 0,
    "last_update":  "",
    "macro_views":  {},
    "next_id":      1,
    "config_lote":  {"modo": "auto"}
}

# ─── HTTP ────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Tron Forex Bot - Dev: Jon Padilha rodando")
    def log_message(self, *a): pass


def run_server():
    if PORT == 0: return
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ─── TELEGRAM ────────────────────────────────────────────────
def send_telegram(msg, chat_id=None):
    cid = chat_id or CHAT_ID
    msg = f"Tron Bot - Crypto x Forex 🤖\n\n{msg}\n\nTron Bot - Dev: Jon Padilha"
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": cid, "text": msg, "parse_mode": "HTML"},
            timeout=10)
        print(f"[TG] {msg[:80].strip()}")
    except Exception as e:
        print(f"Erro TG: {e}")

def send_telegram_foto(caminho, legenda="", chat_id=None):
    """Manda uma imagem (PNG) pro Telegram. Some com o arquivo depois,
    dê certo ou não — é sempre um arquivo temporário."""
    cid = chat_id or CHAT_ID
    try:
        with open(caminho, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": cid, "caption": legenda[:1024], "parse_mode": "HTML"},
                files={"photo": f}, timeout=20)
        if r.status_code != 200:
            print(f"[GRAFICO] Telegram recusou a foto (HTTP {r.status_code}): {r.text[:300]}", flush=True)
        else:
            print(f"[GRAFICO] foto enviada: {legenda[:60]}", flush=True)
    except Exception as e:
        print(f"Erro TG foto: {e}", flush=True)
    finally:
        try: os.remove(caminho)
        except OSError: pass

def get_updates():
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": last_update_id + 1, "timeout": 2}, timeout=8)
        ups = r.json().get("result", [])
        if ups: last_update_id = ups[-1]["update_id"]
        return ups
    except: return []

def _descartar_updates_pendentes():
    """Marca como lidas as mensagens pendentes no Telegram ANTES de
    começar a ouvir comandos. last_update_id é só em memória — a cada
    reinício (manual ou via /reiniciar) ele volta pra 0, e o Telegram
    reentrega o ÚLTIMO comando ainda não confirmado. Se esse comando for
    justamente /reiniciar, vira loop infinito de reinício (foi
    exatamente o que aconteceu). offset=-1 pega só a última pendência
    sem executar nada, e o valor dela vira o novo piso — tudo que já
    estava na fila fica pra trás."""
    global last_update_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": -1, "timeout": 0}, timeout=8)
        ups = r.json().get("result", [])
        if ups:
            last_update_id = ups[-1]["update_id"]
            print(f"[TG] {len(ups)} update(s) pendente(s) descartado(s) no início.")
    except Exception as e:
        print(f"[TG] não consegui descartar updates pendentes: {e}")

def download_photo(file_id):
    r  = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                      params={"file_id": file_id}, timeout=10)
    fp = r.json()["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{fp}",
                        timeout=20).content

# ═══════════════════════════════════════════════════════════════
#  BYBIT V5 — assinatura corrigida
# ═══════════════════════════════════════════════════════════════
def _headers_get(params):
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    # IMPORTANTE: a ordem aqui tem que ser EXATAMENTE a mesma que o requests.get
    # vai usar pra montar a query string (ordem de inserção do dict), senão a
    # assinatura não bate e a Bybit rejeita com "Error sign". NÃO ordenar.
    qs  = "&".join(f"{k}={v}" for k, v in params.items())
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   (ts + BYBIT_API_KEY + rw + qs).encode(),
                   hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": rw}

def _headers_post(body_str):
    ts  = str(int(time.time() * 1000))
    rw  = "5000"
    sig = hmac.new(BYBIT_API_SECRET.encode(),
                   (ts + BYBIT_API_KEY + rw + body_str).encode(),
                   hashlib.sha256).hexdigest()
    return {"X-BAPI-API-KEY": BYBIT_API_KEY, "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": sig, "X-BAPI-RECV-WINDOW": rw,
            "Content-Type": "application/json"}

def bybit_get(path, params=None):
    if not BYBIT_API_KEY: return None
    params = params or {}
    try:
        r = requests.get(f"{BYBIT_URL}{path}", params=params,
                         headers=_headers_get(params), timeout=15)
        return r.json()
    except Exception as e:
        print(f"[BYBIT GET] {e}"); return None

def bybit_post(path, payload):
    if not BYBIT_API_KEY: return None
    body = json.dumps(payload, separators=(',', ':'))
    try:
        r = requests.post(f"{BYBIT_URL}{path}",
                          headers=_headers_post(body), data=body, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[BYBIT POST] {e}"); return None

# ═══════════════════════════════════════════════════════════════
#  BINGX — HMAC-SHA256, respostas normalizadas no formato Bybit
#  (retCode/result) pra reaproveitar toda a lógica que já existe.
# ═══════════════════════════════════════════════════════════════
def _bingx_sign(qs):
    return hmac.new(BINGX_API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()

def _bingx_req(method, path, params=None, signed=True):
    if signed and not BINGX_API_KEY: return None
    p = dict(params or {})
    headers = {}
    url = f"{BINGX_URL}{path}"
    if signed:
        p["recvWindow"] = 5000
        # BingX exige os parâmetros de negócio ORDENADOS alfabeticamente,
        # com "timestamp" sempre por ÚLTIMO (fora do sort) e "signature"
        # depois dele — padrão do exemplo oficial deles. Confirmado
        # testando isolado (diag_bingx.py) contra a API real.
        #
        # A ASSINATURA é sobre o valor CRU (sem url-encode) — a BingX
        # decodifica o que recebe e reconstrói a string com o valor
        # ORIGINAL antes de comparar. Testado e confirmado contra a API
        # real: um valor sem caractere especial (texto puro) funciona
        # igual codificado ou não, mas um valor com {, ", : (como o
        # JSON de stopLoss/takeProfit) só bate quando se assina o JSON
        # cru — assinar a versão url-encoded dá "signature mismatch"
        # toda vez. Só a URL de fato TRANSMITIDA precisa ser
        # url-encoded (senão espaço/chave quebram a requisição HTTP em
        # si), então codifica DEPOIS de já ter assinado o valor cru.
        p = {k: p[k] for k in sorted(p)}
        p["timestamp"] = int(time.time() * 1000)
        qs_assinar = "&".join(f"{k}={v}" for k, v in p.items())
        assinatura = _bingx_sign(qs_assinar)
        qs_enviar = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in p.items())
        url = f"{url}?{qs_enviar}&signature={assinatura}"
        headers["X-BX-APIKEY"] = BINGX_API_KEY
    try:
        if signed:
            r = requests.request(method, url, headers=headers, timeout=15)
        else:
            r = requests.request(method, url, params=p, headers=headers, timeout=15)
        d = r.json()
    except Exception as e:
        print(f"[BINGX {method}] {path}: {e}"); return None
    # normaliza: BingX usa code/msg/data -> vira retCode/retMsg/result
    if isinstance(d, dict) and "code" in d:
        return {"retCode": d.get("code", 0), "retMsg": d.get("msg", ""),
                "result": d.get("data", {})}
    return {"retCode": 0, "retMsg": "", "result": d}

def bingx_get(path, params=None, signed=True):
    return _bingx_req("GET", path, params, signed)

def bingx_post(path, params=None):
    return _bingx_req("POST", path, params, True)

def _bingx_candles(symbol, tf, limit):
    iv = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1h":"1h","4h":"4h"}.get(tf, "1h")
    d = bingx_get(f"{BINGX_SWAP}/quote/klines",
                  {"symbol": bingx_symbol(symbol), "interval": iv, "limit": limit},
                  signed=False)
    if not d or d.get("retCode") not in (0, None): return []
    rows = d.get("result") or []
    out = []
    for k in rows:
        try:
            if isinstance(k, dict):
                out.append({"open": float(k["open"]), "high": float(k["high"]),
                            "low": float(k["low"]), "close": float(k["close"]),
                            "_t": int(k.get("time", 0))})
            else:
                out.append({"open": float(k[1]), "high": float(k[2]),
                            "low": float(k[3]), "close": float(k[4]),
                            "_t": int(k[0])})
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x.get("_t", 0))
    return out

def _bingx_set_leverage(symbol):
    if symbol in _leverage_set["bingx"]: return
    ok = True
    for lado in ("LONG", "SHORT"):
        r = bingx_post(f"{BINGX_SWAP}/trade/leverage", {
            "symbol": bingx_symbol(symbol), "side": lado,
            "leverage": BINGX_LEVERAGE})
        if not r or r.get("retCode") not in (0, None): ok = False
    if ok: _leverage_set["bingx"].add(symbol)

def _bingx_garantir_hedge():
    """Liga o modo hedge (dual position) na BingX, necessário pra manter
    compra e venda abertas ao mesmo tempo no mesmo par."""
    if _hedge_verificado["bingx"] or not ARBITRAGEM_ATIVA or simulacao_de("bingx"):
        return
    r = bingx_post(f"{BINGX_SWAP}/trade/positionSide/dual", {"dualSidePosition": "true"})
    if r and r.get("retCode") in (0, None):
        print("[BINGX] hedge mode ativado (posições long e short simultâneas).")
    else:
        print(f"[BINGX] não consegui ativar hedge mode: {r.get('retMsg','?') if r else 'sem resposta'}. "
              f"Ative manualmente no app (Futuros > Configurações > Modo de posição > Hedge).")
    _hedge_verificado["bingx"] = True


def _bingx_order_futures(symbol, side, qty, sl=None, tp=None):
    _bingx_garantir_hedge()
    _bingx_set_leverage(symbol)
    # Em hedge mode, cada lado é uma posição própria (LONG/SHORT).
    # Em one-way (BOTH), a segunda ordem só fecharia a primeira — por
    # isso a arbitragem EXIGE hedge mode ligado na conta.
    pos_side = ("LONG" if side.upper() == "BUY" else "SHORT") if ARBITRAGEM_ATIVA else "BOTH"
    p = {"symbol": bingx_symbol(symbol), "side": side.upper(),
         "positionSide": pos_side, "type": "MARKET", "quantity": qty}
    if sl:
        p["stopLoss"] = json.dumps({"type": "STOP_MARKET",
                                    "stopPrice": round(float(sl), 6),
                                    "workingType": "MARK_PRICE"})
    if tp:
        p["takeProfit"] = json.dumps({"type": "TAKE_PROFIT_MARKET",
                                      "stopPrice": round(float(tp), 6),
                                      "workingType": "MARK_PRICE"})
    r = bingx_post(f"{BINGX_SWAP}/trade/order", p)
    if r and r.get("retCode") in (0, None):
        res = r.get("result") or {}
        oid = (res.get("order") or {}).get("orderId", "") if isinstance(res, dict) else ""
        return {"ok": True, "order_id": str(oid)}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def _bingx_editar_sltp(symbol, pos_side_bybit, qty, novo_sl, novo_tp):
    """Ajusta SL/TP de uma posição já aberta na BingX. A BingX não expõe
    um endpoint pra 'substituir' o SL/TP anexado na ordem original —
    aqui funciona colocando uma NOVA ordem condicional de fechamento
    no nível pedido; o que disparar primeiro fecha a posição.

    Primeira tentativa real (closePosition=true, sem quantity) deu
    "position not exist" numa conta confirmada em modo Hedge — closePosition
    provavelmente só existe pra modo Unidirecional (só existe UMA posição
    por símbolo ali; em Hedge pode ter LONG e SHORT ao mesmo tempo, então
    "fechar a posição" é ambíguo sem dizer QUAL). Troca pra quantity exata
    da posição + reduceOnly=true, o padrão de fechamento em modo Hedge.
    A ordem condicional antiga (da entrada) pode continuar pendente na
    corretora até a posição fechar — vale conferir no app depois de usar."""
    lado_posicao = "LONG" if pos_side_bybit == "Buy" else "SHORT"
    position_side = lado_posicao if ARBITRAGEM_ATIVA else "BOTH"
    lado_fechamento = "SELL" if pos_side_bybit == "Buy" else "BUY"
    for tipo, preco in (("STOP_MARKET", novo_sl), ("TAKE_PROFIT_MARKET", novo_tp)):
        if not preco:
            continue
        p = {"symbol": bingx_symbol(symbol), "side": lado_fechamento,
             "positionSide": position_side, "type": tipo,
             "stopPrice": round(float(preco), 6), "quantity": qty, "reduceOnly": "true"}
        r = bingx_post(f"{BINGX_SWAP}/trade/order", p)
        if not r or r.get("retCode") not in (0, None):
            return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}
    return {"ok": True, "error": None}

def _bingx_order_spot(symbol, side, qty):
    r = bingx_post(f"{BINGX_SPOT}/trade/order", {
        "symbol": bingx_symbol(symbol), "side": side.upper(),
        "type": "MARKET", "quantity": qty})
    if r and r.get("retCode") in (0, None):
        res = r.get("result") or {}
        return {"ok": True, "order_id": str(res.get("orderId", ""))}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def _bingx_positions_norm():
    """Devolve as posições no MESMO formato que broker_positions() da Bybit,
    pra sincronizar_tracking()/close_*/{posicoes} continuarem funcionando
    sem mudança."""
    r = bingx_get(f"{BINGX_SWAP}/user/positions", {})
    if not r or r.get("retCode") not in (0, None): return None
    lst = []
    for p in (r.get("result") or []):
        try:
            amt = float(p.get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            continue
        if amt == 0: continue
        avg_price = float(p.get("avgPrice", 0) or 0)
        # nome exato do campo de preço atual/PnL na BingX ainda não
        # confirmado contra a API real — tenta os nomes mais prováveis
        # (padrão Binance/BingX), sem quebrar se nenhum bater (só
        # mostra 0, igual já era o comportamento antes disso existir).
        mark_price = 0
        for campo in ("markPrice", "marketPrice"):
            try:
                mark_price = float(p.get(campo) or 0)
                if mark_price: break
            except (TypeError, ValueError):
                pass
        pnl = 0
        for campo in ("unrealizedProfit", "unRealizedProfit", "profitUnreal"):
            try:
                pnl = float(p.get(campo) or 0)
                if pnl: break
            except (TypeError, ValueError):
                pass
        try:
            leverage = float(p.get("leverage", 0) or 0) or BINGX_LEVERAGE
        except (TypeError, ValueError):
            leverage = BINGX_LEVERAGE
        margem = (abs(amt) * avg_price / leverage) if leverage else 0
        lst.append({
            "symbol": str(p.get("symbol", "")).replace("-", ""),
            "side": "Buy" if amt > 0 else "Sell",
            "size": str(abs(amt)),
            "avgPrice": str(avg_price),
            "markPrice": str(mark_price),
            "unrealisedPnl": str(pnl),
            "positionIM": str(margem),
            "stopLoss": str(p.get("stopLoss", "") or ""),
            "takeProfit": str(p.get("takeProfit", "") or ""),
        })
    return {"retCode": 0, "result": {"list": lst}}

def _bingx_account_norm():
    r = bingx_get(f"{BINGX_SWAP}/user/balance", {})
    if not r or r.get("retCode") not in (0, None): return None
    bal = r.get("result") or {}
    if isinstance(bal, dict) and "balance" in bal:
        bal = bal["balance"]
    if isinstance(bal, list):
        bal = bal[0] if bal else {}
    return {"retCode": 0, "result": {"list": [{
        "totalEquity": str(bal.get("equity", 0) or 0),
        "totalAvailableBalance": str(bal.get("availableMargin",
                                    bal.get("balance", 0)) or 0),
        "totalMarginBalance": str(bal.get("balance", 0) or 0),
        "coin": [{"coin": "USDT",
                  "equity": str(bal.get("equity", 0) or 0),
                  "walletBalance": str(bal.get("balance", 0) or 0),
                  "availableToWithdraw": str(bal.get("availableMargin", 0) or 0)}],
    }]}}

def _bingx_last_price(symbol):
    d = bingx_get(f"{BINGX_SWAP}/quote/price",
                  {"symbol": bingx_symbol(symbol)}, signed=False)
    if not d or d.get("retCode") not in (0, None): return None
    res = d.get("result") or {}
    if isinstance(res, list): res = res[0] if res else {}
    try: return float(res.get("price"))
    except (TypeError, ValueError): return None

def _bingx_close_symbol(symbol):
    r = bingx_post(f"{BINGX_SWAP}/trade/closeAllPositions",
                   {"symbol": bingx_symbol(symbol)})
    ok = bool(r and r.get("retCode") in (0, None))
    return ok, ("posicao fechada" if ok else f"erro: {r.get('retMsg','?') if r else 'sem resposta'}")

# ═══════════════════════════════════════════════════════════════

def set_leverage(symbol, exchange=None):
    usando_bingx = (exchange == "bingx") if exchange else USANDO_BINGX
    if usando_bingx:
        return _bingx_set_leverage(symbol)
    if symbol in _leverage_set["bybit"]: return
    r = bybit_post("/v5/position/set-leverage", {
        "category": "linear", "symbol": symbol,
        "buyLeverage": str(BYBIT_LEVERAGE), "sellLeverage": str(BYBIT_LEVERAGE)})
    if r and r.get("retCode") in (0, 110043):
        _leverage_set["bybit"].add(symbol)

# ── Ordens ───────────────────────────────────────────────────
def order_spot(symbol, side, qty):
    if SIMULACAO:
        return {"ok": True, "order_id": f"SIM-{int(time.time()*1000)}"}
    if USANDO_BINGX:
        return _bingx_order_spot(symbol, side, qty)
    r = bybit_post("/v5/order/create", {
        "category": "spot", "symbol": symbol, "side": side,
        "orderType": "Market", "qty": str(qty), "timeInForce": "IOC"})
    if r and r.get("retCode") == 0:
        return {"ok": True, "order_id": r["result"].get("orderId", "")}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def _bybit_garantir_hedge(symbol):
    """Bybit: mode 3 = hedge (posições nos dois sentidos no mesmo par)."""
    if _hedge_verificado["bybit"] or not ARBITRAGEM_ATIVA or simulacao_de("bybit"):
        return
    r = bybit_post("/v5/position/switch-mode", {
        "category": "linear", "symbol": symbol, "mode": 3})
    if r and r.get("retCode") in (0, 110025):   # 110025 = já está nesse modo
        print("[BYBIT] hedge mode ativo.")
    else:
        print(f"[BYBIT] não consegui ativar hedge: {r.get('retMsg','?') if r else 'sem resposta'}")
    _hedge_verificado["bybit"] = True


def order_futures(symbol, side, qty, sl=None, tp=None, exchange=None):
    usando_bingx = (exchange == "bingx") if exchange else USANDO_BINGX
    exch_efetiva = exchange or ("bingx" if usando_bingx else "bybit")
    if simulacao_de(exch_efetiva):
        print(f"[SIMULACAO] {exch_efetiva} {side} {qty} {symbol} SL={sl} TP={tp} — ordem NÃO enviada.")
        return {"ok": True, "order_id": f"SIM-{int(time.time()*1000)}"}
    if usando_bingx:
        return _bingx_order_futures(symbol, side, qty, sl=sl, tp=tp)
    _bybit_garantir_hedge(symbol)
    set_leverage(symbol, exchange)
    p = {"category": "linear", "symbol": symbol, "side": side,
         "orderType": "Market", "qty": str(qty), "timeInForce": "IOC"}
    if ARBITRAGEM_ATIVA:
        p["positionIdx"] = 1 if side == "Buy" else 2   # 1=long, 2=short (hedge)
    if sl: p["stopLoss"]   = str(round(float(sl), 6))
    if tp: p["takeProfit"] = str(round(float(tp), 6))
    r = bybit_post("/v5/order/create", p)
    if r and r.get("retCode") == 0:
        return {"ok": True, "order_id": r["result"].get("orderId", "")}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def order_limit(category, symbol, side, qty, price, sl=None, tp=None):
    if SIMULACAO:
        return {"ok": True, "order_id": f"SIM-{int(time.time()*1000)}"}
    if USANDO_BINGX:
        pos_side = ("LONG" if side.upper() == "BUY" else "SHORT") if ARBITRAGEM_ATIVA else "BOTH"
        p = {"symbol": bingx_symbol(symbol), "side": side.upper(),
             "positionSide": pos_side, "type": "LIMIT",
             "quantity": qty, "price": round(float(price), 6),
             "timeInForce": "GTC"}
        if sl and category == "linear":
            p["stopLoss"] = json.dumps({"type": "STOP_MARKET",
                "stopPrice": round(float(sl), 6), "workingType": "MARK_PRICE"})
        if tp and category == "linear":
            p["takeProfit"] = json.dumps({"type": "TAKE_PROFIT_MARKET",
                "stopPrice": round(float(tp), 6), "workingType": "MARK_PRICE"})
        path = BINGX_SWAP if category == "linear" else BINGX_SPOT
        r = bingx_post(f"{path}/trade/order", p)
        if r and r.get("retCode") in (0, None):
            res = r.get("result") or {}
            oid = (res.get("order") or {}).get("orderId", "") if isinstance(res, dict) else ""
            return {"ok": True, "order_id": str(oid)}
        return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}
    if category == "linear": set_leverage(symbol)
    p = {"category": category, "symbol": symbol, "side": side,
         "orderType": "Limit", "qty": str(qty),
         "price": str(round(float(price), 6)), "timeInForce": "GTC"}
    if sl and category == "linear": p["stopLoss"]   = str(round(float(sl), 6))
    if tp and category == "linear": p["takeProfit"] = str(round(float(tp), 6))
    r = bybit_post("/v5/order/create", p)
    if r and r.get("retCode") == 0:
        return {"ok": True, "order_id": r["result"].get("orderId", "")}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def close_futures_symbol(symbol):
    if SIMULACAO:
        return True, "simulacao: nada a fechar na corretora"
    if USANDO_BINGX:
        ok, msg = _bingx_close_symbol(symbol)
        # cancela qualquer ordem condicional pendente que tenha sobrado
        # (ex: SL/TP de um /editar anterior) — sem isso ela fica reservando
        # margem/limite de risco na corretora mesmo com a posição já
        # fechada, e o PRÓXIMO sinal desse símbolo pode falhar com
        # "Insufficient margin" mesmo a conta tendo saldo livre de sobra.
        cancel_open_orders(symbol, "linear")
        return ok, msg
    r = bybit_get("/v5/position/list", {"category": "linear", "symbol": symbol})
    if not r or r.get("retCode") != 0: return False, "Erro ao buscar posicao"
    closed = 0
    for pos in r.get("result", {}).get("list", []):
        size = float(pos.get("size", 0))
        if size == 0: continue
        side_c = "Sell" if pos["side"] == "Buy" else "Buy"
        # positionIdx: 0 em one-way, 1/2 em hedge (arbitragem) — sem isso
        # a Bybit rejeita a ordem de fechamento quando a conta está em
        # hedge mode ("position idx not match position mode").
        bybit_post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side_c,
            "orderType": "Market", "qty": str(size), "positionIdx": pos.get("positionIdx", 0),
            "reduceOnly": True, "timeInForce": "IOC"})
        closed += 1
    cancel_open_orders(symbol, "linear")
    return closed > 0, f"{closed} posicao(oes) fechada(s)"

def close_futures_all():
    if SIMULACAO:
        return
    if USANDO_BINGX:
        pos = _bingx_positions_norm()
        if pos:
            for p in pos["result"]["list"]:
                _bingx_close_symbol(p["symbol"])
                cancel_open_orders(p["symbol"], "linear")
        return
    r = bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if not r or r.get("retCode") != 0: return
    for pos in r.get("result", {}).get("list", []):
        size = float(pos.get("size", 0))
        if size == 0: continue
        side_c = "Sell" if pos["side"] == "Buy" else "Buy"
        bybit_post("/v5/order/create", {
            "category": "linear", "symbol": pos["symbol"], "side": side_c,
            "orderType": "Market", "qty": str(size), "positionIdx": pos.get("positionIdx", 0),
            "reduceOnly": True, "timeInForce": "IOC"})
        cancel_open_orders(pos["symbol"], "linear")

def cancel_open_orders(symbol, category="linear"):
    if USANDO_BINGX:
        path = BINGX_SWAP if category == "linear" else BINGX_SPOT
        r = bingx_post(f"{path}/trade/allOpenOrders", {"symbol": bingx_symbol(symbol)})
        if r and r.get("retCode") in (0, None): return {"ok": True}
        return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}
    r = bybit_post("/v5/order/cancel-all", {"category": category, "symbol": symbol})
    if r and r.get("retCode") == 0: return {"ok": True}
    return {"ok": False, "error": (r.get("retMsg", "?") if r else "sem resposta")}

def _spot_dec(symbol):
    d = {"BTCUSDT":6,"ETHUSDT":5,"SOLUSDT":3,"BNBUSDT":3,
         "XRPUSDT":2,"DOGEUSDT":0,"ADAUSDT":1}
    return d.get(symbol, 4)

def _floor_qty(qty, symbol):
    f = 10 ** _spot_dec(symbol)
    return int(qty * f) / f

def sell_all_spot(symbol):
    coin = symbol.replace("USDT", "")
    r = bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if not r or r.get("retCode") != 0: return {"ok": False, "error": "erro saldo"}
    coins = r.get("result", {}).get("list", [{}])[0].get("coin", [])
    bal   = next((float(c.get("walletBalance", 0)) for c in coins if c["coin"] == coin), 0)
    if bal <= 0: return {"ok": False, "error": f"Sem saldo de {coin}"}
    qty = _floor_qty(bal * 0.999, symbol)
    if qty <= 0: return {"ok": False, "error": "Saldo insuficiente"}
    return order_spot(symbol, "Sell", qty)

def _futures_dec(symbol):
    """Casas decimais corretas pro qtyStep de FUTUROS, derivadas da quantidade
    fixa já configurada em SYMBOLS (validada e funcionando na Bybit) — NÃO usa
    _spot_dec, que é só pra ordens spot e tem uma granularidade bem mais fina
    (foi isso que causava 'Qty invalid' nas ordens automáticas)."""
    qty_ref = SYMBOLS.get(symbol, {}).get("qty", 1)
    s = f"{qty_ref:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0

def get_min_qty_real(symbol, exchange):
    """Mínimo de quantidade por ordem consultado DIRETO da corretora (não o
    QTY_BTC/etc fixo do .env, que era só um chute) — cacheado, uma consulta
    por par/corretora na vida do processo. Se a consulta falhar por
    qualquer motivo, devolve None e quem chamar usa o piso configurado de
    antes — nunca trava o bot por causa disso."""
    chave = (exchange, symbol)
    if chave in _min_qty_cache:
        return _min_qty_cache[chave]
    minimo = None
    try:
        if exchange == "bingx":
            r = bingx_get(f"{BINGX_SWAP}/quote/contracts", signed=False)
            if r and r.get("retCode") in (0, None):
                alvo = bingx_symbol(symbol)
                for c in (r.get("result") or []):
                    if c.get("symbol") != alvo:
                        continue
                    for campo in ("tradeMinQuantity", "minQty", "size"):
                        try:
                            f = float(c.get(campo))
                            if f > 0:
                                minimo = f
                                break
                        except (TypeError, ValueError):
                            continue
                    break
        else:
            r = bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
            if r and r.get("retCode") == 0:
                lst = r.get("result", {}).get("list", [])
                if lst:
                    try:
                        f = float(lst[0].get("lotSizeFilter", {}).get("minOrderQty"))
                        if f > 0:
                            minimo = f
                    except (TypeError, ValueError):
                        pass
    except Exception as e:
        print(f"[MIN_QTY] {exchange} {symbol}: {e}")
    _min_qty_cache[chave] = minimo
    return minimo

# folga extra do stop técnico em cima do spread real (pedido do Jon: o
# stop nunca pode colar exatamente na origem — precisa sobreviver ao
# spread da corretora e a um topo/fundo duplo raspando o nível, não só
# à folga percentual da pernada). Sem cache: spread muda o tempo todo,
# diferente do mínimo de qty.
SPREAD_FOLGA_MULT = float(os.environ.get("SPREAD_FOLGA_MULT", "2.0"))

def get_spread_real(symbol, exchange):
    """Spread real (ask - bid) consultado direto da corretora. Se a
    consulta falhar ou vier um valor sem sentido, devolve None — quem
    chamar (stop_tecnico) simplesmente não aplica esse piso extra e
    segue com a folga percentual de sempre, nunca trava o bot."""
    try:
        if exchange == "bingx":
            r = bingx_get(f"{BINGX_SWAP}/quote/bookTicker",
                          {"symbol": bingx_symbol(symbol)}, signed=False)
            if r and r.get("retCode") in (0, None):
                res = r.get("result") or {}
                if isinstance(res, list): res = res[0] if res else {}
                bid = float(res.get("bidPrice") or 0)
                ask = float(res.get("askPrice") or 0)
                if bid > 0 and ask > bid:
                    return ask - bid
        else:
            r = bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            if r and r.get("retCode") == 0:
                lst = r.get("result", {}).get("list", [])
                if lst:
                    bid = float(lst[0].get("bid1Price") or 0)
                    ask = float(lst[0].get("ask1Price") or 0)
                    if bid > 0 and ask > bid:
                        return ask - bid
    except (TypeError, ValueError, KeyError, IndexError) as e:
        print(f"[SPREAD] {exchange} {symbol}: {e}")
    return None

def _folga_spread_extra(symbol, preco):
    """Maior spread real entre as corretoras ativas agora, já multiplicado
    por SPREAD_FOLGA_MULT — vira um PISO extra pra folga do stop técnico
    (nunca reduz a folga percentual já calculada, só amplia quando o
    spread for maior que ela). Sanidade: ignora leitura > 1% do preço —
    spread de verdade não chega perto disso, é sinal de campo errado da
    API, e um valor assim explodiria o risco calculado sem necessidade."""
    maior = 0.0
    for exch in EXCHANGES_ATIVAS:
        sp = get_spread_real(symbol, exch)
        if sp and 0 < sp < preco * 0.01:
            maior = max(maior, sp)
    return maior * SPREAD_FOLGA_MULT

SALDO_SIMULADO = float(os.environ.get("SALDO_SIMULADO", "100"))

def get_saldo_usdt(exchange=None):
    """Saldo disponível pra abrir posição NOVA (usado pra travar a margem da
    ordem). Prioriza totalAvailableBalance (o campo que a própria Bybit usa
    pra julgar se cabe uma ordem nova) em vez de availableToWithdraw, que em
    contas UNIFIED costuma vir '0' mesmo com saldo livre pra margem — e como
    vem como STRING, um '0' antigo passava como "verdadeiro" no Python e
    desativava a trava de margem sem avisar."""
    if (simulacao_de(exchange) if exchange else SIMULACAO):
        return SALDO_SIMULADO
    r = broker_account(exchange)
    if not r or r.get("retCode") != 0: return None
    lst = r.get("result", {}).get("list", [])
    if not lst: return None
    acc = lst[0]
    for campo in ("totalAvailableBalance", "totalMarginBalance"):
        v = acc.get(campo)
        try:
            f = float(v)
            if f > 0: return f
        except (TypeError, ValueError):
            pass
    for c in acc.get("coin", []):
        if c.get("coin") == "USDT":
            for campo in ("availableToWithdraw", "walletBalance", "equity"):
                v = c.get(campo)
                try:
                    f = float(v)
                    if f > 0: return f
                except (TypeError, ValueError):
                    pass
    return None

def get_patrimonio_usdt():
    """Total de ativos da conta (equity), igual ao que a Bybit mostra na tela
    'Ativos' do app — diferente de get_saldo_usdt(), que é só o saldo
    DISPONÍVEL pra abrir posição nova (exclui margem já travada em trades
    abertos). Usado só pra EXIBIR o saldo nas mensagens, nunca pra calcular
    tamanho de posição."""
    if SIMULACAO:
        return SALDO_SIMULADO
    r = broker_account()
    if not r or r.get("retCode") != 0: return None
    lst = r.get("result", {}).get("list", [])
    if not lst: return None
    acc = lst[0]
    for campo in ("totalEquity", "totalWalletBalance", "totalMarginBalance", "totalAvailableBalance"):
        v = acc.get(campo)
        try:
            f = float(v)
            if f > 0: return f
        except (TypeError, ValueError):
            pass
    for c in acc.get("coin", []):
        if c.get("coin") == "USDT":
            for campo in ("equity", "walletBalance", "availableToWithdraw"):
                v = c.get(campo)
                try:
                    f = float(v)
                    if f > 0: return f
                except (TypeError, ValueError):
                    pass
    return None

def calc_qty(symbol, entry, stop, exchange=None):
    """Tamanho da posição pelo risco em preço (distância até o stop técnico).
    SEM travas de valor: não descarta sinal por ser 'pequeno' ou 'grande',
    nunca veta uma entrada. Se não der pra calcular, cai no tamanho mínimo
    configurado do par. `exchange` deixa calcular pelo saldo de uma
    corretora específica (multi-corretora).

    Teto pela MARGEM disponível: com stop muito apertado (técnico, colado
    na origem), o dimensionamento por risco % pode pedir uma quantidade
    cujo nocional estoura o saldo da conta — a corretora rejeita a ordem
    INTEIRA com "Insufficient margin" (visto na prática numa conta de
    $50). Isso não é uma trava de risco escolhida pelo bot, é um limite
    físico da corretora — sem o teto, o sinal simplesmente falha por
    completo em vez de abrir menor. Com o teto, a ordem sempre abre,
    só que do tamanho que a margem realmente permite."""
    qty_cfg = SYMBOLS.get(symbol, {}).get("qty", 0)
    # piso operacional: o maior entre o qty configurado (.env) e o mínimo
    # REAL daquela corretora pro par — evita "Qty invalid" quando o .env
    # ficou desatualizado ou nunca bateu com o mínimo de verdade.
    minimo_real = get_min_qty_real(symbol, exchange) if exchange else None
    piso = max(qty_cfg, minimo_real) if minimo_real else qty_cfg
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return piso or None

    saldo_usdt = get_saldo_usdt(exchange)
    if not saldo_usdt or saldo_usdt <= 0:
        return piso or None

    # dimensiona pelo risco percentual, mas sem NUNCA vetar a entrada
    qty = (saldo_usdt * RISCO_PCT) / dist
    qty = round(qty, _futures_dec(symbol))

    # /lote: multiplicador ou quantidade fixa por cima do cálculo por risco.
    # Ajustado no Telegram, sobrevive a reinício (memory["config_lote"]).
    # Sem trava de valor — só muda o tamanho, quem decide entrar é o cenário.
    cfg_lote = memory.get("config_lote", {"modo": "auto"})
    if cfg_lote.get("modo") == "fixo":
        qty = cfg_lote.get("valor", qty)
    elif cfg_lote.get("modo") == "mult":
        qty = round(qty * cfg_lote.get("valor", 1), _futures_dec(symbol))

    # teto pela margem disponível (95% de folga pra taxa/slippage) — só
    # reduz quando o cálculo por risco pediu mais do que a conta aguenta
    # com a alavancagem configurada.
    leverage = leverage_de(exchange)
    if leverage and entry > 0 and saldo_usdt > 0:
        teto_margem = round((saldo_usdt * leverage / entry) * 0.95, _futures_dec(symbol))
        if teto_margem > 0:
            qty = min(qty, teto_margem)

    if qty < piso:
        qty = piso          # piso operacional do par, não uma trava de risco
    return qty if qty > 0 else (piso or None)

def lote_texto():
    """Rótulo do modo de lote ativo agora, pra mostrar no sinal e no /status."""
    cfg = memory.get("config_lote", {"modo": "auto"})
    modo = cfg.get("modo", "auto")
    if modo == "fixo":
        return f"🔧 FIXO {cfg.get('valor')}"
    if modo == "mult":
        return f"✖️ {cfg.get('valor')}x"
    return "🧮 AUTO (risco)"

def freio_diario_ok():
    """Sem freio por valor. O bot opera livre — quem decide é o cenário
    gráfico, não um percentual de perda. Mantida só pra compatibilidade
    com o resto do código, sempre retorna True."""
    return True

def broker_open_auto(symbol, direction, stop, target, qty=None, exchange=None):
    side = "Buy" if direction == "BUY" else "Sell"
    qty_final = qty if qty else SYMBOLS[symbol]["qty"]
    return order_futures(symbol, side, qty_final, sl=stop, tp=target, exchange=exchange)
# ══════════════════════════════════════════════════════════════

def broker_account(exchange=None):
    usando_bingx = (exchange == "bingx") if exchange else USANDO_BINGX
    if usando_bingx:
        return _bingx_account_norm()
    return bybit_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})

def broker_positions(exchange=None):
    usando_bingx = (exchange == "bingx") if exchange else USANDO_BINGX
    if usando_bingx:
        return _bingx_positions_norm()
    return bybit_get("/v5/position/list", {"category": "linear", "settleCoin": "USDT", "limit": 200})

def _parse_sym(s):
    s = s.upper()
    return s if s.endswith("USDT") else s + "USDT"

def _bybit_pnl_real(symbol, direcao):
    """PnL REAL (líquido de taxa) do fechamento mais recente desse
    símbolo/lado na Bybit. Casa pelo lado + horário mais próximo de
    agora — não dá pra casar por ID direto, o closed-pnl é indexado
    pela ordem de FECHAMENTO, e o único ID que a gente guarda é o da
    ABERTURA."""
    r = bybit_get("/v5/position/closed-pnl", {"category": "linear", "symbol": symbol, "limit": 10})
    if not r or r.get("retCode") != 0:
        return None
    lado = "Buy" if direcao == "BUY" else "Sell"
    candidatos = [c for c in r.get("result", {}).get("list", []) if c.get("side") == lado]
    if not candidatos:
        return None
    agora_ms = int(time.time() * 1000)
    melhor = min(candidatos, key=lambda c: abs(int(c.get("updatedTime", 0) or 0) - agora_ms))
    if abs(int(melhor.get("updatedTime", 0) or 0) - agora_ms) > 5 * 60 * 1000:
        return None  # nada recente o suficiente pra confiar que é ESSE fechamento
    try:
        return float(melhor["closedPnl"])
    except (KeyError, ValueError, TypeError):
        return None

def _bingx_pnl_real(symbol, direcao):
    """Mesma ideia de _bybit_pnl_real, via histórico de income da BingX.
    AINDA NÃO CONFIRMADO contra a API real (endpoint/campos por
    verificar) — só usado com fallback seguro em pnl_real(), nunca
    quebra nem inventa valor errado se vier vazio/no formato errado."""
    r = bingx_get(f"{BINGX_SWAP}/user/income",
                  {"symbol": bingx_symbol(symbol), "incomeType": "REALIZED_PNL", "limit": 10})
    if not r or r.get("retCode") not in (0, None):
        return None
    registros = r.get("result") or []
    if not isinstance(registros, list) or not registros:
        return None
    agora_ms = int(time.time() * 1000)
    try:
        melhor = min(registros, key=lambda c: abs(int(c.get("time", 0) or 0) - agora_ms))
        if abs(int(melhor.get("time", 0) or 0) - agora_ms) > 5 * 60 * 1000:
            return None
        return float(melhor["income"])
    except (KeyError, ValueError, TypeError):
        return None

def pnl_real(symbol, direcao, exchange):
    """PnL de verdade (líquido de taxa/slippage) do fechamento que
    ACABOU de acontecer nessa corretora — usado só pra sinais reais
    (nunca simulados, não existe closed-pnl de ordem fake). Nunca
    lança exceção nem afeta o tracking: se falhar por qualquer razão,
    quem chamou cai pro cálculo estimado de sempre."""
    try:
        if exchange == "bingx":
            return _bingx_pnl_real(symbol, direcao)
        return _bybit_pnl_real(symbol, direcao)
    except Exception as e:
        print(f"[PNL_REAL] {exchange} {symbol}: {e}")
        return None

def sincronizar_tracking(symbol, sl_informado=None, tp_informado=None, origem="MANUAL", exchange=None):
    """Depois de QUALQUER ordem manual que mexe numa posição de futuros,
    sincroniza o rastreamento interno (memory['signals']) com o que
    REALMENTE está na corretora agora. Sem isso, um registro velho de uma
    entrada automática anterior fica esquecido lá dentro com um alvo/stop
    diferente do que você acabou de configurar — e quando o preço bate
    nesse alvo velho, o bot manda uma notificação de TAKE PROFIT/STOP
    LOSS falsa, sem fechar nada de verdade na corretora. Isso resolve
    isso: existe no máximo UM registro aberto POR LADO (compra/venda) em
    cada símbolo, e cada um sempre reflete a posição real daquele lado
    (preço médio, tamanho, SL, TP) — com ARBITRAGEM_ATIVA, compra e
    venda podem estar abertas ao mesmo tempo no mesmo par, então nunca
    fundir/cancelar um lado por causa do outro. Também nunca mexe no
    rastreamento de OUTRA corretora: com EXCHANGES_ATIVAS tendo mais de
    uma, cada chamada só enxerga/cancela/funde sinais marcados com essa
    MESMA exchange (registros antigos sem o campo contam como da
    corretora primária, EXCHANGE — mesma convenção usada no resto do
    código)."""
    exch_efetiva = exchange or EXCHANGE
    def _mesma_exchange(s):
        return s.get("exchange", EXCHANGE) == exch_efetiva

    r = broker_positions(exchange)
    posicoes = []
    if r and r.get("retCode") == 0:
        posicoes = [p for p in r.get("result", {}).get("list", [])
                    if p["symbol"] == symbol and float(p.get("size", 0)) > 0]

    if not posicoes:
        # não tem posição aberta NESSA corretora (fechou na hora, ou é
        # spot) — cancela só os registros velhos "aberto" desse símbolo
        # QUE SÃO DESSA corretora, pra não sobrar fantasma rastreado sem
        # posição real por trás (preserva o que outra corretora tiver).
        for s in memory.get("signals", []):
            if s["symbol"] == symbol and s["status"] == "aberto" and _mesma_exchange(s):
                s["status"] = "cancelado"; s["resultado"] = "sem posição real"
        save_memory()
        return

    # sl_informado/tp_informado (vindo do /editar) só valem quando existe
    # UMA posição só nesse símbolo NESSA corretora — com os dois lados
    # abertos ao mesmo tempo não dá pra saber qual dos dois o SL/TP
    # informado é sobre.
    aplicar_informado = len(posicoes) == 1
    direcoes_reais = set()

    for pos in posicoes:
        direcao = "BUY" if pos["side"] == "Buy" else "SELL"
        direcoes_reais.add(direcao)
        entrada = float(pos.get("avgPrice", 0))
        qty     = float(pos.get("size", 0))
        sl = (float(sl_informado) if (aplicar_informado and sl_informado)
              else (float(pos["stopLoss"]) if pos.get("stopLoss") else None))
        tp = (float(tp_informado) if (aplicar_informado and tp_informado)
              else (float(pos["takeProfit"]) if pos.get("takeProfit") else None))

        # funde/atualiza só os registros abertos DO MESMO LADO NESSA
        # corretora — nunca mistura compra com venda, nem uma corretora
        # com outra.
        abertos = [s for s in memory.get("signals", [])
                   if s["symbol"] == symbol and s["status"] == "aberto"
                   and s["direcao"] == direcao and _mesma_exchange(s)]
        if abertos:
            principal = max(abertos, key=lambda s: s["id"])
            for s in abertos:
                if s["id"] != principal["id"]:
                    s["status"] = "cancelado"; s["resultado"] = "fundido"
        else:
            novo_id = memory.get("next_id", len(memory["signals"])+1)
            memory["next_id"] = novo_id + 1
            principal = {"id": novo_id, "symbol": symbol, "direcao": direcao,
                         "exchange": exch_efetiva,
                         "entrada": entrada, "stop": sl or entrada, "alvo": tp or entrada,
                         "risco": 0, "rr": 0, "qty_usada": qty, "atr": 0, "origem": origem,
                         "order_id": "", "data": agora_br().strftime("%d/%m/%Y %H:%M"),
                         "status": "aberto", "resultado": None}
            memory["signals"].append(principal)
            if len(memory["signals"]) > 200: memory["signals"] = memory["signals"][-200:]
        principal["direcao"] = direcao; principal["entrada"] = entrada; principal["qty_usada"] = qty
        principal["exchange"] = exch_efetiva
        if sl: principal["stop"] = sl
        if tp: principal["alvo"] = tp

    # lado que tinha registro aberto NESSA corretora mas não existe mais
    # lá (ex: fechou só um dos dois lados da arbitragem) — cancela só
    # ele, preserva o outro lado e qualquer registro de outra corretora.
    for s in memory.get("signals", []):
        if (s["symbol"] == symbol and s["status"] == "aberto"
                and s["direcao"] not in direcoes_reais and _mesma_exchange(s)):
            s["status"] = "cancelado"; s["resultado"] = "sem posição real"

    save_memory()

def sincronizar_fechamento(symbol):
    """Depois de fechar manualmente uma posição (/fechar), marca o
    registro aberto correspondente com o resultado real (win/loss),
    usando o preço de mercado atual como saída — em vez de deixar
    'aberto' pra sempre no rastreamento ou perder o resultado."""
    preco = get_last_price(symbol)
    alt = False
    for s in memory.get("signals", []):
        if s["symbol"] == symbol and s["status"] == "aberto":
            if preco:
                s["preco_saida"] = preco
                s["fechamento"] = agora_br().strftime("%d/%m/%Y %H:%M")
                lucro = (preco - s["entrada"]) if s["direcao"] == "BUY" else (s["entrada"] - preco)
                s["status"] = "win" if lucro >= 0 else "loss"
                s["resultado"] = fmt_brl(resultado_brl(s))
            else:
                s["status"] = "cancelado"; s["resultado"] = "fechado manualmente"
            alt = True
    if alt: save_memory()

# ─── KRAKEN DATA ─────────────────────────────────────────────
TF_MAP = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}

def _bybit_candles(symbol, tf, limit):
    """Fallback: candles via Bybit public API."""
    interval_map = {"1m":1,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}
    iv = interval_map.get(tf, 60)
    try:
        r = requests.get("https://api.bybit.com/v5/market/kline",
            params={"category":"linear","symbol":symbol,
                    "interval":str(iv),"limit":str(limit)}, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("retCode") != 0: raise Exception(d.get("retMsg","?"))
        rows = d["result"]["list"]
        rows = list(reversed(rows))
        return [{"open":float(k[1]),"high":float(k[2]),
                 "low":float(k[3]),"close":float(k[4])}
                for k in rows]
    except Exception as e:
        print(f"[BYBIT CANDLE] {symbol}/{tf}: {e}"); return []

# Pares que o Kraken nao suporta — usar Bybit
_BYBIT_ONLY = {"SUIUSDT","APTUSDT","OPUSDT","TONUSDT","PEPEUSDT",
               "NEARUSDT","ARBUSDT","AVAXUSDT"}

def get_candles(symbol, tf, limit=120):
    if USANDO_BINGX:
        c = _bingx_candles(symbol, tf, limit)
        if c: return c
        return _bybit_candles(symbol, tf, limit)   # fallback de dados públicos
    if symbol in _BYBIT_ONLY:
        return _bybit_candles(symbol, tf, limit)
    kp = SYMBOLS.get(symbol, {}).get("kraken", symbol)
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": kp, "interval": TF_MAP.get(tf, 60)}, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d.get("error") and d["error"]: raise Exception(str(d["error"]))
        key = [k for k in d["result"] if k != "last"][0]
        return [{"open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4])}
                for k in d["result"][key][-limit:]]
    except Exception as e:
        print(f"[KRAKEN] {symbol}/{tf}: {e}")
        return _bybit_candles(symbol, tf, limit)

# ─── GITHUB MEMORIA ──────────────────────────────────────────
def gh_h():
    return {"Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"}

def load_memory():
    """Carrega a memória. O arquivo LOCAL tem prioridade: ele é gravado a
    cada mudança, enquanto o GitHub recebe só backup periódico — então o
    local é sempre igual ou mais novo. O GitHub serve de recuperação
    quando o arquivo local não existe (máquina nova, Render, etc)."""
    global memory

    # 1) disco
    try:
        if os.path.exists(GITHUB_FILE):
            with open(GITHUB_FILE, encoding="utf-8") as f:
                memory = json.load(f)
            memory.setdefault("macro_views", {})
            memory.setdefault("next_id", len(memory.get("signals", [])) + 1)
            memory.setdefault("config_lote", {"modo": "auto"})
            print(f"[MEM] carregada do disco: {len(memory.get('signals', []))} sinais")
            return
    except Exception as e:
        print(f"[MEM local] arquivo ilegível ({e}) — tentando GitHub.")

    # 2) GitHub, como recuperação
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        r   = requests.get(url, headers=gh_h(), timeout=10)
        if r.status_code == 200:
            memory = json.loads(base64.b64decode(r.json()["content"]).decode())
            memory.setdefault("macro_views", {})
            memory.setdefault("next_id", len(memory.get("signals", [])) + 1)
            memory.setdefault("config_lote", {"modo": "auto"})
            print(f"[MEM] recuperada do GitHub: {len(memory.get('signals', []))} sinais")
            _salvar_local()
    except Exception as e: print(f"[MEM] {e}")

# Intervalo mínimo entre dois envios da memória pro GitHub (segundos).
# Antes cada save_memory() virava um commit — 12 pontos de chamada, um
# commit por trade/sinal/análise, o que gerou dezenas de milhares de
# commits e inchou o repositório. Agora o disco é a fonte de verdade e
# o GitHub recebe só um backup periódico.
GITHUB_SYNC_SEG = int(os.environ.get("GITHUB_SYNC_SEG", "3600"))
_ultimo_push_gh = 0.0

def _salvar_local():
    """Grava a memória no disco. É rápido, não depende de rede e é o que
    o bot relê ao reiniciar (load_memory tenta o GitHub primeiro, mas o
    arquivo local é o mais recente)."""
    try:
        tmp = f"{GITHUB_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
        os.replace(tmp, GITHUB_FILE)   # troca atômica: nunca deixa arquivo pela metade
        return True
    except Exception as e:
        print(f"[MEM local] {e}")
        return False


def _push_github(forcar=False):
    """Envia a memória pro GitHub, no máximo uma vez a cada
    GITHUB_SYNC_SEG. forcar=True ignora o intervalo."""
    global _ultimo_push_gh
    if not GITHUB_TOKEN or not GITHUB_REPO: return
    agora = time.time()
    if not forcar and (agora - _ultimo_push_gh) < GITHUB_SYNC_SEG:
        return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        ct  = base64.b64encode(json.dumps(memory, indent=2, ensure_ascii=False).encode()).decode()
        r   = requests.get(url, headers=gh_h(), timeout=10)
        n_sinais = len(memory.get("signals", []))
        pl  = {"message": f"backup memoria ({n_sinais} sinais)", "content": ct}
        if r.status_code == 200: pl["sha"] = r.json()["sha"]
        resp = requests.put(url, headers=gh_h(), json=pl, timeout=15)
        if resp.status_code in (200, 201):
            _ultimo_push_gh = agora
            print(f"[MEM] backup enviado ao GitHub ({n_sinais} sinais).")
        else:
            print(f"[MEM push] HTTP {resp.status_code}")
    except Exception as e:
        print(f"[MEM push] {e}")


def save_memory(forcar_github=False):
    """Salva local sempre; sincroniza com o GitHub só de tempos em tempos."""
    _salvar_local()
    _push_github(forcar=forcar_github)

# ─── GROQ VISION ─────────────────────────────────────────────
VISION_PROMPT = ('Analise este grafico de trading. Retorne APENAS JSON valido:\n'
                 '{"timeframe":"","tendencia":"up/down/neutral","tipo_onda":"",'
                 '"nivel_entrada":0,"nivel_stop":0,"nivel_alvo":0,"correcao_pct":0,'
                 '"observacoes":"","padroes":[],"qualidade_setup":"alta/media/baixa"}')

def analyze_image(img_bytes):
    if not GROQ_KEY: raise Exception("GROQ_API_KEY nao configurada")
    b64 = base64.b64encode(img_bytes).decode()
    r = requests.post("https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json={"model": "meta-llama/llama-4-scout-17b-16e-instruct",
              "messages": [{"role": "user", "content": [
                  {"type": "text", "text": VISION_PROMPT},
                  {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
              ]}], "max_tokens": 800, "temperature": 0.1}, timeout=30)
    r.raise_for_status()
    t = r.json()["choices"][0]["message"]["content"].strip()
    return json.loads(t.replace("```json", "").replace("```", "").strip())

def process_image(img_bytes, chat_id, caption=""):
    send_telegram("Analisando grafico...", chat_id)
    try: a = analyze_image(img_bytes)
    except Exception as e: send_telegram(f"Erro: {e}", chat_id); return
    a["data"] = agora_br().strftime("%d/%m/%Y %H:%M")
    memory["analyses"].append(a)
    if len(memory["analyses"]) > 100: memory["analyses"] = memory["analyses"][-100:]
    memory["total_prints"] += 1; memory["last_update"] = a["data"]
    save_memory()
    tend = a.get("tendencia", "—"); qual = a.get("qualidade_setup", "—")
    te = "📈" if tend == "up" else ("📉" if tend == "down" else "↔️")
    qe = "🟢" if qual == "alta" else ("🟡" if qual == "media" else "🔴")
    ep = a.get("nivel_entrada"); sp = a.get("nivel_stop"); alvo = a.get("nivel_alvo")
    msg = (f"✅ <b>Grafico analisado!</b>\n📊 {a.get('timeframe','—')} | {tend.upper()} {te}\n"
           f"{qe} Qualidade: <b>{qual.upper()}</b>\n💡 {a.get('observacoes','—')}\n")
    if ep:   msg += f"💰 ${float(ep):,.4f}\n"
    if sp:   msg += f"🛑 ${float(sp):,.4f}\n"
    if alvo: msg += f"🎯 ${float(alvo):,.4f}\n"
    msg += f"🧠 {memory['total_prints']} prints"
    send_telegram(msg, chat_id)

# ─── ANALISE TECNICA ─────────────────────────────────────────
def ema(values, period):
    """Média móvel exponencial — padrão de mercado pra medir tendência."""
    if len(values) < period: return [None]*len(values)
    k = 2/(period+1)
    out = [None]*(period-1)
    m = sum(values[:period])/period
    out.append(m)
    for v in values[period:]:
        m = v*k + m*(1-k)
        out.append(m)
    return out

def rsi(closes, period=14):
    """RSI clássico (Wilder) — mede força/exaustão do movimento."""
    if len(closes) < period+1: return [None]*len(closes)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    out = [None]*period
    avg_g = sum(gains[1:period+1])/period
    avg_l = sum(losses[1:period+1])/period
    out.append(100 if avg_l==0 else 100-(100/(1+avg_g/avg_l)))
    for i in range(period+1, len(closes)):
        avg_g = (avg_g*(period-1)+gains[i])/period
        avg_l = (avg_l*(period-1)+losses[i])/period
        out.append(100 if avg_l==0 else 100-(100/(1+avg_g/avg_l)))
    return out

def atr(candles, period=14):
    """Average True Range — mede volatilidade real do ativo, usado pra
    calcular stop/alvo proporcionais ao movimento normal de cada par
    (em vez de um valor fixo em R$ que não faz sentido pra todo ativo)."""
    if len(candles) < period+1: return [None]*len(candles)
    trs = [None]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    out = [None]*period
    m = sum(trs[1:period+1])/period
    out.append(m)
    for i in range(period+1, len(candles)):
        m = (m*(period-1)+trs[i])/period
        out.append(m)
    return out

_fng_cache = {"valor": None, "classificacao": None, "ts": 0}
def get_fear_greed():
    """Índice Fear & Greed (alternative.me) — API pública, gratuita, sem chave.
    Usado só como filtro de sanidade: evita abrir posição nova num momento de
    euforia/pânico extremo do mercado (historicamente mais instável/imprevisível)."""
    agora = time.time()
    if agora - _fng_cache["ts"] < 3600 and _fng_cache["valor"] is not None:
        return _fng_cache["valor"], _fng_cache["classificacao"]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        v = int(d["value"]); c = d["value_classification"]
        _fng_cache.update({"valor": v, "classificacao": c, "ts": agora})
        return v, c
    except Exception:
        return _fng_cache["valor"], _fng_cache["classificacao"]

def engolfo_alta(c, i):
    if i < 1: return False
    p, a = c[i-1], c[i]
    return p["close"]<p["open"] and a["close"]>a["open"] and a["close"]>=p["open"] and a["open"]<=p["close"]

def engolfo_baixa(c, i):
    if i < 1: return False
    p, a = c[i-1], c[i]
    return p["close"]>p["open"] and a["close"]<a["open"] and a["open"]>=p["close"] and a["close"]<=p["open"]

# ═══════════════════════════════════════════════════════════════
#  VISÃO MACRO (M1 dentro de um cenário maior definido manualmente)
# ═══════════════════════════════════════════════════════════════
# Isso é um caminho de entrada A MAIS, em paralelo ao M15/M5 que já
# funciona — não altera em nada a lógica existente. A ideia: em vez do
# robô só operar o padrão fixo M15/M5, o usuário manda pra ele a leitura
# macro que já fez visualmente (diário/H4 puxando um cenário, com stop e
# alvo maiores condizentes com essa perna maior) via /macro. O robô então
# fica de olho no M1 esperando o gatilho que o usuário descreveu: uma
# pernadinha se formar e corrigir por volta de 50% (tolerância, não
# precisa ser perfeita) na direção do cenário, confirmada por um candle
# fechando a favor — e dispara a entrada usando o MESMO fire_signal() de
# sempre (mesmo tracking, mesmo saldo, mesmo freio diário, etc.), só que
# com o stop/alvo dessa visão maior em vez do ATR fixo do M15/M5.
def _pivots_m1(candles, lado=2):
    """Pivots simples de topo/fundo local num raio de `lado` candles."""
    piv = []
    n = len(candles)
    for i in range(lado, n - lado):
        janela = candles[i-lado:i+lado+1]
        h, l = candles[i]["high"], candles[i]["low"]
        if h == max(c["high"] for c in janela):
            piv.append((i, "high", h))
        if l == min(c["low"] for c in janela):
            piv.append((i, "low", l))
    return piv

# ═══════════════════════════════════════════════════════════════
#  GATILHOS DE M1 — três padrões independentes.
#  Qualquer um deles disparando já autoriza a entrada, desde que a
#  direção bata com o contexto do tempo gráfico maior.
#  IMPORTANTE: todos consideram PAVIO (high/low), não só o corpo —
#  o pavio frequentemente É o movimento de correção rápido de 50%.
# ═══════════════════════════════════════════════════════════════

def _limpa_pivots(piv):
    """_pivots_m1 pode devolver topos/fundos repetidos em sequência (o mesmo
    extremo detectado em candles vizinhos). Isso quebrava a leitura da perna.
    Aqui garantimos alternância topo/fundo, mantendo sempre o extremo mais
    relevante de cada sequência."""
    if not piv: return []
    limpo = [piv[0]]
    for p in piv[1:]:
        ult = limpo[-1]
        if p[1] == ult[1]:
            if (p[1] == "high" and p[2] > ult[2]) or (p[1] == "low" and p[2] < ult[2]):
                limpo[-1] = p
        else:
            limpo.append(p)
    return limpo


# "no mínimo 50%", com folga pra baixo — pavios raramente batem exato.
# Ajustável: TOL_CORRECAO_MIN=0.5 no .env deixa mais rígido.
TOL_CORRECAO = (float(os.environ.get("TOL_CORRECAO_MIN", "0.45")), 1.0)
# quão perto os 3 topos/fundos precisam estar entre si (0.2% do preço)
TOL_TRES_TOPOS = float(os.environ.get("TOL_TRES_TOPOS", "0.002"))

def _corrigiu_50(alto, baixo, preco, direcao, tol=TOL_CORRECAO):
    """Quanto o preço já retraiu de um movimento (alto->baixo ou baixo->alto).
    direcao BUY = veio de baixo pra cima e está corrigindo pra baixo."""
    tam = alto - baixo
    if tam <= 0: return None
    # A direção é a da ORDEM. Numa compra, veio uma perna de alta e o preço
    # está corrigindo pra baixo -> retração medida a partir do topo.
    retr = (alto - preco) / tam if direcao == "BUY" else (preco - baixo) / tam
    return retr if tol[0] <= retr <= tol[1] else None


def gatilho_candle_retracao(c1, direcao):
    """GATILHO 1 — nascimento de candle com retração rápida.
    O candle anterior fez um movimento forte; o candle atual nasce
    corrigindo 50% do RANGE dele (pavio incluído). Entrada no preço atual."""
    if len(c1) < 3: return None
    ant  = c1[-2]          # candle que fez o movimento
    atual = c1[-1]         # candle nascendo agora
    alto, baixo = ant["high"], ant["low"]
    tam = alto - baixo
    if tam <= 0: return None

    # o candle anterior precisa ter sido um movimento de verdade, não um doji:
    # corpo relevante dentro do próprio range
    corpo = abs(ant["close"] - ant["open"])
    if corpo < tam * 0.30: return None

    ant_foi_alta = ant["close"] > ant["open"]
    # compra = candle anterior de alta corrigindo; venda = o inverso
    if direcao == "BUY" and not ant_foi_alta: return None
    if direcao == "SELL" and ant_foi_alta:    return None

    preco = atual["close"]
    retr = _corrigiu_50(alto, baixo, preco, direcao)
    if retr is None: return None
    return {"preco": preco, "desc": f"candle corrigindo {int(retr*100)}% do range anterior"}


def gatilho_pernada_50(c1, direcao, lado=2):
    """GATILHO 2 — pernada na simetria de Elliott corrigida em 50%+.
    Precisa de TRÊS pivots: origem -> extremo (a perna) -> ponto de correção.
    Ler só os dois últimos leria a própria correção como se fosse a perna.
    Usa pavios (high/low) dos pivots, não fechamentos: o extremo do pavio
    é o que define a perna de verdade, e muitas vezes o pavio já É a
    correção rápida de 50%."""
    if len(c1) < lado*2 + 5: return None
    piv = _limpa_pivots(_pivots_m1(c1, lado=lado))
    if len(piv) < 3: return None
    origem, extremo, _correcao = piv[-3], piv[-2], piv[-1]

    alto  = max(origem[2], extremo[2])
    baixo = min(origem[2], extremo[2])
    perna_foi_alta = (origem[1] == "low" and extremo[1] == "high")
    perna_foi_baixa = (origem[1] == "high" and extremo[1] == "low")

    # perna de alta corrigindo = oportunidade de COMPRA (segue o impulso)
    if direcao == "BUY"  and not perna_foi_alta:  return None
    if direcao == "SELL" and not perna_foi_baixa: return None

    # mede a correção contra o preço AGORA (o pavio atual pode já estar no nível)
    preco = c1[-1]["close"]
    retr = _corrigiu_50(alto, baixo, preco, direcao)
    if retr is None:
        # tenta pelo extremo do candle atual — pavio rápido de 50%
        pavio = c1[-1]["low"] if direcao == "BUY" else c1[-1]["high"]
        retr = _corrigiu_50(alto, baixo, pavio, direcao)
        if retr is None: return None
    # sem "M1" fixo no texto: a função é genérica por timeframe, mas hoje
    # só é chamada em M1 (check_macro_m1) — o texto de exibição é montado
    # por quem chama, prefixando o timeframe certo quando precisar.
    return {"preco": preco, "desc": f"pernada corrigida {int(retr*100)}%"}


def gatilho_tres_topos_abc(c1, direcao, lado=2, tol_nivel=None):
    """GATILHO 3 — três topos (ou três fundos) no mesmo nível + ABC
    corrigindo 50%+. Independe da contagem de Elliott: é o padrão de
    exaustão. Procura o melhor TRIO agrupado entre os últimos pivots,
    em vez de pegar cegamente os 3 últimos (um topo ainda em formação
    contaminava o agrupamento)."""
    if tol_nivel is None:
        tol_nivel = TOL_TRES_TOPOS
    if len(c1) < 20: return None
    piv = _limpa_pivots(_pivots_m1(c1, lado=lado))
    tipo = "high" if direcao == "SELL" else "low"
    mesmos = [p for p in piv if p[1] == tipo][-6:]   # janela de busca
    if len(mesmos) < 3: return None

    # varre trios consecutivos, do mais recente pro mais antigo
    trio = None
    for i in range(len(mesmos) - 3, -1, -1):
        cand = mesmos[i:i+3]
        niveis = [p[2] for p in cand]
        media = sum(niveis) / 3
        if media <= 0: continue
        if max(abs(n - media) / media for n in niveis) <= tol_nivel:
            trio = (cand, media)
            break
    if not trio: return None
    cand, media = trio

    # extremo oposto entre os topos = base do movimento (o "B" do ABC)
    i_ini, i_fim = cand[0][0], cand[-1][0]
    trecho = c1[i_ini:i_fim + 1]
    if not trecho: return None
    if tipo == "high":
        alto, baixo = media, min(x["low"] for x in trecho)
    else:
        alto, baixo = max(x["high"] for x in trecho), media

    preco = c1[-1]["close"]
    retr = _corrigiu_50(alto, baixo, preco, direcao)
    if retr is None:
        pavio = c1[-1]["low"] if direcao == "BUY" else c1[-1]["high"]
        retr = _corrigiu_50(alto, baixo, pavio, direcao)
        if retr is None: return None
    return {"preco": preco,
            "desc": f"3 {'topos' if tipo=='high' else 'fundos'} + ABC {int(retr*100)}%"}



# ═══════════════════════════════════════════════════════════════
#  ABC EM CONSTRUÇÃO — operar DENTRO da correção
#  Quando uma onda de impulso termina, o M1 começa a montar um
#  padrão corretivo com figura geométrica (triângulo, cunha, canal,
#  bandeira). Cada sub-perna dessa figura (A, B, C) é uma entrada:
#  não se espera a correção terminar, opera-se a construção dela.
#  A direção de cada entrada é a da sub-perna atual, que ALTERNA —
#  por isso este motor não segue a direção da âncora, e sim a
#  estrutura interna da correção.
# ═══════════════════════════════════════════════════════════════

def detectar_figura(candles, lado=2, min_pivots=4):
    """Identifica a figura geométrica que o timeframe está desenhando, a
    partir da inclinação das linhas que ligam os topos e os fundos.
    Funciona pra candles de QUALQUER tempo gráfico (M1, M15, H1, H4...).
      - convergente  -> triângulo / cunha
      - paralela     -> canal / bandeira
      - divergente   -> megafone
    Retorna a figura, os pivots e o sentido da última sub-perna."""
    piv = _limpa_pivots(_pivots_m1(candles, lado=lado))
    if len(piv) < min_pivots: return None

    topos  = [p for p in piv if p[1] == "high"][-3:]
    fundos = [p for p in piv if p[1] == "low"][-3:]
    if len(topos) < 2 or len(fundos) < 2: return None

    def incl(pts):
        dx = pts[-1][0] - pts[0][0]
        if dx == 0: return 0.0
        return (pts[-1][2] - pts[0][2]) / dx

    inc_t, inc_f = incl(topos), incl(fundos)
    preco = candles[-1]["close"]
    if preco <= 0: return None

    # normaliza as inclinações pelo preço pra comparar entre ativos
    nt, nf = inc_t / preco, inc_f / preco
    LIMIAR = 1e-5

    if nt < -LIMIAR and nf > LIMIAR:
        figura = "triângulo convergente"
    elif nt < -LIMIAR and nf < -LIMIAR and abs(nt - nf) < LIMIAR:
        figura = "canal de baixa"
    elif nt > LIMIAR and nf > LIMIAR and abs(nt - nf) < LIMIAR:
        figura = "canal de alta"
    elif nt < -LIMIAR and nf < -LIMIAR:
        figura = "cunha de baixa"
    elif nt > LIMIAR and nf > LIMIAR:
        figura = "cunha de alta"
    elif nt > LIMIAR and nf < -LIMIAR:
        figura = "megafone"
    else:
        figura = "lateral"

    ultimo = piv[-1]
    # a sub-perna em curso vai do último pivot até agora
    sentido = "alta" if ultimo[1] == "low" else "baixa"
    return {"figura": figura, "pivots": piv, "sentido_atual": sentido,
            "ultimo_pivot": ultimo, "topos": topos, "fundos": fundos}


def detectar_figura_m1(c1, lado=2, min_pivots=4):
    """Wrapper de compatibilidade: detectar_figura aplicado ao M1."""
    return detectar_figura(c1, lado=lado, min_pivots=min_pivots)


def gatilho_abc_construcao(c1, ctx_direcao, lado=2):
    """Opera a sub-perna do ABC que está sendo construída AGORA.

    Lógica: o último pivot do M1 marca o início da sub-perna em curso.
    Se o preço já corrigiu 50%+ da sub-perna ANTERIOR e retomou na
    direção da nova, entra — sem esperar a figura fechar.

    A direção é a da sub-perna, não a da âncora. ctx_direcao entra só
    como informação de contexto (aparece na mensagem), não como filtro:
    dentro de uma correção as pernas vão nos dois sentidos."""
    fig = detectar_figura_m1(c1, lado=lado)
    if not fig: return None
    piv = fig["pivots"]
    if len(piv) < 3: return None

    origem, extremo, atual_piv = piv[-3], piv[-2], piv[-1]
    alto  = max(origem[2], extremo[2])
    baixo = min(origem[2], extremo[2])
    tam = alto - baixo
    if tam <= 0: return None

    preco = c1[-1]["close"]
    # sub-perna em curso: do último pivot em diante
    if atual_piv[1] == "low":
        direcao, ref = "BUY", atual_piv[2]
        avanco = preco - ref
    else:
        direcao, ref = "SELL", atual_piv[2]
        avanco = ref - preco
    if avanco <= 0: return None   # ainda não retomou na direção da sub-perna

    # a sub-perna anterior precisa ter corrigido pelo menos 50%
    retr = abs(extremo[2] - atual_piv[2]) / tam
    if retr < TOL_CORRECAO[0]: return None

    # e o avanço atual ainda deve ser pequeno — entrar NO INÍCIO da perna,
    # não no fim dela (senão vira entrada atrasada)
    if avanco > tam * 0.5: return None

    ctx = f" (âncora {ctx_direcao})" if ctx_direcao else ""
    return {"preco": preco, "direcao": direcao,
            # origem REAL da sub-perna: é daqui que sai o stop técnico
            # dela. Sem isso o stop virava a extremidade genérica dos
            # últimos 20 candles, que não é o setup detectado.
            "origem_perna": ref,
            "desc": f"{fig['figura']} — sub-perna {direcao} do ABC, "
                    f"anterior corrigiu {int(retr*100)}%{ctx}"}


def impulso_corrigido_m1(c1, direcao, lado=2):
    """Acha o IMPULSO que está sendo corrigido — a perna grande que veio
    antes da figura começar. É dele que saem os níveis de 38.2% e 50%,
    que são os alvos reais das sub-pernas do ABC.

    direcao = direção da SUB-PERNA. Numa sub-perna de alta (BUY), o
    impulso corrigido foi de baixa: topo -> fundo. E vice-versa."""
    piv = _limpa_pivots(_pivots_m1(c1, lado=lado))
    if len(piv) < 4: return None

    # varre de trás pra frente procurando a maior perna na direção oposta
    melhor = None
    for i in range(len(piv) - 2, 0, -1):
        a, b = piv[i-1], piv[i]
        if direcao == "BUY" and not (a[1] == "high" and b[1] == "low"):
            continue
        if direcao == "SELL" and not (a[1] == "low" and b[1] == "high"):
            continue
        tam = abs(a[2] - b[2])
        if melhor is None or tam > melhor["tamanho"]:
            melhor = {"inicio": a[2], "fim": b[2], "tamanho": tam,
                      "i_inicio": a[0], "i_fim": b[0]}
    if not melhor or melhor["tamanho"] <= 0:
        return None

    # níveis de retração medidos a partir do FIM do impulso
    ini, fim, tam = melhor["inicio"], melhor["fim"], melhor["tamanho"]
    if direcao == "BUY":   # impulso de baixa (ini alto -> fim baixo); retrai pra cima
        melhor["fib_382"] = fim + tam * 0.382
        melhor["fib_500"] = fim + tam * 0.500
    else:                  # impulso de alta; retrai pra baixo
        melhor["fib_382"] = fim - tam * 0.382
        melhor["fib_500"] = fim - tam * 0.500
    return melhor


def zona_fib_ok(preco, imp, direcao, minimo=0.5, maximo=1.05):
    """Filtro de zona: a entrada só vale quando o preço está na região
    de 50%–100% de retração do impulso — que é onde as entradas
    acontecem na prática (inclusive furando o 50/38.2 e voltando).
    Retorna a posição do preço na retração, ou None se fora da zona."""
    if not imp or imp["tamanho"] <= 0: return None
    fim, tam = imp["fim"], imp["tamanho"]
    # 0 = no fim do impulso, 1 = de volta ao início dele
    pos = (preco - fim) / tam if direcao == "BUY" else (fim - preco) / tam
    # pos pequeno = perto do extremo do impulso (é ali que se entra)
    if not (0 <= pos <= (1 - minimo) + 0.05):
        return None
    return pos


def check_gatilhos_tf(symbol, direcao, tf="1m", candles_qtd=80):
    """Roda os TRÊS gatilhos (candle de retração, pernada de Elliott, 3
    topos/fundos + ABC) no timeframe pedido. Generaliza check_macro_m1
    pra QUALQUER tempo gráfico — os três gatilhos são funções puras de
    candle, não têm nada de específico do M1 — mas hoje só é chamada
    com tf="1m" (via check_macro_m1): a confirmação de entrada é sempre
    em M1, nunca direto no tf âncora (ver comentário do ANCORA_ATIVO)."""
    c1 = get_candles(symbol, tf, candles_qtd)
    if not c1 or len(c1) < 10: return None

    for fn in (gatilho_candle_retracao, gatilho_pernada_50, gatilho_tres_topos_abc):
        try:
            r = fn(c1, direcao)
        except Exception as e:
            print(f"[GATILHO {fn.__name__} {tf}] {symbol}: {e}")
            continue
        if r:
            _ultimo_gatilho[symbol] = r["desc"]
            print(f"[GATILHO {tf}] {symbol} {direcao}: {r['desc']}")
            return r["preco"]
    return None


def check_macro_m1(symbol, view):
    """Wrapper de compatibilidade: check_gatilhos_tf aplicado ao M1."""
    return check_gatilhos_tf(symbol, view["direcao"], tf="1m")


def detectar_perna(symbol, tf, lado=2, candles_qtd=60, tolerancia=(0.38, 0.65)):
    """Generaliza o MESMO critério do M1 (pernada + correção ~50%) pra
    QUALQUER tempo gráfico — H4, H1, M15, o que for. Acha os dois últimos
    pivots (topo/fundo) que formam a perna mais recente, e verifica se o
    preço atual está corrigindo essa perna dentro da faixa de tolerância.
    A direção retornada já é a direção da CORREÇÃO (continuação natural
    do movimento corretivo, não do impulso original) — perna de baixa
    corrigindo = BUY, perna de alta corrigindo = SELL. alvo_50 é o meio
    exato da perna (o nível de 50% que ela busca)."""
    c = get_candles(symbol, tf, candles_qtd)
    if not c or len(c) < lado*2 + 5: return None
    piv = _limpa_pivots(_pivots_m1(c, lado=lado))
    if len(piv) < 2: return None
    penultimo, ultimo = piv[-2], piv[-1]
    if ultimo[1] == penultimo[1]: return None
    preco_atual = c[-1]["close"]
    alto = max(penultimo[2], ultimo[2]); baixo = min(penultimo[2], ultimo[2])
    tamanho = alto - baixo
    if tamanho <= 0: return None
    perna_foi_alta = ultimo[1] == "high"
    direcao = "SELL" if perna_foi_alta else "BUY"
    retr = (alto - preco_atual) / tamanho if direcao == "BUY" else (preco_atual - baixo) / tamanho
    ok = tolerancia[0] <= retr <= tolerancia[1]
    # projeção de 38.2% da onda 1: a partir do fim da correção, o preço
    # tende a estender 38.2% do tamanho da perna original. É o alvo que
    # o Jon usa na mão — mais realista que só o meio da perna (50%).
    proj_382 = tamanho * 0.382
    return {"direcao": direcao, "ok": ok, "retr": round(retr, 2),
            "alto": alto, "baixo": baixo, "alvo_50": (alto + baixo) / 2,
            "tamanho_onda": tamanho, "proj_382": proj_382}

def alvo_projecao_382(preco_entrada, direcao, ctx):
    """Alvo por projeção de 38.2% do tamanho da onda 1 (do timeframe âncora),
    medido a partir do preço de entrada. Direção é a da correção:
    BUY = correção de perna de baixa -> projeta pra cima."""
    if not ctx or not ctx.get("proj_382"): return None
    proj = ctx["proj_382"]
    return preco_entrada + proj if direcao == "BUY" else preco_entrada - proj

def contexto_maior(symbol):
    """Cascata H4 → H1 → M15: acha o tempo gráfico maior mais próximo que
    tem UMA pernada corrigindo ~50% agora — esse é o 'âncora' que autoriza
    o M1 a operar, define a DIREÇÃO certa (a da correção) e o ALVO
    (o nível de 50% dessa pernada maior, não um número arbitrário)."""
    for tf in ("4h", "1h", "15m", "5m"):
        d = detectar_perna(symbol, tf)
        if d and d["ok"]:
            return tf, d
    return None, None


def estrutura_ancora(symbol, tf, lado=2, lookback=150):
    """Estrutura do timeframe âncora (H4/H1/M15): a figura geométrica que
    está sendo desenhada (megafone, triângulo, cunha, canal), os limites
    dela (topo e fundo) e o nível de 50% da última pernada do timeframe.
    É daqui que vem o alvo quando a âncora está em alargamento ou
    correção lateral — mais real que uma projeção fixa de 38.2%."""
    candles = get_candles(symbol, tf, lookback)
    if not candles or len(candles) < 20: return None
    fig   = detectar_figura(candles, lado=lado)
    perna = detectar_perna(symbol, tf)
    if not fig and not perna: return None

    topo = fundo = None
    if fig:
        topos_val  = [p[2] for p in fig["topos"]]
        fundos_val = [p[2] for p in fig["fundos"]]
        topo  = max(topos_val) if topos_val else None
        fundo = min(fundos_val) if fundos_val else None
    if perna:
        if topo  is None: topo  = perna.get("alto")
        if fundo is None: fundo = perna.get("baixo")

    return {"tf": tf, "figura": fig["figura"] if fig else None,
            "topo": topo, "fundo": fundo,
            "alvo_50": perna.get("alvo_50") if perna else None}


def alvo_ancora(preco, direcao, estrutura):
    """Alvo pela estrutura do timeframe âncora: numa venda, o fundo da
    estrutura ou o 50% da pernada dela (o que estiver à frente do
    preço); numa compra, o topo ou o 50%. Tem prioridade sobre a
    projeção de 38.2% do M1 quando a âncora está em alargamento
    (megafone) ou correção lateral/ABC."""
    if not estrutura: return None
    limite = estrutura.get("fundo") if direcao == "SELL" else estrutura.get("topo")
    for candidato in (limite, estrutura.get("alvo_50")):
        if candidato is None: continue
        if (direcao == "SELL" and candidato < preco) or (direcao == "BUY" and candidato > preco):
            return candidato
    return None


def origem_da_pernada(c1, direcao, lado=2):
    """Acha a ORIGEM da pernada que acabou de ser corrigida — o pivot de
    onde o movimento saiu. É este ponto que invalida o setup se for
    perdido, então é aqui que o stop tem que ficar.

    Detalhe que importa: usa os pivots BRUTOS, não os limpos. A limpeza
    funde fundos consecutivos mantendo o mais PROFUNDO, o que puxaria a
    origem pra um pavio antigo sem relação com o setup. Para stop
    queremos o fundo mais RECENTE antes do extremo — o que de fato
    iniciou a pernada."""
    piv = _pivots_m1(c1, lado=lado)
    if len(piv) < 3: return None

    tipo_extremo = "high" if direcao == "BUY" else "low"
    tipo_origem  = "low"  if direcao == "BUY" else "high"

    # extremo da pernada: o mais recente do tipo certo, mas não o
    # último pivot da série (esse já faz parte da correção)
    extremos = [p for p in piv if p[1] == tipo_extremo]
    if not extremos: return None
    extremo = extremos[-1]

    # origem: o pivot do tipo oposto IMEDIATAMENTE anterior ao extremo
    anteriores = [p for p in piv if p[1] == tipo_origem and p[0] < extremo[0]]
    if not anteriores: return None
    origem = anteriores[-1]          # o mais recente, não o mais profundo

    # sanidade: a pernada precisa ter tamanho real
    if abs(extremo[2] - origem[2]) <= 0: return None
    return origem[2]


def stop_tecnico(symbol, direcao, tf="1m", lookback=60, folga_pct=0.08):
    """Stop TÉCNICO: a ORIGEM da pernada corrigida, não a extremidade de
    uma janela fixa de candles. Generaliza stop_tecnico_m1 pra QUALQUER
    tempo gráfico — é o que dá o stop técnico (na origem) na escala
    pedida (M1 no day trade, ou a escala âncora quando chamado a partir
    dela).

    Antes esta função pegava a mínima/máxima dos últimos 20 candles — um
    retângulo arbitrário que não tinha relação com o setup detectado.
    Agora lê a estrutura (pivots) e devolve o ponto que realmente
    invalida o trade, com uma folga pra não colar no pavio — nunca só a
    folga percentual: tem um piso extra do spread real da corretora
    (_folga_spread_extra), pra sobreviver ao custo do spread e a um
    topo/fundo duplo raspando a origem, pedido explícito do Jon."""
    c1 = get_candles(symbol, tf, lookback + 5)
    if not c1 or len(c1) < 10: return None

    origem = origem_da_pernada(c1, direcao)
    preco = c1[-1]["close"]

    if origem is None:
        # último recurso: extremidade da janela (comportamento antigo)
        janela = c1[-min(lookback, len(c1)):]
        origem = (min(c["low"] for c in janela) if direcao == "BUY"
                  else max(c["high"] for c in janela))

    # o stop precisa estar do lado certo do preço
    if direcao == "BUY" and origem >= preco:
        janela = c1[-min(lookback, len(c1)):]
        origem = min(c["low"] for c in janela)
    if direcao == "SELL" and origem <= preco:
        janela = c1[-min(lookback, len(c1)):]
        origem = max(c["high"] for c in janela)

    folga = abs(preco - origem) * folga_pct
    folga = max(folga, _folga_spread_extra(symbol, preco))
    return origem - folga if direcao == "BUY" else origem + folga


def stop_tecnico_m1(symbol, direcao, lookback=60, folga_pct=0.08):
    """Wrapper de compatibilidade: stop_tecnico aplicado ao M1."""
    return stop_tecnico(symbol, direcao, tf="1m", lookback=lookback, folga_pct=folga_pct)

def alvo_m15(symbol, direcao, lookback=150):
    """Alvo pela estrutura do M15 — topo/fundo relevante na direção do
    trade. Ignora os candles mais recentes (~45min) pra não pegar o
    extremo que o preço ACABOU de fazer agora — em tendência forte, o
    'fundo dos últimos 40 candles' é literalmente onde o preço está,
    o que dava um alvo colado no preço (sem espaço real de lucro).
    Se o nível achado ainda ficar perto demais, amplia a busca."""
    c15 = get_candles(symbol, "15m", lookback)
    if not c15 or len(c15) < 20: return None
    preco_atual = c15[-1]["close"]
    a15 = atr(c15, ATR_PERIODO)
    atr_ref = a15[-1] if a15 and a15[-1] else None
    ignorar = 3
    nivel = None
    for janela_tam in (40, 80, lookback):
        fatia = c15[:-ignorar] if ignorar and len(c15) > ignorar else c15
        janela = fatia[-janela_tam:] if len(fatia) > janela_tam else fatia
        if not janela: continue
        nivel = max(c["high"] for c in janela) if direcao == "BUY" else min(c["low"] for c in janela)
        if not atr_ref or abs(nivel - preco_atual) >= atr_ref * 2:
            return nivel  # achou um nível com espaço real de lucro
    return nivel  # nenhum teve distância boa — devolve o mais distante mesmo assim

def alvo_m1_estrutura(symbol, direcao, c1=None, lookback=90):
    """Alvo do FLUXO M1 PURO: o PRÓXIMO topo/fundo real do M1 — não uma
    projeção calculada, é a estrutura que o preço precisa romper pra
    continuar o movimento. Mesma ideia do alvo_m15, só que na escala do
    M1: stop curto (origem da pernada) e alvo curto (a estrutura mais
    próxima), pra trades rápidos que giram volume."""
    c1 = c1 if c1 is not None else get_candles(symbol, "1m", lookback)
    if not c1 or len(c1) < 20: return None
    preco_atual = c1[-1]["close"]
    ignorar = 3
    nivel = None
    for janela_tam in (20, 40, lookback):
        fatia = c1[:-ignorar] if ignorar and len(c1) > ignorar else c1
        janela = fatia[-janela_tam:] if len(fatia) > janela_tam else fatia
        if not janela: continue
        nivel = max(c["high"] for c in janela) if direcao == "BUY" else min(c["low"] for c in janela)
        if nivel != preco_atual:
            return nivel
    return nivel

# Único valor mínimo que o CLAUDE.md permite: RR abaixo de 1:1 é
# matematicamente ilógico (arrisca mais do que pode ganhar), pedido
# explícito do Jon por cima da regra de "sem trava por valor" — vale
# pra QUALQUER motor, checado uma vez só dentro de fire_signal.
RR_MINIMO = float(os.environ.get("RR_MINIMO", "1.0"))

def analyze_symbol(symbol):
    c1  = get_candles(symbol, "1h",  150)   # contexto de tendência
    c15 = get_candles(symbol, "15m", 150)   # timeframe de entrada principal
    if not c1 or not c15 or len(c1) < EMA_LENTA+5 or len(c15) < ATR_PERIODO+5:
        return None
    price = c15[-1]["close"]

    closes1 = [c["close"] for c in c1]
    ef, es = ema(closes1, EMA_RAPIDA), ema(closes1, EMA_LENTA)
    if ef[-1] is None or es[-1] is None:
        return None
    tendencia = "neutral"
    if ef[-1] > es[-1] and closes1[-1] > ef[-1]:  tendencia = "up"
    elif ef[-1] < es[-1] and closes1[-1] < ef[-1]: tendencia = "down"

    closes15 = [c["close"] for c in c15]
    r15 = rsi(closes15, RSI_PERIODO)
    a15 = atr(c15, ATR_PERIODO)
    if r15[-1] is None or r15[-2] is None or a15[-1] is None:
        return None
    r_now, r_prev, atr_now = r15[-1], r15[-2], a15[-1]

    # proxy de "evento/notícia": vela recente com range muito acima do normal
    validos = [a for a in a15[-20:] if a]
    atr_medio = sum(validos)/len(validos) if validos else 0
    ultimo_range = c15[-1]["high"] - c15[-1]["low"]
    pico_vol = bool(atr_medio and ultimo_range > atr_medio * ATR_PICO_MULT)

    fng_valor, fng_classe = get_fear_greed() if FNG_FILTRO_ATIVO else (None, None)
    fng_extremo = bool(FNG_FILTRO_ATIVO and fng_valor is not None and
                       (fng_valor <= FNG_EXTREMO_BAIXO or fng_valor >= FNG_EXTREMO_ALTO))

    bloqueado = pico_vol or fng_extremo
    entry = None
    if not bloqueado and tendencia == "up" and r_prev < RSI_PULLBACK and r_now >= RSI_PULLBACK:
        sl = price - atr_now*ATR_STOP_MULT
        tp = price + atr_now*ATR_STOP_MULT*RR_ALVO
        entry = {"direcao": "BUY", "entrada": price, "stop": sl, "alvo": tp, "atr": atr_now, "origem": "M15", "rsi": round(r_now,1)}
    elif not bloqueado and tendencia == "down" and r_prev > (100-RSI_PULLBACK) and r_now <= (100-RSI_PULLBACK):
        sl = price + atr_now*ATR_STOP_MULT
        tp = price - atr_now*ATR_STOP_MULT*RR_ALVO
        entry = {"direcao": "SELL", "entrada": price, "stop": sl, "alvo": tp, "atr": atr_now, "origem": "M15", "rsi": round(r_now,1)}

    # ── caminho secundário M5: mais frequente, exige confirmação de padrão de candle ──
    entry_m5 = None
    c5 = get_candles(symbol, "5m", 100)
    if c5 and len(c5) >= RSI_PERIODO+5 and not bloqueado:
        closes5 = [c["close"] for c in c5]
        r5, a5 = rsi(closes5, RSI_PERIODO), atr(c5, ATR_PERIODO)
        if r5[-1] is not None and r5[-2] is not None and a5[-1] is not None:
            r5_now, r5_prev, atr5_now = r5[-1], r5[-2], a5[-1]
            preco5 = c5[-1]["close"]; i5 = len(c5)-1
            if tendencia=="up" and r5_prev<RSI_PULLBACK and r5_now>=RSI_PULLBACK and engolfo_alta(c5,i5):
                sl5 = preco5 - atr5_now*ATR_STOP_MULT; tp5 = preco5 + atr5_now*ATR_STOP_MULT*RR_ALVO
                entry_m5 = {"direcao":"BUY","entrada":preco5,"stop":sl5,"alvo":tp5,"atr":atr5_now,"origem":"M5","rsi":round(r5_now,1)}
            elif tendencia=="down" and r5_prev>(100-RSI_PULLBACK) and r5_now<=(100-RSI_PULLBACK) and engolfo_baixa(c5,i5):
                sl5 = preco5 + atr5_now*ATR_STOP_MULT; tp5 = preco5 - atr5_now*ATR_STOP_MULT*RR_ALVO
                entry_m5 = {"direcao":"SELL","entrada":preco5,"stop":sl5,"alvo":tp5,"atr":atr5_now,"origem":"M5","rsi":round(r5_now,1)}

    return {"symbol": symbol, "price": price, "tendencia": tendencia,
            "rsi": round(r_now,1), "atr": atr_now, "pico_vol": pico_vol,
            "fng_valor": fng_valor, "fng_classe": fng_classe, "fng_extremo": fng_extremo,
            "entry": entry, "entry_m5": entry_m5}

def get_last_price(symbol):
    """Preço em tempo real direto do ticker da Bybit — usado só pra confirmar,
    bem antes de enviar a ordem, que o stop ainda é válido pro preço atual
    (o candle usado pra montar o sinal pode já estar alguns segundos velho)."""
    if USANDO_BINGX:
        return _bingx_last_price(symbol)
    d = bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    if d and d.get("retCode") == 0:
        lst = d.get("result", {}).get("list", [])
        if lst:
            try: return float(lst[0]["lastPrice"])
            except Exception: return None
    return None

def filtrar_por_periodo(sinais, arg):
    """Filtra sinais por período pro /performance: hoje, semana (7d), mes (30d),
    um número de dias, ou tudo (padrão quando não informado)."""
    agora = agora_br()
    if not arg or arg.lower() in ("tudo", "all", "geral"):
        return sinais, "Total (tudo)"
    arg_l = arg.lower()
    if arg_l in ("hoje", "today"):
        corte = agora.replace(hour=0, minute=0, second=0, microsecond=0); label = "Hoje"
    elif arg_l in ("semana", "week", "7d"):
        corte = agora - timedelta(days=7); label = "Últimos 7 dias"
    elif arg_l in ("mes", "mês", "month", "30d"):
        corte = agora - timedelta(days=30); label = "Últimos 30 dias"
    elif arg_l.isdigit():
        n = int(arg_l); corte = agora - timedelta(days=n); label = f"Últimos {n} dia(s)"
    else:
        return sinais, "Total (tudo)"
    filtrados = []
    for s in sinais:
        try:
            dt = datetime.strptime(s.get("data", ""), "%d/%m/%Y %H:%M").replace(tzinfo=BR_TZ)
        except Exception:
            continue
        if dt >= corte:
            filtrados.append(s)
    return filtrados, label

def trades_abertos_agora():
    return len([s for s in memory.get("signals", []) if s["status"] == "aberto"])

def _tf_do_grafico(origem):
    """Qual timeframe mostrar no gráfico do sinal, a partir da origem."""
    if origem.startswith("ANCORA-"):
        return origem.split("-", 1)[1].lower()   # "4h", "1h", "30m"
    if origem == "M15": return "15m"
    if origem == "M5":  return "5m"
    return "1m"   # M1-TECNICO/ABC/FLUXO-*/GATILHO/MACRO

def gerar_grafico_sinal(symbol, tf, candles, direcao, entrada, stop, alvo, titulo_extra="",
                         saida=None, resultado_txt=None, venceu=None, qty=None):
    """Gera um PNG do gráfico de candles com entrada/stop/alvo marcados —
    pivots (bolinhas), linha de tendência do topo/fundo (a mesma leitura
    de detectar_figura), a zona de fibonacci da pernada corrigida
    (0.0/38.2/50.0/100.0) e uma faixa destacando a zona de risco
    (entrada até o stop), do jeito que o Jon desenha na mão.

    Quando `saida` é informado (fechamento de trade — TAKE/STOP), o
    mesmo gráfico ganha a linha de saída real e o resultado em cima da
    linha de entrada, tipo "COMPRA 0.01, +4.25 BRL".

    Só um extra VISUAL — se o matplotlib não estiver instalado ou
    qualquer coisa der errado, devolve None e a mensagem de sempre
    continua indo normalmente (nunca trava/derruba o bot por causa do
    gráfico)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        from matplotlib.lines import Line2D
    except ImportError as e:
        print(f"[GRAFICO] {symbol}: matplotlib não disponível ({e}) — pkg install matplotlib / pip install matplotlib.", flush=True)
        return None
    try:
        c = candles[-80:]
        if len(c) < 5:
            print(f"[GRAFICO] {symbol}: só {len(c)} candles de {tf} — precisa de pelo menos 5.", flush=True)
            return None
        piv = _limpa_pivots(_pivots_m1(c, lado=2))

        fig, ax = plt.subplots(figsize=(9.5, 5.5), dpi=140)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")

        largura = 0.55
        for i, k in enumerate(c):
            o, h, l, cl = k["open"], k["high"], k["low"], k["close"]
            cor = "#26a69a" if cl >= o else "#ef5350"
            ax.add_line(Line2D([i, i], [l, h], color=cor, linewidth=1))
            baixo = min(o, cl); alto_corpo = max(o, cl)
            altura = max(alto_corpo - baixo, (h - l) * 0.01 or 0.0001)
            ax.add_patch(Rectangle((i - largura/2, baixo), largura, altura, facecolor=cor, edgecolor=cor, linewidth=0.4))

        precos = [k["high"] for k in c] + [k["low"] for k in c] + [entrada, stop, alvo]
        if saida is not None: precos.append(saida)
        ymin, ymax = min(precos), max(precos)
        faixa_visivel = ymax - ymin
        # margem maior que o normal: sobra espaço pras setas de cenário
        # (mais abaixo) sem estourar pra fora do gráfico
        folga = faixa_visivel * 0.22 or ymax * 0.001
        ax.set_ylim(ymin - folga, ymax + folga)
        ax.set_xlim(-1, len(c) + 18)

        # faixa destacando a zona de risco (entrada até o stop), como as
        # caixas laranjas que o Jon desenha marcando zona de interesse
        ax.axhspan(min(entrada, stop), max(entrada, stop), color="#f5a623", alpha=0.10, zorder=0.5)

        # figura geométrica (a mesma detectar_figura usada no resto do
        # bot) — qualquer estrutura corretiva (triângulo/cunha/megafone/
        # lateral) com 5+ pivots é lida como ABCDE de Elliott: rotula os
        # 5 últimos pivots em vez de bolinhas anônimas. Só um canal
        # limpo (impulso, não correção) fica de fora.
        fig_info = detectar_figura(c, lado=2)
        eh_corretiva = bool(fig_info) and fig_info["figura"] not in ("canal de alta", "canal de baixa")
        if eh_corretiva and len(piv) >= 5:
            letras = ["A", "B", "C", "D", "E"]
            for letra, (i, tipo, preco) in zip(letras, piv[-5:]):
                ax.scatter([i], [preco], s=70, facecolor="#1e63e0", edgecolor="white", linewidth=1, zorder=6)
                ax.annotate(letra, xy=(i, preco), xytext=(0, 10 if tipo == "high" else -14),
                            textcoords="offset points", color="#1e63e0", fontsize=10,
                            fontweight="bold", ha="center", annotation_clip=False)
            # os pivots restantes (fora do ABCDE), se houver, seguem simples
            for (i, tipo, preco) in piv[:-5]:
                ax.scatter([i], [preco], s=42, facecolor="#1e63e0", edgecolor="white", linewidth=0.8, zorder=6)
        else:
            # pivots (bolinhas azuis, como o Jon marca na mão)
            for (i, tipo, preco) in piv:
                ax.scatter([i], [preco], s=42, facecolor="#1e63e0", edgecolor="white", linewidth=0.8, zorder=6)

        # linhas de tendência do topo/fundo — a MESMA leitura de
        # detectar_figura (liga o primeiro ao último pivot de cada tipo).
        topos  = [p for p in piv if p[1] == "high"][-4:]
        fundos = [p for p in piv if p[1] == "low"][-4:]

        def linha_tendencia(pontos):
            if len(pontos) < 2: return
            x = [p[0] for p in pontos]; y = [p[2] for p in pontos]
            ax.plot([x[0], x[-1]], [y[0], y[-1]], color="#1a1a1a", linewidth=1.6,
                    linestyle="--" if eh_corretiva else "-", zorder=4)

        linha_tendencia(topos)
        linha_tendencia(fundos)

        # zona de fibonacci da pernada corrigida (origem -> extremo) e as
        # DUAS setas de cenário (rompe pra cima / rompe pra baixo), do
        # tamanho da própria pernada — mesma ideia dos prints do Jon.
        tipo_extremo = "high" if direcao == "BUY" else "low"
        tipo_origem  = "low" if direcao == "BUY" else "high"
        extremos = [p for p in piv if p[1] == tipo_extremo]
        origens  = [p for p in piv if p[1] == tipo_origem]
        if extremos and origens:
            extremo = extremos[-1]
            anteriores = [p for p in origens if p[0] < extremo[0]]
            if anteriores:
                origem = anteriores[-1]
                tam = extremo[2] - origem[2]
                if faixa_visivel > 0 and abs(tam) >= faixa_visivel * 0.35:
                    x0, x1 = origem[0], extremo[0] if extremo[0] > origem[0] else len(c) - 1
                    x1 = max(x1, x0 + 3)
                    niveis = {"0.0": extremo[2], "38.2": extremo[2] - tam * 0.382,
                              "50.0": extremo[2] - tam * 0.5, "100.0": origem[2]}
                    for label, nivel in niveis.items():
                        ax.plot([x0, x1], [nivel, nivel], color="#f5a623", linewidth=0.9,
                                 linestyle="-", alpha=0.85, zorder=3)
                        ax.annotate(label, xy=(x0, nivel), xytext=(-4, 0), textcoords="offset points",
                                    color="#b8790a", fontsize=7.5, va="center", ha="right", annotation_clip=False)

                preco_atual = c[-1]["close"]
                # tamanho da seta proporcional ao gráfico, não o tamanho
                # bruto da pernada — senão uma pernada grande (ex. âncora
                # H4) faz a seta disparar pra fora da imagem
                projecao = min(abs(tam), faixa_visivel * 0.18) if faixa_visivel > 0 else abs(tam)
                x_seta = len(c) - 1
                for sinal_proj, curva in ((1, 0.35), (-1, -0.35)):
                    ax.annotate("", xy=(x_seta + 9, preco_atual + sinal_proj * projecao),
                                xytext=(x_seta, preco_atual),
                                arrowprops=dict(arrowstyle="-|>", color="#3f51b5",
                                                linewidth=1.6, alpha=0.85,
                                                connectionstyle=f"arc3,rad={curva}"),
                                zorder=9, annotation_clip=False)

        def linha_nivel(preco, cor, label):
            ax.plot([len(c) - 8, len(c) - 1], [preco, preco], color=cor, linestyle="--", linewidth=1.4, zorder=5)
            ax.annotate(f" {label} {preco:,.4f}", xy=(len(c)-1, preco), xytext=(4, 0),
                        textcoords="offset points", color=cor, fontsize=9, va="center",
                        fontweight="bold", annotation_clip=False)

        acao = "COMPRA" if direcao == "BUY" else "VENDA"
        # no fechamento, o lado que bateu (TP ou SL) já vira a linha de
        # saída real logo abaixo — desenhar a linha planejada dele de
        # novo só duplicaria em cima (e em SIMULAÇÃO o valor é idêntico).
        if saida is None or not venceu:
            linha_nivel(alvo, "#2e7d32", "TP")
        if saida is None or venceu:
            linha_nivel(stop, "#e65100", "SL")

        if saida is None:
            # sinal em aberto: linha da entrada com o preço
            linha_nivel(entrada, "#1565c0", "Entrada")
        else:
            # trade fechado: a linha da entrada mostra o resultado, tipo
            # "COMPRA 0.01, +4.25 BRL" — igual ao que o Jon desenha na mão
            qty_txt = f"{qty:g} " if qty is not None else ""
            ax.plot([len(c) - 8, len(c) - 1], [entrada, entrada], color="#1565c0", linestyle="--", linewidth=1.4, zorder=5)
            ax.annotate(f" {acao} {qty_txt}, {resultado_txt}", xy=(len(c)-1, entrada), xytext=(4, 0),
                        textcoords="offset points", color="#1565c0", fontsize=9, va="center",
                        fontweight="bold", annotation_clip=False)
            cor_saida = "#2e7d32" if venceu else "#c62828"
            ax.plot([len(c) - 8, len(c) - 1], [saida, saida], color=cor_saida, linestyle="-", linewidth=1.8, zorder=6)
            ax.annotate(f" {'TAKE' if venceu else 'STOP'} {saida:,.4f}", xy=(len(c)-1, saida), xytext=(4, -12),
                        textcoords="offset points", color=cor_saida, fontsize=9, va="center",
                        fontweight="bold", annotation_clip=False)
            ax.scatter([len(c)-1], [saida], color=cor_saida, s=110, zorder=8, marker="o",
                       edgecolors="black", linewidths=0.8)

        cor_dir = "#26a69a" if direcao == "BUY" else "#ef5350"
        ax.scatter([len(c)-1], [entrada], color=cor_dir, s=150, zorder=7,
                   marker="^" if direcao == "BUY" else "v", edgecolors="black", linewidths=0.8)

        seta = "▲" if direcao == "BUY" else "▼"
        if saida is None:
            titulo_acao = f"{acao} {seta}"
        else:
            titulo_acao = "TAKE PROFIT" if venceu else "STOP LOSS"
        ax.set_title(f"{symbol}  {tf.upper()}  {titulo_acao}{titulo_extra}",
                     color="#1a1a1a", fontsize=13, fontweight="bold", loc="left", pad=12)
        ax.tick_params(colors="#555555", labelsize=8)
        for spine in ax.spines.values(): spine.set_color("#dddddd")
        ax.set_xticks([])
        ax.grid(True, color="#eeeeee", linewidth=0.7)
        ax.set_axisbelow(True)

        caminho = os.path.join(tempfile.gettempdir(), f"sinal_{symbol}_{int(time.time()*1000)}.png")
        fig.tight_layout()
        fig.savefig(caminho, facecolor=fig.get_facecolor())
        plt.close(fig)
        return caminho
    except Exception as e:
        print(f"[GRAFICO] {symbol}: {e}", flush=True)
        return None

def fire_signal(symbol, entry, ignorar_travas=False):
    sym = symbol
    bdir = entry["direcao"]; ep = entry["entrada"]; sp = entry["stop"]; tp = entry["alvo"]
    origem = entry.get("origem", "M15")
    risk = abs(ep - sp)
    if risk <= 0: return
    rr = round(abs(tp-ep)/risk, 1)
    if rr < RR_MINIMO:
        print(f"[SKIP] {sym} {origem}: RR 1:{rr} abaixo do mínimo (1:{RR_MINIMO}).")
        return
    emoji  = "✅" if bdir == "BUY" else "🔴"
    action = "COMPRA" if bdir == "BUY" else "VENDA"

    # trava de setup: impede reentrada no MESMO gatilho a cada ciclo.
    # Compartilhada entre corretoras — é sobre não reagir duas vezes ao
    # MESMO setup detectado, independente de em qual corretora executa.
    chave_setup = f"{sym}|{origem}|{bdir}|{round(sp, 4)}"
    if not ignorar_travas and chave_setup in _setups_executados:
        print(f"[SKIP] {sym}: setup {chave_setup} já foi executado — sem reentrada.")
        return

    # sem trailing — o alvo é fixo. IMPORTANTE: o piso de lucro mínimo só
    # vale pro motor antigo (M15/M5, stop por ATR) — nos motores técnicos
    # o alvo já vem da estrutura real, não mexe aqui.
    # o candle usado pra montar o sinal pode já estar alguns segundos velho —
    # confirma contra o preço AO VIVO antes de mandar qualquer coisa.
    preco_vivo = get_last_price(sym)
    if preco_vivo:
        if (bdir == "BUY"  and sp >= preco_vivo) or (bdir == "SELL" and sp <= preco_vivo):
            print(f"[SKIP] {sym}: preço moveu antes do envio (stop {sp} inválido pro preço atual {preco_vivo}) — sinal descartado.")
            return

    ts = agora_br().strftime('%d/%m/%Y %H:%M')
    blocos = []           # um pedaço de mensagem por corretora
    algum_sucesso = False

    # multi-corretora: cada corretora ativa (EXCHANGES_ATIVAS) recebe sua
    # PRÓPRIA ordem, com sua própria checagem de duplicidade/arbitragem e
    # sua própria quantidade (dimensionada pelo saldo daquela conta) — uma
    # corretora falhar/pular não impede a outra.
    for exch in EXCHANGES_ATIVAS:
        # a corretora está em modo one-way — duas ordens no mesmo símbolo
        # viram UMA posição só, fundida (preço médio). Se já tem um
        # registro "aberto" NESSA corretora pra esse símbolo, uma segunda
        # ordem geraria contagem duplicada de win/loss quando fechar.
        abertos_no_par = [s for s in memory.get("signals", [])
                          if s["symbol"] == sym and s["status"] == "aberto"
                          and s.get("exchange", EXCHANGE) == exch]

        mesmo_lado = next((s for s in abertos_no_par if s["direcao"] == bdir), None)
        if mesmo_lado:
            print(f"[SKIP] {sym} {exch}: já tem {bdir} aberto — não duplica ordem.")
            continue

        lado_oposto = next((s for s in abertos_no_par if s["direcao"] != bdir), None)
        if lado_oposto:
            if not ARBITRAGEM_ATIVA:
                print(f"[SKIP] {sym} {exch}: já tem {lado_oposto['direcao']} aberto e "
                      f"ARBITRAGEM_ATIVA=false — não abre o lado contrário.")
                continue
            # ARBITRAGEM: permitido, MAS nunca no mesmo ponto. Precisa de
            # distância de preço e de tempo entre as duas entradas — é isso
            # que separa dois setups independentes de um hedge que se anula.
            ep_ant = lado_oposto.get("entrada") or 0
            if ep_ant > 0:
                dist = abs(ep - ep_ant) / ep_ant
                if dist < ARB_DIST_MIN_PCT:
                    print(f"[SKIP] {sym} {exch}: entrada a {dist*100:.3f}% da posição "
                          f"{lado_oposto['direcao']} — perto demais, viraria hedge no mesmo ponto.")
                    continue
            ts_ant = _ts_entrada.get(f"{sym}|{lado_oposto['direcao']}|{exch}", 0)
            if ts_ant and (time.time() - ts_ant) < ARB_INTERVALO_MIN:
                falta = int(ARB_INTERVALO_MIN - (time.time() - ts_ant))
                print(f"[SKIP] {sym} {exch}: {falta}s até liberar o lado contrário (intervalo mínimo).")
                continue
            print(f"[ARBITRAGEM] {sym} {exch}: abrindo {bdir} com {lado_oposto['direcao']} "
                  f"já aberto em ${ep_ant:,.4f} — setups independentes.")

        qty_calc = calc_qty(sym, ep, sp, exchange=exch)
        if qty_calc is None:
            print(f"[SKIP] {sym} {exch}: não deu pra montar um tamanho de posição coerente com o saldo/margem disponível.")
            continue

        res = broker_open_auto(sym, bdir, sp, tp, qty=qty_calc, exchange=exch)
        if res and res.get("ok"):
            # só entra no tracking (e pode virar win/loss depois) se a
            # corretora REALMENTE aceitou a ordem — senão os relatórios
            # contam trades fictícios que nunca existiram.
            novo_id = memory.get("next_id", len(memory["signals"])+1)
            memory["next_id"] = novo_id + 1
            sinal = {"id": novo_id, "symbol": sym, "direcao": bdir, "exchange": exch,
                     "entrada": ep, "stop": sp, "alvo": tp, "risco": risk, "rr": rr,
                     "qty_usada": qty_calc, "atr": entry.get("atr", 0), "origem": origem,
                     "order_id": res["order_id"],
                     "data": ts, "status": "aberto", "resultado": None}
            memory["signals"].append(sinal)
            _ts_entrada[f"{sym}|{bdir}|{exch}"] = time.time()
            if len(memory["signals"]) > 200: memory["signals"] = memory["signals"][-200:]
            algum_sucesso = True
            risco_brl_ex = risk * qty_calc * get_usd_brl()
            alvo_brl_ex  = risk * rr * qty_calc * get_usd_brl()
            # sem nome de corretora nem modo (SIMULAÇÃO/REAL/DEMO) aqui —
            # o bot fica em live no YouTube, essa notificação não pode
            # identificar a conta. Ver TAG_CONTA_REAL logo abaixo.
            blocos.append(
                f"\n✅ Ordem executada\n"
                f"📦 {qty_calc} {sym} | {leverage_de(exch)}x | "
                f"⚠️ R$ {risco_brl_ex:,.2f}  |  🏆 R$ {alvo_brl_ex:,.2f}\n"
                f"🆔 <code>{res['order_id']}</code>")
        elif res:
            blocos.append(f"\n❌ Ordem NÃO aberta: {res.get('error','?')}")
            print(f"[FALHOU] {sym} {exch} {bdir} qty={qty_calc}: {res.get('error','?')}")

    if not algum_sucesso:
        return  # nenhuma corretora abriu — não vale a pena notificar o grupo

    _setups_executados.add(chave_setup)
    if len(_setups_executados) > 300:
        _setups_executados.clear()   # evita crescer sem limite
    save_memory()

    if origem in ("M1-TECNICO", "M1-GATILHO", "M1-MACRO", "M1-ABC", "M1-FLUXO-COMPRA", "M1-FLUXO-VENDA"):
        info_extra = entry.get("rsi", "")
        desc_gatilho = f"📊 {info_extra}" if info_extra else "📊 Pernada de M1 corrigindo ~50%"
        desc_stop    = "🛑 Stop (técnico, origem M1)"
    elif origem.startswith("ANCORA-"):
        # compatibilidade com posições antigas (motor ANCORA-H4/H1/M30
        # de disparo direto, removido — agora o M1-TECNICO é o motor
        # âncora oficial, sempre acionado via M1)
        tf_nome = origem.split("-", 1)[1]
        info_extra = entry.get("rsi", "")
        desc_gatilho = f"📊 {info_extra}" if info_extra else f"📊 Pernada {tf_nome} corrigindo ~50%"
        desc_stop    = f"🛑 Stop (técnico, origem {tf_nome})"
    else:
        desc_gatilho = f"📊 Tendência H1 + pullback de RSI ({entry.get('rsi', '')})"
        desc_stop    = "🛑 Stop (ATR)"
    posicao_txt = ("\n📌 Posição de longo prazo (âncora)"
                   if origem == "M1-TECNICO" or origem.startswith("ANCORA-") else "")
    send_telegram(
        f"{emoji} <b>SINAL {action}</b> — {sym}  [{origem}]{posicao_txt}\n"
        f"{desc_gatilho}\n"
        f"💰 Entrada: <b>${ep:,.4f}</b>\n"
        f"{desc_stop}: <b>${sp:,.4f}</b>  🎯 Alvo: <b>${tp:,.4f}</b>\n"
        f"📐 R:R 1:{rr}\n"
        f"⚖️ Lote: {lote_texto()}"
        f"{''.join(blocos)}"
        f"{TAG_CONTA_REAL}\n⏰ {ts} (Brasília)")

    # gráfico é só um extra visual — nunca deixa um erro aqui afetar o
    # sinal (ordem/tracking) que já foi resolvido acima.
    try:
        tf_graf = _tf_do_grafico(origem)
        candles_graf = get_candles(sym, tf_graf, 90)
        if not candles_graf:
            print(f"[GRAFICO] {sym}: sem candles de {tf_graf} pra gerar o gráfico — pulado.", flush=True)
        else:
            caminho = gerar_grafico_sinal(sym, tf_graf, candles_graf, bdir, ep, sp, tp,
                                          titulo_extra=f"  [{origem}]")
            if not caminho:
                print(f"[GRAFICO] {sym}: gerar_grafico_sinal não devolveu imagem — pulado.", flush=True)
            else:
                send_telegram_foto(caminho, f"{emoji} {action} {sym} — {tf_graf.upper()}")
    except Exception as e:
        print(f"[GRAFICO] {sym}: {e}", flush=True)

def check_signals(price_map):
    ab = [s for s in memory.get("signals", []) if s["status"] == "aberto"]
    if not ab: return
    alt = False
    for s in ab:
        p = price_map.get(s.get("symbol", ""))
        if not p: continue
        sym = s.get("symbol", ""); ts = agora_br().strftime("%d/%m/%Y %H:%M")

        hit_tp = (s["direcao"]=="BUY" and p>=s["alvo"]) or (s["direcao"]=="SELL" and p<=s["alvo"])
        hit_sl = (s["direcao"]=="BUY"  and p<=s["stop"]) or (s["direcao"]=="SELL" and p>=s["stop"])
        # Usa o order_id (SIM-... só existe na ordem fake) em vez de
        # simulacao_de() de novo aqui: o .env pode ter mudado entre a
        # abertura e o fechamento do sinal (ex: SIMULACAO_BYBIT trocado
        # no meio do caminho), e o que importa é o que REALMENTE
        # aconteceu quando a ordem foi enviada, não o config atual.
        sim_do_sinal = str(s.get("order_id", "")).startswith("SIM-")

        # ── SIMULAÇÃO: checagem intra-candle ───────────────────────
        # Ler um único preço a cada 60s esconde o que aconteceu DENTRO
        # do minuto. Com stop técnico curto, o preço fura o stop e volta
        # sem o bot ver — e o trade era registrado como win. Aqui olhamos
        # a máxima e a mínima reais dos candles de M1 desde a entrada.
        if sim_do_sinal and not (hit_tp or hit_sl):
            try:
                c1 = get_candles(sym, "1m", 10)
            except Exception:
                c1 = None
            if c1:
                for c in c1[-3:]:
                    if s["direcao"] == "BUY":
                        tocou_sl = c["low"]  <= s["stop"]
                        tocou_tp = c["high"] >= s["alvo"]
                    else:
                        tocou_sl = c["high"] >= s["stop"]
                        tocou_tp = c["low"]  <= s["alvo"]
                    if tocou_sl or tocou_tp:
                        # Os dois no mesmo candle: não dá pra saber a ordem
                        # pelo OHLC. Assume o STOP — é a hipótese
                        # conservadora, e evita inflar o resultado.
                        hit_sl, hit_tp = tocou_sl, (tocou_tp and not tocou_sl)
                        break

        if hit_tp or hit_sl:
            # No real, SL e TP ficam NA CORRETORA e executam no preço
            # exato. Reproduzimos isso aqui — só pro sinal que É simulado
            # (com multi-corretora, um sinal real usa o preço de mercado
            # de verdade, não o valor exato do stop/alvo planejado).
            if sim_do_sinal:
                p = s["stop"] if hit_sl else s["alvo"]
            s["preco_saida"] = p; s["fechamento"] = ts
            lucro = (p - s["entrada"]) if s["direcao"]=="BUY" else (s["entrada"] - p)
            s["status"] = "win" if lucro >= 0 else "loss"
            if not sim_do_sinal:
                # sinal real: puxa o PnL de verdade da corretora (líquido
                # de taxa/slippage) pros relatórios usarem em vez do
                # cálculo estimado só por preço — pedido do Jon depois de
                # ver o resultado do bot divergir do extrato real.
                usd_real = pnl_real(sym, s["direcao"], s.get("exchange", EXCHANGE))
                if usd_real is not None:
                    s["resultado_usd"] = usd_real
            s["resultado"] = fmt_brl(resultado_brl(s))
            alt = True
            venceu = s["status"] == "win"
            # fixo em toda mensagem, sem identificar corretora nem modo —
            # o bot fica em live no YouTube, não pode expor qual conta é
            # real. Ver TAG_CONTA_REAL.
            if venceu:
                send_telegram(f"🏆 <b>TAKE PROFIT!</b> {sym} ✅ <b>{s['resultado']}</b>{TAG_CONTA_REAL}")
            else:
                send_telegram(f"🛑 <b>STOP LOSS</b> {sym} ❌ <b>{s['resultado']}</b>{TAG_CONTA_REAL}")

            # gráfico do resultado — mesmo esquema do sinal, só um extra
            # visual, nunca deixa erro aqui afetar o tracking já resolvido
            try:
                tf_graf = _tf_do_grafico(s.get("origem", "?"))
                candles_graf = get_candles(sym, tf_graf, 90)
                if not candles_graf:
                    print(f"[GRAFICO] {sym}: sem candles de {tf_graf} pra gerar o gráfico de resultado — pulado.", flush=True)
                else:
                    caminho = gerar_grafico_sinal(
                        sym, tf_graf, candles_graf, s["direcao"], s["entrada"], s["stop"], s["alvo"],
                        titulo_extra=f"  [{s.get('origem','?')}]", saida=p, resultado_txt=s["resultado"],
                        venceu=venceu, qty=s.get("qty_usada"))
                    if not caminho:
                        print(f"[GRAFICO] {sym}: gerar_grafico_sinal não devolveu imagem — pulado.", flush=True)
                    else:
                        send_telegram_foto(caminho, f"{'🏆' if venceu else '🛑'} {sym} — {tf_graf.upper()}")
            except Exception as e:
                print(f"[GRAFICO] {sym}: {e}", flush=True)
    if alt: save_memory()

# ═══════════════════════════════════════════════════════════════
#  COMANDOS
# ═══════════════════════════════════════════════════════════════
def _ok_msg(res, descricao):
    if res and res.get("ok"):
        return f"✅ {descricao}\n🆔 <code>{res.get('order_id','')}</code>"
    return f"❌ Erro: {res.get('error','?') if res else 'sem resposta'}"

def handle_command(text, chat_id):
    parts = text.strip().split()
    cmd   = parts[0].lower()

    # ── HELP ────────────────────────────────────────────────
    if cmd in ("/help", "/start", "/commands"):
        modo = modo_texto()
        send_telegram(
            f"🤖 <b>Tron Forex Bot - Dev: Jon Padilha</b> [{modo}]\n\n"
            "📊 <b>MERCADO:</b>\n"
            "/status · /analise · /diag\n"
            "/performance [hoje|semana|mes|N|tudo] · /motores · /zerar · /backup · /reiniciar · /relatorio · /hoje · /saldo · /patrimonio · /posicoes · /ordem (id) · /debug (par) · /editar (par) sl= tp= · /fundir (par) · /status_freio · /retomar · /freio_on · /freio_off\n"
            "/lote · /lote 2 · /lote 0.5 · /lote fixo 0.01 · /lote auto\n\n"
            "🌊 <b>FLUXO M1 PURO (automático, em paralelo):</b>\n"
            "Dois motores independentes (compra e venda), mesmo critério: "
            "perna + correção ~50% direto no M1, sem esperar H4/H1/M15, "
            "stop na origem da pernada, alvo no próximo topo/fundo real do "
            "M1 (cai pra projeção 38.2% só quando não dá pra achar um nível "
            "de estrutura). RR sai técnico — cada leg encadeia na próxima "
            "assim que o fundo/topo anterior é rompido, pra sempre. Os dois "
            "podem disparar no mesmo par ao mesmo tempo (arbitragem — "
            "precisa ARBITRAGEM_ATIVA=true pra coexistir). Trades curtos e "
            "frequentes. Sem comando — sempre ligado.\n\n"
            "⚓ <b>MOTOR ÂNCORA — posições de longo prazo (automático):</b>\n"
            "Cascata H4→H1→M15→M5 (contexto_maior) acha a pernada maior "
            "corrigindo 38-65% e define direção + alvo (estrutura/projeção "
            "da âncora) — mas quem ACIONA a entrada e define o STOP é "
            "sempre o M1, esperando ele confirmar em correspondência. "
            "Nunca abre ordem direto no H4/H1/M30. Cooldown mais longo, "
            "posição fica dias/semanas buscando. Sem comando — sempre "
            "ligado.\n\n"
            "🗺️ <b>VISÃO MACRO (M1 dentro de cenário maior):</b>\n"
            "/macro BTC BUY 105000 118000 [nota]\n"
            "/macro BTC BUY auto auto [nota]  (stop técnico + alvo M15)\n"
            "/macro_off BTC · /macro_status\n\n"
            "⚡ <b>GATILHO MANUAL (stop técnico do M1, alvo de M15):</b>\n"
            "/gatilho compra BTC · /gatilho venda BTC\n\n"
            "💵 <b>SPOT (a mercado):</b>\n"
            "/comprar BTC 0.001\n"
            "/vender BTC 0.001\n"
            "/vendertudo BTC\n\n"
            "⚡ <b>FUTUROS (a mercado):</b>\n"
            "/long BTC 0.001\n"
            "/short BTC 0.001\n"
            "/long BTC 0.001 sl=60000 tp=65000\n\n"
            "📌 <b>LIMITADAS (agenda no preco):</b>\n"
            "/ls BTC 0.001 62000     comprar spot\n"
            "/lsv BTC 0.001 65000    vender spot\n"
            "/ll BTC 0.001 62000     long futuros\n"
            "/lsh BTC 0.001 65000    short futuros\n\n"
            "🔒 <b>FECHAR / CANCELAR:</b>\n"
            "/fechar BTC\n"
            "/fechar tudo\n"
            "/cancelar BTC\n\n"
            "📸 Envie print para analise IA", chat_id)

    # ── STATUS ──────────────────────────────────────────────
    # ── DIAGNÓSTICO: por que não está gerando sinal ──────────
    elif cmd == "/diag":
        alvo = _parse_sym(parts[1]) if len(parts) > 1 else None
        lista = [alvo] if alvo else list(SYMBOLS.keys())[:6]
        linhas = [f"🔍 <b>Diagnóstico</b> [{modo_texto()}]\n"]
        for sym in lista:
            tf_a, ctx = contexto_maior(sym)
            if not ctx:
                linhas.append(f"⚪ <b>{sym}</b>: nenhum tempo gráfico com perna corrigindo 38–65% agora.")
                continue
            dirc = ctx["direcao"]
            linhas.append(f"🟡 <b>{sym}</b>: perna {tf_a.upper()} {dirc} corrigindo {int(ctx['retr']*100)}%")
            preco = check_macro_m1(sym, {"direcao": dirc})
            if not preco:
                linhas.append("   └ M1 ainda não confirmou (perna própria + correção ~50% + candle a favor).")
                continue
            sl = stop_tecnico_m1(sym, dirc)
            tp = alvo_projecao_382(preco, dirc, ctx)
            if sl is None or tp is None:
                linhas.append("   └ não deu pra montar stop/alvo técnico.")
                continue
            risco = abs(preco - sl); alvo_d = abs(tp - preco)
            rr = alvo_d / risco if risco > 0 else 0
            q = calc_qty(sym, preco, sl)
            ja = next((x for x in memory.get("signals", [])
                       if x["symbol"] == sym and x["status"] == "aberto"), None)
            if ja:
                linhas.append(f"   └ pronto pra disparar, mas já tem {ja['direcao']} aberto no par.")
            else:
                linhas.append(f"   └ ✅ SETUP VÁLIDO — RR {rr:.2f}, qty {q}. Dispara no próximo ciclo.")
        linhas.append(f"\n📂 Trades abertos: {trades_abertos_agora()} | Modo livre 🔓 (sem travas de valor)")
        send_telegram("\n".join(linhas), chat_id)

    elif cmd == "/status":
        modo = modo_texto()
        msg  = f"📡 <b>Status</b> [{modo}]\n"
        for sym in SYMBOLS:
            d = analyze_symbol(sym)
            if not d: msg += f"⚠️ {sym}\n"; continue
            em = "🟢" if d["tendencia"]=="up" else ("🔴" if d["tendencia"]=="down" else "⚪")
            msg += f"{em} <b>{sym}</b> ${d['price']:,.4f} {d['tendencia'].upper()} RSI:{d['rsi']}\n"
        corretoras_txt = " + ".join(f"{nome_corretora(e)} [{modo_texto_ex(e)}]" for e in EXCHANGES_ATIVAS)
        msg += (f"\n🏦 Corretoras ativas: {corretoras_txt}\n"
                f"🛡️ Freio diário: {'🛑 pausado' if _freio_diario.get('pausado') else '✅ ativo'} | "
                f"Trades abertos: {trades_abertos_agora()} (sem limite)\n"
                f"⚖️ Lote: {lote_texto()}\n"
                f"⏰ {agora_br().strftime('%d/%m %H:%M')} (Brasília)")
        send_telegram(msg, chat_id)

    # ── ANALISE ─────────────────────────────────────────────
    elif cmd == "/analise":
        msg = "📊 <b>Analise</b>\n"
        for sym in SYMBOLS:
            d = analyze_symbol(sym)
            if not d: continue
            em = "📈" if d["tendencia"]=="up" else ("📉" if d["tendencia"]=="down" else "⚪")
            sig = "🚀" if d["entry"] else "⏳"
            msg += (f"{em} <b>{sym}</b> ${d['price']:,.4f} {sig}\n"
                    f"RSI:{d['rsi']}  ATR:{d['atr']:.4f}"
                    f"{'  ⚠️pico vol.' if d['pico_vol'] else ''}\n")
        send_telegram(msg, chat_id)

    # ── PERFORMANCE ─────────────────────────────────────────
    elif cmd == "/performance":
        periodo_arg = parts[1] if len(parts) > 1 else None
        sinais, periodo_label = filtrar_por_periodo(memory.get("signals", []), periodo_arg)
        if not sinais:
            send_telegram(f"📊 Nenhum sinal no período: {periodo_label}. (Uso: /performance [hoje|semana|mes|N|tudo])", chat_id); return
        wins    = [s for s in sinais if s["status"] == "win"]
        losses  = [s for s in sinais if s["status"] == "loss"]
        abertos = [s for s in sinais if s["status"] == "aberto"]
        total_f = len(wins) + len(losses)
        wr      = (len(wins)/total_f*100) if total_f > 0 else 0
        fechados = wins + losses
        lucro_brl = sum(resultado_brl(s) or 0 for s in fechados)
        sym_stats = ""
        for sym in SYMBOLS:
            ss = [s for s in sinais if s.get("symbol") == sym]
            if not ss: continue
            sw = [s for s in ss if s["status"] == "win"]
            sl = [s for s in ss if s["status"] == "loss"]
            tf2 = len(sw) + len(sl)
            wr2 = (len(sw)/tf2*100) if tf2 > 0 else 0
            brl2 = sum(resultado_brl(s) or 0 for s in (sw + sl))
            sym_stats += f"• {sym}: {len(sw)}W/{len(sl)}L WR:{wr2:.0f}% {fmt_brl(brl2)}\n"
        saldo_txt = saldo_brl_txt()
        send_telegram(
            f"📊 <b>Performance — {periodo_label}</b>\n"
            f"________________________\n"
            f"📈 Total: <b>{len(sinais)}</b> sinais\n"
            f"✅ Wins: <b>{len(wins)}</b>  ❌ Losses: <b>{len(losses)}</b>  ⏳ <b>{len(abertos)}</b>\n"
            f"🎯 Win Rate: <b>{wr:.1f}%</b>\n"
            f"💰 Resultado: <b>{fmt_brl(lucro_brl)}</b>"
            f"{saldo_txt}\n"
            f"________________________\n"
            f"{sym_stats}"
            f"________________________\n"
            f"⏰ {agora_br().strftime('%d/%m %H:%M')} (Brasília)", chat_id)

    # ── RELATORIO ───────────────────────────────────────────
    # ── DESEMPENHO POR MOTOR E POR GATILHO ──────────────────
    elif cmd in ("/motores", "/pormotor"):
        sinais = [x for x in memory.get("signals", []) if x["status"] in ("win", "loss")]
        if len(parts) > 1:
            sinais = filtrar_por_periodo(sinais, parts[1])
        if not sinais:
            send_telegram("Nenhum trade fechado ainda pra analisar.", chat_id); return

        def agrupa(chave_fn, titulo):
            grupos = {}
            for x in sinais:
                k = chave_fn(x)
                if not k: continue
                g = grupos.setdefault(k, {"w": 0, "l": 0, "brl": 0.0,
                                          "g_brl": 0.0, "p_brl": 0.0})
                v = resultado_brl(x) or 0.0
                g["brl"] += v
                if x["status"] == "win":
                    g["w"] += 1; g["g_brl"] += v
                else:
                    g["l"] += 1; g["p_brl"] += abs(v)
            if not grupos: return ""
            txt = f"\n<b>{titulo}</b>\n"
            # ordena pelo resultado, do melhor pro pior
            for k, g in sorted(grupos.items(), key=lambda kv: -kv[1]["brl"]):
                n = g["w"] + g["l"]
                wr = (g["w"] / n * 100) if n else 0
                gm = g["g_brl"] / g["w"] if g["w"] else 0
                pm = g["p_brl"] / g["l"] if g["l"] else 0
                rr = (gm / pm) if pm else 0
                # taxa de acerto necessária pra empatar com esse R:R
                be = (1 / (1 + rr) * 100) if rr else 0
                marca = "🟢" if g["brl"] >= 0 else "🔴"
                txt += (f"{marca} <b>{k}</b> — {n} trades | WR {wr:.0f}%\n"
                        f"    {fmt_brl(g['brl'])} | ganho médio R$ {gm:.2f} | perda média R$ {pm:.2f}\n"
                        f"    R:R real 1:{rr:.2f} → precisa de {be:.0f}% pra empatar"
                        f"{' ✅' if wr >= be and be else ' ⚠️'}\n")
            return txt

        def gatilho_de(x):
            """Extrai o tipo de gatilho da descrição salva no sinal."""
            d = (x.get("rsi") or "")
            if not isinstance(d, str): return None
            d = d.lower()
            if "3 topos" in d or "3 fundos" in d: return "3 topos/fundos + ABC"
            if "sub-perna" in d:                  return "ABC em construção"
            if "candle corrigindo" in d:          return "candle com retração"
            # "pernada" cobre tanto o texto antigo ("pernada M1 corrigida")
            # quanto o novo ("pernada corrigida") — esse gatilho é sempre
            # do M1 (Fluxo M1 puro ou M1-TECNICO acionando dentro da
            # âncora maior).
            if "pernada" in d:                    return "pernada corrigida 50%"
            if "pullback" in d or "tendência" in d: return "RSI + tendência"
            return None

        def ancora_de(x):
            d = (x.get("rsi") or "")
            if not isinstance(d, str): return None
            for tf in ("4H", "1H", "30M", "15M", "5M"):
                if d.upper().startswith(tf): return f"âncora {tf}"
            return None

        total = sum(resultado_brl(x) or 0 for x in sinais)
        w = len([x for x in sinais if x["status"] == "win"])
        msg = (f"📊 <b>Desempenho por motor</b>\n"
               f"{len(sinais)} trades | WR {w/len(sinais)*100:.0f}% | {fmt_brl(total)}\n")
        msg += agrupa(lambda x: x.get("origem", "?"), "Por motor")
        msg += agrupa(gatilho_de, "Por gatilho de M1")
        msg += agrupa(ancora_de, "Por tempo gráfico âncora")
        msg += ("\n💡 O ✅/⚠️ compara a taxa de acerto real com a "
                "necessária pro R:R daquele grupo.")
        send_telegram(msg, chat_id)

    # ── ZERAR HISTÓRICO ─────────────────────────────────────
    elif cmd == "/zerar":
        confirmou = len(parts) > 1 and parts[1].lower() in ("confirmar", "sim", "ok")
        fechados = len([x for x in memory.get("signals", []) if x["status"] in ("win","loss")])
        abertos  = len([x for x in memory.get("signals", []) if x["status"] == "aberto"])

        if not confirmou:
            send_telegram(
                f"⚠️ <b>Zerar histórico</b>\n\n"
                f"Isso apaga {fechados} trade(s) fechado(s) e {abertos} aberto(s) "
                f"do registro do bot. O relatório recomeça do zero a partir de agora.\n\n"
                f"⚠️ Não fecha posições na corretora — só limpa o histórico interno.\n"
                f"{'🧪 Você está em SIMULAÇÃO, então não há posição real.' if SIMULACAO else '🔴 MODO REAL: confira /posicoes antes.'}\n\n"
                f"Para confirmar: <code>/zerar confirmar</code>", chat_id)
            return

        # backup antes de apagar — dá pra recuperar se foi engano
        try:
            with open("memory_backup.json", "w") as f:
                json.dump(memory, f)
            bkp = "💾 backup salvo em memory_backup.json"
        except Exception as e:
            bkp = f"⚠️ não consegui salvar backup ({e})"

        memory["signals"] = []
        _setups_executados.clear()
        _ultimo_gatilho.clear()
        _ts_entrada.clear()
        last_signal_time.clear()
        save_memory()

        send_telegram(
            f"🧹 <b>Histórico zerado.</b>\n"
            f"{fechados} fechado(s) e {abertos} aberto(s) removidos.\n"
            f"{bkp}\n\n"
            f"📊 Operando: {', '.join(SYMBOLS.keys())}\n"
            f"O relatório recomeça a partir de agora.", chat_id)

    elif cmd == "/backup":
        n = len(memory.get("signals", []))
        _push_github(forcar=True)
        send_telegram(f"💾 Backup enviado ao GitHub ({n} sinais no registro).", chat_id)

    elif cmd == "/reiniciar":
        send_telegram("🔄 Buscando atualização no GitHub...", chat_id)
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        # memory.json é versionado (o backup automático usa a API do
        # GitHub direto), mas o arquivo local muda toda hora conforme o
        # bot roda — sempre ia conflitar com git pull. Descarta só as
        # mudanças locais NESSE arquivo antes de puxar (o bot recria
        # sozinho no próximo save; nunca é a versão "certa" de qualquer
        # jeito, é só o último save automático).
        subprocess.run(["git", "checkout", "--", "memory.json"],
                       cwd=repo_dir, capture_output=True, text=True, timeout=10)
        try:
            r = subprocess.run(["git", "pull", "--quiet", "origin", "main"],
                               cwd=repo_dir, capture_output=True, text=True, timeout=30)
        except Exception as e:
            send_telegram(f"❌ Não consegui rodar git pull: {e}", chat_id); return
        if r.returncode != 0:
            send_telegram(f"❌ git pull falhou, NÃO reiniciei:\n<code>{r.stderr.strip()[:500]}</code>", chat_id)
            return
        caminho = os.path.abspath(__file__)
        try:
            with open(caminho, encoding="utf-8") as f:
                ast.parse(f.read())
        except SyntaxError as e:
            send_telegram(f"❌ Código atualizado tem erro de sintaxe, NÃO reiniciei:\n<code>{e}</code>", chat_id)
            return
        save_memory(forcar_github=True)
        send_telegram("✅ Atualizado e validado — reiniciando agora...", chat_id)
        time.sleep(1)
        os.execv(sys.executable, [sys.executable, caminho])

    elif cmd == "/relatorio":
        sinais = memory.get("signals", [])
        if not sinais: send_telegram("📊 Nenhum sinal ainda.", chat_id); return
        wins   = [s for s in sinais if s["status"] == "win"]
        losses = [s for s in sinais if s["status"] == "loss"]
        ab     = [s for s in sinais if s["status"] == "aberto"]
        tf2    = len(wins) + len(losses)
        wr     = (len(wins)/tf2*100) if tf2 > 0 else 0
        rn_brl = sum(resultado_brl(s) or 0 for s in (wins + losses))
        send_telegram(
            f"📊 <b>Relatorio</b>\n"
            f"✅ {len(wins)}W ❌ {len(losses)}L ⏳ {len(ab)}\n"
            f"🎯 WR: <b>{wr:.0f}%</b> 💰 <b>{fmt_brl(rn_brl)}</b>\n"
            f"⏰ {agora_br().strftime('%d/%m %H:%M')} (Brasília)", chat_id)
        for sym in SYMBOLS:
            ss = [s for s in sinais if s.get("symbol") == sym]
            if not ss: continue
            linhas = []
            for s in ss[-10:]:
                em = "✅" if s["status"]=="win" else ("❌" if s["status"]=="loss" else "⏳")
                linhas.append(f"{em} #{s['id']} {s.get('direcao','?')} "
                              f"${s['entrada']:,.2f}→{s.get('resultado','aberto')}")
            send_telegram(f"📋 <b>{sym}</b>\n"+"\n".join(linhas), chat_id)

    # ── HOJE ────────────────────────────────────────────────
    elif cmd == "/hoje":
        sinais = memory.get("signals", [])
        hoje   = agora_br().strftime("%d/%m/%Y")
        hs     = [s for s in sinais if s.get("data", "").startswith(hoje)]
        if not hs: send_telegram(f"📅 Nenhum sinal hoje ({hoje}).", chat_id); return
        wh = [s for s in hs if s["status"] == "win"]
        lh = [s for s in hs if s["status"] == "loss"]
        rn_brl = sum(resultado_brl(s) or 0 for s in (wh + lh))
        wr = (len(wh)/max(len(wh)+len(lh), 1))*100
        hist = "\n".join(
            f"{'✅' if s['status']=='win' else '❌' if s['status']=='loss' else '⏳'} "
            f"#{s['id']} {s.get('symbol','')} {s.get('direcao','?')}"
            f"→{s.get('resultado','aberto')}" for s in hs)
        saldo_txt = saldo_brl_txt()
        send_telegram(
            f"📅 <b>Hoje</b> ({hoje})\n"
            f"✅{len(wh)} ❌{len(lh)} WR:{wr:.0f}% {fmt_brl(rn_brl)}{saldo_txt}\n{hist}", chat_id)

    # ── SALDO ───────────────────────────────────────────────
    elif cmd == "/saldo":
        r = broker_account()
        if not r or r.get("retCode") != 0:
            send_telegram(f"❌ {r}", chat_id); return
        coins  = r.get("result", {}).get("list", [{}])[0].get("coin", [])
        linhas = [f"• {c['coin']}: {float(c.get('equity') or c.get('walletBalance', 0)):.4f}"
                  for c in coins if float(c.get("equity") or c.get("walletBalance", 0)) > 0]
        modo = modo_texto()
        send_telegram(f"💰 <b>Saldo [{modo}]</b>\n" + ("\n".join(linhas) or "Vazio"), chat_id)

    # ── PATRIMONIO (depósito + lucro manual + saldo atual, tudo em BRL) ─
    elif cmd == "/patrimonio":
        saldo = get_patrimonio_usdt()
        if not saldo:
            send_telegram("❌ Não consegui buscar o saldo da corretora agora.", chat_id); return
        saldo_brl = saldo * get_usd_brl()
        total_com_manual = saldo_brl + MANUAL_PROFITS_BRL
        resultado_liquido = total_com_manual - DEPOSITO_TOTAL_BRL
        roi_pct = (resultado_liquido / DEPOSITO_TOTAL_BRL * 100) if DEPOSITO_TOTAL_BRL > 0 else 0
        send_telegram(
            f"💼 <b>Patrimônio Geral</b>\n"
            f"________________________\n"
            f"📥 Depósito total: {fmt_num_brl(DEPOSITO_TOTAL_BRL)}\n"
            f"✋ Lucro manual (fora do robô, já realizado): {fmt_num_brl(MANUAL_PROFITS_BRL)}\n"
            f"🏦 Saldo atual na corretora (Trading Unificado): {fmt_num_brl(saldo_brl)}\n"
            f"________________________\n"
            f"💰 Resultado líquido total: <b>{fmt_brl(resultado_liquido)}</b>\n"
            f"📈 ROI sobre o depósito: <b>{roi_pct:+.1f}%</b>\n"
            f"⏰ {agora_br().strftime('%d/%m %H:%M')} (Brasília)", chat_id)

    # ── POSICOES ────────────────────────────────────────────
    elif cmd == "/posicoes":
        # consulta TODAS as corretoras ativas — com mais de uma ligada
        # (EXCHANGES_ATIVAS), a mesma posição pode existir em lugares
        # diferentes ao mesmo tempo, e cada uma tem sua própria API.
        lista = []
        for exch in EXCHANGES_ATIVAS:
            r = broker_positions(exch)
            if r and r.get("retCode") == 0:
                for p in r.get("result", {}).get("list", []):
                    if float(p.get("size", 0)) > 0:
                        lista.append((exch, p))
        if not lista: send_telegram("📭 Nenhuma posicao aberta.", chat_id); return
        cotacao = get_usd_brl()
        msg = "📊 <b>Posições abertas</b>\n"
        for exch, p in lista:
            pnl  = float(p.get("unrealisedPnl", 0)); ep = float(p.get("avgPrice", 0))
            mark = float(p.get("markPrice", 0) or 0)
            qty  = float(p.get("size", 0) or 0)
            sl_v = p.get("stopLoss"); tp_v = p.get("takeProfit")
            sl   = sl_v or "—"; tp = tp_v or "—"
            margem = float(p.get("positionIM", 0) or 0)
            roi  = (pnl / margem * 100) if margem > 0 else 0
            em   = "🟢" if pnl >= 0 else "🔴"
            risco_txt, alvo_txt = "", ""
            try:
                if sl_v: risco_txt = f"  |  ⚠️ Risco: R$ {abs(ep - float(sl_v)) * qty * cotacao:,.2f}"
                if tp_v: alvo_txt  = f"  |  🏆 Potencial: R$ {abs(float(tp_v) - ep) * qty * cotacao:,.2f}"
            except (TypeError, ValueError):
                pass
            msg += (f"{em} <b>{p['side']} {p.get('size')} {p['symbol']}</b> ({nome_corretora(exch)})\n"
                    f"   Entrada: ${ep:,.4f} | Atual: ${mark:,.4f}\n"
                    f"   🛑 SL: {sl}  🎯 TP: {tp}{risco_txt}{alvo_txt}\n"
                    f"   PnL: R$ {pnl*cotacao:+,.2f} (${pnl:+.2f}) | ROI: {roi:+.1f}%\n")
        send_telegram(msg, chat_id)

    # ── ORDEM (diagnóstico por ID) ───────────────────────────
    elif cmd == "/ordem":
        if len(parts) < 2: send_telegram("Uso: /ordem (id)", chat_id); return
        oid = parts[1]
        r_open = bybit_get("/v5/order/realtime", {"category": "linear", "orderId": oid, "settleCoin": "USDT"})
        r_hist = bybit_get("/v5/order/history",  {"category": "linear", "orderId": oid, "settleCoin": "USDT"})
        achado = None
        origem = ""
        for r, tag in ((r_open, "aberta"), (r_hist, "histórico")):
            if r and r.get("retCode") == 0:
                lst = r.get("result", {}).get("list", [])
                if lst:
                    achado = lst[0]; origem = tag; break
        if not achado:
            send_telegram(f"❌ Não encontrei nenhuma ordem com ID <code>{oid}</code>.", chat_id); return
        msg = (f"🔎 <b>Ordem</b> ({origem}) <code>{oid}</code>\n"
               f"Símbolo: {achado.get('symbol')}\n"
               f"Lado: {achado.get('side')}  Qtd: {achado.get('qty')}\n"
               f"Status: <b>{achado.get('orderStatus')}</b>\n"
               f"Preço médio de execução: {achado.get('avgPrice') or '—'}\n"
               f"SL: {achado.get('stopLoss') or '—'}  TP: {achado.get('takeProfit') or '—'}\n"
               f"Motivo rejeição: {achado.get('rejectReason') or '—'}\n"
               f"Criada: {achado.get('createdTime')}  Atualizada: {achado.get('updatedTime')}")
        send_telegram(msg, chat_id)

    # ── DEBUG (diagnóstico ao vivo por símbolo) ──────────────
    elif cmd == "/debug":
        if len(parts) < 2: send_telegram("Uso: /debug BTC", chat_id); return
        sym = parts[1].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        if sym not in SYMBOLS:
            send_telegram(f"❌ Símbolo {sym} não configurado no robô.", chat_id); return
        d = analyze_symbol(sym)
        if not d:
            send_telegram(f"❌ Sem candles suficientes pra {sym} agora (precisa de histórico de H1 e M15).", chat_id); return
        entry, entry_m5 = d["entry"], d.get("entry_m5")
        msg = (f"🔍 <b>Debug {sym}</b>\n"
               f"Preço: ${d['price']:,.4f}\n"
               f"Tendência H1 (EMA{EMA_RAPIDA}/EMA{EMA_LENTA}): <b>{d['tendencia']}</b>\n"
               f"RSI({RSI_PERIODO}) M15 agora: <b>{d['rsi']}</b>  (gatilho compra ≥{RSI_PULLBACK:.0f} vindo de baixo, "
               f"venda ≤{100-RSI_PULLBACK:.0f} vindo de cima)\n"
               f"ATR({ATR_PERIODO}) M15: {d['atr']:.4f}  |  Pico de volatilidade: {'⚠️ SIM' if d['pico_vol'] else 'não'}\n")
        if FNG_FILTRO_ATIVO:
            msg += f"Fear & Greed: {d.get('fng_valor','?')} ({d.get('fng_classe','?')}) {'⚠️ EXTREMO' if d.get('fng_extremo') else ''}\n"
        if d["tendencia"] == "neutral":
            msg += "\n⚪ H1 está NEUTRO (EMAs não alinhadas) — robô não opera nesse par até definir tendência."
        if entry:
            msg += (f"\n✅ <b>ENTRADA M15 ATIVA</b> — {entry['direcao']}\n"
                    f"Entrada: ${entry['entrada']:,.4f}\n"
                    f"Stop (ATR x{ATR_STOP_MULT}): ${entry['stop']:,.4f}\n"
                    f"Alvo (RR 1:{RR_ALVO}): ${entry['alvo']:,.4f}\n")
        else:
            msg += "\n❌ Sem entrada M15 agora."
        if entry_m5:
            msg += (f"\n⚡ <b>ENTRADA M5 ATIVA</b> (rápida, com padrão de candle) — {entry_m5['direcao']}\n"
                    f"Entrada: ${entry_m5['entrada']:,.4f}  Stop: ${entry_m5['stop']:,.4f}  Alvo: ${entry_m5['alvo']:,.4f}\n")
        if entry or entry_m5:
            msg += ("\n⚠️ Se apareceu aqui mas não abriu ordem, cheque /saldo, o freio diário (/status_freio) "
                    f"— o bot opera livre, sem teto de trades.")
        send_telegram(msg, chat_id)

    # ── RETOMAR (reativa o auto-trade depois do freio diário) ─
    elif cmd == "/retomar":
        _freio_diario["pausado"] = False
        send_telegram("✅ Pausa do freio de perda diária removida manualmente — auto-trade voltou a operar.", chat_id)

    # ── FREIO_ON / FREIO_OFF (liga/desliga o freio de perda diária) ─
    elif cmd == "/freio_off":
        _freio_diario["ativo"] = False
        _freio_diario["pausado"] = False
        send_telegram("🔕 Freio de perda diária DESLIGADO. O robô não vai mais se autopausar por queda de saldo no dia.", chat_id)

    elif cmd == "/freio_on":
        _freio_diario["ativo"] = True
        send_telegram(f"🔔 Freio de perda diária LIGADO (pausa automática se cair {PERDA_DIARIA_MAX_PCT*100:.0f}% no dia).", chat_id)

    # ── STATUS_FREIO (mostra o estado do freio diário) ───────
    elif cmd == "/status_freio":
        si = _freio_diario.get("saldo_inicial")
        msg = (f"🛡️ <b>Freio de perda diária</b>\n"
               f"Ativo: {'✅ sim' if _freio_diario.get('ativo') else '🔕 desligado'}\n"
               f"Dia: {_freio_diario.get('data')}\n"
               f"Saldo inicial do dia: ${si:.2f}\n" if si else
               f"🛡️ <b>Freio de perda diária</b>\nAtivo: {'✅ sim' if _freio_diario.get('ativo') else '🔕 desligado'}\n"
               f"Ainda não inicializado hoje.\n")
        msg += f"Pausado: {'🛑 SIM' if _freio_diario.get('pausado') else '✅ não'}\n"
        msg += f"Limite configurado: {PERDA_DIARIA_MAX_PCT*100:.0f}% de queda no dia\n"
        msg += f"Trades simultâneos: {trades_abertos_agora()} (sem teto)"
        send_telegram(msg, chat_id)

    # ── EDITAR (altera SL/TP de uma posição já aberta) ───────
    elif cmd == "/editar":
        # Uso: /editar BTC sl=61000 tp=64000   (pode mandar só sl= ou só tp=)
        # Com ARBITRAGEM_ATIVA e/ou mais de uma corretora ativa e a MESMA
        # posição aberta em mais de um lugar ao mesmo tempo, informe o
        # lado e/ou a corretora: /editar BTC compra bingx sl=...
        if len(parts) < 2:
            send_telegram("Uso: /editar BTC sl=61000 tp=64000 (pode mandar só um dos dois).\n"
                           "Com mais de uma posição no mesmo símbolo (arbitragem e/ou "
                           "duas corretoras), informe o lado e/ou a corretora: "
                           "/editar BTC compra bingx sl=... ou /editar BTC venda bybit tp=...", chat_id); return
        sym = parts[1].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        resto = parts[2:]
        lado_arg = None
        exch_arg = None
        while resto and resto[0].lower() in ("compra", "venda", "bingx", "bybit"):
            tok = resto[0].lower()
            if tok in ("compra", "venda"):
                lado_arg = "Buy" if tok == "compra" else "Sell"
            else:
                exch_arg = tok
            resto = resto[1:]
        novo_sl = novo_tp = None
        for p in resto:
            if p.lower().startswith("sl="): novo_sl = p.split("=", 1)[1]
            elif p.lower().startswith("tp="): novo_tp = p.split("=", 1)[1]
        if not novo_sl and not novo_tp:
            send_telegram("Informe pelo menos sl= ou tp=. Ex: /editar BTC sl=61000", chat_id); return

        # Descobre em qual(is) corretora(s)/lado existe posição real,
        # consultando cada corretora ativa (não só a global padrão) —
        # com EXCHANGES_ATIVAS tendo mais de uma, a mesma posição pode
        # existir em lugares diferentes ao mesmo tempo.
        exchanges_pra_checar = [exch_arg] if exch_arg else EXCHANGES_ATIVAS
        candidatos = []
        for exch in exchanges_pra_checar:
            r = broker_positions(exch)
            if r and r.get("retCode") == 0:
                for p in r.get("result", {}).get("list", []):
                    if p["symbol"] == sym and float(p.get("size", 0)) > 0:
                        candidatos.append((exch, p))
        if lado_arg:
            candidatos = [(e, p) for e, p in candidatos if p["side"] == lado_arg]
        if not candidatos:
            lado_txt = f" do lado {'compra' if lado_arg == 'Buy' else 'venda'}" if lado_arg else ""
            exch_txt = f" na {nome_corretora(exch_arg)}" if exch_arg else ""
            send_telegram(f"❌ Não achei posição aberta em {sym}{lado_txt}{exch_txt}.", chat_id); return
        if len(candidatos) > 1:
            opcoes = ", ".join(f"{nome_corretora(e)}/{'compra' if p['side']=='Buy' else 'venda'}"
                                for e, p in candidatos)
            send_telegram(f"⚠️ {sym} tem mais de uma posição aberta ao mesmo tempo ({opcoes}). "
                           "Informe o lado e/ou a corretora: /editar BTC compra bingx sl=...", chat_id); return
        exch, pos = candidatos[0]

        if exch == "bingx":
            res = _bingx_editar_sltp(sym, pos["side"], pos.get("size"), novo_sl, novo_tp)
        else:
            # positionIdx: 0 em one-way, 1/2 em hedge (arbitragem) — usa o
            # da própria posição, nunca fixo, senão a Bybit rejeita em
            # hedge mode.
            payload = {"category": "linear", "symbol": sym, "positionIdx": pos.get("positionIdx", 0)}
            if novo_sl: payload["stopLoss"] = novo_sl
            if novo_tp: payload["takeProfit"] = novo_tp
            r = bybit_post("/v5/position/trading-stop", payload)
            res = {"ok": bool(r and r.get("retCode") == 0),
                   "error": (r.get("retMsg", "?") if r else "sem resposta")}

        if res["ok"]:
            sincronizar_tracking(sym, sl_informado=novo_sl, tp_informado=novo_tp, origem="MANUAL", exchange=exch)
            partes = []
            if novo_sl: partes.append(f"🛑 SL → {novo_sl}")
            if novo_tp: partes.append(f"🎯 TP → {novo_tp}")
            lado_txt = 'compra' if pos['side'] == 'Buy' else 'venda'
            send_telegram(f"✅ {sym} ({lado_txt}, {nome_corretora(exch)}) atualizado:\n" + "\n".join(partes), chat_id)
        else:
            send_telegram(f"❌ Falha ao editar {sym} ({nome_corretora(exch)}): {res['error']}", chat_id)

    # ── FUNDIR (corrige rastreamento duplicado quando 2+ ordens no mesmo
    # símbolo viraram 1 posição só numa corretora — mantém só 1 registro
    # aberto POR LADO/corretora, sincronizado com a posição real, cancela
    # os outros pra não contar win/loss em dobro quando fechar).
    # Reaproveita sincronizar_tracking() — mesma lógica de fusão que já
    # roda depois de /editar e ordens manuais, só que chamada aqui pra
    # TODAS as corretoras ativas (antes era hardcoded só Bybit).
    elif cmd == "/fundir":
        if len(parts) < 2:
            send_telegram("Uso: /fundir BTC — quando 2+ ordens no mesmo par viraram 1 posição só na corretora.", chat_id); return
        sym = parts[1].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        antes = {s["id"] for s in memory.get("signals", []) if s["symbol"] == sym and s["status"] == "aberto"}
        if len(antes) < 2:
            send_telegram(f"{sym} não tem registros duplicados abertos (encontrei {len(antes)}). Nada pra fundir.", chat_id); return
        for exch in EXCHANGES_ATIVAS:
            sincronizar_tracking(sym, origem="MANUAL", exchange=exch)
        restantes = [s for s in memory.get("signals", []) if s["symbol"] == sym and s["status"] == "aberto"]
        canceladas = sorted(antes - {s["id"] for s in restantes})
        if not restantes:
            send_telegram(f"❌ {sym}: não achei posição real em nenhuma corretora ativa — não dá pra sincronizar.", chat_id); return
        resumo = "\n".join(
            f"Mantido: #{s['id']} ({nome_corretora(s.get('exchange', EXCHANGE))} {s['direcao']}, "
            f"entrada ${s['entrada']:,.4f}, qty {s['qty_usada']}, SL {s['stop']:,.4f}, TP {s['alvo']:,.4f})"
            for s in restantes)
        send_telegram(
            f"🔗 <b>{sym} fundido</b>\n{resumo}\n"
            f"Cancelados do tracking (não contam win/loss): "
            f"{', '.join('#'+str(i) for i in canceladas) or 'nenhum'}", chat_id)

    # ══ VISÃO MACRO (M1 dentro de cenário maior, definido por você) ═══
    # Caminho de entrada A MAIS, em paralelo ao M15/M5 — não mexe no que
    # já funciona. Uso: /macro BTC BUY 105000 118000 [nota livre]
    #   BTC     = símbolo
    #   BUY/SELL = direção do cenário (o robô só compra/vende nessa direção
    #              enquanto essa visão estiver ativa pro símbolo)
    #   105000  = stop (pode ser bem mais largo que o ATR, é seu, baseado
    #              na estrutura do gráfico maior)
    #   118000  = alvo (o alvo grande que você está enxergando no
    #              diário/H4 — sem limite, o robô vai atrás dele)
    #   nota    = opcional, só pra você lembrar o motivo depois
    elif cmd == "/macro":
        if len(parts) < 5:
            send_telegram(
                "Uso: /macro BTC BUY 105000 118000 [nota]\n"
                "ou:  /macro BTC BUY auto auto [nota]  (stop técnico do M1 + alvo de M15, sem digitar valor)\n"
                "BTC = símbolo | BUY/SELL = direção do cenário maior | "
                "stop | alvo | nota opcional.\n"
                "O robô passa a vigiar o M1 e só dispara entrada nessa direção, "
                "com esse stop/alvo, quando o M1 formar a pernadinha e corrigir "
                "~50% que você descreveu.", chat_id); return
        sym = parts[1].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        if sym not in SYMBOLS:
            send_telegram(f"❌ Símbolo {sym} não configurado no robô.", chat_id); return
        direcao = parts[2].upper()
        if direcao not in ("BUY", "SELL"):
            send_telegram("Direção precisa ser BUY ou SELL.", chat_id); return
        auto = parts[3].lower() == "auto" or parts[4].lower() == "auto"
        if auto:
            stop, alvo = "auto", "auto"
        else:
            try:
                stop = float(parts[3]); alvo = float(parts[4])
            except ValueError:
                send_telegram("Stop e alvo precisam ser números (ou 'auto'). Ex: /macro BTC BUY 105000 118000", chat_id); return
        nota = " ".join(parts[5:])
        memory["macro_views"][sym] = {
            "direcao": direcao, "stop": stop, "alvo": alvo, "nota": nota,
            "criado_em": agora_br().strftime("%d/%m/%Y %H:%M"), "ativo": True}
        save_memory()
        send_telegram(
            f"🗺️ <b>Visão macro ativada — {sym}</b>\n"
            f"Direção: {direcao}  |  Stop: {'técnico (origem M1)' if auto else stop}  |  "
            f"Alvo: {'estrutura M15' if auto else alvo}\n"
            f"{('Nota: ' + nota) if nota else ''}\n"
            f"Vigiando o M1 pra pernadinha + correção ~50% na direção certa. "
            f"Use /macro_off {parts[1]} pra desligar.", chat_id)

    elif cmd == "/macro_off":
        if len(parts) < 2:
            send_telegram("Uso: /macro_off BTC", chat_id); return
        sym = parts[1].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        if sym in memory["macro_views"]:
            del memory["macro_views"][sym]
            save_memory()
            send_telegram(f"🗺️ Visão macro de {sym} desligada.", chat_id)
        else:
            send_telegram(f"Não tinha visão macro ativa em {sym}.", chat_id)

    elif cmd == "/macro_status":
        views = memory.get("macro_views", {})
        if not views:
            send_telegram("🗺️ Nenhuma visão macro ativa no momento.", chat_id); return
        msg = "🗺️ <b>Visões macro ativas</b>\n"
        for sym, v in views.items():
            msg += (f"\n<b>{sym}</b> — {v['direcao']}\n"
                    f"Stop: {v['stop']}  |  Alvo: {v['alvo']}\n"
                    f"{('Nota: ' + v['nota'] + chr(10)) if v.get('nota') else ''}"
                    f"Desde: {v.get('criado_em','?')}\n")
        send_telegram(msg, chat_id)

    # ══ GATILHO MANUAL (você viu a pernadinha de M1 corrigir 38-50%, avisa e o robô abre na hora) ══
    # Uso: /gatilho compra BTC   ou   /gatilho venda BTC
    # Sem espera, sem detecção automática — é você confirmando visualmente
    # que o gatilho de M1 aconteceu. O robô entra IMEDIATAMENTE no preço
    # atual, com stop/alvo calculados pelo ATR do M1 (mesmo estilo do
    # M15/M5 que já funciona, só que no tempo gráfico de M1).
    elif cmd == "/gatilho":
        if len(parts) < 3:
            send_telegram(
                "Uso: /gatilho compra BTC  ou  /gatilho venda BTC\n"
                "Use quando você enxergar a pernadinha de M1 corrigindo entre "
                "38% e 50% — o robô abre a ordem na hora, no preço atual.", chat_id); return
        acao = parts[1].lower()
        if acao not in ("compra", "venda"):
            send_telegram("Ação precisa ser 'compra' ou 'venda'. Ex: /gatilho compra BTC", chat_id); return
        direcao = "BUY" if acao == "compra" else "SELL"
        sym = parts[2].upper()
        if not sym.endswith("USDT"): sym += "USDT"
        if sym not in SYMBOLS:
            send_telegram(f"❌ Símbolo {sym} não configurado no robô.", chat_id); return
        c1m = get_candles(sym, "1m", ATR_PERIODO + 10)
        if not c1m or len(c1m) < ATR_PERIODO + 2:
            send_telegram(f"❌ Não consegui pegar candles de M1 pra {sym} agora. Tenta de novo.", chat_id); return
        preco = get_last_price(sym) or c1m[-1]["close"]
        sl = stop_tecnico_m1(sym, direcao)
        tp = alvo_m15(sym, direcao)
        if sl is None or tp is None:
            send_telegram(f"❌ Não consegui calcular stop técnico / alvo de M15 pra {sym} agora. Tenta de novo.", chat_id); return
        if (direcao == "BUY" and (sl >= preco or tp <= preco)) or (direcao == "SELL" and (sl <= preco or tp >= preco)):
            send_telegram(
                f"⚠️ {sym}: a origem técnica do M1 ou o alvo de M15 não fazem "
                f"sentido com o preço atual (${preco:,.4f}) — pode ser que o "
                f"movimento já tenha ido longe demais. Sinal descartado.", chat_id); return
        a1 = atr(c1m, ATR_PERIODO)
        atr_now = a1[-1] if a1 and a1[-1] is not None else 0
        entry_gatilho = {"direcao": direcao, "entrada": preco, "stop": sl, "alvo": tp,
                          "atr": atr_now, "origem": "M1-GATILHO", "rsi": ""}
        fire_signal(sym, entry_gatilho, ignorar_travas=True)

    # ══ ORDENS SPOT ══════════════════════════════════════════

    elif cmd == "/comprar":
        if len(parts) < 3: send_telegram("Uso: /comprar BTC 0.001", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]
        res = order_spot(sym, "Buy", qty)
        send_telegram(_ok_msg(res, f"Spot BUY {qty} {sym}"), chat_id)

    elif cmd == "/vender":
        if len(parts) < 3: send_telegram("Uso: /vender BTC 0.001", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]
        res = order_spot(sym, "Sell", qty)
        send_telegram(_ok_msg(res, f"Spot SELL {qty} {sym}"), chat_id)

    elif cmd == "/vendertudo":
        if len(parts) < 2: send_telegram("Uso: /vendertudo BTC", chat_id); return
        sym = _parse_sym(parts[1])
        res = sell_all_spot(sym)
        send_telegram(_ok_msg(res, f"Spot SELL ALL {sym}"), chat_id)

    # ══ ORDENS FUTUROS ═══════════════════════════════════════

    elif cmd == "/long":
        if len(parts) < 3: send_telegram("Uso: /long BTC 0.001 [sl=60000] [tp=65000]", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]
        sl  = next((p.split("=")[1] for p in parts if p.lower().startswith("sl=")), None)
        tp  = next((p.split("=")[1] for p in parts if p.lower().startswith("tp=")), None)
        res = order_futures(sym, "Buy", qty, sl=sl, tp=tp)
        if res and res.get("ok"):
            sincronizar_tracking(sym, sl_informado=sl, tp_informado=tp, origem="MANUAL")
        send_telegram(_ok_msg(res, f"Futuros LONG {qty} {sym}"), chat_id)

    elif cmd == "/short":
        if len(parts) < 3: send_telegram("Uso: /short BTC 0.001 [sl=65000] [tp=60000]", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]
        sl  = next((p.split("=")[1] for p in parts if p.lower().startswith("sl=")), None)
        tp  = next((p.split("=")[1] for p in parts if p.lower().startswith("tp=")), None)
        res = order_futures(sym, "Sell", qty, sl=sl, tp=tp)
        if res and res.get("ok"):
            sincronizar_tracking(sym, sl_informado=sl, tp_informado=tp, origem="MANUAL")
        send_telegram(_ok_msg(res, f"Futuros SHORT {qty} {sym}"), chat_id)

    # ══ ORDENS LIMITADAS ═════════════════════════════════════

    elif cmd == "/ls":
        if len(parts) < 4: send_telegram("Uso: /ls BTC 0.001 62000", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]; price = parts[3]
        res = order_limit("spot", sym, "Buy", qty, price)
        send_telegram(_ok_msg(res, f"Limit Spot BUY {qty} {sym} @ ${float(price):,.2f}"), chat_id)

    elif cmd == "/lsv":
        if len(parts) < 4: send_telegram("Uso: /lsv BTC 0.001 65000", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]; price = parts[3]
        res = order_limit("spot", sym, "Sell", qty, price)
        send_telegram(_ok_msg(res, f"Limit Spot SELL {qty} {sym} @ ${float(price):,.2f}"), chat_id)

    elif cmd == "/ll":
        if len(parts) < 4: send_telegram("Uso: /ll BTC 0.001 62000 [sl=60000] [tp=65000]", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]; price = parts[3]
        sl  = next((p.split("=")[1] for p in parts if p.lower().startswith("sl=")), None)
        tp  = next((p.split("=")[1] for p in parts if p.lower().startswith("tp=")), None)
        res = order_limit("linear", sym, "Buy", qty, price, sl=sl, tp=tp)
        send_telegram(_ok_msg(res, f"Limit Long {qty} {sym} @ ${float(price):,.2f}"), chat_id)

    elif cmd == "/lsh":
        if len(parts) < 4: send_telegram("Uso: /lsh BTC 0.001 65000 [sl=67000] [tp=62000]", chat_id); return
        sym = _parse_sym(parts[1]); qty = parts[2]; price = parts[3]
        sl  = next((p.split("=")[1] for p in parts if p.lower().startswith("sl=")), None)
        tp  = next((p.split("=")[1] for p in parts if p.lower().startswith("tp=")), None)
        res = order_limit("linear", sym, "Sell", qty, price, sl=sl, tp=tp)
        send_telegram(_ok_msg(res, f"Limit Short {qty} {sym} @ ${float(price):,.2f}"), chat_id)

    # ══ FECHAR / CANCELAR ════════════════════════════════════

    elif cmd == "/fechar":
        arg = " ".join(parts[1:]).lower() if len(parts) > 1 else "tudo"
        if arg == "tudo":
            send_telegram("⚠️ Fechando todos futuros...", chat_id)
            simbolos_abertos = list({s["symbol"] for s in memory.get("signals", []) if s["status"] == "aberto"})
            close_futures_all()
            for sim in simbolos_abertos:
                sincronizar_fechamento(sim)
            send_telegram("✅ Todos futuros fechados.", chat_id)
        else:
            sym = _parse_sym(parts[1])
            ok, msg2 = close_futures_symbol(sym)
            if ok: sincronizar_fechamento(sym)
            send_telegram(f"{'✅' if ok else '❌'} {msg2}", chat_id)

    elif cmd == "/cancelar":
        if len(parts) < 2: send_telegram("Uso: /cancelar BTC", chat_id); return
        sym = _parse_sym(parts[1])
        cancel_open_orders(sym, "linear")
        cancel_open_orders(sym, "spot")
        send_telegram(f"✅ Ordens pendentes canceladas: {sym}", chat_id)

    # ── LOTE (ajusta a quantidade operada sem reiniciar o bot) ────
    elif cmd == "/lote":
        if len(parts) == 1:
            linhas = [f"⚖️ <b>Lote</b> — modo atual: {lote_texto()}\n"]
            for sym in SYMBOLS:
                d = analyze_symbol(sym)
                if not d:
                    linhas.append(f"⚪ {sym}: sem dado agora")
                    continue
                dist = d["atr"] * ATR_STOP_MULT
                q = calc_qty(sym, d["price"], d["price"] - dist)
                linhas.append(f"• <b>{sym}</b>: {q}")
            send_telegram("\n".join(linhas), chat_id)
            return
        arg = parts[1].lower()
        if arg == "auto":
            memory["config_lote"] = {"modo": "auto"}
            save_memory()
            send_telegram(f"✅ Lote: {lote_texto()}", chat_id)
            return
        if arg == "fixo":
            if len(parts) < 3:
                send_telegram("Uso: /lote fixo 0.01", chat_id); return
            try:
                valor = float(parts[2])
            except ValueError:
                send_telegram("Valor inválido. Uso: /lote fixo 0.01", chat_id); return
            memory["config_lote"] = {"modo": "fixo", "valor": valor}
            save_memory()
            send_telegram(f"✅ Lote: {lote_texto()}", chat_id)
            return
        try:
            mult = float(arg)
        except ValueError:
            send_telegram("Uso: /lote (mostra atual) · /lote 2 · /lote 0.5 · /lote fixo 0.01 · /lote auto", chat_id)
            return
        memory["config_lote"] = {"modo": "mult", "valor": mult}
        save_memory()
        send_telegram(f"✅ Lote: {lote_texto()}", chat_id)

    else:
        send_telegram("Comando nao reconhecido. /help", chat_id)

# ─── LOOP COMANDOS ───────────────────────────────────────────
def commands_loop():
    print("Ouvindo comandos...")
    while True:
        try:
            for upd in get_updates():
                msg  = upd.get("message") or upd.get("edited_message")
                if not msg: continue
                cid  = str(msg["chat"]["id"])
                text = msg.get("text", "")
                if text.startswith("/"):
                    print(f"[CMD] {text}")
                    handle_command(text, cid)
                elif msg.get("photo"):
                    photo   = msg["photo"][-1]
                    caption = msg.get("caption", "")
                    try:
                        img = download_photo(photo["file_id"])
                        threading.Thread(target=process_image,
                                         args=(img, cid, caption), daemon=True).start()
                    except Exception as e:
                        send_telegram(f"Erro foto: {e}", cid)
        except Exception as e:
            print(f"Erro cmd loop: {e}")
        time.sleep(2)

# ─── LOOP PRINCIPAL ──────────────────────────────────────────
_loop_n      = 0
STATUS_EVERY = max(1, int(4*3600/CHECK_INTERVAL))

def main_loop():
    global _loop_n
    while True:
        try:
            _loop_n += 1
            price_map = {}
            if _loop_n % STATUS_EVERY == 0:
                msg = f"📡 {agora_br().strftime('%d/%m %H:%M')} (Brasília)\n"
                for sym in SYMBOLS:
                    d = analyze_symbol(sym)
                    if d:
                        em = "🟢" if d["tendencia"]=="up" else ("🔴" if d["tendencia"]=="down" else "⚪")
                        msg += f"{em} {sym} ${d['price']:,.4f} RSI:{d['rsi']}\n"
                msg += f"\n🔓 Modo livre | Trades abertos: {trades_abertos_agora()}"
                send_telegram(msg)
            results = {}
            def _an(sym): results[sym] = analyze_symbol(sym)
            ths = [threading.Thread(target=_an, args=(s,), daemon=True) for s in SYMBOLS]
            for t in ths: t.start()
            for t in ths: t.join(timeout=30)
            for sym, data in results.items():
                if not data: continue
                price_map[sym] = data["price"]
                entry, entry_m5 = data["entry"], data.get("entry_m5")
                print(f"[{agora_br().strftime('%H:%M')}] {sym} ${data['price']:,.4f} "
                      f"tendência:{data['tendencia']} RSI:{data['rsi']} "
                      f"M15:{'SINAL' if entry else 'wait'} M5:{'SINAL' if entry_m5 else 'wait'}")
                now_ts = time.time()
                if entry:
                    key = f"{sym}_M15"
                    if now_ts - last_signal_time.get(key, 0) >= SIGNAL_COOLDOWN:
                        fire_signal(sym, entry)
                        last_signal_time[key] = now_ts
                    else:
                        print(f"  [{sym} M15 cooldown]")
                if entry_m5:
                    key = f"{sym}_M5"
                    if now_ts - last_signal_time.get(key, 0) >= SIGNAL_COOLDOWN:
                        fire_signal(sym, entry_m5)
                        last_signal_time[key] = now_ts
                    else:
                        print(f"  [{sym} M5 cooldown]")

                # ── MOTOR ÂNCORA (M1-TECNICO) — mesmo critério que você usa
                # na mão: pernada + correção ~50%, em CASCATA pelos tempos
                # gráficos maiores primeiro (H4 → H1 → M15 → M5) pra achar a
                # âncora (direção certa + alvo real da pernada maior), mas
                # quem ACIONA a entrada e define o STOP é sempre o M1,
                # puxando o gatilho fino DENTRO dessa janela — nunca entra
                # direto no tf âncora. Cooldown mais longo (SIGNAL_COOLDOWN_
                # ANCORA): são posições de longo prazo, não day trade. Roda
                # em paralelo ao M15/M5/macro acima, sem alterar nada deles.
                tf_ancora, ctx = contexto_maior(sym)
                if ANCORA_ATIVO and ctx:
                    direcao_tec = ctx["direcao"]
                    key = f"{sym}_M1TEC"
                    if now_ts - last_signal_time.get(key, 0) >= SIGNAL_COOLDOWN_ANCORA:
                        preco_tec = check_macro_m1(sym, {"direcao": direcao_tec})
                        if preco_tec:
                            sl_tec = stop_tecnico_m1(sym, direcao_tec)
                            # ALVO: se a âncora estiver em alargamento (megafone)
                            # ou correção lateral/ABC, o alvo da ESTRUTURA da
                            # âncora (fundo/topo dela, ou o 50% da pernada) tem
                            # prioridade — é o cenário real de continuação.
                            # Senão, cai na projeção de 38.2% da onda 1.
                            estrutura = estrutura_ancora(sym, tf_ancora)
                            tp_tec = None
                            alvo_desc = None
                            if estrutura and estrutura.get("figura") in ("megafone", "lateral"):
                                tp_tec = alvo_ancora(preco_tec, direcao_tec, estrutura)
                                if tp_tec is not None:
                                    alvo_desc = f"{'fundo' if direcao_tec == 'SELL' else 'topo'} {tf_ancora.upper()}"
                            if tp_tec is None:
                                tp_tec = alvo_projecao_382(preco_tec, direcao_tec, ctx)
                                alvo_desc = "proj. 38.2%"
                            if tp_tec is None:
                                tp_tec = ctx["alvo_50"]
                                alvo_desc = "50% da pernada"
                            if sl_tec is not None and tp_tec is not None:
                                risco_dist = abs(preco_tec - sl_tec)
                                alvo_dist  = abs(tp_tec - preco_tec)
                                if risco_dist > 0:
                                    desc_ancora = (f"{tf_ancora.upper()} {estrutura['figura']}"
                                                   if estrutura and estrutura.get("figura")
                                                   else f"{tf_ancora.upper()} corrigindo {int(ctx['retr']*100)}%")
                                    entry_tec = {"direcao": direcao_tec, "entrada": preco_tec,
                                                 "stop": sl_tec, "alvo": tp_tec,
                                                 "atr": data.get("atr", 0), "origem": "M1-TECNICO",
                                                 "rsi": (f"{desc_ancora}"
                                                         f" | M1: {_ultimo_gatilho.get(sym, 'gatilho')}"
                                                         f" | alvo {alvo_desc}")}
                                    fire_signal(sym, entry_tec, ignorar_travas=True)
                                    last_signal_time[key] = now_ts

                # ── ABC EM CONSTRUÇÃO — opera as sub-pernas da correção,
                # sem esperar a figura fechar. Roda em paralelo, direção
                # própria (a da sub-perna), não a da âncora. M1 é só
                # critério de ENTRADA e STOP — o ALVO é sempre o cenário
                # maior (H4/H1/M15) da âncora, nunca o impulso local de M1
                # (senão o alvo fica curto demais, RR invertido).
                if ABC_CONSTRUCAO_ATIVO and ctx:
                    key = f"{sym}_ABC"
                    if now_ts - last_signal_time.get(key, 0) >= SIGNAL_COOLDOWN_ABC:
                        try:
                            c1_abc = get_candles(sym, "1m", 80)
                            fig = detectar_figura_m1(c1_abc) if c1_abc else None
                            g = gatilho_abc_construcao(c1_abc, ctx["direcao"]) if c1_abc else None
                        except Exception as e:
                            print(f"[ABC] {sym}: {e}"); g = None; fig = None
                        if g and fig:
                            d_abc = g["direcao"]
                            # impulso que está sendo corrigido -> só pro filtro
                            # de ZONA de entrada (critério de M1); o alvo não
                            # sai daqui.
                            imp = impulso_corrigido_m1(c1_abc, d_abc)
                            # filtro de zona: só entra na região de retração
                            # onde as entradas realmente acontecem
                            pos_fib = zona_fib_ok(g["preco"], imp, d_abc) if imp else None
                            if ZONA_FIB_ATIVA and imp is not None and pos_fib is None:
                                print(f"[SKIP] {sym} ABC: fora da zona de retração 50-100%.")
                                sl_abc = tp_abc = None
                            else:
                                # stop técnico DA SUB-PERNA (o pivot que a
                                # originou), não a extremidade genérica dos
                                # últimos 20 candles.
                                sl_abc = g.get("origem_perna")
                                if sl_abc is None:
                                    sl_abc = stop_tecnico_m1(sym, d_abc)
                                else:
                                    # folga mínima pra não colar no pavio
                                    folga = abs(g["preco"] - sl_abc) * 0.10
                                    sl_abc = sl_abc - folga if d_abc == "BUY" else sl_abc + folga
                                # ALVO PRIMÁRIO: projeção de 38.2% da onda do
                                # timeframe âncora (H4/H1/M15) — mesmo cálculo
                                # do M1-TECNICO. Stop curto no M1, alvo longo
                                # no cenário maior.
                                tp_abc = alvo_projecao_382(g["preco"], d_abc, ctx)
                                if tp_abc is None:
                                    tp_abc = ctx["alvo_50"]
                                g["desc"] += " | alvo proj. 38.2% do cenário maior"
                            if sl_abc is not None and tp_abc is not None:
                                coerente = ((d_abc == "BUY"  and sl_abc < g["preco"] < tp_abc) or
                                            (d_abc == "SELL" and tp_abc < g["preco"] < sl_abc))
                                if coerente:
                                    entry_abc = {"direcao": d_abc, "entrada": g["preco"],
                                                 "stop": sl_abc, "alvo": tp_abc,
                                                 "atr": data.get("atr", 0), "origem": "M1-ABC",
                                                 "rsi": g["desc"]}
                                    fire_signal(sym, entry_abc, ignorar_travas=True)
                                    last_signal_time[key] = now_ts

                # ── FLUXO M1 PURO — réplica do operacional manual: DOIS
                # motores independentes, um só pra COMPRA e um só pra
                # VENDA, com o MESMO critério (perna + correção ~50% no
                # M1, stop na origem, alvo no próximo topo/fundo real) —
                # cada um com cooldown próprio, então os dois podem
                # disparar no mesmo ciclo. É exatamente a "arbitragem"
                # manual do Jon: dois gatilhos técnicos independentes que
                # às vezes coexistem no mesmo par (ARBITRAGEM_ATIVA=true
                # decide se os dois lados podem ficar abertos ao mesmo
                # tempo; senão, o segundo lado é ignorado até o primeiro
                # fechar). Roda em paralelo ao M1-TECNICO/M1-ABC (que
                # buscam alvo de M15/H4).
                if FLUXO_M1_ATIVO:
                    for direcao_fluxo, tag_fluxo in (("BUY", "M1-FLUXO-COMPRA"), ("SELL", "M1-FLUXO-VENDA")):
                        key = f"{sym}_{tag_fluxo}"
                        if now_ts - last_signal_time.get(key, 0) < SIGNAL_COOLDOWN_FLUXO:
                            continue
                        preco_fluxo = check_macro_m1(sym, {"direcao": direcao_fluxo})
                        if not preco_fluxo:
                            continue
                        sl_fluxo = stop_tecnico_m1(sym, direcao_fluxo)
                        # ALVO: o próximo topo/fundo REAL do M1 (estrutura),
                        # não uma projeção — é o desenho que o Jon opera na
                        # mão (perna + correção 50%, stop na origem, alvo no
                        # último fundo/topo) e o RR sai técnico, sem forçar
                        # um número maior. RR_MINIMO continua como piso de
                        # segurança pra descartar setups sem lógica. Só cai
                        # pra projeção de 38.2% quando a estrutura não dá um
                        # nível utilizável. Continua sem esperar H4/H1/M15
                        # de propósito (é o que diferencia esse motor do
                        # M1-TECNICO/M1-ABC).
                        tp_fluxo = alvo_m1_estrutura(sym, direcao_fluxo)
                        alvo_desc = "próximo topo" if direcao_fluxo == "BUY" else "próximo fundo"
                        if tp_fluxo is None:
                            ctx_fluxo = detectar_perna(sym, "1m")
                            if ctx_fluxo and ctx_fluxo["direcao"] == direcao_fluxo:
                                tp_fluxo = alvo_projecao_382(preco_fluxo, direcao_fluxo, ctx_fluxo)
                                alvo_desc = "projeção 38.2% da onda"
                        if sl_fluxo is None or tp_fluxo is None:
                            continue
                        coerente = ((direcao_fluxo == "BUY"  and sl_fluxo < preco_fluxo < tp_fluxo) or
                                    (direcao_fluxo == "SELL" and tp_fluxo < preco_fluxo < sl_fluxo))
                        if not coerente:
                            continue
                        entry_fluxo = {"direcao": direcao_fluxo, "entrada": preco_fluxo,
                                       "stop": sl_fluxo, "alvo": tp_fluxo,
                                       "atr": data.get("atr", 0), "origem": tag_fluxo,
                                       "rsi": (f"Fluxo M1 ({'compra' if direcao_fluxo == 'BUY' else 'venda'}): "
                                               f"{_ultimo_gatilho.get(sym, 'gatilho')}"
                                               f" | alvo {alvo_desc}")}
                        fire_signal(sym, entry_fluxo, ignorar_travas=True)
                        last_signal_time[key] = now_ts

                # ── visão macro (M1), só roda pra símbolo com /macro ativo ──
                view = memory.get("macro_views", {}).get(sym)
                if view and view.get("ativo"):
                    key = f"{sym}_MACRO"
                    if now_ts - last_signal_time.get(key, 0) >= SIGNAL_COOLDOWN:
                        preco_macro = check_macro_m1(sym, view)
                        if preco_macro:
                            sl_macro = view["stop"]; tp_macro = view["alvo"]
                            era_auto = sl_macro == "auto" or tp_macro == "auto"
                            if era_auto:
                                sl_macro = stop_tecnico_m1(sym, view["direcao"])
                                tp_macro = alvo_m15(sym, view["direcao"])
                            if sl_macro is not None and tp_macro is not None:
                                entry_macro = {"direcao": view["direcao"], "entrada": preco_macro,
                                               "stop": sl_macro, "alvo": tp_macro,
                                               "atr": data.get("atr", 0), "origem": "M1-MACRO",
                                               "rsi": data.get("rsi", "")}
                                fire_signal(sym, entry_macro)
                                last_signal_time[key] = now_ts

            check_signals(price_map)
        except Exception as e:
            print(f"[ERRO] {e}")
            import traceback; traceback.print_exc()
        time.sleep(CHECK_INTERVAL)

# ─── START ───────────────────────────────────────────────────
def _chave_ex(exchange):
    return (BINGX_API_KEY if exchange == "bingx" else BYBIT_API_KEY)

corretoras_status = " + ".join(
    f"{nome_corretora(e)} [{modo_texto_ex(e)}] {leverage_de(e)}x: "
    f"{'🧪 simulação' if simulacao_de(e) else ('✅ OK' if _chave_ex(e) else '❌ sem chaves')}"
    for e in EXCHANGES_ATIVAS)
print(f"Tron Forex Bot - Dev: Jon Padilha | {corretoras_status}")
print(f"Simbolos: {', '.join(SYMBOLS.keys())}")
threading.Thread(target=run_server, daemon=True).start()
load_memory()
_descartar_updates_pendentes()
threading.Thread(target=commands_loop, daemon=True).start()
send_telegram(
    f"🤖 <b>Tron Forex Bot - Dev: Jon Padilha iniciado!</b>\n"
    f"📊 {', '.join(SYMBOLS.keys())}\n"
    # sem nome de corretora nem modo aqui — o bot fica em live no
    # YouTube, essa mensagem não pode identificar qual conta é real.
    f"🏦 {len(EXCHANGES_ATIVAS)} corretora(s) ativa(s)"
    f"{TAG_CONTA_REAL}\n"
    f"🛠️ Código atualizado em: {ultima_atualizacao_texto()} (Brasília)\n"
    f"/help para comandos\n"
    f"🧠 {memory['total_prints']} prints\n"
    f"⏰ {agora_br().strftime('%d/%m/%Y %H:%M')} (Brasília)")
main_loop()
