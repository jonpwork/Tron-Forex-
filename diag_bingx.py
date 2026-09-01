#!/usr/bin/env python3
"""
Diagnostico isolado da assinatura da BingX — roda separado do app.py,
sem depender de nada do bot, so pra ver a resposta CRUA da corretora
(sem o filtro/normalizacao que o app.py aplica).

Uso (no Termux, dentro da pasta do bot):
    cd ~/tronforex
    python diag_bingx.py

Le BINGX_API_KEY / BINGX_API_SECRET / BINGX_MODE direto do .env que
ja esta na mesma pasta (nao precisa colar chave em lugar nenhum).
"""
import os, time, hmac, hashlib, json
import urllib.request
import urllib.parse

# ── le o .env do diretorio atual (mesmo parser simplificado do app.py) ──
env = {}
if os.path.exists(".env"):
    with open(".env") as f:
        for linha in f:
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                env[k.strip()] = v
else:
    print("ERRO: .env nao encontrado nesta pasta. Roda de dentro de ~/tronforex.")
    raise SystemExit(1)

API_KEY    = env.get("BINGX_API_KEY", "")
API_SECRET = env.get("BINGX_API_SECRET", "")
MODE       = env.get("BINGX_MODE", "real").strip().lower()
BASE_URL   = "https://open-api-vst.bingx.com" if MODE == "demo" else "https://open-api.bingx.com"

print(f"Modo: {MODE}  |  Base URL: {BASE_URL}")
print(f"API_KEY: {len(API_KEY)} chars (começa com '{API_KEY[:6]}...')")
print(f"API_SECRET: {len(API_SECRET)} chars (começa com '{API_SECRET[:6]}...')")
print()

# ── 1) relogio local vs relogio do servidor (BingX tem endpoint publico) ──
try:
    with urllib.request.urlopen(f"{BASE_URL}/openApi/swap/v2/server/time", timeout=10) as r:
        d = json.loads(r.read())
    server_ms = d.get("data", {}).get("serverTime") or d.get("serverTime")
    local_ms  = int(time.time() * 1000)
    if server_ms:
        diff = local_ms - int(server_ms)
        print(f"[RELOGIO] local - servidor BingX = {diff} ms")
        if abs(diff) > 5000:
            print("  >>> RELOGIO DESSINCRONIZADO! Isso sozinho pode causar erro de assinatura/timestamp.")
            print("  >>> Ative data/hora automatica (por rede) no Android e roda de novo.")
    else:
        print(f"[RELOGIO] resposta inesperada do endpoint de horario: {d}")
except Exception as e:
    print(f"[RELOGIO] não consegui checar (endpoint pode ter outro nome): {e}")
print()

# ── 2) assinatura, EXATAMENTE como o exemplo oficial da BingX faz ──
def sign(params_ordenados_str, secret):
    return hmac.new(secret.encode("utf-8"), params_ordenados_str.encode("utf-8"),
                     hashlib.sha256).hexdigest()

def parse_param(params_map, timestamp, encode=False):
    sorted_keys = sorted(params_map)
    if encode:
        params_str = "&".join(f"{k}={urllib.parse.quote(str(params_map[k]), safe='')}" for k in sorted_keys)
    else:
        params_str = "&".join(f"{k}={params_map[k]}" for k in sorted_keys)
    if params_str:
        return params_str + "&timestamp=" + str(timestamp)
    return "timestamp=" + str(timestamp)

