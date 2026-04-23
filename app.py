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
    try
