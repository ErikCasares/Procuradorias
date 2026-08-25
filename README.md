# Procuradorias

Pipeline de dois agentes para triagem de execuções fiscais (HERA Tecnologia / PGMS).

- **Agente 1** — `agente1.py`: lê os PDFs de `processos pra analiser/`, faz OCR nas
  páginas digitalizadas e **extrai** os dados de cada processo (partes, CDA, valor,
  datas, última movimentação). É **100% determinístico** — regras e expressões
  regulares, sem IA e sem chamada a serviço externo. Escreve a planilha de revisão em
  `resultados/` e o JSON de repasse em `JSON/`, ambos com os mesmos campos.
- **Agente 2** — `agente2.py`: lê a extração do Agente 1 e **ordena o trabalho do
  procurador**. Também sem IA: são regras determinísticas. Para cada processo produz
  prioridade (ALTA / MEDIA / BAIXA), ação recomendada, justificativa, alerta de
  prescrição (art. 174 do CTN) e observações. Entrega em JSON por lote e num Excel
  acumulado, ordenado por prioridade e valor.
- **API e painel** — `webapp.py`: expõe o pipeline em duas superfícies — a API v1 para
  sistemas externos (SIAP) e o painel do procurador. É o que fica publicado no domínio.

Os agentes **não emitem juízo sobre o processo**. Não existe classificação APTO /
NÃO APTO: o Agente 1 extrai o que está no PDF e o Agente 2 sugere uma ordem de
atendimento. A decisão é do procurador.

## Autenticação

**O serviço falha fechado.** Sem `API_TOKENS` a API recusa tudo com `503`; sem senha
o painel não abre. Uma configuração incompleta deixa o serviço inacessível, nunca
aberto — os relatórios carregam nome, CPF/CNPJ e valor de dívida.

**O ambiente guarda hashes, nunca segredos.** Variável de ambiente não é cofre: o
valor aparece na UI do Easypanel, em `docker inspect`, em `/proc/<pid>/environ` e em
dump de erro. Guardando só o hash, quem ler qualquer um desses caminhos não autentica.

### Gerando credenciais

```bash
python gerar_credencial.py api siap      # token de um consumidor da API
python gerar_credencial.py painel        # senha do painel do procurador
```

O gerador mostra duas coisas: o **segredo**, que aparece uma única vez e não fica
gravado em lugar nenhum, e o **hash**, que você cola no ambiente do serviço. Se o
consumidor perder o token, você emite outro — não há como recuperar o original.

| Variável | Para quê |
| ------------------- | --------------------------------------------------------- |
| `API_TOKENS` | `rotulo:sha256:<hash>`, separado por vírgula. O rótulo identifica o consumidor no log e isola os lotes dele. |
| `SENHA_PAINEL_HASH` | Hash scrypt da senha do painel |
| `COOKIE_SEGURO` | `1` em produção — marca a sessão como Secure (só HTTPS) |
| `HORAS_SESSAO` | Validade da sessão do painel (padrão 12) |
| `MAX_MB_LOTE` | Tamanho máximo de um lote, em MB (padrão 500) |

Por que dois algoritmos: os tokens da API são 256 bits aleatórios, sem força bruta
viável, então SHA-256 direto basta. A senha do painel é escolhida por gente, com
entropia baixa e atacável por dicionário — essa usa scrypt, lento de propósito.

### Rotação sem parar a integração

Dois hashes com o mesmo rótulo funcionam ao mesmo tempo:

```ini
API_TOKENS=siap:sha256:<novo>,siap:sha256:<antigo>
```

Adicione o novo, avise o consumidor, espere a troca, remova o antigo.

Os formatos antigos em texto puro (`API_TOKENS=siap:<token>` e `SENHA_PAINEL`) ainda
funcionam, para não trancar um serviço já configurado, mas o startup avisa em log.
Troque pelos hashes assim que puder.

### Isolamento entre consumidores

Cada consumidor só enxerga os próprios lotes: o token de um recebe `404` no lote de
outro, e o Excel acumulado do Agente 2 é recortado para conter **apenas** os processos
dos lotes daquele token. Vale para todas as rotas de download, inclusive o relatório
acumulado.