def enviar(method, path, params_map):
    ts = int(time.time() * 1000)
    # ASSINA o valor CRU (sem url-encode) -- confirmado contra a API
    # real que a BingX reconstroi a string com o valor original antes
    # de comparar. So a URL de fato ENVIADA precisa ser url-encoded
    # (senao espaco/chave quebram a requisicao HTTP em si).
    params_str_assinar = parse_param(params_map, ts, encode=False)
    signature = sign(params_str_assinar, API_SECRET)
    params_str_enviar = parse_param(params_map, ts, encode=True)
    url = f"{BASE_URL}{path}?{params_str_enviar}&signature={signature}"
    req = urllib.request.Request(url, method=method, headers={"X-BX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode()
            print(f"HTTP {r.status}")
            print(body)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}")
        print(e.read().decode())
    except Exception as e:
        print(f"ERRO DE CONEXAO: {e}")

print("── Teste 1: saldo da conta (GET, so recvWindow) ──")
enviar("GET", "/openApi/swap/v2/user/balance", {"recvWindow": "5000"})
print()

print("── Teste 2: mesma chamada, SEM recvWindow (so timestamp) ──")
enviar("GET", "/openApi/swap/v2/user/balance", {})
print()

# Testes 3 e 4 sao POST com varios parametros de negocio, igual uma
# ordem de verdade -- mas SEM risco de dinheiro (nao abrem posicao,
# so ajustam configuracao da conta). Servem pra isolar se o problema
# e especifico de POST/multi-parametro, ja que o Teste 1/2 (GET) deu
# certo mas so tinha 0 ou 1 parametro de negocio (nada pra competir
# com o timestamp na ordenacao alfabetica).
print("── Teste 3: ajustar alavancagem (POST, varios parametros) ──")
# usa a MESMA alavancagem ja configurada no .env (nao muda nada de
# verdade na conta -- so reenvia o valor que ja deveria estar setado)
_alavancagem = env.get("BINGX_LEVERAGE", env.get("BYBIT_LEVERAGE", "5"))
enviar("POST", "/openApi/swap/v2/trade/leverage",
       {"symbol": "BTC-USDT", "side": "LONG", "leverage": _alavancagem})
print()

print("── Teste 4: ativar hedge mode (POST, o mesmo que ja falha no bot) ──")
enviar("POST", "/openApi/swap/v2/trade/positionSide/dual", {"dualSidePosition": "true"})
print()

# Teste 5: os MESMOS parametros de uma ordem real (incluindo
# stopLoss/takeProfit como JSON embutido -- exatamente o formato que
# ainda falha no bot), mas com um symbol que NAO EXISTE. Se a
# assinatura estiver certa, a BingX processa a autenticacao normal e
# só DEPOIS reclama que o simbolo nao existe -- zero risco de abrir
# ordem de verdade, mas testa a assinatura no formato exato que
# importa.
print("── Teste 5: 'ordem' com simbolo falso (testa assinatura c/ stopLoss/takeProfit, SEM risco) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
    "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": 78000.0, "workingType": "MARK_PRICE"}),
    "takeProfit": json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": 76000.0, "workingType": "MARK_PRICE"}),
})
print()

# Teste 5 tambem falhou -- bisseca pra achar EXATAMENTE qual parametro
# quebra a assinatura. Todos com symbol falso, zero risco de dinheiro.
print("── Teste 5a: só symbol/side/positionSide/type/quantity (SEM stopLoss/takeProfit) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
})
print()

print("── Teste 5b: os mesmos + SÓ stopLoss (sem takeProfit) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
    "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": 78000.0, "workingType": "MARK_PRICE"}),
})
print()

print("── Teste 5c: stopLoss como JSON COMPACTO (sem espaco nenhum) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
    "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": 78000.0, "workingType": "MARK_PRICE"},
                            separators=(",", ":")),
})
print()

# 5a passou (erro de negocio, nao de assinatura) e 5b/5c ainda falham
# IGUAL, mesmo sem espaco -- entao nao e sobre o CONTEUDO do JSON.
# Testa se e o PARAMETRO "stopLoss" em si (nome/posicao/tratamento
# especial), independente do valor ser JSON valido ou nao.
print("── Teste 5d: stopLoss = texto qualquer, NAO e JSON (so pra ver se o erro muda) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
    "stopLoss": "textoqualquersemjsonnenhum",
})
print()

# 5d passou igual ao 5a (texto puro sem caractere especial nao muda
# nada) -- entao o problema e mesmo os caracteres { " : do JSON.
# ACHADO: a BingX decodifica o valor recebido e assina o texto CRU
# (nao o url-encoded) antes de comparar -- o oposto do que o fix
# anterior fazia. Corrigido em app.py e no enviar()/parse_param() aqui
# em cima: agora assina o valor CRU, só url-encoda na hora de montar
# a URL de fato transmitida. Teste 6 repete o cenario do 5b/5c com o
# fix novo pra confirmar contra a API real antes de mexer no bot.
print("── Teste 6: stopLoss JSON de novo, agora com o fix (assina cru, envia encoded) ──")
enviar("POST", "/openApi/swap/v2/trade/order", {
    "symbol": "TESTFAKE-USDT", "side": "SELL", "positionSide": "BOTH",
    "type": "MARKET", "quantity": "0.01",
    "stopLoss": json.dumps({"type": "STOP_MARKET", "stopPrice": 78000.0, "workingType": "MARK_PRICE"}),
    "takeProfit": json.dumps({"type": "TAKE_PROFIT_MARKET", "stopPrice": 76000.0, "workingType": "MARK_PRICE"}),
})
