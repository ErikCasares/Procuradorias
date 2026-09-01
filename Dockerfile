FROM python:3.12-slim

# PYTHONIOENCODING/LANG: os relatórios e o log dos agentes têm acento e
# caractere de caixa (═ ┌ •). Sem UTF-8 explícito, o print quebra conforme a
# locale do host — o script sai com erro e a rota devolve 500 mesmo tendo
# encontrado o processo.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PORT=3000

# tesseract-ocr-por  → OCR_IDIOMA = 'por' (agente1.py)
# poppler-utils      → backend do pdf2image (convert_from_path)
# tzdata             → para que TZ valha nos timestamps dos relatórios
# curl               → HEALTHCHECK
# gosu               → largar o privilégio no entrypoint (ver entrypoint.sh)
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-por \
        poppler-utils \
        tzdata \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# [Fase 5] Cache de OCR e estado do merge em volumes PERSISTENTES (senão sumiriam
# ao recriar o container, pois cairiam em /app, dentro da imagem):
#   - estado do merge  -> /app/JSON  (importante; vai junto no backup do histórico)
#   - cache de OCR      -> /app/dados (regenerável; não precisa de backup)
# Ambas as pastas já são montadas como volume. Valem também no Easypanel (docker run).
# Sobrescrevíveis por ambiente se o operador quiser outro caminho.
ENV PASTA_CACHE=/app/dados/cache_ocr \
    ESTADO_PATH=/app/JSON/estado_atual_processos.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agente1.py agente2.py webapp.py buscar_processo.py gerar_credencial.py ./
COPY entrypoint.sh /entrypoint.sh

# Pastas de trabalho — montadas como volumes em produção. Criadas aqui para o
# Agente 1 não quebrar no os.listdir() se algum volume faltar.
#
# O chown vale só para o que NÃO for sobreposto por um bind-mount de host: um
# bind-mount traz o dono do host (root, no Easypanel) e ignora isto. Quem acerta
# o dono do que foi montado é o entrypoint, em tempo de execução.
RUN mkdir -p "/app/processos pra analiser" /app/JSON /app/resultados /app/dados/lotes /app/dados/cache_ocr \
    && useradd --create-home --uid 10001 hera \
    && chown -R hera:hera /app \
    && chmod +x /entrypoint.sh

EXPOSE 3000

# O Easypanel usa isto para saber se o container subiu de fato. /health não
# exige autenticação e não devolve dado de processo.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-3000}/health" || exit 1

# Sem USER: o container PRECISA começar como root para ajustar o dono das
# pastas montadas. O entrypoint desce para 'hera' antes de servir qualquer
# requisição — o uvicorn nunca roda com privilégio.
ENTRYPOINT ["/entrypoint.sh"]

# Padrão: interface web (é o que o Easypanel publica no domínio).
# Os agentes isolados são disparados via `docker compose run`.
#
# --proxy-headers + --forwarded-allow-ips: atrás do Easypanel/Traefik, TODA
# requisição chega com o IP do proxy. Sem confiar no X-Forwarded-For, o freio
# de tentativas de senha vê um IP só e volta a ser global — dez erros trancam
# o painel para todo mundo. Confiar no cabeçalho só é seguro porque o container
# não é exposto direto: quem fala com ele é sempre o proxy.
CMD ["sh", "-c", "uvicorn webapp:app --host 0.0.0.0 --port ${PORT:-3000} --proxy-headers --forwarded-allow-ips='*'"]