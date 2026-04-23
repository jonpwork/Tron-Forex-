import ccxt
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import streamlit as st
from telegram import Bot
import time

# Configs
TELEGRAM_TOKEN = 'SEU_TOKEN_AQUI'
CHAT_ID = 'SEU_CHAT_ID'
BINANCE_API_KEY = ''  # Não precisa para dados públicos
BINANCE_SECRET = ''   # Não precisa para dados públicos

# Binance
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_SECRET,
})

# Telegram
bot = Bot(token=TELEGRAM_TOKEN)

# Função para pegar dados
def get_data(symbol='BTC/USDT', timeframe='1m'):
    bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=500)
    df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

# Envelope
def envelope(df, period=100):
    df['ma'] = df['close'].rolling(window=period).mean()
    df['upper'] = df['ma'] + (df['close'].rolling(window=period).std() * 2)
    df['lower'] = df['ma'] - (df['close'].rolling(window=period).std() * 2)
    return df

# Sinal
def sinal(df):
    if df['close'].iloc[-1] > df['upper'].iloc[-1] and df['close'].iloc[-2] <= df['upper'].iloc[-2]:
        return 'COMPRA'
    elif df['close'].iloc[-1] < df['lower'].iloc[-1] and df['close'].iloc[-2] >= df['lower'].iloc[-2]:
        return 'VENDA'
    return None

# Streamlit
def main():
    st.set_page_config(page_title="Bitcoin Envelope", layout="wide")
    st.title('📈 Bitcoin Envelope')
    st.subheader('Estratégia de Envelope (100 períodos)')

    # Sidebar
    with st.sidebar:
        st.header('Configurações')
        symbol = st.selectbox('Ativo', ['BTC/USDT', 'ETH/USDT'])
        timeframe = st.selectbox('Timeframe', ['1m', '5m', '15m', '1h'])
        st.write('Sinal enviado para o Telegram')

    # Dados
    df = get_data(symbol, timeframe)
    df = envelope(df)

    # Gráfico
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(df['time'], df['close'], label=symbol, color='blue')
    ax.plot(df['time'], df['upper'], label='Upper', color='green')
    ax.plot(df['time'], df['lower'], label='Lower', color='red')
    ax.legend()
    st.pyplot(fig)

    # Sinal
    s = sinal(df)
    if s:
        st.write(f'🔥 Sinal: **{s}**')
        if st.button('Enviar para Telegram'):
            bot.send_message(chat_id=CHAT_ID, text=f'Sinal: {s} em {symbol}')
            st.success('Sinal enviado!')

    # Atualizar
    if st.button('Atualizar'):
        st.experimental_rerun()

if __name__ == '__main__':
    main()
