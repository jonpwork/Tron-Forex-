import streamlit as st
import pandas as pd
import numpy as np
import ccxt
import yfinance as yf
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import time
import threading
from datetime import datetime, timedelta
import json
import os

# ─────────────────────────────────────────────
#  SECRETS — lê do Streamlit Cloud ou env vars
#  No Streamlit Cloud: Settings > Secrets
#  Formato do secrets.toml:
#    BINANCE_KEY    = "sua_api_key"
#    BINANCE_SECRET = "seu_secret"
#    TG_TOKEN       = "seu_token"
#    TG_CHAT_ID     = "seu_chat_id"
# ─────────────────────────────────────────────
def _get_secret(key: str, fallback: str = "") -> str:
    """Tenta st.secrets primeiro, depois variável de ambiente, depois fallback."""
    try:
        val = st.secrets.get(key, "")
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(key, fallback)

_SECRET_BINANCE_KEY    = _get_secret("BINANCE_KEY")
_SECRET_BINANCE_SECRET = _get_secret("BINANCE_SECRET")
_SECRET_TG_TOKEN       = _get_secret("TG_TOKEN")
_SECRET_TG_CHAT_ID     = _get_secret("TG_CHAT_ID")

# ─────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Envelope Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─────────────────────────────────────────────
#  CUSTOM CSS — dark terminal aesthetic
# ─────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Syne:wght@400;700;800&display=swap');

html, body, [class*="css"] {
    background-color: #0a0c10;
    color: #e2e8f0;
    font-family: 'Syne', sans-serif;
}

/* sidebar */
section[data-testid="stSidebar"] {
    background-color: #0d1117;
    border-right: 1px solid #1e2530;
}

/* inputs */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stSelectbox > div > div {
    background-color: #161b22 !important;
    border: 1px solid #30363d !important;
    color: #e2e8f0 !important;
    font-family: 'Share Tech Mono', monospace !important;
    border-radius: 4px !important;
}

