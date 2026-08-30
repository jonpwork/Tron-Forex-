#!/data/data/com.termux/files/usr/bin/bash
PROJETO="$HOME/tronforex"
DOWNLOAD="/storage/emulated/0/Download/app.py"
DESTINO="$PROJETO/app.py"
cd "$PROJETO" || exit 1
if [ -f "$DOWNLOAD" ]; then
    [ -f "$DESTINO" ] && cp "$DESTINO" "$PROJETO/app_backup.py" && echo "Backup salvo."
    cp "$DOWNLOAD" "$DESTINO"
    echo "app.py atualizado da pasta Download."
else
    echo "Nenhum app.py novo na Download - usando o do projeto."
fi
[ ! -f "$DESTINO" ] && echo "Nao achei app.py. Baixe o codigo como app.py." && exit 1
[ ! -f "$PROJETO/.env" ] && echo "AVISO: .env nao encontrado."
if ! python -c "import ast; ast.parse(open('$DESTINO').read())" 2>/tmp/erro; then
    echo "Erro de sintaxe:"; cat /tmp/erro
    [ -f "$PROJETO/app_backup.py" ] && cp "$PROJETO/app_backup.py" "$DESTINO" && echo "Versao anterior restaurada."
    exit 1
fi
echo "Sintaxe OK."
pkill -f "python.*app.py" 2>/dev/null && echo "Instancia anterior encerrada."
sleep 1
echo "Iniciando o bot..."
exec python "$DESTINO"
