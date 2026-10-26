# ATIVIDADE AVALIATIVA

## Desenvolvimento de Aplicação em Rede utilizando Sockets

**Disciplina**: Fundamentos de Redes de Computadores

Após o estudo do Capítulo 2 — Camada de Aplicação, do livro *Redes de Computadores e a Internet: Uma Abordagem Top-Down*, de Kurose e Ross, cada equipe deverá desenvolver uma aplicação computacional capaz de realizar comunicação entre processos utilizando Programação em Sockets.

## DESAFIO

Sua equipe deverá propor e desenvolver uma aplicação que utilize comunicação em rede por meio de sockets, utilizando **TCP ou UDP**, de acordo com as características e necessidades da aplicação.

A escolha do protocolo deverá ser tecnicamente justificada.

A aplicação deverá possuir, no mínimo:

- um cliente;
- um servidor;
- comunicação efetiva entre os processos;
- utilização de sockets;
- protocolo próprio para as mensagens da aplicação;
- tratamento de situações de erro;
- documentação técnica.

## O PROJETO DEVERÁ RESPONDER

1. Qual problema a aplicação resolve?
2. Quem são os processos envolvidos na comunicação?
3. Como ocorre a comunicação entre cliente e servidor?
4. Qual protocolo de transporte foi escolhido: TCP ou UDP?
5. Por que esse protocolo é adequado à aplicação?
6. Qual é o protocolo utilizado pela aplicação?
7. Quais mensagens podem ser trocadas entre cliente e servidor?
8. O que aconteceria se o outro protocolo de transporte fosse utilizado?
9. Quais situações de erro podem ocorrer?
10. Como essas situações são tratadas pela aplicação?

## ENTREGA

Cada equipe deverá entregar:

## 1. Código-fonte da aplicação

Contendo cliente e servidor.

## 2. README

Apresentando:

- descrição da aplicação;
- integrantes;
- arquitetura;
- protocolo TCP ou UDP;
- justificativa da escolha;
- endereço IP e porta;
- protocolo da aplicação;
- mensagens utilizadas;
- instruções para execução.

## 3. Diagrama da arquitetura

Representando cliente, servidor e comunicação em rede.

## 4. Captura de tráfego

A equipe deverá utilizar o Wireshark para analisar a comunicação da aplicação e apresentar evidências do tráfego gerado.

## APRESENTAÇÃO

Cada equipe terá até 10 minutos para apresentar o projeto.

A apresentação deverá contemplar:

- problema;
- solução;
- arquitetura;
- escolha TCP/UDP;
- protocolo da aplicação;
- demonstração do funcionamento;
- análise do tráfego no Wireshark.

Durante a apresentação, os integrantes poderão ser questionados individualmente sobre o funcionamento do código e os conceitos de redes envolvidos.

## AVALIAÇÃO

| Critério | Valor |
| --- | --- |
| Funcionamento cliente-servidor | 2,0 |
| Implementação dos sockets | 1,5 |
| Protocolo da aplicação | 1,5 |
| Justificativa TCP/UDP | 1,5 |
| Tratamento de erros | 1,0 |
| Organização do código | 0,5 |
| Documentação | 0,5 |
| Apresentação | 0,5 |
| Domínio conceitual | 1,0 |
| TOTAL | 10,0 |

## DESAFIO EXTRA

As equipes que desejarem poderão implementar funcionalidades adicionais, como múltiplos clientes simultâneos, autenticação, persistência de dados, logs ou mecanismos próprios de confiabilidade sobre UDP.

O objetivo principal não é desenvolver a aplicação mais complexa, mas demonstrar que a equipe compreende como aplicações utilizam a rede para realizar comunicação entre processos e como as características do TCP e do UDP influenciam o projeto de uma aplicação distribuída.
