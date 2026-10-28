# Notas de demonstração e captura

Registro das verificações manuais da Etapa 7 e do roteiro de apresentação.
Complementa o README §11 e os critérios AC-01..AC-15 de
[REQUISITOS-SERVIDOR.md](../docs/REQUISITOS-SERVIDOR.md) §9.

## Ambiente

| Item | Valor |
| --- | --- |
| Sistema | Fedora Linux 44 em WSL2 (kernel 6.18) |
| Python | 3.14.5 |
| Captura | `tcpdump` 4.99.6, interface `lo` |
| Leitura/validação | `tshark`/`capinfos`/`mergecap` 4.6.9 |
| Filtro de exibição | `udp.port == 6000` |

A captura foi feita no loopback do WSL2 porque o host Windows não tem Npcap
instalado. O código executado é exatamente o mesmo do README §10, e o arquivo
`telemetria.pcapng` abre no Wireshark para Windows sem conversão. O tráfego é
real (socket UDP, `127.0.0.1:6000`); apenas a interface de captura é a do WSL.

Cada cenário foi capturado separadamente e os arquivos foram unidos com
`mergecap -F pcapng`. Os quadros têm comentários de pacote (coluna **Comments**
no Wireshark ou *File > Capture File Properties*) marcando os pontos de
interesse.

## Cenários da captura

| Cenário | Comando resumido | Resultado observado | Quadros |
| --- | --- | --- | --- |
| A — sessão normal | coletor padrão; sensor `--id s01 --interval 2 --count 4` | `REG`, `ACK`, `DATA` 1..4 de 2 em 2 s, `BYE`, `ACK\|BYE`; `lost=0` | 1–8 |
| B — ACK perdido | coletor `--drop-ack 0.5 --seed 1`; sensor `--interval 2 --count 2` | primeiro `ACK` descartado; `REG` repetido 1,001 s depois com a **mesma sessão**; `ACK` só depois do segundo `REG` | 9–15 |
| C — perda de DATA | sensor `--loss 0.3 --seed 1 --interval 1 --count 6` | `DATA` seq 1 e 5 descartados antes do envio; chegam 2, 3, 4 e 6; coletor registra `LOSS ... gap=1 lost=1` | 16–23 |
| D — reinício sem BYE | sensor `--interval 1 --count 100`; `kill -9` após 3 leituras; novo sensor com o mesmo `id` | 15 respostas `NAME_IN_USE`, `SILENT after=15`, novo `REG` aceito com sessão diferente | 24–64 |

Tempos relativos no arquivo unido: A em 0–6 s, B em 10,8–13,8 s, C em 18,5–23,5 s
e D em 28,2–47,1 s.

## Evidências do README §11

| # | Evidência | Onde ver no `telemetria.pcapng` |
| --- | --- | --- |
| 1 | Ausência de handshake | Quadro 1 é `REG\|1\|s01\|2\|a89e5d0d`; não há nenhum pacote TCP no arquivo (`tshark -Y tcp` retorna 0). |
| 2 | Payload legível | *Follow UDP Stream* mostra `REG`, `DATA` e `ACK` em ASCII. Quadros 1–3: `REG`, `ACK\|REG`, `DATA ... \|21.5`. |
| 3 | Cabeçalho UDP de 8 bytes | Quadro 1 tem `udp.length=28` e 20 bytes de carga: 28 − 20 = 8 bytes de cabeçalho UDP. Nenhum cabeçalho TCP de 20 bytes é usado. |
| 4 | Retransmissão do `REG` | Quadros 9 e 10, mesma sessão `181411fd`, 1,001 s de diferença; o `ACK` só aparece no quadro 11. O log do coletor mostra `DROP ... kind=ack`. |
| 5 | Lacuna de sequência | Quadros 18 (seq 2), 19 (seq 3), 20 (seq 4) e 21 (seq 6): falta a seq 5. O log do cenário C mostra `LOSS ... gap=1 lost=1`. A seq 1 foi descartada antes do envio e não conta como perda. |
| 6 | Sem controle de congestionamento | `DATA` do cenário A em 0,002 s, 2,004 s, 4,004 s e 6,004 s: o ritmo é o do aplicativo, sem redução por perda ou ACK. |
| 7 | Sessão | Quadro 24 usa `4ba22859` (processo antigo); quadro 29 usa `3a70ff04` (processo novo) e recebe `NAME_IN_USE`; quadro 59 tem o novo `REG` aceito. Nos quadros 9/10 a retransmissão mantém a mesma sessão, separando perda de ACK de reinício. |

## Verificação V-01..V-08 (PLAN §8)

