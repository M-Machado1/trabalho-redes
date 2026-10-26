# Requisitos do coletor (servidor TelemP/1.0)

**Status:** rascunho para revisão · **Versão do documento:** 0.1 · **Data:** 2026-10-26

Especifica o comportamento do processo **coletor**. O formato das mensagens está em
[PROTOCOLO.md](PROTOCOLO.md); este documento não repete a gramática, apenas referencia.

---

## 1. Objetivo

Receber datagramas de N sensores, manter o estado de cada um, detectar perdas,
duplicatas, reordenações e silêncio, e apresentar o resultado sem nunca abortar por
causa de entrada malformada.

## 2. Registro de decisões

| ID | Decisão | Origem |
| --- | --- | --- |
| D-01 | Sessão de 8 hex no protocolo, separando retransmissão de reinício | confirmado |
| D-02 | Silêncio = `max(15 s, 3 × interval)` | confirmado |
| D-03 | `id` liberado após o timeout de silêncio; `BYE` só antecipa a liberação | confirmado |
| D-04 | Dois documentos: protocolo e servidor | confirmado |
| D-05 | Campo `version` em toda mensagem | assumido |
| D-06 | `ACK` também para `BYE` | assumido |
| D-07 | Identificadores e CLI em inglês; mensagens de usuário em PT-BR | assumido (AGENTS.md) |
| D-08 | `ERR` sem correlação, advisory, com limite de taxa | assumido |
| D-09 | `src/` vira pacote; execução com `python -m src.collector` | assumido |

## 3. Requisitos funcionais

| ID | Requisito |
| --- | --- |
| RF-01 | Abrir um socket UDP IPv4, `SOCK_DGRAM`, e ligar em `--host:--port` (padrão `0.0.0.0:6000`). Aceitar `--port 0` (porta efêmera) e registrar em log o endereço efetivo obtido por `getsockname()`. |
| RF-02 | Loop single-thread com `selectors`: acordar a cada ≤ 0,5 s, drenar até 64 datagramas por vez e executar a varredura de expiração a cada acordar. |
| RF-03 | Ler cada datagrama com buffer de 65 535 bytes e tratar `len(raw) > 512` como `TOO_LONG` (o `recvfrom(512)` do Python trunca em silêncio e não permite detectar o excesso). |
| RF-04 | Validar o datagrama na ordem definida no PROTOCOLO §8 e responder ao **endereço de origem** do datagrama, nunca ao endereço guardado na tabela. |
| RF-05 | `REG`: criar a sessão, renovar sessão existente (idempotente por `id`+`session`), recusar sessão nova para `id` ativo com `NAME_IN_USE` e enviar `ACK` somente em caso de sucesso. |
| RF-06 | Ciclo de vida do `id`: `BYE` libera imediatamente; sem `BYE`, o `id` é liberado no instante em que a entrada vira `silent` (`--release-after-silence`, padrão 0 s, permite exigir uma espera extra). Assim um sensor morto por `kill -9` não fica bloqueado para sempre. |
| RF-07 | `DATA`: exigir `id`+`session` registrados (`NOT_REGISTERED` caso contrário), validar a faixa de `value` e atualizar o estado. O primeiro `DATA` da sessão define a linha de base e não conta perda. |
| RF-08 | `BYE`: marcar `departed`, enviar `ACK` do `BYE`, manter a linha para o relatório por `--retention` segundos e depois removê-la. |
| RF-09 | Detectar silêncio com relógio monotônico: `offline_after = max(--offline-min, --offline-factor × interval)`. A transição `active → silent` deve ocorrer em até 1 s depois do limite. |
| RF-10 | Manter contadores por sessão conforme §7, com janela de 64 sequências para distinguir duplicata de atrasado. |
| RF-11 | Guardar as últimas `--max-readings` leituras por sensor (padrão 120) em memória, para o resumo final. Não há banco de dados. |
| RF-12 | Limitar a memória: `--max-sensors` (padrão 256); com a tabela cheia, remover a entrada `departed` mais antiga, depois a `silent` mais antiga e, se todas estiverem `active`, recusar `REG` com `SERVER_ERROR`. |
| RF-13 | Enviar no máximo 1 `ERR` por IP de origem por segundo (burst 3) e 20 `ERR`/s no total; acima disso, descartar em silêncio e contar em `err_suppressed`. Cada `ERR` tem no máximo 64 bytes e não ecoa dados da requisição. |
| RF-14 | Nunca criar entrada de tabela a partir de `DATA` ou `BYE`. |
| RF-15 | Isolar falhas: `try/except` por datagrama; qualquer exceção inesperada é registrada e o loop continua. |
| RF-16 | Encerrar por `Ctrl+C`/`SIGTERM`: parar o loop, imprimir o resumo final, fechar o socket e sair com 0. |
| RF-17 | Expor a lógica de tratamento em uma unidade testável sem sockets: `handle(raw: bytes, addr) -> list[Reply]`, onde `Reply` é `(bytes, addr)`. |
| RF-18 | Injetar falha para demonstração: `--drop-ack P` descarta `ACK`/`ERR` com probabilidade P, registrando cada descarte. Serve para a evidência de retransmissão de `REG` no Wireshark. |

