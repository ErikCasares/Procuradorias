# Procuradorias

Pipeline de dois agentes para triagem de execuções fiscais (HERA Tecnologia / PGMS).

- **Agente 1** — `promptV7.1.py`: lê os PDFs de `processos pra analiser/`, faz OCR nas
  páginas escaneadas, classifica cada processo (APTO / NÃO APTO) com apoio da OpenAI e
  escreve o Excel de revisão em `resultados/` e o JSON de repasse em `JSON/`.
- **Agente 2** — `agente2.py`: monitora `JSON/`, analisa os processos APTO
  (prioridade, ação recomendada, alerta de prescrição) e gera o histórico acumulativo
  e o relatório Excel do procurador em `resultados/`.
- **API e painel** — `webapp.py`: expõe o pipeline em duas superfícies — a API v1 para
  sistemas externos (SIAP) e o painel do procurador. É o que fica publicado no domínio.

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
| `MAX_MB_LOTE` | Tamanho máximo de um lote (padrão 200) |

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

## API v1 — para sistemas externos (SIAP)

Autenticação por token Bearer em todas as rotas:

```http
Authorization: Bearer <token>
```

**O processamento é assíncrono.** OCR e GPT levam minutos por lote, e nenhum proxy
mantém a conexão aberta tanto tempo. O envio responde na hora com um `lote_id`; o
consumidor acompanha por polling. Os lotes rodam em fila serial — um por vez, porque
o OCR já satura a CPU.

| Rota | Uso |
| ----------------------------------- | ------------------------------------------------ |
| `POST /api/v1/lotes` | Envia PDFs (multipart, campo `arquivos`). Responde `202` com o `lote_id` |
| `GET /api/v1/lotes` | Lista os lotes deste consumidor |
| `GET /api/v1/lotes/{id}` | Estado do lote, com log |
| `GET /api/v1/lotes/{id}/resultado` | Priorização completa (`409` enquanto não concluir) |
| `GET /api/v1/lotes/{id}/planilha` | Excel de revisão do lote |
| `GET /health` | Healthcheck, sem autenticação |

Cada consumidor só enxerga os próprios lotes — o token de um recebe `404` no lote de
outro.

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

### Sempre confira `avisos`

O Agente 1 trata OCR quebrado, erro de API e PDF ilegível internamente e **encerra
com código de sucesso**. Um lote pode chegar a `concluido` tendo classificado zero
processos. Por isso a resposta traz `resumo` e `avisos`:

```json
{
  "status": "concluido",
  "resumo": "0 de 12 processo(s) classificados como APTO",
  "avisos": [
    "Nenhum processo foi classificado como APTO — o resultado sairá vazio",
    "OpenAI recusou a autenticação (401) — verifique a OPENAI_API_KEY"
  ]
}
```

`status: "concluido"` com `avisos` não vazio significa que o lote rodou até o fim mas
algo deu errado no caminho. Trate como falha.

## Painel do procurador

Na raiz (`/`), protegido por `SENHA_PAINEL`. Envia PDFs, acompanha os lotes com log
ao vivo e baixa os relatórios acumulados. Usa a mesma fila da API.

## Rodando com Docker

**Pré-requisito:** Docker Desktop instalado e em execução.

### 1. Configurar a chave da OpenAI

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

Edite o `.env` e coloque sua `OPENAI_API_KEY`. Ela só é usada pelo Agente 1, mas é
obrigatória — o cliente OpenAI é instanciado já no import do script. No Easypanel,
defina-a em **Ambiente** nas configurações do serviço.

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
Swagger (ou o domínio configurado). O log do startup diz quais
consumidores da API foram habilitados — se aparecer `API_TOKENS não configurado`, a
API está recusando tudo.

### Rodando os agentes pela linha de comando

Alternativa à web, para lotes manuais ou depuração:

```bash
# Agente 1 — processa os PDFs já presentes em "processos pra analiser/"
docker compose run --rm agente1

# Agente 2 — num arquivo específico
docker compose run --rm agente1 python agente2.py --arquivo JSON/resultados_procesosV7_1_agente2.json

# Agente 2 como watcher contínuo (opcional — a web já o chama a cada lote;
# só é útil se algo depositar JSON em JSON/ por fora da web)
docker compose --profile watcher up -d agente2

# Parar tudo
docker compose down
```

### Pastas

As três pastas são montadas como volumes — o que o container escreve aparece direto
no seu disco:

| Pasta                     | Papel                                             |
| ------------------------- | ------------------------------------------------- |
| `processos pra analiser/` | Entrada do uso manual por linha de comando        |
| `dados/lotes/<id>/`       | Pastas isoladas de cada lote da API/painel        |
| `JSON/`                   | Troca entre os agentes + histórico JSONL          |
| `resultados/`             | Excel de revisão (Agente 1) e de priorização (A2) |

Os agentes aceitam `PASTA_ENTRADA`, `PASTA_JSON` e `PASTA_RESULTADOS` por variável de
ambiente — é assim que cada lote ganha espaço próprio. Sem elas, o comportamento é o
original: subpastas ao lado do script.

**No Easypanel, monte volumes persistentes** em `/app/JSON`, `/app/resultados` e
`/app/dados`. Sem isso, recriar o container apaga o histórico acumulado e todos os
lotes já processados.
