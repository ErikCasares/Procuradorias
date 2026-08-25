# Resposta aos pontos levantados — IA PGMS & Hera

Retorno ponto a ponto sobre os sete itens levantados no grupo. Onde houve correção, ela
já está aplicada e testada; onde há uma pergunta em aberto, ela está marcada como tal.

---

## Resumo

| # | Ponto | Situação |
| - | ----------------------------------------- | -------------------------------- |
| 1 | Informações da planilha do Agente 1 no JSON | Corrigido — o download estava falhando |
| 2 | O que o Agente 2 faz | Explicado abaixo |
| 3 | Pacote Docker e implantação | Corrigido — havia dois impedimentos |
| 4 | APTO / NÃO APTO desnecessário | Removido |
| 5 | Espanhol nas saídas | Removido |
| 6 | Última movimentação via MNI | **Pergunta em aberto** — ver ponto 6 |
| 7 | Falha com arquivo acima de 100 MB | Corrigido — duas causas distintas |

---

## 1. Incluir no JSON do Agente 1 as informações da planilha

**O conteúdo já era o mesmo** — a planilha e o JSON do Agente 1 carregam exatamente os
mesmos campos, extraídos da mesma passagem pelo PDF. O problema era outro, e mais grave:
**o download do JSON estava quebrado e devolvia erro em todas as tentativas**, por uma
falha no nome do arquivo procurado. Na prática, esse JSON nunca chegou às mãos de vocês.

Corrigido. O arquivo é obtido em:

```http
GET /api/v1/lotes/{lote_id}/arquivos/agente1_json
```

E `GET /api/v1/lotes/{lote_id}/arquivos` lista tudo que aquele lote deixou disponível,
com tamanho e link de cada item.

## 2. "Não entendi o que o Agente 2 faz"

Justo — a documentação não explicava. Em uma frase: **o Agente 2 organiza a fila de
trabalho do procurador.**

Ele não lê PDF e **não usa inteligência artificial**. Trabalha sobre o que o Agente 1
extraiu e aplica regras determinísticas — o mesmo processo produz sempre o mesmo
resultado. Para cada processo, entrega:

- **prioridade** — ALTA, MEDIA ou BAIXA
- **ação recomendada**
- **justificativa** da prioridade atribuída
- **alerta de prescrição**, com base no art. 174 do CTN
- **observações**

A prioridade é atribuída assim:

| Prioridade | Critério |
| ---------- | ----------------------------------------------------------- |
| **ALTA** | dívida ≥ R$ 5.000, **ou** risco de prescrição, **ou** 5 anos ou mais sem movimentação |
| **MEDIA** | dívida entre R$ 1.000 e R$ 5.000 |
| **BAIXA** | dívida abaixo de R$ 1.000 |

> **Os valores R$ 5.000 e R$ 1.000 são uma definição provisória nossa, feita apenas para
> ordenar o trabalho.** Não vêm de norma e **não são critério de ajuizamento**.
> Precisamos que a PGMS confirme quais valores usar. Assim que vocês definirem, a troca
> é de configuração — não exige alteração de código nem nova versão do sistema.

O resultado sai em JSON por lote e num **Excel acumulado**, já ordenado por prioridade e
valor: o procurador abre e trabalha de cima para baixo.

## 3. Pacote Docker e instruções de implantação

O pacote está pronto. Encontramos e corrigimos **dois problemas que impediam a subida**
do sistema — em ambos os casos, o comando de instalação falhava antes de o serviço
iniciar:

- exigia uma chave de API da OpenAI que **nenhuma parte do sistema usa mais** (o Agente 1
  passou a ser totalmente determinístico);
- apontava para um arquivo de script que não existe mais no projeto.

As instruções completas estão no `README.md`, incluindo um passo que **não pode ser
pulado**: aumentar o limite de tamanho de requisição no proxy de entrada (Traefik, no
Easypanel, ou Nginx). Esse ponto é a causa direta do erro relatado no item 7 — detalhe
logo abaixo.

## 4. APTO / NÃO APTO não é mais necessário

**Já removido.** O Agente 1 hoje é 100% determinístico: ele extrai o que está no
processo e não emite juízo sobre viabilidade. Restava apenas um campo vazio de
`total_aptos` no JSON, resquício da versão anterior, que também foi retirado.

## 5. Colunas em espanhol — traduzir também no JSON

As colunas do Excel do Agente 1 já estavam em português. Mas havia espanhol em três
lugares que chegavam a vocês, e todos foram traduzidos:

- **todo o log do Agente 2** — que aparece ao vivo no painel e no acompanhamento da API;
- **chaves do JSON** — `generado_em`, `total_procesados`, `es_execucao_fiscal`,
  `version_agente2`;