## API v1 — para sistemas externos (SIAP)

Autenticação por token Bearer em todas as rotas:

```http
Authorization: Bearer <token>
```

**O processamento é assíncrono.** O OCR leva minutos por lote, e nenhum proxy mantém a
conexão aberta tanto tempo. O envio responde na hora com um `lote_id`; o consumidor
acompanha por polling. Os lotes rodam em fila serial — um por vez, porque o OCR já
satura a CPU.

| Rota | Uso |
| ----------------------------------- | ------------------------------------------------ |
| `POST /api/v1/lotes` | Envia PDFs (multipart, campo `arquivos`). Responde `202` com o `lote_id` |
| `GET /api/v1/lotes` | Lista os lotes deste consumidor |
| `GET /api/v1/lotes/{id}` | Estado do lote, com log |
| `GET /api/v1/lotes/{id}/resultado` | Priorização completa (`409` enquanto não concluir) |
| `GET /api/v1/lotes/{id}/arquivos` | Lista o que dá para baixar, com tamanho e URL |
| `GET /api/v1/lotes/{id}/arquivos/{tipo}` | Baixa um artefato — ver a tabela abaixo |
| `GET /api/v1/lotes/{id}/planilha/agente1` | Atalho para `arquivos/agente1_planilha` |
| `GET /api/v1/lotes/{id}/planilha/agente2` | Atalho para `arquivos/agente2_planilha` |
| `GET /health` | Healthcheck, sem autenticação |

### Artefatos de um lote

| `tipo` | Formato | Conteúdo |
| ------------------ | ----- | ------------------------------------------------- |
| `agente1_planilha` | xlsx | Revisão do Agente 1 — só deste lote |
| `agente1_json`     | json | A mesma extração do Agente 1 em JSON — os mesmos campos da planilha, só deste lote |
| `agente2_json`     | json | Priorização do Agente 2 — só deste lote |
| `agente2_planilha` | xlsx | Priorização do Agente 2 — **acumulado**: todos os processos dos seus lotes |

`agente1_json` é a rota para quem quer os dados da planilha do Agente 1 de forma
estruturada, sem ler Excel.

### Documentação interativa

| Rota | Uso |
| ---------------------- | ------------------------------------------------------ |
| `GET /api/docs` | Swagger. Clique em **Authorize**, cole o token e teste as rotas pelo navegador |
| `GET /api/redoc` | A mesma documentação em página de leitura corrida — é o que se manda para quem vai integrar |
| `GET /api/openapi.json` | OpenAPI cru, para gerar cliente com `openapi-generator` |

Sai do próprio código: os campos de resposta, os códigos de erro e o fluxo do polling
estão descritos nas rotas de `webapp.py`, então a documentação não envelhece à parte da
implementação. As três páginas são públicas — mostram o formato da API, nunca dados de
processo, que continuam exigindo token.

### Ciclo de vida

`na_fila` → `processando` → `concluido` | `erro`

Exemplo:

```bash
# 1. Enviar
curl -X POST https://SEU-DOMINIO/api/v1/lotes \
     -H "Authorization: Bearer $TOKEN" \
     -F "arquivos=@processo1.pdf" -F "arquivos=@processo2.pdf"
# → 202 {"lote_id":"20260729-143000-a1b2c3","status":"na_fila", ...}

# 2. Acompanhar até sair de na_fila/processando
curl https://SEU-DOMINIO/api/v1/lotes/20260729-143000-a1b2c3 \
     -H "Authorization: Bearer $TOKEN"

# 3. Buscar a priorização
curl https://SEU-DOMINIO/api/v1/lotes/20260729-143000-a1b2c3/resultado \
     -H "Authorization: Bearer $TOKEN"
```

Um lote pode ter um único PDF — não há mínimo.

### Sempre confira `avisos`

O Agente 1 trata OCR quebrado e PDF ilegível internamente e **encerra com código de
sucesso**. Um lote pode chegar a `concluido` sem ter extraído nada. Por isso a resposta
traz `resumo` e `avisos`:

