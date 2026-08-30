#!/data/data/com.termux/files/usr/bin/bash
cd ~/tronforex
termux-wake-lock 2>/dev/null && echo "[WAKE] Wake lock ativado" || echo "[WAKE] Sem wake lock"
echo "Bot iniciando... CTRL+C para parar."
python app.py
termux-wake-unlock 2>/dev/null
