FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3000

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

COPY promptV7.1.py agente2.py webapp.py gerar_credencial.py ./

# Pastas de trabalho — montadas como volumes pelo docker-compose.
# Criadas aqui para o Agente 1 não quebrar no os.listdir() se o volume faltar.
RUN mkdir -p "/app/processos pra analiser" /app/JSON /app/resultados /app/dados/lotes

EXPOSE 3000

# Padrão: interface web (é o que o Easypanel publica no domínio).
# Os agentes isolados são disparados via `docker compose run`.
CMD ["sh", "-c", "uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-3000}"]