```json
{
  "status": "concluido",
  "resumo": "3 de 12 processo(s) extraídos com sucesso",
  "avisos": [
    "9 processo(s) não puderam ser lidos — PDF ilegível ou OCR sem resultado"
  ]
}
```

`status: "concluido"` com `avisos` não vazio significa que o lote rodou até o fim mas
algo deu errado no caminho. Trate como falha.

Um PDF ilegível **não** gera prioridade: o erro aparece no JSON, na planilha e nos
avisos, em vez de virar um resultado aparentemente válido.

## Priorização do Agente 2

A prioridade é **operacional** — serve para ordenar a fila do procurador:

| Prioridade | Critério |
| ---------- | --------------------------------------------------------------- |
| **ALTA**   | dívida ≥ `LIMIAR_PRIORIDADE_ALTA` (R$ 5.000) **ou** risco de prescrição (art. 174 do CTN) **ou** ≥ 5 anos sem movimentação |
| **MEDIA**  | dívida entre `LIMIAR_PRIORIDADE_MEDIA` e `LIMIAR_PRIORIDADE_ALTA` (R$ 1.000 a R$ 5.000) |
| **BAIXA**  | dívida abaixo de `LIMIAR_PRIORIDADE_MEDIA` (R$ 1.000) |

> **Os valores R$ 5.000 e R$ 1.000 são provisórios, definidos pela HERA apenas para
> ordenar o trabalho.** Não derivam de norma e **não são critério de ajuizamento**.
> Devem ser confirmados com a PGMS. Quando o valor oficial for definido, basta trocar
> `LIMIAR_PRIORIDADE_ALTA` / `LIMIAR_PRIORIDADE_MEDIA` no ambiente — sem alterar código
> nem reconstruir a imagem.

A **última movimentação** continua sendo extraída do PDF porque a priorização depende
dela: é o que alimenta a regra dos 5 anos e o alerta de prescrição. Se a PGMS preferir
fornecê-la pelo MNI, o campo pode passar a ser aceito como entrada em vez de extraído.

## Painel do procurador

Na raiz (`/`), protegido por senha. Envia PDFs, acompanha os lotes com log ao vivo e
baixa os relatórios acumulados. Usa a mesma fila da API.

## Implantação no Easypanel (produção)

**O Easypanel não usa o `docker-compose.yml`.** Ele constrói o `Dockerfile` a partir do
repositório e tira a configuração da própria interface. As seções seguintes, com
`docker compose`, valem para uso local e linha de comando — em produção, o que manda é o
que está configurado no serviço.

Consequência prática: **nenhuma variável do `docker-compose.yml` chega ao container em
produção sozinha.** Todas precisam ser preenchidas na aba **Environment**.

### 1. Fonte

Aponte o serviço para o repositório e o branch `main`. O build usa o `Dockerfile` da
raiz — não é preciso indicar comando de build.

### 2. Environment

Cole as variáveis abaixo. As duas primeiras são **obrigatórias** — sem elas o serviço
sobe recusando tudo (ver **Autenticação**):

```ini
API_TOKENS=siap:sha256:<hash>
SENHA_PAINEL_HASH=<hash scrypt>
COOKIE_SEGURO=1
HORAS_SESSAO=12
MAX_MB_LOTE=500
TIMEOUT_AGENTE_S=3600
TZ=America/Campo_Grande
OCR_DPI=200
OCR_MAX_LOTE=10
OCR_MAX_WORKERS=2
LIMIAR_PRIORIDADE_ALTA=5000
LIMIAR_PRIORIDADE_MEDIA=1000
RETENCAO_PDF_DIAS=7
RETENCAO_LOTE_DIAS=90
```

`COOKIE_SEGURO=1` só funciona com HTTPS ativo no domínio — que é o padrão do Easypanel.
Com ele em `0` em produção, o cookie de sessão do painel trafega sem proteção.

### 3. Volumes

Monte os três como persistentes. Sem eles, **recriar o container apaga o histórico
acumulado e todos os lotes já processados**:

