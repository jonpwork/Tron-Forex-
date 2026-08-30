---
name: print
description: Traduz um print de gráfico (setup manual do Jon) direto em lógica no app.py do Tron Forex Bot, sem precisar de uma explicação longa por escrito. Use sempre que o usuário anexar uma ou mais imagens de gráfico (TradingView, MT5, etc.) pedindo pra incluir/ajustar um padrão no bot.
---

# /print — atalho de "print vira código"

Objetivo: o Jon manda o(s) print(s) do gráfico com as marcações dele
(setas, linhas de SL/TP, fib, labels tipo "Sell"/"Buy"/"Take") e uma
frase curta (às vezes só "inclui isso" ou "roda junto com o resto").
Este skill evita que ele precise reescrever o parágrafo de spec toda
vez — a leitura da imagem + o mapeamento pros primitivos do bot é
trabalho seu, não dele.

## Passo 1 — Leia a imagem como um trader leria

Para cada imagem anexada, identifique:
- **Timeframe** (M1, M5, M15, H1...) e se é gráfico de preço puro ou
  já vem com marcações (linhas de fib, canais, labels Buy/Sell/Take,
  SL/TP).
- **Onde entra**: qual candle/nível/retração dispara a entrada (ex:
  correção de 50% de uma pernada, toque em fib 38.2/50, rompimento de
  três topos).
- **Onde fica o stop**: normalmente a origem da pernada/perna que está
  sendo corrigida — não ATR, não % fixo (regra do CLAUDE.md).
- **Onde fica o alvo**: projeção (38.2% da onda), estrutura real
  (próximo topo/fundo, fundo/topo da âncora) ou nível específico
  marcado na imagem.
- **Direção**: BUY ou SELL, e a lógica por trás (segue o impulso após
  correção, ou fade de um topo/fundo).
- Se vier mais de uma imagem, trate como o MESMO cenário visto em
  timeframes diferentes (âncora + gatilho fino), a menos que o texto
  do Jon diga o contrário.

Se depois de olhar a imagem com calma a direção ou o alvo ainda ficar
ambíguo (duas leituras plausíveis, com consequência real de dinheiro
diferente entre elas), pare e pergunte objetivamente ANTES de
implementar — mas só nesse caso. Não pergunte por preguiça de olhar a
imagem com atenção.

## Passo 2 — Confira contra o que já existe antes de inventar

Leia `CLAUDE.md` (a seção de estratégia é protegida — não alterar sem
pedir) e localize, em `app.py`, os primitivos já prontos antes de
escrever qualquer coisa nova:

- `_pivots_m1` / `_limpa_pivots` — pivots de topo/fundo em qualquer
  timeframe.
- `detectar_perna(symbol, tf)` — perna + correção 38-65%, em qualquer
  timeframe (inclusive M1 puro).
- `detectar_figura(candles, lado)` — figura geométrica (megafone,
  triângulo, cunha, canal, lateral), generalizada pra qualquer TF.
- `contexto_maior(symbol)` — cascata H4→H1→M15→M5 procurando a âncora.
- `estrutura_ancora(symbol, tf)` / `alvo_ancora(preco, direcao, estrutura)`
  — alvo pela estrutura real da âncora (topo/fundo/50%).
- `stop_tecnico_m1(symbol, direcao)` — stop na origem da pernada do M1.
- `alvo_m15(symbol, direcao)` / `alvo_m1_estrutura(symbol, direcao)` —
  alvo por estrutura real (próximo topo/fundo) em M15 ou M1.
- `alvo_projecao_382(preco, direcao, ctx)` — projeção de 38.2% da onda.
- `gatilho_candle_retracao` / `gatilho_pernada_50` / `gatilho_tres_topos_abc`
  / `gatilho_abc_construcao` — os padrões de gatilho fino de M1.
- `check_macro_m1(symbol, view)` — roda os 3 gatilhos de M1 na direção
  pedida e devolve o preço de entrada confirmado.
- `calc_qty` (respeita `/lote`), `fire_signal` (dispara e notifica),
  motores já registrados em `main_loop()`: M15/M5, M1-TECNICO, M1-ABC,
  M1-FLUXO, M1-MACRO, M1-GATILHO (manual).

Prefira SEMPRE combinar essas peças a escrever uma função nova do
zero. Só crie algo novo quando o padrão do print realmente não existe
ainda (como aconteceu com `alvo_m1_estrutura` e `estrutura_ancora`).

## Passo 3 — Implemente

- Se for uma variação de motor existente (ex: mudar de onde vem o
  alvo), edite o bloco dele em `main_loop()`.
- Se for um padrão novo e independente, crie um motor próprio com
  `origem` própria (ex: `"M1-FLUXO"`), cooldown próprio
  (`last_signal_time[f"{sym}_TAG"]`) e registre a `origem` na lista de
  `fire_signal` que usa a descrição estilo M1 (stop técnico).
- Sem trava de valor nova (freio, teto de trade, RR mínimo) a menos
  que o Jon peça explicitamente — regra fixa do CLAUDE.md.
- Comentários só onde o "porquê" não é óbvio; sem docstring gigante
  pra função trivial.

## Passo 4 — Valide e suba

1. `python3 -m py_compile app.py` — nunca pule isso.
2. `git add app.py && git commit` com mensagem curta explicando o que
   o print pedia e o que foi implementado.
3. `git fetch origin main` — o bot em produção faz commit automático
   de `memory.json` (backups periódicos); é comum haver commits novos
   lá. `git merge origin/main` (sem rebase) pra trazer isso sem
   conflito real (é só dado, não código).
4. Push pra `main` E pra branch de trabalho atual, do mesmo jeito que
   já vem sendo feito nesta sessão.

## Passo 5 — Resuma curto

Feche com um resumo de poucas linhas: o que o print mostrava, o que
foi mapeado pra qual função/motor, e UMA ressalva honesta quando
fizer sentido (ex: "roda em SIMULACAO antes pra confirmar que a
direção está lendo certo"). Não repita o processo interno, só o
resultado.