## 4. Requisitos não funcionais

| ID | Requisito |
| --- | --- |
| RNF-01 | Apenas biblioteca padrão (`socket`, `selectors`, `time`, `argparse`, `secrets`, `json`, `logging`); Python ≥ 3.10. |
| RNF-02 | Latência de detecção de silêncio ≤ 1 s após o limite. |
| RNF-03 | Memória limitada por `max_sensors × (estado + max_readings)`; nenhum crescimento por `id` forjado. |
| RNF-04 | 10 000 datagramas aleatórios não derrubam o processo nem o deixam lento. |
| RNF-05 | Funcionar em Windows e Linux; documentar a diferença de `ConnectionRefusedError` no sensor (ICMP port unreachable). |
| RNF-06 | Nenhuma operação bloqueante > 100 ms no loop principal. |
| RNF-07 | Timeouts com `time.monotonic()`; `time.time()` apenas para exibição e para o `timestamp` do sensor. |
| RNF-08 | 50 sensores a 1 Hz com uso de CPU < 5% na máquina da demonstração. |
| RNF-09 | Lógica testável sem abrir socket; testes com `unittest` (`python -m unittest`). |

## 5. Modelo de estado

Duas dimensões independentes, o que elimina a ambiguidade do diagrama de 3 estados do
README ("Offline pode mandar `DATA`?"):

| Dimensão | Valores |
| --- | --- |
| registro | `none` (sem linha) · `session` (linha com sessão válida) |
| vivacidade | `active` · `silent` · `departed` |

Mapeamento com o README: `Desconhecido` = sem linha; `Online` = `active`;
`Offline` = `silent`; `BYE` = `departed` (linha retida para relatório).

| De | Evento | Para |
| --- | --- | --- |
| sem linha | `REG` válido | `active` |
| `active` | `DATA` válido ou `REG` da mesma sessão | `active` (renova) |
| `active` | tick > `offline_after` | `silent` |
| `active`/`silent` | `BYE` da sessão válida | `departed` |
| `silent` | `DATA` da sessão válida | `active` |
| `silent` | `REG` de sessão nova, após `--release-after-silence` | `active` (nova linha; contadores antigos vão para o log) |
| `active` | `REG` de sessão nova | `NAME_IN_USE`, sem mudança |
| `departed` | `REG` de sessão nova | `active` (nova linha) |
| `departed` | `--retention` expirado | linha removida |

Campos da linha:

| Campo | Tipo | Nota |
| --- | --- | --- |
| `id`, `session` | str | chave da linha |
| `addr` | (ip, porta) | último endereço de origem |
| `interval` | int | último valor declarado |
| `status` | enum | `active` / `silent` / `departed` |
| `first_seen`, `registered_at` | float (wall) | exibição |
| `last_seen_mono` | float (monotonic) | timeout |
| `last_seen_wall` | float | exibição |
| `last_seq` | int \| None | `None` até o primeiro `DATA` |
| `received`, `lost`, `dup`, `late` | int | §7 |
| `last_value`, `last_value_ts` | float, int | última leitura aceita |
| `recent` | set + deque(64) | janela de sequências |
| `readings` | deque(max=120) | histórico para o resumo |

## 6. Processamento de um datagrama

1. `selectors` indica leitura → `recvfrom(65535)` → `(raw, addr)`.
2. `len(raw) > 512` → `ERR TOO_LONG` (se o limite de RF-13 permitir) e fim.
3. Decodificar ASCII estrito → falha: `ERR BAD_REQUEST` e fim.
4. `split("|")`; conferir tipo conhecido, `version == 1` e a contagem exata de campos
   do tipo → falha: `ERR BAD_REQUEST` e fim.
5. Validar a sintaxe de cada campo (id, session, seq, timestamp, value, interval) →
   `BAD_REQUEST`, ou `BAD_INTERVAL` para faixa de intervalo.
6. Despachar:
   - `REG`: aplicar RF-05 e responder `ACK`.
   - `DATA`: verificar registro/sessão; verificar faixa de valor; aplicar contadores e
     atualizar valor; não responder.
   - `BYE`: aplicar RF-08 e responder `ACK`.
7. Qualquer exceção: log com *traceback* em nível DEBUG, opcionalmente
   `ERR SERVER_ERROR` (sujeito a RF-13), e o loop continua.

A ordem do passo 6 é deliberada: um sensor não registrado recebe `NOT_REGISTERED` (e
sabe que precisa reenviar `REG`) antes de ser acusado de valor inválido.

## 7. Contadores

Seja `s` o `seq` do `DATA` recebido e `last` o último aceito (`None` antes do primeiro):

| Caso | Condição | Efeito |
| --- | --- | --- |
| linha de base | `last is None` | aceita; `last = s`; `received += 1`; sem perda |
| em sequência | `s == last + 1` | aceita; `received += 1` |
| lacuna | `s > last + 1` | `lost += s - last - 1`; aceita; `received += 1` |
| duplicata | `s == last` ou `s` na janela | descarta; `dup += 1`; estado inalterado |
| atrasado | `s < last` e fora da janela | descarta; `late += 1`; estado inalterado |

