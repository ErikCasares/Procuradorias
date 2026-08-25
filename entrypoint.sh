#!/bin/sh
# Entrypoint — ajusta o dono das pastas montadas e larga o privilégio.
#
# POR QUE ISTO EXISTE
#
# O serviço roda como 'hera' (não-root). Mas as quatro pastas de trabalho vêm
# de fora do container, e um bind-mount de host SOBREPÕE o diretório da imagem:
# o `chown hera:hera` feito no build simplesmente não vale para o que foi
# montado por cima. Volume NOMEADO herdaria o dono da imagem; bind-mount, não.
#
# O Easypanel monta bind-mount de host (/etc/easypanel/projects/.../volumes/...),
# criado como root:root. Com USER fixo no Dockerfile, o container sobe, o
# /health responde verde — e o primeiro upload morre com "permission denied",
# porque só aí alguém tenta escrever.
#
# Então: começa como root, acerta o dono do que estiver montado, e só então
# desce para 'hera' com gosu. O privilégio é usado por um instante e largado
# ANTES de servir a primeira requisição.
#
# Se o container já foi iniciado como não-root (docker run --user, ou uma
# plataforma que force isso), não há o que ajustar — segue direto.

set -e

USUARIO='hera'
PASTAS='/app/JSON /app/resultados /app/dados'
PASTA_ENTRADA='/app/processos pra analiser'

ajustar_dono() {
    for pasta in "$@"; do
        [ -d "$pasta" ] || continue

        # Só percorre a árvore quando o dono está errado. `dados/` guarda os
        # PDFs recebidos — pode ter milhares de arquivos, e um `chown -R` cego
        # a cada restart atrasaria o start sem necessidade.
        dono=$(stat -c '%U' "$pasta" 2>/dev/null || echo '?')
        [ "$dono" = "$USUARIO" ] && continue

        # `|| true`: num volume de rede (NFS, CIFS) o chown pode ser recusado e
        # ainda assim a escrita funcionar. Falhar aqui derrubaria um serviço que
        # rodaria bem — o erro real, se houver, aparece no primeiro upload.
        chown -R "$USUARIO:$USUARIO" "$pasta" 2>/dev/null \
            || echo "[entrypoint] AVISO: não consegui ajustar o dono de '$pasta'."
    done
}

if [ "$(id -u)" = "0" ]; then
    # As pastas podem não existir se o volume foi montado num caminho novo.
    mkdir -p $PASTAS "$PASTA_ENTRADA" 2>/dev/null || true
    ajustar_dono $PASTAS "$PASTA_ENTRADA"

    if command -v gosu >/dev/null 2>&1; then
        echo "[entrypoint] Pastas prontas. Iniciando como '$USUARIO' (sem privilégio)."
        exec gosu "$USUARIO" "$@"
    fi

    # gosu ausente: seguir como root é pior que o desenhado, mas é melhor do
    # que não subir. Dito em voz alta para não passar despercebido no log.
    echo "[entrypoint] AVISO: gosu não encontrado — o serviço vai rodar como ROOT."
    echo "[entrypoint] Reconstrua a imagem para restaurar a execução sem privilégio."
fi

exec "$@"
