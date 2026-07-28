FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# tesseract-ocr-por  → OCR_IDIOMA = 'por' (promptV7.1.py)
# poppler-utils      → backend do pdf2image (convert_from_path)
# tzdata             → para que TZ valha nos timestamps dos relatórios
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-por \
        poppler-utils \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY promptV7.1.py agente2.py ./

# Pastas de trabalho — montadas como volumes pelo docker-compose.
# Criadas aqui para o Agente 1 não quebrar no os.listdir() se o volume faltar.
RUN mkdir -p "/app/processos pra analiser" /app/JSON /app/resultados

# Padrão: watcher do Agente 2 (processo de longa duração).
# O Agente 1 é disparado sob demanda via `docker compose run`.
CMD ["python", "agente2.py", "--watch"]
