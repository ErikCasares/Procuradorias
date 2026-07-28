# Procuradorias

Pipeline de dois agentes para triagem de execuções fiscais (HERA Tecnologia / PGMS).

- **Agente 1** — `promptV7.1.py`: lê os PDFs de `processos pra analiser/`, faz OCR nas
  páginas escaneadas, classifica cada processo (APTO / NÃO APTO) com apoio da OpenAI e
  escreve o Excel de revisão em `resultados/` e o JSON de repasse em `JSON/`.
- **Agente 2** — `agente2.py`: monitora `JSON/`, analisa os processos APTO
  (prioridade, ação recomendada, alerta de prescrição) e gera o histórico acumulativo
  e o relatório Excel do procurador em `resultados/`.

## Rodando com Docker

**Pré-requisito:** Docker Desktop instalado e em execução.

### 1. Configurar a chave da OpenAI

```bash
cp .env.example .env      # PowerShell: Copy-Item .env.example .env
```

Edite o `.env` e coloque sua `OPENAI_API_KEY`. Ela só é usada pelo Agente 1, mas é
obrigatória — o cliente OpenAI é instanciado já no import do script.

### 2. Construir a imagem

```bash
docker compose build
```

A imagem já traz Tesseract (idioma `por`) e Poppler, que o OCR e o `pdf2image` exigem.

### 3. Rodar o Agente 2 (watcher contínuo)

```bash
docker compose up -d agente2
docker compose logs -f agente2
```

Fica de pé processando qualquer `*_agente2.json` novo que aparecer em `JSON/`.

### 4. Rodar o Agente 1 (lote pontual)

Coloque os PDFs em `processos pra analiser/` e execute:

```bash
docker compose run --rm agente1
```

Ele processa o lote, escreve o JSON em `JSON/` e encerra. O Agente 2, se estiver de pé,
detecta o arquivo no próximo ciclo (10s) e gera a priorização.

### Comandos úteis

```bash
# Agente 2 em modo pontual, num arquivo específico
docker compose run --rm agente2 python agente2.py --arquivo JSON/resultados_procesosV7_1_agente2.json

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