| Caminho no container | Guarda |
| -------------------- | ------------------------------------------- |
| `/app/JSON`          | Troca entre os agentes e o histórico JSONL  |
| `/app/resultados`    | Planilhas de revisão e de priorização       |
| `/app/dados`         | Os lotes recebidos pela API e pelo painel   |

### 4. Limite de upload no proxy

**Não pule este passo.** Aumente o limite de corpo da requisição do serviço para pelo
menos o valor de `MAX_MB_LOTE`. É o mesmo ponto descrito em **Liberar o tamanho do
upload no proxy**, e a causa direta da falha com arquivos de 100 MB.

### 5. Conferir que subiu do jeito certo

O container começa como root apenas para ajustar o dono das pastas montadas e **larga o
privilégio antes de atender a primeira requisição**. Isso é necessário porque o
bind-mount do Easypanel é criado como `root` e sobrepõe o dono definido na imagem — sem
o ajuste, o serviço sobe, o `/health` fica verde e só o **primeiro upload** falha, com
permissão negada.

Depois do deploy, procure esta linha no log do serviço:

```text
[entrypoint] Pastas prontas. Iniciando como 'hera' (sem privilégio).
```

É a confirmação de que tudo correu como desenhado. Se em vez dela aparecer:

| Linha no log | O que significa |
| ------------------------------------------ | --------------------------------- |
| `AVISO: gosu não encontrado` | O serviço está rodando **como root**. Funciona, mas sem o isolamento — reconstrua a imagem |
| `AVISO: não consegui ajustar o dono` | O volume recusou o `chown`. Pode funcionar mesmo assim; se o upload falhar por permissão, é aqui |

Os dois casos **degradam com aviso em vez de derrubar o container** — de propósito, para
que um detalhe de volume não impeça a subida. Mas nenhum dos dois é o comportamento
pretendido, e ambos passam despercebidos por quem não lê o log.

> **Finais de linha.** O `entrypoint.sh` precisa chegar à VPS com LF. O
> `.gitattributes` garante isso. Se o repositório foi clonado **antes** desse arquivo
> existir, refaça os finais de linha antes de implantar — com CRLF, o container morre no
> start com `no such file or directory`, mensagem que não diz nada sobre a causa:
>
> ```bash
> git rm --cached -r . && git reset --hard
> ```

### 6. Primeiro deploy

Suba primeiro num serviço de **staging** e faça o percurso do cliente de ponta a ponta:
enviar um PDF grande pelo painel, acompanhar até `concluido`, baixar a planilha.

Três coisas **não podem ser validadas fora de um container** e só se provam neste
primeiro deploy. Confira as três:

| O que | Como conferir |
| ------------------------------ | ------------------------------------------------ |
| A imagem constrói | O build do Easypanel termina sem erro |
| O container larga o privilégio | A linha `[entrypoint] Pastas prontas...` no log — ver o passo 5 |
| O OCR roda de verdade | Envie um PDF **digitalizado** e abra a coluna **Confiança OCR (%)** da planilha do Agente 1 |

O terceiro merece atenção: Poppler e Tesseract só existem dentro da imagem, então o
caminho de OCR nunca é exercitado no desenvolvimento. Se a **Confiança OCR (%)** vier
`0,0%` numa página que visivelmente tem texto, o OCR não rodou — e o sintoma seria um
lote concluindo com texto vazio, sem erro aparente.

## Rodando com Docker (local e linha de comando)

**Pré-requisito:** Docker Desktop instalado e em execução. Para produção no Easypanel,
veja a seção anterior.

### 1. Configurar as credenciais

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

Gere os hashes com `gerar_credencial.py` (ver **Autenticação**) e cole `API_TOKENS` e
`SENHA_PAINEL_HASH` no `.env`. Sem os dois, o `docker compose up` para com uma mensagem
dizendo qual falta. **Não há chave de API de terceiros para configurar** — o pipeline
não usa serviço externo.

### 2. Construir a imagem

```bash
docker compose build
```

A imagem já traz Tesseract (idioma `por`) e Poppler, que o OCR e o `pdf2image` exigem.

### 3. Subir o serviço

```bash
docker compose up -d web
docker compose logs -f web
```