| # | Cenário | Resultado |
| --- | --- | --- |
| V-01 | Sessão normal (`--interval 1 --count 4`) | 4 leituras, `lost=0`, `BYE` registrado e confirmado; resumo `received=4`. |
| V-02 | Perda de `DATA` (`--loss 0.3 --seed 1`) | `DATA` 1 e 5 descartados; coletor recebeu 2, 3, 4 e 6; `lost=1`, igual à lacuna 4→6. |
| V-03 | Perda de `ACK` (`--drop-ack 0.5 --seed 1`) | Primeiro `ACK` descartado; dois `REG` com a mesma sessão, 1,002 s de intervalo; `REG` idempotente e `received=2`. |
| V-04 | Reinício sem `BYE` | `NAME_IN_USE` por 15 s; `SILENT after=15`; `REPLACED` com a sessão antiga e `REG` aceito com sessão nova em 16 s. |
| V-05 | Silêncio | Sensor morto com `kill -9` após 3 leituras; `SILENT after=15` 15,3 s após o último `DATA`; linha mantida com os contadores. |
| V-06 | Duplicata/atraso | 70 leituras; `seq=70` repetida → `dup=1`; `seq=1` reenviada fora da janela → `late=1`; `last_seq=70` e último valor preservado. |
| V-07 | Entrada inválida | `lixo` → `ERR\|BAD_REQUEST`; 600 bytes → `ERR\|TOO_LONG`; `DATA` sem registro → `ERR\|NOT_REGISTERED`; `REG` válido em seguida recebe `ACK`, sem linha criada pelos erros. |
| V-08 | Muitos sensores | 20 sensores × 3 leituras; `sensors=20`, `received=60`, `lost=0`, `dup=0`, `late=0`; todos os `BYE` confirmados. |

## Critérios de aceitação AC-01..AC-15

| Critério | Resultado |
| --- | --- |
| AC-01 | `received=3`, `lost=0`, `dup=0`, `late=0`, `last_seq=3`. |
| AC-02 | `DATA` 1 e 3 → `lost=1`. |
| AC-03 | `DATA` 2 repetida com valor 99.9 → `dup=1`, `last_seq=2`, último valor 42.5. |
| AC-04 | `DATA` 3 e depois 2 → `late=1`, `last_seq=3`. |
| AC-05 | `REG` reenviado após ACK perdido → novo `ACK`, `received` e `last_seq` preservados, `idempotent=true`. |
| AC-06 | Sessão nova para `id` ativo → `ERR NAME_IN_USE`, sessão e contadores antigos intactos. |
| AC-07 | `BYE` seguido de `REG` de sessão nova → `ACK\|BYE` e `ACK\|REG`, novo registro imediato. |
| AC-08 | Silêncio > `max(15 s, 3 × intervalo)` → `SILENT after=15` (V-05). |
| AC-09 | 20 datagramas malformados do mesmo IP → 3 respostas `ERR` e 17 `ERR_SUPPRESSED`; processo vivo e `REG` seguinte aceito. |
| AC-10 | Datagrama de 600 bytes → `TOO_LONG`; resposta de 41 bytes (limite 64). |
| AC-11 | `DATA` de `id` desconhecido → `NOT_REGISTERED`, nenhuma linha criada. |
| AC-12 | Reinício sem `BYE` → `NAME_IN_USE` até o silêncio; registro aceito depois do timeout (V-04). |
| AC-13 | `--port 0` → porta efetiva registrada no log; sensor registrou e `received=1`. |
| AC-14 | `--drop-ack 0.5` → `DROP` no log e retransmissão visível na captura (cenário B). |
| AC-15 | 50 sensores a 1 Hz por 60 s → 3000 leituras, `lost=0`, `dup=0`, `late=0`, CPU do coletor 0,99%, RSS 18 476 kB. |

## Reprodução

Captura de um cenário (exemplo A):

```sh
sudo tcpdump -i lo -U -w A.pcap 'udp port 6000'
python3 -m src.collector --host 127.0.0.1 --port 6000
python3 -m src.sensor --host 127.0.0.1 --port 6000 --id s01 --interval 2 --count 4
```

Depois de encerrar o `tcpdump` com `Ctrl+C`, unir e anotar:

```sh
mergecap -F pcapng -w telemetria.pcapng A.pcap B.pcap C.pcap D.pcap
editcap --capture-comment "TelemProto/1.0 - cenarios A-D" telemetria.pcapng final.pcapng
```

## Roteiro de apresentação (10 min)

1. Problema e solução (1 min): telemetria de muitos sensores, leitura nova vale mais que leitura atrasada.
2. Arquitetura e sockets (2 min): um socket UDP no coletor, um por sensor, protocolo TelemProto/1.0.
3. TCP × UDP (2 min): justificativa da seção 4 do README e o que mudaria com TCP.
4. Protocolo e erros (2 min): mensagens, sessão, retransmissão de `REG`/`BYE`, `NAME_IN_USE`, `ERR`.
5. Demonstração ao vivo (2 min): coletor + dois sensores, tabela e `--drop-ack`.
6. Wireshark (1 min): abrir `capture/telemetria.pcapng`, filtro `udp.port == 6000`, *Follow UDP Stream* e as evidências 1–7 acima.
