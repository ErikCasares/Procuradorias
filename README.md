# Procuradorias

Pipeline de dois agentes para triagem de execuções fiscais (HERA Tecnologia / PGMS).

- **Agente 1** — `promptV7.1.py`: lê os PDFs de `processos pra analiser/`, faz OCR nas
  páginas escaneadas, classifica cada processo (APTO / NÃO APTO) com apoio da OpenAI e
  escreve o Excel de revisão em `resultados/` e o JSON de repasse em `JSON/`.
- **Agente 2** — `agente2.py`: monitora `JSON/`, analisa os processos APTO
  (prioridade, ação recomendada, alerta de prescrição) e gera o histórico acumulativo
  e o relatório Excel do procurador em `resultados/`.
- **Web** — `webapp.py`: interface para o procurador. Faz upload dos PDFs, dispara o
  pipeline (Agente 1 → Agente 2), mostra o log em tempo real e entrega os relatórios.
  É o que fica publicado no domínio.

## Interface web

Publicada na porta `3000`. Fluxo em três passos: enviar os PDFs, processar o lote,
baixar os relatórios.

Ao final de cada lote a tela informa **quantos processos foram classificados como APTO**
e destaca avisos quando algo falhou silenciosamente — OCR sem Poppler, chave da OpenAI
recusada, PDF sem texto extraível, memória insuficiente. Isso importa porque o Agente 1
trata esses erros internamente e encerra com código de sucesso: sem os avisos, um lote
que classificou zero processos pareceria ter dado tudo certo.

Rotas de API, caso queira automatizar:

| Rota | Uso |
| ------------------------- | ---------------------------------------- |
| `GET /health` | Healthcheck (Easypanel) |
| `POST /api/upload` | Envia PDFs (multipart, campo `arquivos`) |
| `GET /api/arquivos` | Lista a fila de entrada |
| `DELETE /api/arquivos/{nome}` | Remove um PDF da fila |
| `POST /api/processar` | Dispara o lote (409 se já houver um) |
| `GET /api/status` | Estado, log, resumo e avisos |
| `GET /api/resultados` | Lista os relatórios gerados |
| `GET /api/resultados/{nome}` | Baixa um relatório |
| `GET /api/docs` | Swagger |

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

### 3. Subir a interface web

```bash
docker compose up -d web
docker compose logs -f web
```

Acesse `http://localhost:3000` (ou o domínio configurado). É por aqui que o procurador
envia os processos e baixa a priorização — a web chama os dois agentes internamente.

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
| `processos pra analiser/` | Entrada: PDFs dos processos (só o Agente 1 lê)    |
| `JSON/`                   | Troca entre os agentes + histórico JSONL          |
| `resultados/`             | Excel de revisão (Agente 1) e de priorização (A2) |