/* buttons */
.stButton > button {
    background: linear-gradient(135deg, #00d4aa, #00a8ff);
    color: #0a0c10;
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 0.85rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: none;
    border-radius: 4px;
    padding: 0.5rem 1.5rem;
    transition: opacity 0.2s;
}
.stButton > button:hover { opacity: 0.85; }

/* metric cards */
.metric-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 1rem 1.25rem;
    font-family: 'Share Tech Mono', monospace;
}
.metric-label { color: #8b949e; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; }
.metric-value { color: #e2e8f0; font-size: 1.6rem; font-weight: 700; margin-top: 0.25rem; }
.metric-value.green { color: #3fb950; }
.metric-value.red { color: #f85149; }
.metric-value.blue { color: #58a6ff; }

/* signal badge */
.signal-badge {
    display: inline-block;
    padding: 0.3rem 0.9rem;
    border-radius: 3px;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.85rem;
    font-weight: 700;
    letter-spacing: 0.15em;
    text-transform: uppercase;
}
.signal-buy  { background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid #3fb950; }
.signal-sell { background: rgba(248,81,73,0.15);  color: #f85149; border: 1px solid #f85149; }
.signal-wait { background: rgba(139,148,158,0.1); color: #8b949e; border: 1px solid #30363d; }

/* log box */
.log-box {
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 4px;
    padding: 1rem;
    font-family: 'Share Tech Mono', monospace;
    font-size: 0.78rem;
    color: #8b949e;
    max-height: 220px;
    overflow-y: auto;
    line-height: 1.7;
}

/* section headers */
h1, h2, h3 { font-family: 'Syne', sans-serif !important; }
.section-title {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    color: #8b949e;
    margin-bottom: 0.75rem;
    border-bottom: 1px solid #21262d;
    padding-bottom: 0.5rem;
}

/* status dot */
.dot { width:8px; height:8px; border-radius:50%; display:inline-block; margin-right:6px; }
.dot-green { background:#3fb950; box-shadow: 0 0 6px #3fb950; }
.dot-red   { background:#f85149; }
.dot-gray  { background:#8b949e; }

div[data-testid="stMetric"] { background: #161b22; border-radius:6px; padding:0.75rem 1rem; border:1px solid #21262d; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  SESSION STATE INIT
# ─────────────────────────────────────────────
def _init_state():
    defaults = {
        "bot_running": False,
        "log": [],
        "positions": {},      # symbol -> {side, entry, qty, pnl}
        "last_signals": {},   # symbol -> BUY/SELL/WAIT
        "last_prices": {},
        "last_upper": {},
        "last_lower": {},
        "trade_history": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ─────────────────────────────────────────────
#  HELPERS — TELEGRAM
# ─────────────────────────────────────────────
def send_telegram(token: str, chat_id: str, msg: str):
    if not token or not chat_id or token == "SEU_TOKEN_AQUI":
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=5)
    except Exception:
        pass

def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log.insert(0, f"[{ts}] {msg}")
    st.session_state.log = st.session_state.log[:120]

# ─────────────────────────────────────────────
#  DATA FETCHING
# ─────────────────────────────────────────────
ASSET_CONFIG = {
    "BTC/USDT":  {"source": "binance", "yf_ticker": None},
    "SPX":       {"source": "yfinance", "yf_ticker": "^GSPC"},
    "EURUSD":    {"source": "yfinance", "yf_ticker": "EURUSD=X"},
}

def fetch_binance_ohlcv(symbol: str, api_key: str, secret: str, periods: int = 200) -> pd.DataFrame:
    try:
        exchange = ccxt.binance({"apiKey": api_key, "secret": secret})
        raw = exchange.fetch_ohlcv(symbol, timeframe="1m", limit=periods)
        df = pd.DataFrame(raw, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df
    except Exception as e:
        add_log(f"❌ Binance fetch error: {e}")
        return pd.DataFrame()

def fetch_yf_ohlcv(ticker: str, periods: int = 200) -> pd.DataFrame:
    try:
        df = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
        if df.empty:
            df = yf.download(ticker, period="5d", interval="5m", progress=False, auto_adjust=True)
        df.columns = [c.lower() for c in df.columns]
        df.index.name = "timestamp"
        return df.tail(periods)
    except Exception as e:
        add_log(f"❌ YFinance fetch error ({ticker}): {e}")
        return pd.DataFrame()

def fetch_data(symbol: str, api_key: str, secret: str, periods: int = 200) -> pd.DataFrame:
    cfg = ASSET_CONFIG.get(symbol, {})
    if cfg.get("source") == "binance":
        return fetch_binance_ohlcv(symbol, api_key, secret, periods)
    yf_ticker = cfg.get("yf_ticker", symbol)
    return fetch_yf_ohlcv(yf_ticker, periods)

# ─────────────────────────────────────────────
#  ENVELOPE INDICATOR
# ─────────────────────────────────────────────
def calc_envelope(df: pd.DataFrame, period: int = 100, pct: float = 0.5) -> pd.DataFrame:
    df = df.copy()
    df["ma"]    = df["close"].rolling(period).mean()
    df["upper"] = df["ma"] * (1 + pct / 100)
    df["lower"] = df["ma"] * (1 - pct / 100)
    return df

# ─────────────────────────────────────────────
#  SIGNAL DETECTION
#  BUY  → price broke UPPER then pulled back to touch UPPER (breakout + retest)
#  SELL → price broke LOWER then pulled back to touch LOWER (breakdown + retest)
# ─────────────────────────────────────────────
def detect_signal(df: pd.DataFrame, lookback: int = 5) -> str:
    if len(df) < lookback + 2:
        return "WAIT"

    recent = df.iloc[-(lookback + 1):]
    close  = recent["close"].values
    upper  = recent["upper"].values
    lower  = recent["lower"].values

    # ── BUY: at some point in lookback, close was ABOVE upper (broke out)
    #         and now current close is <= upper (pulled back / retest)
    broke_up     = any(close[i] > upper[i] for i in range(len(close) - 1))
    retest_upper = close[-1] <= upper[-1] and close[-1] >= df["ma"].iloc[-1]

    # ── SELL: at some point in lookback, close was BELOW lower (broke down)
    #          and now current close is >= lower (pulled back / retest)
    broke_down   = any(close[i] < lower[i] for i in range(len(close) - 1))
    retest_lower = close[-1] >= lower[-1] and close[-1] <= df["ma"].iloc[-1]

    if broke_up and retest_upper:
        return "BUY"
    if broke_down and retest_lower:
        return "SELL"
    return "WAIT"

# ─────────────────────────────────────────────
#  ORDER EXECUTION — BINANCE
# ─────────────────────────────────────────────
def place_order_binance(api_key: str, secret: str, symbol: str, side: str,
                         order_type: str, qty: float = None, usdt_amount: float = None):
    try:
        exchange = ccxt.binance({"apiKey": api_key, "secret": secret, "options": {"defaultType": "spot"}})
        sym = symbol.replace("/", "")
        price = exchange.fetch_ticker(symbol)["last"]

        if qty is None and usdt_amount:
            qty = usdt_amount / price

        markets = exchange.load_markets()
        precision = markets[symbol]["precision"]["amount"] if symbol in markets else 6
        qty = float(exchange.amount_to_precision(symbol, qty))

        order = exchange.create_order(symbol, order_type, side.lower(), qty)
        add_log(f"✅ ORDER {side} {qty} {symbol} @ ~{price:.4f}")
        return order, price
    except Exception as e:
        add_log(f"❌ Order error ({symbol}): {e}")
        return None, None

# ─────────────────────────────────────────────
#  CHART
# ─────────────────────────────────────────────
def build_chart(df: pd.DataFrame, symbol: str) -> go.Figure:
    fig = make_subplots(rows=1, cols=1)

    # Candles
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"],
        increasing_line_color="#3fb950", decreasing_line_color="#f85149",
        name=symbol, showlegend=False
    ))

    # Envelope bands
    fig.add_trace(go.Scatter(x=df.index, y=df["upper"], name="Upper",
        line=dict(color="#58a6ff", width=1, dash="dot"), fill=None))
    fig.add_trace(go.Scatter(x=df.index, y=df["lower"], name="Lower",
        line=dict(color="#f85149", width=1, dash="dot"),
        fill="tonexty", fillcolor="rgba(88,166,255,0.04)"))
    fig.add_trace(go.Scatter(x=df.index, y=df["ma"], name="MA",
        line=dict(color="#8b949e", width=1)))

    fig.update_layout(
        paper_bgcolor="#0a0c10", plot_bgcolor="#0d1117",
        font=dict(family="Share Tech Mono", color="#8b949e", size=11),
        margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(showgrid=False, color="#30363d", rangeslider=dict(visible=False)),
        yaxis=dict(showgrid=True, gridcolor="#161b22", color="#30363d"),
        legend=dict(bgcolor="#0d1117", bordercolor="#21262d"),
        height=380,
    )
    return fig

# ─────────────────────────────────────────────
#  SIDEBAR — SETTINGS
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div style="font-family:Syne;font-weight:800;font-size:1.3rem;color:#e2e8f0;margin-bottom:0.25rem;">⬡ ENVELOPE BOT</div>', unsafe_allow_html=True)
    st.markdown('<div style="font-family:Share Tech Mono;font-size:0.7rem;color:#8b949e;margin-bottom:1.5rem;">M1 · Breakout + Retest Strategy</div>', unsafe_allow_html=True)

    # ── BINANCE API ──────────────────────────────
    st.markdown('<div class="section-title">BINANCE API</div>', unsafe_allow_html=True)

    if _SECRET_BINANCE_KEY:
        st.markdown(
            '<div style="font-family:Share Tech Mono;font-size:0.72rem;color:#3fb950;margin-bottom:0.4rem;">'
            '✔ API Key carregada via Secrets</div>', unsafe_allow_html=True
        )
        api_key    = _SECRET_BINANCE_KEY
        api_secret = _SECRET_BINANCE_SECRET
        # Mostra inputs bloqueados para confirmar visualmente
        st.text_input("API Key", value="••••••••••••••••", disabled=True)
        st.text_input("Secret",  value="••••••••••••••••", disabled=True)
    else:
        st.markdown(
            '<div style="font-family:Share Tech Mono;font-size:0.72rem;color:#f0a800;margin-bottom:0.4rem;">'
            '⚠ Secret não encontrado — insira manualmente</div>', unsafe_allow_html=True
        )
        api_key    = st.text_input("API Key", type="password", placeholder="Binance API Key")
        api_secret = st.text_input("Secret",  type="password", placeholder="Binance Secret")

    # ── TELEGRAM ─────────────────────────────────
    st.markdown('<div class="section-title" style="margin-top:1rem;">TELEGRAM</div>', unsafe_allow_html=True)

    if _SECRET_TG_TOKEN:
        st.markdown(
            '<div style="font-family:Share Tech Mono;font-size:0.72rem;color:#3fb950;margin-bottom:0.4rem;">'
            '✔ Token carregado via Secrets</div>', unsafe_allow_html=True
        )
        tg_token  = _SECRET_TG_TOKEN
        tg_chatid = _SECRET_TG_CHAT_ID
        st.text_input("Bot Token", value="••••••••••••••••", disabled=True)
        st.text_input("Chat ID",   value=f"••••{tg_chatid[-4:]}" if len(tg_chatid) >= 4 else "••••", disabled=True)
    else:
        st.markdown(
            '<div style="font-family:Share Tech Mono;font-size:0.72rem;color:#f0a800;margin-bottom:0.4rem;">'
            '⚠ Secret não encontrado — insira manualmente</div>', unsafe_allow_html=True
        )
        tg_token  = st.text_input("Bot Token", type="password", placeholder="Telegram Bot Token")
        tg_chatid = st.text_input("Chat ID",   placeholder="Telegram Chat ID")

    st.markdown('<div class="section-title" style="margin-top:1rem;">ATIVOS</div>', unsafe_allow_html=True)
    active_symbols = st.multiselect(
        "Pares ativos",
        options=list(ASSET_CONFIG.keys()),
        default=["BTC/USDT"]
    )

    st.markdown('<div class="section-title" style="margin-top:1rem;">INDICADOR</div>', unsafe_allow_html=True)
    env_period = st.number_input("Período MA", min_value=5, max_value=500, value=100, step=5)
    env_pct    = st.number_input("Envelope %", min_value=0.01, max_value=10.0, value=0.5, step=0.01, format="%.2f")
    lookback   = st.number_input("Lookback candles (retest)", min_value=2, max_value=20, value=5, step=1)

    st.markdown('<div class="section-title" style="margin-top:1rem;">RISCO POR TRADE</div>', unsafe_allow_html=True)
    risk_mode = st.selectbox("Modo", ["USDT fixo", "% do saldo"])
    if risk_mode == "USDT fixo":
        risk_value = st.number_input("Valor (USDT)", min_value=1.0, value=50.0, step=1.0)
    else:
        risk_value = st.number_input("% do saldo", min_value=0.1, max_value=100.0, value=1.0, step=0.1)

    order_type  = st.selectbox("Tipo de ordem", ["market", "limit"])
    auto_trade  = st.toggle("🤖 Operar automaticamente", value=False)

    st.markdown('<div class="section-title" style="margin-top:1rem;">CONTROLE</div>', unsafe_allow_html=True)
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("▶ START", use_container_width=True):
            st.session_state.bot_running = True
            add_log("🚀 Bot iniciado")
    with col_b:
        if st.button("■ STOP", use_container_width=True):
            st.session_state.bot_running = False
            add_log("⛔ Bot parado")

    refresh_s = st.slider("Intervalo (seg)", 10, 120, 30, 5)

# ─────────────────────────────────────────────
#  HEADER
# ─────────────────────────────────────────────
status_dot  = '<span class="dot dot-green"></span>' if st.session_state.bot_running else '<span class="dot dot-red"></span>'
status_text = "ATIVO" if st.session_state.bot_running else "PARADO"
st.markdown(
    f'<div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1.5rem;">'
    f'<span style="font-family:Syne;font-size:1.6rem;font-weight:800;color:#e2e8f0;">ENVELOPE BOT</span>'
    f'<span style="font-family:Share Tech Mono;font-size:0.7rem;color:#8b949e;">&nbsp;M1 · Breakout + Retest</span>'
    f'<span style="margin-left:auto;">{status_dot}<span style="font-family:Share Tech Mono;font-size:0.75rem;color:#8b949e;">{status_text}</span></span>'
    f'</div>',
    unsafe_allow_html=True
)

# ─────────────────────────────────────────────
#  MAIN LOOP — fetch data + detect signals
# ─────────────────────────────────────────────
if not active_symbols:
    st.info("Selecione ao menos um ativo no painel lateral.")
    st.stop()

charts_placeholder = st.empty()
metrics_placeholder = st.empty()
log_placeholder     = st.empty()

def run_cycle():
    for sym in active_symbols:
        df = fetch_data(sym, api_key, api_secret, periods=env_period + 50)
        if df.empty:
            st.session_state.last_signals[sym] = "WAIT"
            continue

        df = calc_envelope(df, period=env_period, pct=env_pct)
        df.dropna(inplace=True)
        if df.empty:
            continue

        signal = detect_signal(df, lookback=int(lookback))
        price  = df["close"].iloc[-1]

        st.session_state.last_signals[sym] = signal
        st.session_state.last_prices[sym]  = price
        st.session_state.last_upper[sym]   = df["upper"].iloc[-1]
        st.session_state.last_lower[sym]   = df["lower"].iloc[-1]

        # Store df for chart (only last sym for now)
        st.session_state[f"df_{sym}"] = df

        # ── Act on signal
        if signal in ("BUY", "SELL") and auto_trade:
            existing = st.session_state.positions.get(sym)

            # Close opposite position first
            if existing and existing["side"] != signal:
                close_side = "sell" if existing["side"] == "BUY" else "buy"
                if sym == "BTC/USDT":
                    place_order_binance(api_key, api_secret, sym, close_side, order_type, qty=existing["qty"])
                pnl = (price - existing["entry"]) * existing["qty"] if existing["side"] == "BUY" else (existing["entry"] - price) * existing["qty"]
                st.session_state.trade_history.append({
                    "time": datetime.now().strftime("%H:%M:%S"), "symbol": sym,
                    "side": existing["side"], "entry": existing["entry"],
                    "exit": price, "pnl": round(pnl, 4)
                })
                del st.session_state.positions[sym]
                add_log(f"🔄 Fechou {existing['side']} {sym} | PnL: {pnl:+.4f}")

            # Open new position
            if sym not in st.session_state.positions:
                usdt_amt = risk_value if risk_mode == "USDT fixo" else None
                order, exec_price = None, price
                if sym == "BTC/USDT":
                    order, exec_price = place_order_binance(
                        api_key, api_secret, sym, signal.lower(), order_type, usdt_amount=usdt_amt
                    )
                else:
                    add_log(f"⚠️ {sym}: execução manual necessária (não na Binance)")
                    exec_price = price

                qty = (usdt_amt or 0) / (exec_price or 1)
                st.session_state.positions[sym] = {"side": signal, "entry": exec_price, "qty": qty, "pnl": 0}
                add_log(f"{'🟢' if signal=='BUY' else '🔴'} {signal} {sym} @ {exec_price:.4f}")

                # Telegram alert
                emoji = "🟢" if signal == "BUY" else "🔴"
                msg = f"{emoji} *{signal}* `{sym}`\nPreço: `{exec_price:.4f}`\nEstrategia: Envelope {env_period} / {env_pct}%"
                send_telegram(tg_token, tg_chatid, msg)

        elif signal != "WAIT" and not auto_trade:
            add_log(f"⚡ Sinal {signal} em {sym} @ {price:.4f} (bot manual)")
            emoji = "🟢" if signal == "BUY" else "🔴"
            send_telegram(tg_token, tg_chatid,
                f"{emoji} *SINAL {signal}* `{sym}`\nPreço: `{price:.4f}` | Modo: manual")

run_cycle()

# ─────────────────────────────────────────────
#  RENDER CHARTS
# ─────────────────────────────────────────────
with charts_placeholder.container():
    st.markdown('<div class="section-title">GRÁFICOS — ENVELOPE M1</div>', unsafe_allow_html=True)
    cols = st.columns(len(active_symbols))
    for i, sym in enumerate(active_symbols):
        with cols[i]:
            df_key = f"df_{sym}"
            if df_key in st.session_state and not st.session_state[df_key].empty:
                st.plotly_chart(build_chart(st.session_state[df_key], sym), use_container_width=True, config={"displayModeBar": False})
            else:
                st.info(f"Aguardando dados: {sym}")

# ─────────────────────────────────────────────
#  METRICS ROW
# ─────────────────────────────────────────────
with metrics_placeholder.container():
    st.markdown('<div class="section-title">STATUS POR ATIVO</div>', unsafe_allow_html=True)
    m_cols = st.columns(len(active_symbols))
    for i, sym in enumerate(active_symbols):
        with m_cols[i]:
            sig   = st.session_state.last_signals.get(sym, "WAIT")
            price = st.session_state.last_prices.get(sym, 0)
            upper = st.session_state.last_upper.get(sym, 0)
            lower = st.session_state.last_lower.get(sym, 0)
            pos   = st.session_state.positions.get(sym)

            sig_class = {"BUY": "signal-buy", "SELL": "signal-sell"}.get(sig, "signal-wait")
            price_fmt = f"{price:,.5f}" if price < 10 else f"{price:,.2f}"

            pos_html = ""
            if pos:
                pnl_color = "green" if (price - pos["entry"]) * (1 if pos["side"] == "BUY" else -1) > 0 else "red"
                raw_pnl   = (price - pos["entry"]) * pos["qty"] if pos["side"] == "BUY" else (pos["entry"] - price) * pos["qty"]
                pos_html  = f'<div style="margin-top:0.5rem;font-family:Share Tech Mono;font-size:0.72rem;color:#8b949e;">POS: {pos["side"]} @ {pos["entry"]:.4f} <span style="color:{"#3fb950" if pnl_color=="green" else "#f85149"};">PnL {raw_pnl:+.4f}</span></div>'

            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-label">{sym}</div>
                <div class="metric-value">{price_fmt}</div>
                <div style="margin-top:0.5rem;">
                    <span class="signal-badge {sig_class}">{sig}</span>
                </div>
                <div style="margin-top:0.5rem;font-family:Share Tech Mono;font-size:0.7rem;color:#8b949e;">
                    ▲ {upper:,.4f} &nbsp; ▼ {lower:,.4f}
                </div>
                {pos_html}
            </div>
            """, unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  BOTTOM — Log + Trade History
# ─────────────────────────────────────────────
st.markdown("---")
col_log, col_hist = st.columns([1, 1])

with col_log:
    st.markdown('<div class="section-title">LOG DE EVENTOS</div>', unsafe_allow_html=True)
    log_html = "<br>".join(st.session_state.log[:30]) if st.session_state.log else "Sem eventos ainda..."
    st.markdown(f'<div class="log-box">{log_html}</div>', unsafe_allow_html=True)

with col_hist:
    st.markdown('<div class="section-title">HISTÓRICO DE TRADES</div>', unsafe_allow_html=True)
    if st.session_state.trade_history:
        hist_df = pd.DataFrame(st.session_state.trade_history)
        st.dataframe(
            hist_df.style.applymap(
                lambda v: "color:#3fb950" if isinstance(v, float) and v > 0 else ("color:#f85149" if isinstance(v, float) and v < 0 else ""),
                subset=["pnl"]
            ),
            use_container_width=True, height=220
        )
    else:
        st.markdown('<div class="log-box" style="color:#8b949e;">Nenhum trade ainda...</div>', unsafe_allow_html=True)

# ─────────────────────────────────────────────
#  AUTO-REFRESH
# ─────────────────────────────────────────────
if st.session_state.bot_running:
    time.sleep(refresh_s)
    st.rerun()