Acesse `http://localhost:3000` para o painel e `http://localhost:3000/api/docs` para o
Swagger (ou o domínio configurado). O log do startup diz quais consumidores da API
foram habilitados — se aparecer `API_TOKENS não configurado`, a API está recusando
tudo.

### 4. Liberar o tamanho do upload no proxy — obrigatório

**Este é o passo que mais causa falha de implantação.** O serviço aceita lotes até
`MAX_MB_LOTE` (500 MB por padrão), mas quem recebe a requisição primeiro é o proxy do
Easypanel (Traefik) ou o Nginx à frente dele — e o padrão deles é bem menor. O upload
de um PDF grande é recusado **antes de chegar ao serviço**, com `413`.

No Easypanel, no serviço, aumente o limite de corpo da requisição para pelo menos o
valor de `MAX_MB_LOTE`. Em Nginx próprio:

```nginx
client_max_body_size 500m;
```

Sem isso, um PDF de 100 MB falha no envio mesmo com o serviço funcionando
perfeitamente.

### Atrás de um proxy reverso

O container já sobe confiando nos cabeçalhos `X-Forwarded-*` (`--proxy-headers`), e isso
é necessário, não cosmético: atrás do Traefik ou do Nginx, **toda** requisição chega com
o IP do proxy. Sem ler o cabeçalho, o freio de tentativas de senha do painel enxerga um
único IP para o mundo inteiro — dez erros de senha de qualquer pessoa trancariam o painel
para todos, o procurador incluído.

Confiar no cabeçalho é seguro aqui porque o container **nunca** é exposto diretamente: só
o proxy alcança a porta. Se algum dia ele for publicado sem proxy à frente, essa opção
precisa sair.

O serviço roda como usuário não-root (`hera`, uid 10001) e expõe um `HEALTHCHECK` em
`/health`, que é como o Easypanel sabe se o container subiu de fato. `/health` não exige
autenticação e não devolve dado de processo.

### Ajustes de ambiente

| Variável | Padrão | Para quê |
| ------------------------- | ------ | ----------------------------------------- |
| `MEM_LIMITE`              | `2g`   | Teto de memória do container. Mínimo recomendado: 2 GB |
| `TIMEOUT_AGENTE_S`        | `3600` | Tempo máximo por etapa. Estourou, o processo é morto e o lote vira `erro` — em vez de travar a fila |
| `OCR_DPI`                 | `200`  | Resolução do OCR. Afeta a **qualidade** do reconhecimento |
| `OCR_MAX_LOTE`            | `10`   | Páginas convertidas por vez |
| `OCR_MAX_WORKERS`         | `2`    | Processos paralelos de OCR |
| `LIMIAR_PRIORIDADE_ALTA`  | `5000` | Ver **Priorização do Agente 2** |
| `LIMIAR_PRIORIDADE_MEDIA` | `1000` | Ver **Priorização do Agente 2** |
| `RETENCAO_PDF_DIAS`       | `7`    | Apaga os PDFs recebidos de lotes concluídos há mais dias que isto. `0` desliga |
| `RETENCAO_LOTE_DIAS`      | `90`   | Remove o lote inteiro — pasta e registro — depois deste prazo. `0` desliga |

Se o container for morto por falta de memória (o log mostra `código -9`), baixe
`OCR_MAX_LOTE` e `OCR_MAX_WORKERS` **antes** de mexer no `OCR_DPI` — o DPI é o que
determina se o texto digitalizado será reconhecido.

### Retenção em disco

Nada limpava `dados/lotes/` antes: os PDFs recebidos ficavam para sempre. Com arquivos
de 100 MB, um volume de 50 GB enche em cerca de 500 lotes — e, cheio, o serviço passa a
recusar envios **sem deixar claro o motivo**.

Por isso a limpeza é automática e em duas etapas:

| Prazo | O que acontece |
| ------------------------- | ------------------------------------------------- |
| `RETENCAO_PDF_DIAS` (7)   | Os **PDFs enviados** de lotes concluídos são apagados. A planilha e o JSON gerados **permanecem** |
| `RETENCAO_LOTE_DIAS` (90) | O lote inteiro sai — pasta e registro |

