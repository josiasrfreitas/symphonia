# GRE-166 — Sair da sessão antes de ficar sem espaço

Pesquisa de 2026-08-10. Nada foi instalado nem alterado — nem config dos CLIs, nem estado do Orca.
As evidências vêm dos binários instalados, do código-fonte do Codex, e de arquivos de sessão reais
desta máquina: 139 sessões do Codex e 17 pastas de sessão do Claude Code.

---

## Resposta curta

**Dá pra fazer handoff em 30-40% de contexto restante. O mecanismo existe nos dois CLIs e é simples:
uma conta de dividir, feita por um script, disparada por um gancho que já está instalado.**

Três peças:

1. **Saber quanto sobrou.** Os dois CLIs escrevem o gasto de tokens no arquivo da sessão, a cada
   turno. É só somar e dividir. Testei em sessões reais e funciona — inclusive nesta conversa aqui,
   que está em 30,8% restante agora.
2. **Ser avisado na hora certa.** Os dois têm um gancho `Stop`, que roda toda vez que o agente
   termina de responder. É o momento exato de decidir: continuo ou passo o bastão? O Orca **já
   instalou** esse gancho nos dois, apontando para `~/.orca/agent-hooks/`.
3. **Passar o bastão.** No Claude Code o gancho `Stop` consegue segurar o agente e injetar uma
   instrução nele (campo `additionalContext`, descrito no próprio binário como "texto injetado no
   contexto do modelo"). Ou seja: dá pra mandar o agente escrever o handoff antes de ele parar. No
   Codex o gancho não injeta, então lá o handoff tem que ser disparado de fora.

O detector está pronto e testado em
`/private/tmp/claude-501/.../scratchpad/context-left.py` (copie para um lugar definitivo antes de usar).

**A ressalva que importa:** no Codex isso é confiável, porque o arquivo de sessão traz o tamanho da
janela junto. No Claude Code **não traz** — o arquivo diz quanto foi gasto, mas não de quanto. Você
tem que saber por fora se a sessão é de 200 mil ou 1 milhão. Se errar, a conta mente feio: testei
numa sessão antiga e deu "115% usado", que é impossível — era uma sessão de 1 milhão medida contra
200 mil. **Esse é o único ponto frágil do plano, e é do lado do Claude Code.**

---

## 1. Como saber quanto sobrou

### Claude Code

O arquivo fica em `~/.claude/projects/<pasta-do-projeto>/<id-da-sessão>.jsonl`. Cada resposta do
agente grava um bloco `usage`. O contexto em uso é a soma de três campos:

```
input_tokens + cache_read_input_tokens + cache_creation_input_tokens
```

Exemplo real, o último bloco desta conversa:

```json
{"input_tokens": 2, "cache_creation_input_tokens": 1534,
 "cache_read_input_tokens": 132835, "output_tokens": 298, ...}
```

Somando: 134.371. Numa janela de 200 mil, sobrou 32,8%.

O `cache_read_input_tokens` é quem carrega quase tudo — é o histórico inteiro sendo relido a cada
turno. Ignorar esse campo (erro fácil de cometer) faz a conta dar quase zero e o alarme nunca toca.

**O que falta:** o tamanho da janela não está no arquivo. Só o nome do modelo (`claude-fable-5`).
Existe uma flag `exceeds_200k_tokens` no binário, o que confirma que 200 mil é o padrão e 1 milhão é
uma opção ligada por fora. Então o script precisa receber a janela como parâmetro, ou manter uma
tabelinha de modelo → janela, que envelhece.

### Codex

O arquivo fica em `~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl`. Aqui é melhor: **o tamanho da
janela vem junto**, então a conta é auto-suficiente.

```json
{"type":"event_msg","payload":{"type":"token_count",
  "info":{"last_token_usage":{"total_tokens":232065},
          "model_context_window":258400}, ...}}
```

Um detalhe que o script precisa tratar: quando a sessão compacta (linha `{"type":"compacted",...}`),
a janela recomeça. Se você continuar somando por cima, o número fica errado e o alarme toca à toa.
O detector zera a contagem ao ver essa linha.

### Testado em sessões reais

```
CLAUDE (esta conversa)   used=138.451  window=200.000  left=30,8%
CODEX  27/07             used=201.084  window=258.400  left=22,2%
CODEX  03/08             used= 23.686  window=258.400  left=90,8%
```

E varrendo as sessões recentes do Codex, várias já passaram do ponto onde você quereria ter saído:
38,3% · 48,3% · 45,0% · 58,5% · 21,4% restante.

---

## 2. Como ser avisado na hora certa

O gancho `Stop` roda toda vez que o agente termina de responder — antes de devolver o controle. É o
ponto natural de decisão, porque o trabalho está num estado coerente (nenhuma ferramenta pela metade)
e o gasto de tokens acabou de ser gravado no arquivo.

**Os dois CLIs já têm esse gancho instalado pelo Orca.** Confirmado nesta máquina:

- Claude Code — `~/.claude/settings.json` tem `UserPromptSubmit`, `Stop`, `StopFailure`,
  `SubagentStart` e outros, todos chamando `~/.orca/agent-hooks/claude-hook.sh`.
- Codex — `~/.codex/hooks.json` tem `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
  `PermissionRequest`, `SubagentStart`, `SubagentStop` e `Stop`, todos chamando
  `~/.orca/agent-hooks/codex-hook.sh`. Os ganchos precisam ser confiados: o `config.toml` guarda um
  `trusted_hash` de cada um, e mudar o script exige reconfiar.

Esses scripts do Orca fazem uma coisa só: mandam o payload por HTTP para o Orca rodando local
(`127.0.0.1:$ORCA_AGENT_HOOK_PORT/hook/codex`, com token). **Ou seja, o trilho já existe e já chega
no Orca.** O que falta é o Orca fazer a conta e agir — não é preciso construir encanamento novo.

Se preferir não depender do Orca, dá pra encadear um segundo gancho no mesmo evento.

---

## 3. Como passar o bastão

### Claude Code — dá pra segurar o agente e mandar ele escrever o handoff

O gancho `Stop` consegue impedir a parada e injetar texto no modelo. Confirmado no binário v2.1.226:

- `additionalContext` — descrito literalmente como *"Text injected into model context"*
- `hookSpecificOutput.additionalContext` — o caminho do campo na resposta do gancho
- `stop_hook_active` — flag que vem na entrada do gancho, para ele saber que já bloqueou uma vez e
  não entrar em loop. O próprio binário instrui: *"For Stop/SubagentStop hooks, check
  stop_hook_active in the input and return success while it's true."*
- `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` — teto de quantas vezes pode bloquear

Então o fluxo é: sobrou menos de 35% → o gancho bloqueia o Stop e injeta *"escreva o handoff em
tal arquivo e pare"* → o agente escreve → na segunda passada `stop_hook_active` está ligado, o gancho
deixa parar → o Orca abre a próxima worktree com aquele handoff como prompt.

**A parte boa:** o handoff é escrito pelo agente que ainda está com o contexto na cabeça, não
reconstruído depois por alguém de fora.

### Codex — o gancho avisa, mas não injeta

Não achei equivalente ao `additionalContext` no Codex. O gancho `Stop` serve como aviso, e o handoff
tem que ser disparado por fora — o Orca manda o texto para o terminal
(`orca terminal send`) pedindo o handoff, ou abre a worktree seguinte já com o resumo.

Alternativa mais grosseira, mas que funciona sem gancho nenhum: o `orca terminal wait --for tui-idle`
já detecta quando o Codex ficou parado; nesse momento, ler o rollout e decidir.

### Abrindo a próxima sessão

```
orca worktree create --name <tarefa> --no-parent --agent claude \
  --prompt "$(cat handoff.md)"
```

---

## 4. Por que 30-40% é um bom lugar para cortar

Não é arbitrário, e os números da sua máquina explicam por quê.

O Codex compacta sozinho aos **90% da janela** (regra `(w * 9) / 10`, confirmada no fonte em
`codex-rs/protocol/src/openai_models.rs`). Medindo as 10 compactações reais que aconteceram aqui, o
disparo real caiu entre 80% e 94% — a checagem roda entre turnos, então o último turno passa da
linha antes de o alarme tocar.

Ou seja: se você esperar o CLI reclamar, já era. **Aos 10% restantes ele não avisa, não erra, não
para — ele apaga o histórico, escreve um resumo no lugar, e continua trabalhando como se nada
tivesse acontecido.** Depois entrega como se estivesse tudo bem. Sair aos 30-40% te dá espaço de
sobra para o agente escrever um handoff decente antes disso.

**Contexto não pode ser um número fixo de tokens.** Todas as suas sessões antigas do Codex registram
janela de 258.400. O cache de modelos atual (`~/.codex/models_cache.json`) diz 272.000 — para os
mesmos modelos. A janela mudou embaixo de nomes que não mudaram. Trabalhe sempre com porcentagem,
lendo o tamanho da janela na hora.

---

## 5. O que fazer, em ordem

1. Guardar o detector num lugar definitivo (hoje está em
   `/private/tmp/claude-501/.../scratchpad/context-left.py`).
2. Resolver o tamanho da janela do lado Claude Code. É o único ponto frágil. Opções: passar por
   config, ou ler da linha de status.
3. Fazer o Orca chamar o detector quando o gancho `Stop` chegar — o trilho já existe.
4. Escrever o texto que vai ser injetado no agente aos 35%. Esse texto é o que determina a qualidade
   do handoff; vale caprichar mais nele do que na mecânica.
5. Testar de propósito, barato: `codex exec -c model_auto_compact_token_limit=3000` força o Codex a
   compactar quase de imediato, e aí dá pra ver o alarme tocar de verdade em vez de supor.

---

## O que ficou sem confirmar

- Não rodei o gancho de ponta a ponta. Confirmei que os campos existem no binário e que a conta
  funciona nos arquivos reais, mas não vi um handoff acontecer.
- Não sei se o Codex tem algum jeito de injetar texto pelo gancho. Procurei e não achei; pode existir
  e ter passado.
- Não achei nenhuma marca de compactação nos 17 diretórios de sessão do Claude Code aqui. Ou nunca
  aconteceu, ou o arquivo é reescrito quando acontece. Isso não afeta o plano de handoff (que lê o
  gasto de tokens, não a marca de compactação), mas afeta qualquer ideia de auditar depois.

---

## Apêndice — o que acontece se você deixar estourar

Registrado porque era a pergunta original do ticket.

**Nenhum dos dois para com erro.** Os dois apagam o histórico, põem um resumo no lugar, continuam, e
reportam sucesso. Código de saída 0 nos dois casos. Não existe código de saída específico para isso
em nenhum dos dois.

No Claude Code há ainda uma camada mais silenciosa, o *microcompact*, que joga fora resultados de
ferramentas antigas e **não mostra nada na tela** — o código de desenho retorna `null` para esse
evento, literalmente.

**Visto de fora, é invisível.** Uma sessão que estourou, esqueceu quase tudo e entregou trabalho ruim
é idêntica a uma sessão saudável: mesmo código de saída, mesmo sinal de vida, mesmo "entregue". Isso
não é uma falha no vocabulário `delivered | declined | failed` — é queda silenciosa de qualidade
vestida de `delivered`. Por isso o handoff preventivo é a resposta certa, e não detectar depois.

Se ainda assim quiser detectar depois, as marcas são:

- Codex — linha `{"type":"compacted","payload":{"window_number":N,...}}` no rollout. `window_number`
  é um contador de quantas vezes rolou. Aconteceu em 10 das 139 sessões locais (7,2%).
- Claude Code — linha `{"type":"system","subtype":"compact_boundary","compactMetadata":{...}}` no
  transcript, com `preTokens`/`postTokens` e gatilho `compact_auto` ou `compact_manual`.

Uma armadilha: `codex exec --json` **não emite** o evento de compactação. O mapeador descarta
(`_ => None`, em `exec/src/event_processor_with_jsonl_output.rs`). No modo humano ele imprime
`context compacted` apagadinho no stderr. Já o `claude -p --output-format stream-json` **emite**
normalmente. Para o Codex, o arquivo de rollout é o único canal confiável.

---

## Fontes

Inspecionado nesta máquina, só leitura:

- `/Users/josiasribeiro/.local/share/claude/versions/2.1.226` — binário do Claude Code; todos os
  nomes de campo e mensagens vieram de dentro dele
- `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.codex/config.toml` — ganchos já instalados
- `~/.orca/agent-hooks/{claude,codex}-hook.sh` — o que o Orca faz com os ganchos
- `~/.codex/sessions/**/rollout-*.jsonl` — 139 sessões reais, 10 com compactação
- `~/.codex/models_cache.json` — janelas por modelo
- `~/.claude/projects/**/*.jsonl` — 17 pastas de sessão

Código-fonte do `openai/codex` @ `1c042dd` (bate com o `codex-cli 0.145.0` instalado):
`core/src/session/context_window.rs`, `core/src/session/turn.rs`, `core/src/compact.rs`,
`protocol/src/openai_models.rs`, `protocol/src/protocol.rs`, `protocol/src/error.rs`,
`exec/src/event_processor_with_jsonl_output.rs`, `exec/src/event_processor_with_human_output.rs`,
`models-manager/src/model_info.rs`, `tui/src/bottom_pane/footer.rs`.