- **Perda é contada por datagrama faltante**, não por evento: `seq` 5 depois de 2
  significa 2 perdidos (`lost += 2`).
- A janela guarda as 64 sequências mais altas já vistas. `dup` e `late` são
  indistinguíveis sem ela.
- O `timestamp` do sensor não entra na conta: ordem é definida por `seq`.

## 8. Interface

### 8.1 Linha de comando

| Flag | Padrão | Função |
| --- | --- | --- |
| `--host` | `0.0.0.0` | endereço de bind |
| `--port` | `6000` | porta UDP |
| `--max-sensors` | `256` | limite de linhas |
| `--max-readings` | `120` | histórico por sensor |
| `--offline-min` | `15` | piso do timeout de silêncio (s) |
| `--offline-factor` | `3` | multiplicador do intervalo |
| `--release-after-silence` | `0` | tempo extra para liberar o `id` sem `BYE` (s) |
| `--retention` | `60` | tempo de retenção de linha `departed` (s) |
| `--refresh` | `1.0` | período de redesenho da tabela (s) |
| `--drop-ack` | `0.0` | probabilidade de descartar `ACK`/`ERR` (demonstração) |
| `--json` | desligado | eventos em NDJSON em vez da tabela |
| `--log-file` | — | arquivo adicional de log |
| `--verbose` | desligado | nível DEBUG |

### 8.2 Saída

- **stdout:** tabela (TTY, redesenhada com ANSI) ou snapshots (não-TTY); com `--json`,
  um evento NDJSON por linha.
- **stderr:** log. Formato `hh:mm:ss.mmm LEVEL evento chave=valor ...`, mensagens em
  PT-BR.
- Colunas da tabela: `id`, `status`, `addr`, `interval`, `seq`, `value`, `age`,
  `lost`, `dup`, `late`.
- Eventos: `REG`, `DATA` (só em DEBUG), `LOSS`, `DUP`, `LATE`, `SILENT`, `BYE`,
  `EVICT`, `ERR_SENT`, `ERR_SUPPRESSED`, `DROP`.

### 8.3 Códigos de saída

| Código | Situação |
| --- | --- |
| 0 | encerramento normal (`Ctrl+C`) |
| 2 | argumento inválido |
| 3 | falha de socket/bind (porta ocupada, endereço inválido) |
| 4 | erro interno não tratado fora do loop |

## 9. Critérios de aceitação

| ID | Cenário | Resultado esperado |
| --- | --- | --- |
| AC-01 | `REG` + `DATA` 1, 2, 3 | `received=3`, `lost=0`, `dup=0`, `late=0` |
| AC-02 | `REG` + `DATA` 1, 3 | `lost=1` |
| AC-03 | `DATA` 2 repetido | `dup=1`, valor e `last_seq` inalterados |
| AC-04 | `DATA` 3 e depois 2 | `late=1`, `last_seq=3` |
| AC-05 | `REG` reenviado após `ACK` perdido | novo `ACK`, contadores e `seq` preservados |
| AC-06 | `REG` com sessão nova para `id` ativo | `ERR NAME_IN_USE`, estado antigo intacto |
| AC-07 | `BYE` e `REG` imediato de sessão nova | aceito na hora |
| AC-08 | Silêncio > `max(15, 3×interval)` | `silent` em ≤ 1 s após o limite |
| AC-09 | 10 000 datagramas aleatórios | processo vivo, `ERR` limitado, sem entrada nova |
| AC-10 | Datagrama de 600 bytes | `TOO_LONG`, resposta ≤ 64 B |
| AC-11 | `DATA` de `id` desconhecido | `NOT_REGISTERED`, nenhuma linha criada |
| AC-12 | Reinício sem `BYE` (matar o sensor) | `NAME_IN_USE` até o silêncio; o sensor retenta e registra depois do timeout |
| AC-13 | `--port 0` | loga a porta efetiva; cliente consegue registrar |
| AC-14 | `--drop-ack 0.5` com um sensor | log de `DROP` e retransmissão de `REG` visível no Wireshark |
| AC-15 | 50 sensores a 1 Hz por 60 s | nenhum `lost` falso; CPU < 5% |

## 10. Estrutura de código sugerida

```
src/
├── __init__.py
├── protocol.py     # constantes, encode/decode, ProtocolError(code)
├── collector.py    # SensorTable + Collector (I/O, selectors, CLI)
└── sensor.py       # cliente
tests/
├── test_protocol.py    # round-trip, todos os exemplos do PROTOCOLO §11
├── test_collector.py   # AC-01..AC-12 sem socket
└── test_integration.py # AC-13, AC-14 (porta efêmera)
```

`collector.py` fica dividido em duas camadas: `SensorTable` (estado + contadores,
testável puro) e `Collector` (socket, `selectors`, tabela de saída, CLI).

## 11. Fora de escopo

- Autenticação, criptografia e proteção contra origem forjada.
- Persistência em banco; o histórico é só memória.
- Exportação de métricas (Prometheus etc.).
- IPv6.
- Controle de congestionamento próprio além dos limites de RF-12 e RF-13.
