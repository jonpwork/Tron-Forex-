# Tron Forex Bot — contexto do projeto

Bot de trading em Python rodando no Termux (Android).
Arquivo principal: app.py. Roda com `bash rodar.sh`.
Corretoras: BingX (EXCHANGE=bingx) e Bybit. Toda config vem do .env.

## Estratégia (não alterar sem pedir)
- Ancora: cascata H4 > H1 > M15 > M5 procurando pernada corrigida 38-65%
- Gatilho em M1, tres padroes independentes:
  1. candle com retracao de 50% do range anterior (pavio conta)
  2. pernada de Elliott corrigida 50%+ (3 pivots: origem > extremo > correcao)
  3. tres topos/fundos no mesmo nivel + ABC corrigindo 50%+
- Motor M1-ABC: opera as sub-pernas DENTRO da correcao, sem esperar fechar
- Stop: SEMPRE tecnico, na origem da pernada corrigida (nunca ATR, nunca % fixo)
- Alvo: projecao de 38.2% da onda 1 / do impulso corrigido
- Arbitragem: compra e venda podem coexistir no mesmo par, nunca no mesmo
  ponto (exige distancia de preco e de tempo)

## Regras
- NAO adicionar travas por valor (freio diario, teto de trades).
  O bot opera livre, por cenario grafico. EXCEÇÃO pedida pelo Jon
  (01/09/2026): RR mínimo 1:1 em TODO sinal, de qualquer motor —
  RR menor que isso é ilógico (arrisca mais do que pode ganhar).
  Controlado por RR_MINIMO no .env (padrão 1.0), checado uma vez
  dentro de fire_signal. Não reverter sem o Jon pedir de novo.
- Stops curtos com alvos grandes sao o desenho, nao um bug.
- Nunca colocar chaves de API no codigo — so no .env.
- Validar sintaxe antes de considerar pronto.
- .env, .env.save e memory*.json nunca vao pro git.