- um marcador `SIN_NP_None`, que aparecia dentro do Excel quando o número do processo
  não era localizado — hoje sai em português e sem o `None`.

## 6. Retirar "última movimentação" do processamento (obtida via MNI)

**Aqui precisamos alinhar antes de mudar**, e é o único ponto que depende de vocês.

A última movimentação **não é só um campo exibido** — o Agente 2 depende dela para duas
regras centrais da priorização:

1. a regra de **5 anos sem movimentação → prioridade ALTA**;
2. o **alerta de prescrição** do art. 174 do CTN.

Se ela for simplesmente removida da extração hoje, a priorização deixa de funcionar como
descrito no item 2. Por isso ela continua sendo calculada por enquanto.

**A pergunta:** vocês preferem que o sistema passe a **receber** essa data do MNI, em vez
de extraí-la do PDF? Essa é uma mudança que faz sentido — o dado do MNI é mais confiável
que o extraído — e nós a implementamos. Só precisamos confirmar o formato em que ela
viria e a partir de quando. Enquanto isso não estiver definido, manter a extração é o que
preserva a priorização funcionando.

## 7. Falha com arquivo acima de 100 MB

Investigamos e encontramos **duas causas independentes**. As duas foram tratadas.

**(a) O serviço aguenta — quem recusava era o proxy de entrada.**

Testamos com o arquivo real de vocês: **105 MB, 415 páginas**. O envio levou 1,2 segundo
e o lote foi concluído em 61 segundos. O sistema em si nunca teve problema com o tamanho.

O que rejeitava era o servidor que fica *à frente* do serviço (o proxy do Easypanel), cujo
limite padrão de upload é bem menor que 100 MB. Ele recusava o arquivo **antes de ele
chegar ao sistema**. Por isso a mensagem de erro era confusa e não parecia falha de
tamanho. Está documentado no README como passo obrigatório de implantação, e a mensagem
exibida na tela nesse caso agora explica exatamente o que aconteceu e onde ajustar.

**(b) Havia um consumo de memória excessivo com PDFs grandes.**

Um problema real, e este era nosso. Em arquivos grandes, o Agente 1 chegava a **1.648 MB
de memória** — o suficiente para o servidor encerrar o processo à força em containers de
1 a 2 GB. Eram duas causas somadas: o leitor de PDF não liberava as páginas já lidas, e o
OCR carregava dezenas de imagens de uma vez.

Corrigido e **medido**: o pico caiu para **243 MB** — cerca de 6,8 vezes menos. Os
parâmetros que governam esse consumo também ficaram ajustáveis por configuração, para dar
folga em servidores menores.

**Sobre lotes pequenos:** também levantado por vocês — lotes com poucos processos, ou com
um único PDF, funcionam normalmente. Não há quantidade mínima.

---

## Além dos sete pontos

No trabalho de revisão que acompanhou essas correções, alguns outros problemas foram
encontrados e corrigidos. Dois merecem ser mencionados por afetarem o resultado que
chega a vocês:

- **Processos ilegíveis geravam prioridade mesmo assim.** Quando um PDF não podia ser
  lido, o sistema seguia adiante e produzia uma prioridade **sem base em dado real** —
  indistinguível, no relatório, de uma prioridade legítima. Agora a falha aparece
  explicitamente no JSON, na planilha e nos avisos do lote. Nenhum processo ilegível
  recebe prioridade.

- **O Excel acumulado duplicava linhas.** Um processo reenviado aparecia mais de uma vez
  no relatório do procurador. Corrigido.

Também reforçamos o isolamento entre consumidores da API: cada token acessa
exclusivamente os próprios lotes e o próprio relatório acumulado, sem qualquer
possibilidade de ver dados de outro. Considerando que os relatórios carregam nome,
CPF/CNPJ e valor de dívida, tratamos esse ponto como prioritário.

Também foi incluída uma rotina de **limpeza automática de disco**, que antes não existia:
os PDFs enviados são apagados 7 dias após a conclusão do lote (a planilha e o JSON
gerados permanecem), e o lote inteiro sai após 90 dias. Ambos os prazos são
configuráveis, e podem ser desligados se a PGMS preferir retenção integral. Sem isso, o
acúmulo de arquivos grandes acabaria por esgotar o disco do servidor — e um servidor com
disco cheio passa a recusar envios sem explicar o motivo.

O sistema conta hoje com **118 testes automatizados**, incluídos no repositório e
executados a cada alteração. Eles cobrem todas as rotas da API e do painel, a
autenticação, o isolamento entre consumidores e cada um dos problemas descritos acima —
de forma que nenhum deles possa retornar despercebido.