O PDF original continua com quem o enviou, então apagá-lo cedo não perde nada; o que o
sistema produziu sobrevive aos 7 dias. Para guardar tudo indefinidamente, defina as duas
variáveis como `0` — mas então acompanhe o espaço em disco.

### Rodando os agentes pela linha de comando

Alternativa à web, para lotes manuais ou depuração:

```bash
# Agente 1 — processa os PDFs já presentes em "processos pra analiser/"
docker compose --profile batch run --rm agente1

# Agente 2 — num arquivo específico gerado pelo Agente 1
docker compose --profile batch run --rm agente1 \
    python agente2.py --arquivo JSON/<nome_do_lote>_agente2.json

# Agente 2 como watcher contínuo (opcional — a web já o chama a cada lote;
# só é útil se algo depositar JSON em JSON/ por fora da web)
docker compose --profile watcher up -d agente2

# Parar tudo
docker compose down
```

### Pastas

As pastas são montadas como volumes — o que o container escreve aparece direto no seu
disco:

| Pasta                     | Papel                                             |
| ------------------------- | ------------------------------------------------- |
| `processos pra analiser/` | Entrada do uso manual por linha de comando        |
| `dados/lotes/<id>/`       | Pastas isoladas de cada lote da API/painel        |
| `JSON/`                   | Troca entre os agentes + histórico JSONL          |
| `resultados/`             | Planilha de revisão (Agente 1) e de priorização (A2) |

Os agentes aceitam `PASTA_ENTRADA`, `PASTA_JSON` e `PASTA_RESULTADOS` por variável de
ambiente — é assim que cada lote ganha espaço próprio. Sem elas, o comportamento é o
original: subpastas ao lado do script.

**No Easypanel, monte volumes persistentes** em `/app/JSON`, `/app/resultados` e
`/app/dados`. Sem isso, recriar o container apaga o histórico acumulado e todos os
lotes já processados.

O serviço roda como usuário não-root (`hera`, uid 10001). **Não é preciso ajustar o dono
dos volumes à mão**: o container inicia como root apenas para acertar as pastas montadas
e desce para `hera` antes de atender qualquer requisição — ver
**Conferir que subiu do jeito certo**.

O ajuste é condicional: percorre a árvore só quando o dono está errado. Num `dados/` com
milhares de PDFs, um `chown -R` a cada reinício atrasaria a subida e poderia estourar o
`start-period` do healthcheck.

## Testes

```bash
python -m pytest
```

São 118 testes em `tests/`, cobrindo todas as rotas da API v1 e do painel:
autenticação, isolamento entre consumidores, injeção de argumento, path traversal, PDF
ilegível, retenção em disco e o freio de tentativas de login.

Os testes de OCR (`test_ocr.py`) usam dublês de `pdf2image`, `pytesseract` e `PIL`.
Verificam a **lógica** — quais páginas são lidas, se cada imagem é liberada, se o
`OCR_DPI` configurado é respeitado, se leitura sem confiança não contamina a média, se
falha ao renderizar deixa a página vazia em vez de derrubar o lote. **Não** verificam a
qualidade do reconhecimento, o que exigiria Poppler e Tesseract instalados.

**Podem ser rodados com dados reais em disco.** Cada sessão copia os `.py` para uma
pasta temporária e trabalha lá — os testes não tocam em `JSON/`, `resultados/` nem
`dados/`.

Parte deles confere os valores padrão **lidos pela aplicação** — `OCR_DPI`,
`OCR_MAX_LOTE`, `OCR_MAX_WORKERS`, `TIMEOUT_AGENTE_S`, `RETENCAO_PDF_DIAS`,
`RETENCAO_LOTE_DIAS` e os dois limiares de prioridade. Mudar qualquer um deles sem
atualizar este README faz o teste falhar. É de propósito: é o que impede a documentação
de envelhecer em silêncio.

`MEM_LIMITE` é a exceção — vive no `docker-compose.yml` e nenhum código Python o lê, então
nenhum teste o cobre. Ao alterá-lo, atualize a tabela acima manualmente.
