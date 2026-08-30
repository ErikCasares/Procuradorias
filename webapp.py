"""
API e painel — Triagem de Execuções Fiscais
HERA Tecnologia / PGMS — Contrato nº 01/2026

Duas superfícies sobre o mesmo pipeline (Agente 1 → Agente 2):

    /api/v1/*   API para sistemas externos (SIAP). Autenticação por token
                Bearer. Assíncrona: o lote é aceito na hora e processado numa
                fila; o consumidor acompanha por polling.

    /painel     Interface do procurador. Autenticação por senha, sessão em
                cookie. Mesmo pipeline, uso manual.

    /api/docs   Swagger — o contrato que o integrador lê e testa. ReDoc em
                /api/redoc, OpenAPI cru em /api/openapi.json.

AUTENTICAÇÃO FALHA FECHADA — sem API_TOKENS configurado a API recusa tudo com
503, e sem SENHA_PAINEL o painel não abre. É deliberado: uma configuração
incompleta deixa o serviço inacessível, nunca aberto. Os relatórios carregam
nome, CPF/CNPJ e valor de dívida, então o modo degradado seguro é negar.

Por que assíncrono: OCR + GPT levam minutos por lote e qualquer proxy derruba
a conexão antes do fim. O lote entra numa fila serial — um por vez, porque o
OCR já satura a CPU.

Isolamento: cada lote recebe pastas próprias em dados/lotes/<id>/, passadas
aos agentes por variável de ambiente. Sem isso, dois lotes simultâneos se
sobrescreveriam — o JSON de traspasse do Agente 1 tem nome fixo.

Uso:
    uvicorn webapp:app --host 0.0.0.0 --port 3000
"""

import os
import re
import sys
import json
import shutil
import hashlib
import tempfile
import secrets
import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import (
    Cookie, Depends, FastAPI, File, Form, HTTPException,
    Query, Request, Security, UploadFile,
)
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

# A rota /api/v1/processos executa `buscar_processo.py` como subprocess (Opção A):
# roda o arquivo inteiro e devolve a saída formatada do main(), para a API e o
# terminal nunca divergirem. Por isso não importamos mais buscar_dados aqui.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [web] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webapp")


# ════════════════════════════════════════════════════════════════
# CONFIGURAÇÃO
# ════════════════════════════════════════════════════════════════

BASE_DIR      = Path(__file__).resolve().parent

# Compartilhadas — o histórico acumulativo do procurador vive aqui
PASTA_JSON    = BASE_DIR / "JSON"
PASTA_RESULT  = BASE_DIR / "resultados"

# Por lote
PASTA_DADOS   = BASE_DIR / "dados"
PASTA_LOTES   = PASTA_DADOS / "lotes"
ARQ_REGISTRO  = PASTA_DADOS / "registro.json"

# Planilhas recortadas por consumidor, montadas sob demanda. Não é histórico:
# pode ser apagada a qualquer momento que a próxima chamada refaz.
PASTA_RECORTES = PASTA_DADOS / "recortes"


# Pasta de entrada do uso manual por linha de comando (compatibilidade)
PASTA_ENTRADA_PADRAO = BASE_DIR / "processos pra analiser"

# Prefixos que somem do log devolvido ao consumidor: ele não precisa da árvore
# de diretórios do servidor, e mostrá-la só ajuda quem for atacar o serviço.
# Os mais longos primeiro, para o prefixo maior casar antes do menor.
_PREFIXOS_INTERNOS = tuple(sorted(
    (str(PASTA_LOTES), str(PASTA_DADOS), str(PASTA_JSON), str(PASTA_RESULT),
     str(PASTA_ENTRADA_PADRAO), str(BASE_DIR)),
    key=len, reverse=True,
))

MAX_MB_LOTE    = int(os.getenv("MAX_MB_LOTE", "500"))

# Teto por etapa (Agente 1, depois Agente 2). A fila é serial: sem teto, um
# agente travado segura todos os lotes seguintes para sempre. Uma hora é folga
# suficiente para um lote grande e ainda assim destrava o serviço sozinho.
TIMEOUT_AGENTE_S = int(os.getenv("TIMEOUT_AGENTE_S", "3600"))

# Retenção em disco. Os PDFs recebidos são o que pesa — o do cliente tem 100 MB
# cada. Zero desliga a limpeza (mantém tudo, como era antes).
RETENCAO_PDF_DIAS  = int(os.getenv("RETENCAO_PDF_DIAS", "7"))
RETENCAO_LOTE_DIAS = int(os.getenv("RETENCAO_LOTE_DIAS", "90"))

# Quantas linhas de log ficam GRAVADAS por lote. Em memória vale MAX_LINHAS_LOG;
# no arquivo, menos: o registro é reserializado inteiro a cada salvamento.
MAX_LINHAS_LOG_DISCO = 120
MAX_LINHAS_LOG = 500
HORAS_SESSAO   = int(os.getenv("HORAS_SESSAO", "12"))
COOKIE_SEGURO  = os.getenv("COOKIE_SEGURO", "0") == "1"


# ── Credenciais ──────────────────────────────────────────────────
#
# O ambiente guarda HASHES, não segredos. Variável de ambiente não é cofre:
# o valor aparece na UI do Easypanel, em `docker inspect`, em /proc/<pid>/environ
# e em dump de erro. Com hash, quem ler qualquer um desses caminhos não autentica.
#
# Os segredos são emitidos por gerar_credencial.py, que mostra o valor uma única
# vez e nunca o grava.
#
# SHA-256 direto basta para os tokens da API — são 256 bits aleatórios, sem
# força bruta viável. A senha do painel é escolhida por gente, entropia baixa,
# atacável por dicionário: essa usa scrypt, lento de propósito.

SCRYPT_MAXMEM = 64 * 1024 * 1024
PREFIXO_TOKEN = "pgms_live_"   # marca dos segredos emitidos por gerar_credencial.py

_tokens_legado = []    # rótulos ainda configurados em texto puro
_erros_config  = []    # credenciais malformadas — nunca autenticariam ninguém


def _hash_valido(valor: str) -> bool:
    """SHA-256 em hexadecimal: 64 caracteres de 0-9a-f."""
    return len(valor) == 64 and all(c in "0123456789abcdef" for c in valor)


def _carregar_tokens() -> dict:
    """
    API_TOKENS no formato "rotulo:sha256:<hash>", separado por vírgula.
    Devolve {hash: rotulo}.

    Aceita ainda "rotulo:<token>" em texto puro — formato antigo, mantido para
    não trancar um serviço já configurado. Nesse caso o hash é calculado aqui e
    o rótulo entra em _tokens_legado, que vira aviso no startup.

    Um hash malformado é DESCARTADO com erro em _erros_config, não aceito
    silenciosamente. Guardar um hash que nenhum token gera faria a API recusar
    todo mundo com 401 sem explicar por quê — o erro mais caro de diagnosticar.
    """
    bruto = os.getenv("API_TOKENS", "").strip()
    tokens = {}
    for parte in bruto.split(","):
        parte = parte.strip()
        if not parte:
            continue

        # O segredo emitido pelo gerador começa com o prefixo. Se ele aparece
        # aqui, o par foi invertido: colaram o token no lugar do hash.
        if parte.startswith(PREFIXO_TOKEN) or f":{PREFIXO_TOKEN}" in parte:
            _erros_config.append(
                "API_TOKENS contém o TOKEN em vez do hash — o valor que começa "
                f"com '{PREFIXO_TOKEN}' é o segredo que vai para o consumidor, não "
                "para o ambiente. Use a linha 'API_TOKENS=...' que o gerador "
                "imprime no passo 2."
            )
            continue

        if ":" not in parte:
            _erros_config.append(
                f"API_TOKENS: a entrada {parte[:20]!r}... não tem ':' separando "
                "o rótulo do hash. O formato é 'rotulo:sha256:<hash>'."
            )
            continue

        rotulo, _, resto = parte.partition(":")
        rotulo, resto = rotulo.strip(), resto.strip()
        if not rotulo or not resto:
            _erros_config.append(f"API_TOKENS: entrada incompleta em {parte[:30]!r}")
            continue

        if resto.lower().startswith("sha256:"):
            digest = resto[7:].strip().lower()
            if not _hash_valido(digest):
                _erros_config.append(
                    f"API_TOKENS['{rotulo}']: o hash tem {len(digest)} caractere(s) e "
                    f"deveria ter 64 em hexadecimal. Parece estar em base64 ou truncado. "
                    f"Gere de novo com 'python gerar_credencial.py api {rotulo}'."
                )
                continue
        else:
            digest = hashlib.sha256(resto.encode()).hexdigest()
            _tokens_legado.append(rotulo)

        tokens[digest] = rotulo
    return tokens


def _conferir_scrypt(senha: str, guardado: str) -> bool:
    """Valida contra 'scrypt$n$r$p$salt_hex$hash_hex'."""
    try:
        alg, n, r, p, salt_hex, hash_hex = guardado.split("$")
        if alg != "scrypt":
            return False
        calc = hashlib.scrypt(
            senha.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), maxmem=SCRYPT_MAXMEM, dklen=32,
        )
        return secrets.compare_digest(calc.hex(), hash_hex)
    except Exception:
        return False


def _validar_hash_senha(valor: str) -> bool:
    """Confere o formato scrypt$n$r$p$salt_hex$hash_hex antes de aceitar."""
    if not valor:
        return False
    partes = valor.split("$")
    if len(partes) != 6 or partes[0] != "scrypt":
        _erros_config.append(
            "SENHA_PAINEL_HASH não está no formato esperado "
            "'scrypt$n$r$p$salt$hash'. Gere de novo com "
            "'python gerar_credencial.py painel'."
        )
        return False
    try:
        int(partes[1]), int(partes[2]), int(partes[3])
        bytes.fromhex(partes[4]), bytes.fromhex(partes[5])
    except ValueError:
        _erros_config.append(
            "SENHA_PAINEL_HASH tem o formato certo mas conteúdo inválido. "
            "Gere de novo com 'python gerar_credencial.py painel'."
        )
        return False
    return True


TOKENS            = _carregar_tokens()
_hash_senha_bruto = os.getenv("SENHA_PAINEL_HASH", "").strip()
SENHA_PAINEL_HASH = _hash_senha_bruto if _validar_hash_senha(_hash_senha_bruto) else ""
SENHA_PAINEL      = os.getenv("SENHA_PAINEL", "").strip()   # legado, em texto puro
PAINEL_ATIVO      = bool(SENHA_PAINEL_HASH or SENHA_PAINEL)


def _senha_confere(enviada: str) -> bool:
    if SENHA_PAINEL_HASH:
        return _conferir_scrypt(enviada, SENHA_PAINEL_HASH)
    if SENHA_PAINEL:
        return secrets.compare_digest(enviada, SENHA_PAINEL)
    return False


# ════════════════════════════════════════════════════════════════
# AUTENTICAÇÃO
# ════════════════════════════════════════════════════════════════

_sessoes = {}   # token de sessão → validade (datetime)

# Falhas de login POR IP. Global e sem IP, dez senhas erradas de qualquer origem
# trancavam o painel para todo mundo por cinco minutos — inclusive para o
# procurador que sabe a senha. Num serviço publicado, isso é negação de serviço
# de graça.
_falhas_login = {}          # ip → deque de timestamps
MAX_FALHAS_LOGIN = 10
JANELA_FALHAS_MIN = 5

# Declarado como esquema de segurança para o Swagger mostrar o botão Authorize —
# o integrador cola o token uma vez e testa todas as rotas. auto_error=False
# porque as mensagens de erro daqui explicam o que fazer; as do FastAPI não.
_bearer = HTTPBearer(
    scheme_name="Token do consumidor",
    description=(
        "Token emitido por `python gerar_credencial.py api <rotulo>`. "
        "Cole só o token — o Swagger acrescenta o prefixo `Bearer`."
    ),
    auto_error=False,
)


def autenticar_api(
    credencial: HTTPAuthorizationCredentials = Security(_bearer),
) -> str:
    """Valida o Bearer token e devolve o rótulo do consumidor."""
    if not TOKENS:
        raise HTTPException(
            503,
            "API sem credenciais configuradas. Defina API_TOKENS no ambiente "
            "do serviço no formato 'rotulo:token'.",
        )

    if not credencial or credencial.scheme.lower() != "bearer":
        raise HTTPException(
            401,
            "Envie o token no cabeçalho: Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    enviado = credencial.credentials.strip()
    # Hash do que chegou, comparado com os hashes guardados. compare_digest é
    # de tempo constante — o tempo de resposta não vaza quanto do token acertou.
    digest = hashlib.sha256(enviado.encode()).hexdigest()
    for guardado, rotulo in TOKENS.items():
        if secrets.compare_digest(digest, guardado):
            return rotulo

    raise HTTPException(401, "Token inválido", headers={"WWW-Authenticate": "Bearer"})


def _sessao_valida(cookie: str) -> bool:
    if not cookie:
        return False
    validade = _sessoes.get(cookie)
    if not validade:
        return False
    if datetime.now() > validade:
        _sessoes.pop(cookie, None)
        return False
    return True


def exigir_painel(sessao: str = Cookie(None)):
    if not PAINEL_ATIVO:
        raise HTTPException(
            503,
            "Painel sem senha configurada. Gere o hash com "
            "'python gerar_credencial.py painel' e defina SENHA_PAINEL_HASH.",
        )
    if not _sessao_valida(sessao):
        raise HTTPException(401, "Sessão expirada ou ausente")
    return True


# ════════════════════════════════════════════════════════════════
# REGISTRO DE LOTES
# ════════════════════════════════════════════════════════════════

_lotes = {}          # id → dict
_fila = None         # asyncio.Queue, criada no lifespan
_lock = asyncio.Lock()


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _garantir_pastas():
    for p in (PASTA_JSON, PASTA_RESULT, PASTA_LOTES, PASTA_ENTRADA_PADRAO):
        p.mkdir(parents=True, exist_ok=True)


def _salvar_registro():
    """
    Persiste o registro para os lotes sobreviverem a um restart.

    Grava uma projeção enxuta: o log vai aparado nas últimas
    MAX_LINHAS_LOG_DISCO linhas. O registro é reserializado INTEIRO a cada
    salvamento, então cada linha guardada é multiplicada pelo número de lotes —
    com mil lotes, um log completo por lote vira dezenas de MB reescritos a cada
    mudança de estado.
    """
    try:
        enxuto = {}
        for lid, lote in _lotes.items():
            copia = dict(lote)
            registro_log = lote.get("log") or []
            if len(registro_log) > MAX_LINHAS_LOG_DISCO:
                copia["log"] = registro_log[-MAX_LINHAS_LOG_DISCO:]
            enxuto[lid] = copia

        tmp = ARQ_REGISTRO.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(enxuto, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ARQ_REGISTRO)
    except Exception as e:
        log.error(f"Não foi possível salvar o registro de lotes: {e}")


async def _salvar_registro_async():
    """
    Mesmo salvamento, fora do event loop.

    json.dump do registro inteiro é síncrono e, com muitos lotes, segura o
    servidor por centenas de milissegundos — enquanto isso nenhuma outra
    requisição é atendida.
    """
    await asyncio.to_thread(_salvar_registro)


def _limpar_antigos() -> None:
    """
    Aplica a retenção: primeiro os PDFs recebidos, depois o lote inteiro.

    Os PDFs são o volume: o original continua com quem enviou, e o que o serviço
    produziu (planilha, JSON) fica até a retenção do lote. Sem isto, nada nunca
    era apagado e o volume enchia em silêncio.
    """
    if not (RETENCAO_PDF_DIAS or RETENCAO_LOTE_DIAS):
        return

    agora = datetime.now()
    pdfs_apagados = lotes_apagados = 0

    for lote_id, lote in list(_lotes.items()):
        if lote.get("status") in ("na_fila", "processando"):
            continue
        marca = lote.get("concluido_em") or lote.get("criado_em")
        if not marca:
            continue
        try:
            quando = datetime.strptime(marca, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        idade = (agora - quando).days

        if RETENCAO_LOTE_DIAS and idade >= RETENCAO_LOTE_DIAS:
            shutil.rmtree(_dir_lote(lote_id), ignore_errors=True)
            _lotes.pop(lote_id, None)
            lotes_apagados += 1
            continue

        if RETENCAO_PDF_DIAS and idade >= RETENCAO_PDF_DIAS:
            entrada = _dir_lote(lote_id) / "entrada"
            if entrada.exists():
                shutil.rmtree(entrada, ignore_errors=True)
                pdfs_apagados += 1

    if pdfs_apagados or lotes_apagados:
        log.info(
            f"Retenção: PDFs de {pdfs_apagados} lote(s) apagados "
            f"(> {RETENCAO_PDF_DIAS} dias), {lotes_apagados} lote(s) removidos "
            f"(> {RETENCAO_LOTE_DIAS} dias)"
        )
        _salvar_registro()


def _carregar_registro():
    global _lotes
    if not ARQ_REGISTRO.exists():
        return
    try:
        with open(ARQ_REGISTRO, encoding="utf-8") as f:
            _lotes = json.load(f)
        # Um lote que ficou "processando" num restart não vai retomar sozinho
        for lote in _lotes.values():
            if lote.get("status") in ("na_fila", "processando"):
                lote["status"] = "erro"
                lote["erro"] = "Serviço reiniciado durante o processamento — reenvie o lote"
        log.info(f"Registro carregado: {len(_lotes)} lote(s)")
    except Exception as e:
        log.error(f"Registro ilegível, começando vazio: {e}")
        _lotes = {}


def _dir_lote(lote_id: str) -> Path:
    return PASTA_LOTES / lote_id


def _decorrido(inicio: str | None, fim: str | None = None) -> int | None:
    """Segundos entre dois timestamps do registro. Sem 'fim', conta até agora."""
    if not inicio:
        return None
    try:
        i = datetime.strptime(inicio, "%Y-%m-%d %H:%M:%S")
        f = datetime.strptime(fim, "%Y-%m-%d %H:%M:%S") if fim else datetime.now()
    except (ValueError, TypeError):
        return None
    return max(0, int((f - i).total_seconds()))


def _lotes_na_frente(lote: dict) -> int:
    """
    Quantos lotes precisam terminar antes deste começar.

    A fila é serial, então o que está processando também conta — quem espera
    quer saber quantas rodadas faltam, não a posição numa lista.
    """
    criado = lote.get("criado_em") or ""
    return sum(
        1
        for outro in _lotes.values()
        if outro.get("id") != lote.get("id")
        and (
            outro.get("status") == "processando"
            or (outro.get("status") == "na_fila" and (outro.get("criado_em") or "") < criado)
        )
    )


_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Nome fixo do relatório do Agente 2 (REPORTE_XLSX em agente2.py). É ACUMULATIVO:
# regerado do histórico inteiro a cada lote, então traz também os lotes anteriores.
_XLSX_AGENTE2 = "historial_agente2.xlsx"

# Histórico acumulativo do Agente 2 (HISTORIAL_JSONL em agente2.py) — é a fonte
# a partir da qual se monta o recorte de cada consumidor.
_JSONL_AGENTE2 = "historial_agente2.jsonl"


def _arquivos_do_lote(lote: dict) -> dict:
    """
    Artefatos deste lote que existem em disco: tipo → (caminho, nome, media_type).

    Chave fixa em vez de nome de arquivo na URL — o consumidor nunca escolhe o
    caminho, então não há como escapar da pasta.
    """
    lid  = lote["id"]
    d    = _dir_lote(lid)
    mapa = {}

    if lote.get("planilha"):
        p = d / "resultados" / lote["planilha"]
        if p.exists():
            mapa["agente1_planilha"] = (p, f"lote_{lid}_agente1.xlsx", _XLSX)

    p = d / "json" / "saida_agente1_V8.json"
    if p.exists():
        mapa["agente1_json"] = (p, f"lote_{lid}_agente1.json", "application/json")

    p = PASTA_JSON / f"lote_{lid}_agente2_resultado.json"
    if p.exists():
        mapa["agente2_json"] = (p, f"lote_{lid}_agente2.json", "application/json")

    p = PASTA_RESULT / _XLSX_AGENTE2
    if p.exists():
        mapa["agente2_planilha"] = (p, "priorizacao_acumulada.xlsx", _XLSX)

    return mapa


DESCRICAO_ARQUIVO = {
    "agente1_planilha": "Planilha de revisão do Agente 1 (Excel) — só deste lote",
    "agente1_json"    : "Extração e classificação do Agente 1 (JSON) — só deste lote",
    "agente2_json"    : "Priorização do Agente 2 (JSON) — só deste lote",
    "agente2_planilha": "Relatório de priorização do Agente 2 (Excel) — ACUMULADO: "
                        "todos os processos dos SEUS lotes, não só os deste",
}


def _origens_do_consumidor(consumidor: str) -> set:
    """
    Nomes de arquivo que o Agente 2 grava no campo 'origem_lote' dos lotes deste
    consumidor. É a chave que recorta o histórico compartilhado.
    """
    return {
        f"lote_{lote['id']}_agente2.json"
        for lote in _lotes.values()
        if lote.get("origem") == consumidor
    }


def _planilha_a2_recortada(consumidor: str):
    """
    Monta a planilha de priorização com APENAS os lotes deste consumidor e
    devolve o caminho, ou None se ele ainda não tem processo no histórico.

    Existe porque resultados/historial_agente2.xlsx é único, global e
    acumulativo: entregá-lo direto a um consumidor da API vazaria nome,
    CPF/CNPJ e valor de dívida dos processos de todos os outros.
    """
    historico = PASTA_JSON / _JSONL_AGENTE2
    origens = _origens_do_consumidor(consumidor)
    if not historico.exists() or not origens:
        return None
    try:
        import agente2
        PASTA_RECORTES.mkdir(parents=True, exist_ok=True)
        destino = PASTA_RECORTES / f"priorizacao_{consumidor}.xlsx"
        gerado = agente2.gerar_reporte_xlsx(str(historico), str(destino), origens=origens)
        return Path(gerado) if gerado else None
    except Exception:
        log.exception(f"Falha ao montar a planilha do consumidor '{consumidor}'")
        return None


def _publico(lote: dict, incluir_log=False, incluir_analises=False) -> dict:
    """Projeção do lote para resposta — esconde caminhos internos."""
    status = lote["status"]
    saida = {
        "lote_id"      : lote["id"],
        "status"       : status,
        "origem"       : lote.get("origem"),
        "criado_em"    : lote.get("criado_em"),
        "iniciado_em"  : lote.get("iniciado_em"),
        "concluido_em" : lote.get("concluido_em"),
        "arquivos"     : lote.get("arquivos", []),
        "campos"       : lote.get("campos") or "todas",   # [Fase 3] etapas pedidas
        "etapa"        : lote.get("etapa"),
        # Quem espera precisa de sinal de vida: um lote silencioso por oito
        # minutos é indistinguível de um lote travado. O tempo é calculado
        # aqui, no relógio do serviço — o do navegador pode estar noutro fuso.
        "decorrido_s"     : _decorrido(
            lote.get("iniciado_em") or lote.get("criado_em"),
            lote.get("concluido_em"),
        ),
        "lotes_na_frente" : _lotes_na_frente(lote) if status == "na_fila" else None,
        # O que dá para baixar deste lote, para o consumidor não ter que
        # tentar cada rota e colecionar 404.
        # Também em lote com erro: o Agente 1 pode ter concluído e deixado a
        # planilha pronta antes de o Agente 2 falhar.
        "downloads"       : sorted(_arquivos_do_lote(lote)) if status in ("concluido", "erro") else [],
        "resumo"       : lote.get("resumo"),
        "avisos"       : lote.get("avisos", []),
        "erro"         : lote.get("erro"),
        "totais"       : lote.get("totais"),
    }
    if incluir_log:
        saida["log"] = lote.get("log", [])
    if incluir_analises:
        saida["analises"] = lote.get("analises", [])
    return saida


# ════════════════════════════════════════════════════════════════
# DIAGNÓSTICO DO DESFECHO
# ════════════════════════════════════════════════════════════════
#
# O Agente 1 trata OCR quebrado e PDF ilegível internamente e ainda assim encerra
# com código 0 — um lote pode "terminar bem" tendo extraído zero processos. Sem
# isto o consumidor recebe status de sucesso e planilha vazia.

# Casa com a linha final do Agente 1: "  9 de 12 processo(s) extraído(s) com sucesso."
_RE_RESUMO = re.compile(r"(\d+)\s+de\s+(\d+)\s+processo\(s\)\s+extra[íi]do\(s\)\s+com\s+sucesso")

_SINTOMAS = (
    (("poppler",),         "Poppler indisponível — o OCR não conseguiu renderizar as páginas escaneadas"),
    (("tesseract",),       "Tesseract indisponível — o OCR não rodou"),
    (("extracao falhou",), "Algum PDF não pôde ser lido — veja 'Erro na extração' na planilha do Agente 1"),
    (("fora de escopo",),  "Algum arquivo não é uma execução fiscal — confira antes de usar o resultado"),
    (("sem texto extraível", "sin texto extraíble"),
                           "Algum PDF ficou sem texto extraível — confira a qualidade do digitalizado"),
    (("memoryerror", "out of memory", "killed"),
                           "Memória insuficiente durante o OCR — reduza OCR_MAX_WORKERS ou OCR_DPI"),
)


def _diagnosticar(linhas: list) -> tuple:
    """
    Lê o log dos agentes e responde duas perguntas que o status sozinho não
    responde: quanto do lote saiu de fato, e o que deu errado no caminho.
    """
    texto = "\n".join(linhas)
    baixo = texto.lower()

    avisos = [msg for gatilhos, msg in _SINTOMAS
              if any(g.lower() in baixo for g in gatilhos)]

    resumo = None
    m = _RE_RESUMO.search(texto)
    if m:
        ok, total = int(m.group(1)), int(m.group(2))
        resumo = f"{ok} de {total} processo(s) extraído(s) com sucesso"
        if total == 0:
            avisos.insert(0, "Nenhum processo foi lido — o resultado sairá vazio")
        elif ok == 0:
            avisos.insert(0, "Nenhum processo pôde ser extraído — o resultado sairá vazio")
        elif ok < total:
            avisos.insert(0, f"{total - ok} de {total} processo(s) não puderam ser extraídos")
    else:
        # O Agente 1 sempre imprime essa linha ao terminar. A ausência dela
        # significa que ele morreu antes de exportar — não que rodou bem.
        avisos.insert(0, "O Agente 1 não chegou a exportar o resultado — trate como falha")

    return resumo, avisos


# ════════════════════════════════════════════════════════════════
# PIPELINE
# ════════════════════════════════════════════════════════════════

# Um erro de biblioteca despeja 10-15 linhas de traceback no log do lote, que
# é devolvido ao consumidor e mostrado no painel. Medido: uma única falha de
# OCR encheu 100 das 188 linhas do lote, empurrando o resto para fora do teto.
# O que resta útil é a ÚLTIMA linha — o tipo e a mensagem da exceção.
_INICIO_TRACEBACK = "Traceback (most recent call last)"
_CONTINUA_TRACEBACK = (
    "During handling of the above exception",
    "The above exception was the direct cause",
)


def _encurtar_caminhos(linha: str) -> str:
    """
    Tira os prefixos das pastas do serviço, deixando o caminho relativo.

    Só remove PREFIXO CONHECIDO. A versão anterior encurtava qualquer token
    com barra, e comia dado do processo: o CNPJ "13.504.675/0001-10" saía
    como "0001-10" no log do cliente.
    """
    for base in _PREFIXOS_INTERNOS:
        if base in linha:
            linha = linha.replace(base + "\\", "").replace(base + "/", "").replace(base, "")
    return linha


class _FiltroLog:
    """
    Transcreve a saída de um agente para o log do lote, colapsando cada
    traceback na sua linha de exceção. Tem estado porque um traceback ocupa
    várias linhas e só se sabe onde termina ao chegar nele.
    """

    def __init__(self):
        self.no_traceback = False

    def __call__(self, linha: str):
        if not linha:
            return None

        if _INICIO_TRACEBACK in linha or any(m in linha for m in _CONTINUA_TRACEBACK):
            self.no_traceback = True
            return None

        if self.no_traceback:
            # Quadro do traceback (indentado) ou a costura entre dois deles:
            # segue engolindo.
            if linha.startswith((" ", "\t")) or any(m in linha for m in _CONTINUA_TRACEBACK):
                return None
            # Primeira linha não indentada: é a exceção. Guarda essa e sai
            # do modo traceback.
            self.no_traceback = False
            return _encurtar_caminhos(linha)

        return _encurtar_caminhos(linha)

async def _executar(cmd: list, ambiente: dict, lote: dict, etapa: str) -> int:
    """
    Roda um agente e transcreve a saída dele para o log do lote.

    Com teto de tempo: passado TIMEOUT_AGENTE_S o processo é morto e a etapa
    falha. Sem isso, um agente travado segura a fila serial indefinidamente e
    todo lote seguinte fica em 'na_fila' sem explicação nenhuma.
    """
    lote["etapa"] = etapa
    lote["log"].append(f"{_agora()}  ── {etapa} ──")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "PYTHONIOENCODING": "utf-8", **ambiente},
    )

    filtrar = _FiltroLog()

    async def _transcrever():
        async for bruto in proc.stdout:
            linha = filtrar(bruto.decode("utf-8", errors="replace").rstrip())
            if linha:
                lote["log"].append(f"{_agora()}  {linha}")
                if len(lote["log"]) > MAX_LINHAS_LOG:
                    del lote["log"][:-MAX_LINHAS_LOG]
        return await proc.wait()

    try:
        return await asyncio.wait_for(_transcrever(), timeout=TIMEOUT_AGENTE_S)
    except asyncio.TimeoutError:
        minutos = TIMEOUT_AGENTE_S // 60
        lote["log"].append(
            f"{_agora()}  TEMPO ESGOTADO: a etapa passou de {minutos} min e foi interrompida"
        )
        log.error(f"Lote {lote['id']}: '{etapa}' estourou {TIMEOUT_AGENTE_S}s — matando o processo")
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            f"A etapa '{etapa}' passou de {minutos} minutos e foi interrompida. "
            "Reenvie o lote com menos processos, ou reduza OCR_MAX_LOTE/OCR_DPI."
        )

async def _processar_lote(lote: dict):
    lote_id = lote["id"]
    d = _dir_lote(lote_id)
    entrada, saida_json, saida_result = d / "entrada", d / "json", d / "resultados"
    for p in (saida_json, saida_result):
        p.mkdir(parents=True, exist_ok=True)

    lote["status"]      = "processando"
    lote["iniciado_em"] = _agora()
    _salvar_registro()

    try:
        # ── Agente 1 — pastas isoladas deste lote ──
        rc = await _executar(
            [sys.executable, "agente1.py"],
            {
                "PASTA_ENTRADA"   : str(entrada),
                "PASTA_JSON"      : str(saida_json),
                "PASTA_RESULTADOS": str(saida_result),
                # [Fase 3] etapas pedidas para este lote ("" = todas). O merge
                # (Fase 4) combina extrações parciais com o estado compartilhado.
                "CAMPOS"          : lote.get("campos") or "",
            },
            lote,
            "Agente 1 — extração e OCR",
        )
        if rc != 0:
            raise RuntimeError(f"Agente 1 encerrou com código {rc}")

        traspasse = saida_json / "saida_agente1_V8.json"
        if not traspasse.exists():
            raise RuntimeError("O Agente 1 não gerou o JSON de traspasse")
        # ── [V7.2] Auditoria — anexa as classificações do lote ao histórico compartilhado ──
        # O Agente 1 grava um JSONL na sua pasta ISOLADA (saida_json); aqui anexamos
        # ao arquivo COMPARTILHADO (PASTA_JSON), que acumula TODAS as decisões —
        # inclusive NÃO APTO — entre lotes. Append-only. Falha aqui não derruba o lote.
        try:
            audit_lote = saida_json / "historico_extracoes.jsonl"
            if audit_lote.exists():
                PASTA_JSON.mkdir(parents=True, exist_ok=True)
                destino = PASTA_JSON / "historico_extracoes.jsonl"
                with open(audit_lote, encoding="utf-8") as _src, \
                     open(destino, "a", encoding="utf-8") as _dst:
                    _dst.write(_src.read())
                lote["log"].append(
                    f"{_agora()}  Auditoria: classificações anexadas ao histórico compartilhado"
                )
            else:
                lote["log"].append(
                    f"{_agora()}  AVISO: Agente 1 não gerou historico_extracoes.jsonl"
                )
        except Exception as e:
            lote["log"].append(f"{_agora()}  ERRO ao anexar auditoria: {e}")
            log.exception(f"Falha ao anexar auditoria do lote {lote_id}")
            
        # ── Agente 2 — nome de arquivo por lote, pastas COMPARTILHADAS ──
        # O nome derivado do lote evita que um lote sobrescreva o resultado do
        # outro; as pastas compartilhadas mantêm o histórico do procurador
        # acumulando entre lotes, que é o desenho original do Agente 2.
        entrada_a2 = PASTA_JSON / f"lote_{lote_id}_agente2.json"
        shutil.copy2(traspasse, entrada_a2)

        rc = await _executar(
            [sys.executable, "agente2.py", "--arquivo", str(entrada_a2)],
            {"PASTA_JSON": str(PASTA_JSON), "PASTA_RESULTADOS": str(PASTA_RESULT)},
            lote,
            "Agente 2 — priorização jurídico-fiscal",
        )
        if rc != 0:
            raise RuntimeError(f"Agente 2 encerrou com código {rc}")

        # ── Coleta do resultado ──
        resultado = PASTA_JSON / f"lote_{lote_id}_agente2_resultado.json"
        if resultado.exists():
            with open(resultado, encoding="utf-8") as f:
                payload = json.load(f)
            lote["analises"] = payload.get("analises", [])
            lote["totais"]   = payload.get("metadata", {}).get("prioridades")
        else:
            lote["analises"] = []
            lote["totais"]   = None

        planilha = next(iter(sorted(saida_result.glob("*.xlsx"))), None)
        lote["planilha"] = planilha.name if planilha else None

        lote["status"] = "concluido"
        lote["etapa"]  = "concluído"
        lote["log"].append(f"{_agora()}  ── Lote concluído ──")

    except Exception as e:
        lote["status"] = "erro"
        lote["etapa"]  = "erro"
        lote["erro"]   = str(e)
        lote["log"].append(f"{_agora()}  ERRO: {e}")
        log.exception(f"Falha no lote {lote_id}")

        # O que o Agente 1 já produziu continua valendo. Sem registrar a
        # planilha aqui, uma falha do Agente 2 tornava inalcançável um Excel
        # que está em disco e é útil — o procurador perdia a extração inteira
        # por causa da etapa seguinte.
        try:
            resgatada = next(iter(sorted(saida_result.glob("*.xlsx"))), None)
            if resgatada:
                lote["planilha"] = resgatada.name
                lote["log"].append(
                    f"{_agora()}  A planilha do Agente 1 ficou pronta antes da falha "
                    "e continua disponível para download"
                )
        except Exception:
            log.exception(f"Lote {lote_id}: falha ao resgatar a planilha do Agente 1")

    finally:
        resumo, avisos = _diagnosticar(lote.get("log", []))
        lote["resumo"] = resumo
        lote["avisos"] = avisos
        lote["concluido_em"] = _agora()
        await _salvar_registro_async()


async def _worker():
    """Consome a fila em série — o OCR já satura a CPU, paralelizar só piora."""
    log.info("Worker de lotes iniciado")
    while True:
        lote_id = await _fila.get()
        try:
            lote = _lotes.get(lote_id)
            if lote:
                await _processar_lote(lote)
        except Exception:
            log.exception(f"Worker falhou no lote {lote_id}")
        finally:
            _fila.task_done()
            # Depois de cada lote, e não num timer: é aqui que o disco acabou
            # de crescer, e é o único momento em que a fila está livre.
            try:
                await asyncio.to_thread(_limpar_antigos)
            except Exception:
                log.exception("Falha na limpeza de lotes antigos")


# ════════════════════════════════════════════════════════════════
# CONTRATO DA API — modelos de resposta
# ════════════════════════════════════════════════════════════════
#
# Existem pelo Swagger: sem eles o /api/docs mostra "Successful Response" sem
# corpo nenhum e quem for integrar precisa adivinhar os campos por tentativa.
#
# extra="allow" nos modelos que vêm do Agente 2 é deliberado: o response_model
# DESCARTA campo não declarado. Quando o Agente 2 ganhar a análise por Gemini,
# os campos novos passariam a sumir da resposta em silêncio — o modelo
# documenta o mínimo garantido, não um teto.

class StatusLote(str, Enum):
    na_fila     = "na_fila"
    processando = "processando"
    concluido   = "concluido"
    erro        = "erro"


class TipoArquivo(str, Enum):
    """O que cada agente deixa para trás — ver DESCRICAO_ARQUIVO."""
    agente1_planilha = "agente1_planilha"
    agente1_json     = "agente1_json"
    agente2_json     = "agente2_json"
    agente2_planilha = "agente2_planilha"


class ArquivoLote(BaseModel):
    tipo      : TipoArquivo
    descricao : str
    formato   : str = Field(description="xlsx ou json")
    tamanho_kb: float
    url       : str = Field(description="Rota de download, com o mesmo token Bearer")


class ListaArquivos(BaseModel):
    arquivos: list[ArquivoLote]


class Erro(BaseModel):
    detail: str = Field(description="O que impediu a chamada, em texto legível")


class Totais(BaseModel):
    """Quantos processos caíram em cada prioridade."""
    ALTA : int
    MEDIA: int
    BAIXA: int


class Analise(BaseModel):
    model_config = ConfigDict(extra="allow")

    prioridade: str | None = Field(
        None,
        description="Ordena o trabalho do procurador; NÃO é critério de ajuizamento. "
                    "ALTA: dívida ≥ R$ 5.000, OU risco de prescrição (art. 174 CTN), "
                    "OU 5 anos sem movimentação. MEDIA: R$ 1.000 a R$ 5.000. "
                    "BAIXA: abaixo de R$ 1.000. Os limiares são provisórios e "
                    "ajustáveis por LIMIAR_PRIORIDADE_ALTA / LIMIAR_PRIORIDADE_MEDIA.",
        examples=["ALTA"],
    )
    acao_recomendada : str | None = None
    justificativa    : str | None = None
    alerta_prescricao: bool | None = Field(
        None, description="Risco de prescrição quinquenal — CTN art. 174"
    )
    observacoes: list[str] = Field(
        default_factory=list,
        description="Pontos de atenção para o procurador: dado faltante, OCR ruim, prescrição",
    )


class ProcessoAnalisado(BaseModel):
    model_config = ConfigDict(extra="allow")

    id_lote        : str | None = None
    numero_processo: str | None = None
    nome_executado : str | None = None
    analise        : Analise | None = None
    erro           : str | None = Field(
        None,
        description="Preenchido quando ESTE processo falhou na análise. "
                    "Os demais do lote seguem normalmente.",
    )
    processado_em: str | None = None


class EntidadesProcesso(BaseModel):
    """Dados que o Agente 1 extraiu do PDF — todos como texto, como saíram."""
    model_config = ConfigDict(extra="allow")

    numero_processo : str | None = None
    cpf_cnpj        : str | None = None
    nome_executado  : str | None = None
    nome_exequente  : str | None = None
    tipo_tributo    : str | None = Field(None, examples=["IPTU"])
    exercicio       : str | None = None
    numero_cda      : str | None = None
    data_inscricao  : str | None = None
    valor_original  : str | None = Field(None, examples=["R$ 12.480,55"])
    valor_atualizado: str | None = None
    vara            : str | None = None


class TriagemAgente1(BaseModel):
    """Extração e classificação do Agente 1 para um processo."""
    model_config = ConfigDict(extra="allow")

    lote_id  : str | None = Field(None, description="Lote em que este processo foi triado")
    id_lote  : str | None = Field(
        None,
        description="Nome do PDF de origem — o Agente 1 chama o arquivo assim. "
                    "Não confundir com `lote_id`.",
    )
    entidades      : EntidadesProcesso | None = None
    decisao_agente1: str | None = Field(
        None,
        description="APTO ou NÃO APTO. Na prática vem sempre APTO: o processo "
                    "reprovado na triagem não entra no resultado.",
        examples=["APTO"],
    )
    motivo_agente1     : str | None = None
    status_citacao     : str | None = None
    resultado_penhora  : str | None = None
    ultima_movimentacao: str | None = Field(None, examples=["2019-03-14"])
    confianca_ocr_media: float | None = Field(
        None,
        description="Média da confiança do OCR nas páginas digitalizadas, de 0 a 100. "
                    "Valor baixo pede conferência no PDF.",
        examples=[92.4],
    )


class ClassificacaoAuditoria(BaseModel):
    """
    Uma classificação registrada na auditoria (historico_extracoes.jsonl).
    Diferente de agente1/agente2, cobre TODAS as decisões — inclusive NÃO APTO.
    Registro plano: sem 'entidades' aninhadas.
    """
    model_config = ConfigDict(extra="allow")

    classificado_em    : str | None = Field(None, examples=["2026-08-05T09:30:00"])
    numero_processo    : str | None = None
    decisao            : str | None = Field(None, examples=["NÃO APTO"])
    motivo             : str | None = Field(None, examples=["Penhora de imóvel efetivada"])
    fonte_decisao      : str | None = None
    ultima_movimentacao: str | None = None
    status_citacao     : str | None = None
    resultado_penhora  : str | None = None
    id_lote            : str | None = Field(None, description="Nome do PDF de origem")
    nome_executado     : str | None = None
    cpf_cnpj           : str | None = None


class AuditoriaProcesso(BaseModel):
    """
    Bloco de auditoria de um processo: a classificação vigente (a mais recente)
    mais o histórico completo. O histórico é APPEND-ONLY, então um processo pode
    ter várias linhas (foi reclassificado entre lotes) — daí `total`.
    """
    atual    : ClassificacaoAuditoria | None = None
    historico: list[ClassificacaoAuditoria] = Field(default_factory=list)
    total    : int = 0


class ProcessoConsultado(BaseModel):
    numero_processo: str = Field(description="Número como está gravado, com a pontuação original")
    encontrado_em: list[str] = Field(
        description="Quais fontes têm o processo: 'agente1', 'agente2' e/ou 'auditoria'"
    )
    agente1: TriagemAgente1 | None = Field(
        None, description="Nulo quando só o Agente 2 tem o processo"
    )
    agente2: ProcessoAnalisado | None = Field(
        None, description="Nulo enquanto o Agente 2 não analisou o processo"
    )
    auditoria: AuditoriaProcesso | None = Field(
        None,
        description="Classificação de triagem — TODAS as decisões, inclusive NÃO APTO. "
                    "É aqui que aparece um processo reprovado na triagem, que não entra "
                    "em `agente1`/`agente2`. Nulo se o lote foi processado antes da auditoria.",
    )


class Lote(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {
        "lote_id"     : "20260729-143000-a1b2c3",
        "status"      : "concluido",
        "origem"      : "siap",
        "criado_em"   : "2026-07-29 14:30:00",
        "iniciado_em" : "2026-07-29 14:30:01",
        "concluido_em": "2026-07-29 14:38:12",
        "arquivos"    : ["processo1.pdf", "processo2.pdf"],
        "etapa"       : "concluído",
        "decorrido_s"    : 491,
        "lotes_na_frente": None,
        "resumo"      : "9 de 12 processo(s) classificados como APTO",
        "avisos"      : [],
        "erro"        : None,
        "totais"      : {"ALTA": 4, "MEDIA": 3, "BAIXA": 2},
    }})

    lote_id: str = Field(description="Identificador do lote — use nas demais rotas")
    status : StatusLote
    origem : str | None = Field(
        None, description="Rótulo do consumidor que enviou o lote, ou 'painel'"
    )
    criado_em   : str | None = None
    iniciado_em : str | None = Field(None, description="Nulo enquanto o lote está na fila")
    concluido_em: str | None = None
    arquivos    : list[str] = Field(default_factory=list, description="PDFs recebidos neste lote")
    campos      : str | None = Field(
        "todas",
        description="Etapas extraídas neste lote (Fase 3): citacao, penhora, "
                    "movimentacao, sinais, entidades, tipo — ou 'todas'. "
                    "Etapas não pedidas são reaproveitadas do estado anterior (merge).",
        examples=["todas"],
    )
    etapa       : str | None = Field(None, description="Passo corrente, para exibir a quem espera")
    decorrido_s : int | None = Field(
        None,
        description="Segundos de espera na fila, de processamento em curso, ou o "
                    "total gasto depois de concluído — conforme o status. Vem do "
                    "relógio do serviço, não depende do fuso de quem consulta.",
        examples=[491],
    )
    lotes_na_frente: int | None = Field(
        None,
        description="Quantos lotes precisam terminar antes deste começar. "
                    "Só vem preenchido com status 'na_fila'.",
    )
    downloads: list[TipoArquivo] = Field(
        default_factory=list,
        description="Artefatos disponíveis para este lote. Baixe em "
                    "`GET /api/v1/lotes/{lote_id}/arquivos/{tipo}`.",
    )
    resumo      : str | None = Field(
        None, examples=["9 de 12 processo(s) classificados como APTO"]
    )
    avisos: list[str] = Field(
        default_factory=list,
        description="NÃO NULO com status 'concluido' significa que o lote rodou até o "
                    "fim mas algo deu errado no caminho. Trate como falha.",
    )
    erro  : str | None = Field(None, description="Preenchido quando status é 'erro'")
    totais: Totais | None = Field(None, description="Só depois de concluído")


class LoteComLog(Lote):
    log: list[str] = Field(
        default_factory=list,
        description=f"Saída dos agentes, últimas {MAX_LINHAS_LOG} linhas",
    )


class LoteComAnalises(Lote):
    analises: list[ProcessoAnalisado] = Field(
        default_factory=list, description="Um item por processo APTO"
    )


class ListaLotes(BaseModel):
    lotes: list[Lote]


class ListaLotesComLog(BaseModel):
    lotes: list[LoteComLog]


class Saude(BaseModel):
    status     : str = Field(examples=["ok"])
    api_ativa  : bool = Field(description="False quando falta API_TOKENS — a API recusa tudo com 503")
    na_fila    : int
    processando: int


class Relatorio(BaseModel):
    nome         : str
    tamanho_kb   : float
    modificado_em: str


class ListaRelatorios(BaseModel):
    arquivos: list[Relatorio]


# Respostas de erro comuns a toda a API v1 — repetidas em cada rota só para o
# Swagger mostrar o corpo que o consumidor vai receber.
_ERROS_AUTH = {
    401: {"model": Erro, "description": "Token ausente ou inválido"},
    503: {"model": Erro, "description": "Serviço sem API_TOKENS configurado — falha fechada"},
}
_ERRO_LOTE = {
    404: {"model": Erro, "description": "Lote inexistente ou de outro consumidor"},
}


# ════════════════════════════════════════════════════════════════
# APLICAÇÃO
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _fila
    _garantir_pastas()
    _carregar_registro()
    try:
        _limpar_antigos()
    except Exception:
        log.exception("Falha na limpeza de lotes antigos — seguindo mesmo assim")
    _fila = asyncio.Queue()
    tarefa = asyncio.create_task(_worker())

    # Credencial malformada nunca autentica ninguém, mas falha como se o token
    # estivesse errado. Gritar aqui evita horas de caça a um 401 sem causa.
    for problema in _erros_config:
        log.error("=" * 70)
        log.error(f"CREDENCIAL INVÁLIDA — {problema}")
        log.error("=" * 70)

    if not TOKENS:
        log.warning("API_TOKENS não configurado — a API responderá 503 a tudo")
    else:
        log.info(f"API habilitada para: {', '.join(sorted(set(TOKENS.values())))}")
    if _tokens_legado:
        log.warning(
            f"Token em TEXTO PURO no ambiente para: {', '.join(sorted(set(_tokens_legado)))}. "
            "Troque pelo hash — 'python gerar_credencial.py api <rotulo>'."
        )
    if not PAINEL_ATIVO:
        log.warning("Senha do painel não configurada — o painel não abrirá")
    elif not SENHA_PAINEL_HASH:
        log.warning(
            "SENHA_PAINEL está em TEXTO PURO no ambiente. Gere o hash com "
            "'python gerar_credencial.py painel' e use SENHA_PAINEL_HASH."
        )

    yield
    tarefa.cancel()


_DESCRICAO = """
Triagem automatizada de execuções fiscais — **HERA Tecnologia / PGMS**, contrato nº 01/2026.

Envie os PDFs dos processos; o serviço extrai os dados (com OCR quando a página é
digitalizada), classifica cada processo em APTO / NÃO APTO e devolve a priorização
jurídico-fiscal: prioridade, ação recomendada e alerta de prescrição.

## Autenticação

Todas as rotas `/api/v1/*` exigem o token do consumidor:

```http
Authorization: Bearer <token>
```

Clique em **Authorize**, no alto da página, para testar por aqui — o token passa a valer
para todas as rotas. Ele é emitido por `python gerar_credencial.py api <rotulo>` e
aparece uma única vez; perdido, emite-se outro.

Cada consumidor só enxerga os próprios lotes. O lote de outro token responde `404`, nunca
`403` — a existência do lote alheio também não vaza.

Se **tudo** responder `503`, o serviço está sem `API_TOKENS`. É deliberado: configuração
incompleta deixa o serviço fechado, nunca aberto.

## O processamento é assíncrono

OCR e GPT levam minutos por lote e nenhum proxy segura a conexão tanto tempo. O envio
responde `202` na hora com um `lote_id`; acompanhe por polling. Os lotes rodam em fila
serial — um por vez, porque o OCR já satura a CPU.

1. `POST /api/v1/lotes` → `202` com o `lote_id`
2. `GET /api/v1/lotes/{lote_id}` a cada ~30 s, até o status sair de `na_fila`/`processando`
3. `GET /api/v1/lotes/{lote_id}/resultado` quando o status for `concluido`

Ciclo de vida: `na_fila` → `processando` → `concluido` | `erro`

## Consulta por número de processo

Quando a pergunta parte do processo e não do lote:

```http
GET /api/v1/processos?numero=0752821-68.2013.8.05.0001
```

Devolve a triagem do Agente 1 e a priorização do Agente 2 do processo, procurando em
todos os lotes já enviados por este consumidor. A pontuação do número é indiferente.

## Sempre confira `avisos`

A extração trata OCR quebrado, erro de API e PDF ilegível internamente e **encerra com
sucesso**. Um lote pode chegar a `concluido` tendo classificado zero processos. Por isso
toda resposta traz `resumo` e `avisos`:

```json
{
  "status": "concluido",
  "resumo": "0 de 12 processo(s) classificados como APTO",
  "avisos": ["Nenhum processo foi classificado como APTO — o resultado sairá vazio"]
}
```

`status: "concluido"` com `avisos` não vazio significa que o lote rodou até o fim mas algo
deu errado no caminho. **Trate como falha.**
"""

URL_OPENAPI = "/api/openapi.json"

app = FastAPI(
    title="Triagem de Execuções Fiscais — PGMS",
    version="1.0",
    description=_DESCRICAO,
    docs_url=None,          # servido à mão logo abaixo, com o enviador de lotes
    redoc_url="/api/redoc",
    openapi_url=URL_OPENAPI,
    openapi_tags=[
        {
            "name": "Lotes",
            "description": "Envio e acompanhamento dos lotes. É o contrato da integração.",
        },
        {
            "name": "Processos",
            "description": "Consulta de um processo pelo número CNJ, atravessando os lotes já enviados.",
        },
        {
            "name": "Serviço",
            "description": "Estado do serviço. Sem autenticação, sem dado sensível.",
        },
    ],
    lifespan=lifespan,
)


def _nome_pdf_seguro(nome: str) -> str:
    limpo = Path(nome or "").name
    if not limpo.lower().endswith(".pdf"):
        raise HTTPException(400, f"Somente arquivos .pdf são aceitos: {limpo!r}")
    if limpo in (".", "..") or not limpo.strip():
        raise HTTPException(400, "Nome de arquivo inválido")
    return limpo


_ETAPAS_VALIDAS = {"citacao", "penhora", "movimentacao", "sinais", "entidades", "tipo"}


def _normalizar_campos(campos: str) -> str:
    """
    Valida/normaliza a seleção de etapas (Fase 3 do Agente 1). Devolve string
    separada por vírgula com as etapas válidas, ou "" quando é 'todas' (ou vazio),
    que é como o Agente 1 entende "processar tudo". Etapas inválidas são descartadas.
    """
    if not campos:
        return ""
    pedidos = [c.strip().lower() for c in campos.split(",") if c.strip()]
    seen, out = set(), []
    for c in pedidos:
        if c in _ETAPAS_VALIDAS and c not in seen:
            seen.add(c)
            out.append(c)
    if not out or set(out) == _ETAPAS_VALIDAS:
        return ""          # tudo -> Agente 1 roda todas as etapas (default)
    return ",".join(out)


async def _gravar_lote(arquivos: list, origem: str, campos: str = "") -> dict:
    """Cria o lote em disco e o enfileira."""
    lote_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    entrada = _dir_lote(lote_id) / "entrada"
    entrada.mkdir(parents=True, exist_ok=True)

    nomes, total_bytes = [], 0
    for arq in arquivos:
        nome = _nome_pdf_seguro(arq.filename)

        # Dois PDFs de pastas diferentes podem chegar com o mesmo nome — o
        # painel deixa juntar arquivos de várias pastas num lote só. Sem
        # desambiguar, o segundo sobrescreveria o primeiro e o lote
        # processaria um processo a menos sem avisar ninguém.
        if nome in nomes:
            base, seq = nome[:-4], 2
            while f"{base}-{seq}.pdf" in nomes:
                seq += 1
            nome = f"{base}-{seq}.pdf"

        destino = entrada / nome
        with open(destino, "wb") as f:
            while chunk := await arq.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_MB_LOTE * 1024 * 1024:
                    f.close()
                    shutil.rmtree(_dir_lote(lote_id), ignore_errors=True)
                    raise HTTPException(413, f"Lote excede o limite de {MAX_MB_LOTE} MB")
                f.write(chunk)
        nomes.append(nome)

    if not nomes:
        shutil.rmtree(_dir_lote(lote_id), ignore_errors=True)
        raise HTTPException(400, "Nenhum PDF enviado")

    lote = {
        "id"          : lote_id,
        "status"      : "na_fila",
        "origem"      : origem,
        "criado_em"   : _agora(),
        "iniciado_em" : None,
        "concluido_em": None,
        "arquivos"    : nomes,
        "campos"      : _normalizar_campos(campos),   # [Fase 3] etapas pedidas ("" = todas)
        "etapa"       : "na fila",
        "log"         : [],
        "analises"    : [],
        "avisos"      : [],
        "resumo"      : None,
        "erro"        : None,
        "totais"      : None,
        "planilha"    : None,
    }

    async with _lock:
        _lotes[lote_id] = lote
        _salvar_registro()
    await _fila.put(lote_id)

    log.info(f"Lote {lote_id} recebido de '{origem}' — {len(nomes)} PDF(s), "
             f"{round(total_bytes/1024/1024, 1)} MB")
    return lote


# ════════════════════════════════════════════════════════════════
# API v1 — consumo externo (SIAP)
# ════════════════════════════════════════════════════════════════

@app.get("/api/docs", include_in_schema=False)
async def documentacao():
    """
    Swagger com um enviador de lotes por cima.

    O formulário que o Swagger UI gera para um campo do tipo lista aceita um
    arquivo por linha e não abre seleção múltipla — para mandar uma pasta de
    processos é inviável. Como a página é servida por nós, um enviador próprio
    entra antes dela: mesma rota, mesmo token, mas com arrastar-e-soltar,
    escolha de pasta inteira e acompanhamento do lote até o resultado.
    """
    pagina = bytes(get_swagger_ui_html(
        openapi_url=URL_OPENAPI,
        title=f"{app.title} — API",
    ).body).decode()
    return HTMLResponse(pagina.replace('<div id="swagger-ui">',
                                       _ENVIO_DOCS + '<div id="swagger-ui">'))


@app.get(
    "/health",
    tags=["Serviço"],
    summary="Healthcheck",
    response_model=Saude,
)
async def health():
    """Healthcheck do Easypanel — sem autenticação, sem dado sensível."""
    return {
        "status"     : "ok",
        "api_ativa"  : bool(TOKENS),
        "na_fila"    : sum(1 for l in _lotes.values() if l["status"] == "na_fila"),
        "processando": sum(1 for l in _lotes.values() if l["status"] == "processando"),
    }


@app.post(
    "/api/v1/lotes",
    tags=["Lotes"],
    summary="Enviar processos para triagem",
    status_code=202,
    response_model=Lote,
    responses={
        202: {"model": Lote, "description": "Lote aceito e enfileirado"},
        400: {"model": Erro, "description": "Nenhum PDF enviado, ou arquivo que não é .pdf"},
        413: {"model": Erro, "description": f"Lote acima de {MAX_MB_LOTE} MB"},
        **_ERROS_AUTH,
    },
)
async def criar_lote(
    arquivos: list[UploadFile] = File(..., description="Um ou mais PDFs de processos"),
    campos: str = Form("", description="Etapas a extrair, separadas por vírgula "
                       "(citacao,penhora,movimentacao,sinais,entidades,tipo). "
                       "Vazio = todas. Etapas não pedidas são reaproveitadas do estado anterior."),
    consumidor: str = Depends(autenticar_api),
):
    """
    Envio `multipart/form-data`, campo `arquivos` repetido — um por PDF. **Todos os
    PDFs de uma requisição formam UM lote**, processado de uma vez.

    Responde **202 na hora**: o processamento é assíncrono e leva minutos. Guarde o
    `lote_id` e acompanhe em `GET /api/v1/lotes/{lote_id}` até o status virar
    `concluido` ou `erro`.

    ### Mandando muitos PDFs de uma vez

    O formulário aqui embaixo é do Swagger UI, que desenha **uma linha por arquivo** e
    não abre seleção múltipla — é limitação da página, não da API. Com poucos arquivos,
    clique em *Add string item* e escolha um por linha; todos entram no mesmo lote.

    Para uma pasta inteira, use uma destas:

    ```bash
    # Linux/Mac — a pasta toda numa requisição
    curl -X POST https://SEU-DOMINIO/api/v1/lotes \\
         -H "Authorization: Bearer $TOKEN" \\
         $(printf -- '-F arquivos=@%s ' *.pdf)
    ```

    ```powershell
    # Windows PowerShell — $campos, não $args: $args é variável reservada
    $campos = Get-ChildItem *.pdf | ForEach-Object { '-F'; "arquivos=@$($_.Name)" }
    curl.exe -X POST https://SEU-DOMINIO/api/v1/lotes `
             -H "Authorization: Bearer $TOKEN" @campos
    ```

    Ou abra o **painel** em `/` e arraste os arquivos — mesma fila, mesmo pipeline.
    """
    lote = await _gravar_lote(arquivos, origem=consumidor, campos=campos)
    return _publico(lote)


@app.get(
    "/api/v1/lotes",
    tags=["Lotes"],
    summary="Listar meus lotes",
    response_model=ListaLotes,
    responses=_ERROS_AUTH,
)
async def listar_lotes(
    consumidor: str = Depends(autenticar_api),
    limite: int = Query(50, ge=1, le=500, description="Quantos lotes trazer"),
):
    """Lotes enviados por este consumidor, mais recentes primeiro. Sem log nem análises."""
    meus = [l for l in _lotes.values() if l.get("origem") == consumidor]
    meus.sort(key=lambda l: l.get("criado_em") or "", reverse=True)
    return {"lotes": [_publico(l) for l in meus[:limite]]}


@app.get(
    "/api/v1/lotes/{lote_id}",
    tags=["Lotes"],
    summary="Consultar o estado de um lote",
    response_model=LoteComLog,
    responses={**_ERROS_AUTH, **_ERRO_LOTE},
)
async def consultar_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """
    Rota do polling. Enquanto o status for `na_fila` ou `processando`, repita —
    a cada ~30 s basta. Traz o log dos agentes, útil para diagnosticar um lote travado.
    """
    return _publico(_lote_do_consumidor(lote_id, consumidor), incluir_log=True)


@app.get(
    "/api/v1/lotes/{lote_id}/resultado",
    tags=["Lotes"],
    summary="Baixar a priorização do lote",
    response_model=LoteComAnalises,
    responses={
        409: {"model": Erro, "description": "Lote ainda não concluído — continue o polling"},
        **_ERROS_AUTH,
        **_ERRO_LOTE,
    },
)
async def resultado_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """
    Priorização completa: um item em `analises` por processo APTO, com prioridade,
    ação recomendada e alerta de prescrição.

    Só responde com status `concluido`; antes disso devolve `409`. Confira `avisos`
    antes de consumir — lote concluído com avisos rodou até o fim mas deu errado.
    """
    lote = _lote_do_consumidor(lote_id, consumidor)
    if lote["status"] != "concluido":
        raise HTTPException(409, f"Lote ainda em '{lote['status']}' — aguarde 'concluido'")
    return _publico(lote, incluir_analises=True)


def _lote_do_consumidor(lote_id: str, consumidor: str) -> dict:
    lote = _lotes.get(lote_id)
    if not lote or lote.get("origem") != consumidor:
        raise HTTPException(404, "Lote não encontrado")
    return lote


def _resposta_arquivo(lote: dict, tipo: str, consumidor: str | None = None) -> FileResponse:
    """
    consumidor=None significa painel: o procurador enxerga o acervo inteiro por
    desenho. Com um consumidor, a planilha acumulada do Agente 2 é recortada aos
    lotes dele — o arquivo em resultados/ é global e entregá-lo cru vazaria os
    processos dos demais consumidores.
    """
    if tipo == "agente2_planilha" and consumidor is not None:
        recorte = _planilha_a2_recortada(consumidor)
        if not recorte:
            raise HTTPException(
                404,
                "Ainda não há processos priorizados nos seus lotes. "
                "Aguarde o lote concluir e tente de novo.",
            )
        return FileResponse(recorte, filename="priorizacao.xlsx", media_type=_XLSX)

    achado = _arquivos_do_lote(lote).get(tipo)
    if not achado:
        raise HTTPException(
            404,
            f"Este lote não tem '{tipo}'. Veja em 'downloads' o que existe — "
            "um lote que terminou em erro pode não ter gerado nada.",
        )
    caminho, nome, media = achado
    return FileResponse(caminho, filename=nome, media_type=media)


@app.get(
    "/api/v1/lotes/{lote_id}/arquivos",
    tags=["Lotes"],
    summary="Listar o que dá para baixar deste lote",
    response_model=ListaArquivos,
    responses={**_ERROS_AUTH, **_ERRO_LOTE},
)
async def arquivos_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """
    Os artefatos dos dois agentes, com tamanho e rota de download.

    Atenção ao `agente2_planilha`: o Excel de priorização é **acumulativo** — o
    Agente 2 o regera do histórico inteiro a cada lote, então ele traz também os
    processos dos lotes anteriores. Para o recorte deste lote use `agente2_json`.
    """
    lote = _lote_do_consumidor(lote_id, consumidor)
    return {
        "arquivos": [
            {
                "tipo"      : tipo,
                "descricao" : DESCRICAO_ARQUIVO[tipo],
                "formato"   : caminho.suffix.lstrip("."),
                "tamanho_kb": round(caminho.stat().st_size / 1024, 1),
                "url"       : f"/api/v1/lotes/{lote_id}/arquivos/{tipo}",
            }
            for tipo, (caminho, _, _) in sorted(_arquivos_do_lote(lote).items())
        ]
    }


@app.get(
    "/api/v1/lotes/{lote_id}/arquivos/{tipo}",
    tags=["Lotes"],
    summary="Baixar um artefato do lote",
    response_class=FileResponse,
    responses={
        200: {"content": {_XLSX: {}, "application/json": {}}, "description": "O arquivo"},
        404: {"model": Erro, "description": "Lote ou artefato inexistente"},
        **_ERROS_AUTH,
    },
)
async def baixar_arquivo_lote(
    lote_id: str,
    tipo: TipoArquivo,
    consumidor: str = Depends(autenticar_api),
):
    """Download direto. O mesmo token Bearer das demais rotas."""
    return _resposta_arquivo(_lote_do_consumidor(lote_id, consumidor), tipo.value, consumidor)


_RESPOSTAS_XLSX: dict[int | str, dict[str, Any]] = {
    200: {"content": {_XLSX: {}}, "description": "Arquivo .xlsx"},
    404: {"model": Erro, "description": "Lote inexistente, de outro consumidor, ou sem a planilha"},
    **_ERROS_AUTH,
}


@app.get(
    "/api/v1/lotes/{lote_id}/planilha/agente1",
    tags=["Lotes"],
    summary="Baixar a planilha do Agente 1",
    response_class=FileResponse,
    responses=_RESPOSTAS_XLSX,
)
async def planilha_agente1(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """
    Excel de revisão da extração: um processo por linha, com a classificação
    APTO / NÃO APTO e o motivo.

    **Só deste lote.** Sai da pasta isolada do lote.
    """
    return _resposta_arquivo(_lote_do_consumidor(lote_id, consumidor), "agente1_planilha", consumidor)


@app.get(
    "/api/v1/lotes/{lote_id}/planilha/agente2",
    tags=["Lotes"],
    summary="Baixar a planilha do Agente 2",
    response_class=FileResponse,
    responses=_RESPOSTAS_XLSX,
)
async def planilha_agente2(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """
    Excel de priorização jurídico-fiscal: prioridade, ação recomendada e alerta
    de prescrição.

    **Atenção — este arquivo é ACUMULADO.** O Agente 2 o regera a partir do
    histórico inteiro a cada lote, então ele traz também os processos dos lotes
    anteriores, não só os deste. É o desenho do relatório do procurador, que
    existe para dar a visão do acervo.

    Para o recorte exato deste lote, use `GET /api/v1/lotes/{lote_id}/resultado`
    (mesma priorização, em JSON) ou `/arquivos/agente2_json`.
    """
    return _resposta_arquivo(_lote_do_consumidor(lote_id, consumidor), "agente2_planilha", consumidor)


# ════════════════════════════════════════════════════════════════
# CONSULTA POR NÚMERO DE PROCESSO
# ════════════════════════════════════════════════════════════════
#
# A pasta JSON/ é compartilhada: mistura os lotes de todos os consumidores e os
# que o procurador subiu pelo painel. A consulta por número roda sobre uma cópia
# recortada aos lotes de quem chamou, para ficar com o MESMO isolamento das
# rotas por lote_id — o número CNJ é público e, sem o recorte, bastava informá-lo
# para ler nome, CPF/CNPJ e valor da dívida de processo alheio.

_RE_ARQUIVO_LOTE = re.compile(r"^lote_(.+)_agente2\.json$")

# Número CNJ, com ou sem a pontuação. Serve de guarda contra o valor
# começando com '-', que o argparse do buscar_processo.py leria como flag.
_RE_NUMERO_CNJ = re.compile(r"[0-9][0-9.\-/]{0,40}")


def _lote_da_origem(nome_arquivo: str) -> dict | None:
    """
    Lote que originou um arquivo da pasta JSON/ — chamam-se 'lote_<id>_agente2.json'.

    Nulo para nome fora desse padrão: são as rodadas manuais por linha de
    comando, que não pertencem a consumidor nenhum.
    """
    achado = _RE_ARQUIVO_LOTE.match(Path(nome_arquivo or "").name)
    return _lotes.get(achado.group(1)) if achado else None


def _filtro_do_consumidor(consumidor: str):
    """Predicado que a busca aplica a cada arquivo: só os lotes deste consumidor."""
    def do_consumidor(nome_arquivo: str) -> bool:
        lote = _lote_da_origem(nome_arquivo)
        return bool(lote) and lote.get("origem") == consumidor
    return do_consumidor


def _sem_internos(rec: dict | None) -> dict | None:
    """Tira as chaves internas '_...' que a busca anexa (ex.: _origem_arquivo,
    _total_classificacoes). Usado no bloco de auditoria, que é um registro plano
    e não passa por _com_lote_id."""
    if not rec:
        return None
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def _com_lote_id(achado: dict | None) -> dict | None:
    """Troca a procedência interna pelo lote_id — o consumidor não vê nome de arquivo."""
    if not achado:
        return None
    origem = achado.get("origem_lote") or achado.get("_origem_arquivo") or ""
    lote   = _lote_da_origem(origem)
    limpo  = {
        k: v for k, v in achado.items()
        if not k.startswith("_") and k != "origem_lote"
    }
    limpo["lote_id"] = lote["id"] if lote else None
    return limpo


def _pasta_recortada(destino: Path, consumidor: str) -> None:
    """
    Preenche `destino` com os artefatos que o buscar_processo.py lê, contendo
    apenas o que pertence a este consumidor:

      • lote_<id>_agente2.json   → só os lotes com origem == consumidor
      • historial_agente2.jsonl  → só as linhas cujo 'origem_lote' é de um deles
      • historico_extracoes.jsonl→ só as linhas dos PDFs desses lotes

    A pasta JSON/ real é compartilhada entre todos os consumidores e com o
    painel. Apontar o script direto para ela entregava processo alheio a
    qualquer token válido.
    """
    origens = _origens_do_consumidor(consumidor)
    if not origens:
        return

    for nome in origens:
        fonte = PASTA_JSON / nome
        if fonte.exists():
            shutil.copy2(fonte, destino / nome)

    # Arquivos dos PDFs deste consumidor — chave do recorte da auditoria, que
    # é um registro plano, sem campo de origem.
    arquivos_meus = {
        arq
        for lote in _lotes.values()
        if lote.get("origem") == consumidor
        for arq in lote.get("arquivos", [])
    }

    for nome, chave in ((_JSONL_AGENTE2, "origem_lote"), ("historico_extracoes.jsonl", "arquivo")):
        fonte = PASTA_JSON / nome
        if not fonte.exists():
            continue
        permitido = origens if chave == "origem_lote" else arquivos_meus
        with open(fonte, encoding="utf-8") as entrada, \
             open(destino / nome, "w", encoding="utf-8") as saida:
            for linha in entrada:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    rec = json.loads(linha)
                except json.JSONDecodeError:
                    continue          # linha corrompida: não é deste nem de ninguém
                if rec.get(chave) in permitido:
                    saida.write(linha + "\n")


@app.get(
    "/api/v1/processos",
    tags=["Processos"],
    summary="Consultar um processo pelo número",
    response_class=PlainTextResponse,
    responses={
        400: {"model": Erro, "description": "Número fora do formato CNJ"},
        404: {"model": Erro, "description": "Nenhum processo com esse número nos lotes deste consumidor"},
        **_ERROS_AUTH,
    },
)
async def consultar_processo(
    numero: str = Query(
        ...,
        min_length=1,
        description="Número CNJ do processo. A pontuação é indiferente — "
                    "'0752821-68.2013.8.05.0001' e '07528216820138050001' acham o mesmo.",
        examples=["0752821-68.2013.8.05.0001"],
    ),
    consumidor: str = Depends(autenticar_api),
):
    """
    Executa o `buscar_processo.py` como script (não apenas a função `buscar_dados`)
    e devolve a saída formatada COMPLETA do `main()` em texto puro — a mesma visão
    do CLI: Agente 1 (dados extraídos), Agente 2 (priorização) e auditoria.

    A busca cobre os lotes **deste consumidor** — o mesmo isolamento das rotas
    por `lote_id`. Um processo enviado por outro consumidor responde `404`.

    Responde `404` quando o número não aparece em fonte nenhuma, e `500` se o
    próprio `buscar_processo.py` falhar ao executar.
    """
    # O número vai como argv para o subprocess. Sem esta guarda, um valor
    # começando com '-' é lido pelo argparse como FLAG, não como número:
    # '-h' devolvia o help com HTTP 200, '--pasta=/x' redirecionava a busca
    # para outra pasta do disco. Só dígitos e a pontuação do CNJ passam.
    numero = numero.strip()
    if not _RE_NUMERO_CNJ.fullmatch(numero):
        raise HTTPException(
            400,
            "Número de processo inválido. Use o número CNJ — só dígitos, "
            "pontos, hífens e barras. Ex.: 0752821-68.2013.8.05.0001",
        )

    script = BASE_DIR / "buscar_processo.py"

    # Subprocess isolado, mesmo padrão dos agentes:
    #  - caminho ABSOLUTO (BASE_DIR/…): evita o "código 2" de arquivo não achado.
    #  - stdin=DEVNULL: se cair no modo interativo (número vazio), não trava no input().
    #  - stdout piped → sys.stdout.isatty() é False lá dentro, então _cor() devolve
    #    texto SEM códigos ANSI. A saída chega limpa.
    # A pasta JSON/ é compartilhada por todos os consumidores e pelo painel.
    # O script é apontado para uma cópia recortada aos lotes de quem pediu —
    # sem isso, qualquer token lia processo alheio informando o número CNJ,
    # que é público.
    try:
        with tempfile.TemporaryDirectory(prefix="consulta-") as tmp:
            recorte = Path(tmp)
            await asyncio.to_thread(_pasta_recortada, recorte, consumidor)

            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script),
                "--pasta", str(recorte),
                "--",              # encerra as opções: o que vem depois é posicional
                numero,
                cwd=str(BASE_DIR),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # O relatório usa acento e caractere de caixa. Sem isto, uma
                # locale não-UTF-8 derruba o script no print e a consulta
                # devolve 500 tendo encontrado o processo.
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            bruto_out, bruto_err = await proc.communicate()
    except Exception as e:
        log.exception("Falha ao lançar buscar_processo.py")
        raise HTTPException(500, "Não foi possível consultar o processo agora. Tente de novo.")

    saida = bruto_out.decode("utf-8", errors="replace")
    erro  = bruto_err.decode("utf-8", errors="replace")

    # [v2] Contrato de saída do buscar_processo.py (ver EXIT_NAO_ENCONTRADO lá):
    #   0 = encontrado   3 = não encontrado   qualquer outro = falha real → 500
    #
    # ANTES a decisão vinha de farejar o stdout ('não encontrado' in saida). Isso
    # colidia com valores de dado do próprio processo — "Citação não encontrado",
    # "Penhora não encontrado" — e devolvia o relatório de SUCESSO com HTTP 404.
    # O código de saída é inequívoco e não olha o conteúdo (nem o dado sensível).
    EXIT_NAO_ENCONTRADO = 3

    if proc.returncode == EXIT_NAO_ENCONTRADO:
        log.info("Processo não encontrado — numero=%s consumidor=%s", numero, consumidor)
        raise HTTPException(404, saida.strip() or f"Nenhum processo com o número {numero}.")

    if proc.returncode != 0:
        # Ex.: 2 = Python não achou buscar_processo.py; outros = exceção no script.
        log.error(
            "buscar_processo.py saiu com código %s\nSTDERR:\n%s\nSTDOUT:\n%s",
            proc.returncode, erro, saida,
        )
        # O stderr traz caminho interno e traceback. Vai para o log do serviço,
        # que é de quem opera; ao consumidor vai só o que ele pode agir.
        raise HTTPException(
            500,
            "A consulta falhou no servidor. Avise o suporte informando o "
            f"número {numero} e o horário.",
        )

    return PlainTextResponse(saida)


# ════════════════════════════════════════════════════════════════
# PAINEL — uso manual do procurador
# ════════════════════════════════════════════════════════════════

@app.post("/painel/login", include_in_schema=False, summary="Abrir sessão no painel")
async def login(request: Request, senha: str = Form(...)):
    if not PAINEL_ATIVO:
        raise HTTPException(503, "Painel sem senha configurada")

    ip = request.client.host if request.client else "desconhecido"
    corte = datetime.now() - timedelta(minutes=JANELA_FALHAS_MIN)

    # Só as falhas DESTE IP contam. Quem erra a senha em outro lugar não tranca
    # o painel do procurador.
    recentes = _falhas_login.setdefault(ip, deque(maxlen=MAX_FALHAS_LOGIN * 2))
    while recentes and recentes[0] < corte:
        recentes.popleft()

    if len(recentes) >= MAX_FALHAS_LOGIN:
        raise HTTPException(429, "Tentativas demais. Aguarde alguns minutos.")

    if not _senha_confere(senha):
        recentes.append(datetime.now())
        log.warning(f"Senha incorreta no painel (origem {ip})")
        await asyncio.sleep(1)
        raise HTTPException(401, "Senha incorreta")

    # Acertou: zera o contador deste IP e limpa quem já saiu da janela, para o
    # dicionário não crescer sem limite.
    _falhas_login.pop(ip, None)
    for outro in [k for k, v in _falhas_login.items() if not v or v[-1] < corte]:
        _falhas_login.pop(outro, None)

    token = secrets.token_urlsafe(32)
    _sessoes[token] = datetime.now() + timedelta(hours=HORAS_SESSAO)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "sessao", token,
        httponly=True, samesite="lax", secure=COOKIE_SEGURO,
        max_age=HORAS_SESSAO * 3600,
    )
    log.info("Login no painel")
    return resp


@app.post("/painel/logout", include_in_schema=False, summary="Encerrar a sessão")
async def logout(sessao: str = Cookie(None)):
    _sessoes.pop(sessao or "", None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sessao")
    return resp


@app.post("/painel/lotes", include_in_schema=False, summary="Enviar lote pelo painel", response_model=Lote)
async def painel_criar(
    arquivos: list[UploadFile] = File(...),
    campos: str = Form(""),
    _=Depends(exigir_painel),
):
    lote = await _gravar_lote(arquivos, origem="painel", campos=campos)
    return _publico(lote)


@app.get(
    "/painel/lotes",
    include_in_schema=False,
    summary="Listar todos os lotes",
    response_model=ListaLotesComLog,
)
async def painel_listar(
    _=Depends(exigir_painel),
    limite: int = Query(30, ge=1, le=500),
):
    """Ao contrário da API, o painel enxerga os lotes de todas as origens."""
    todos = sorted(_lotes.values(), key=lambda l: l.get("criado_em") or "", reverse=True)
    return {"lotes": [_publico(l, incluir_log=True) for l in todos[:limite]]}


@app.get(
    "/painel/lotes/{lote_id}/arquivos/{tipo}",
    include_in_schema=False,
    summary="Baixar um artefato do lote pelo painel",
    response_class=FileResponse,
    responses={404: {"model": Erro, "description": "Lote ou artefato inexistente"}},
)
async def painel_baixar_arquivo(lote_id: str, tipo: TipoArquivo, _=Depends(exigir_painel)):
    """
    Mesmos arquivos da API, com a sessão do painel em vez do token — o
    procurador não tem token, e sem isto a planilha do Agente 1, que fica na
    pasta isolada do lote, ficava inalcançável para ele.

    O painel enxerga lotes de qualquer origem, inclusive os do SIAP.
    """
    lote = _lotes.get(lote_id)
    if not lote:
        raise HTTPException(404, "Lote não encontrado")
    return _resposta_arquivo(lote, tipo.value)


@app.get(
    "/painel/relatorios",
    include_in_schema=False,
    summary="Listar os relatórios acumulados",
    response_model=ListaRelatorios,
)
async def painel_relatorios(_=Depends(exigir_painel)):
    _garantir_pastas()
    arqs = [p for p in PASTA_RESULT.iterdir() if p.is_file() and not p.name.startswith(".")]
    arqs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "arquivos": [
            {
                "nome": p.name,
                "tamanho_kb": round(p.stat().st_size / 1024, 1),
                "modificado_em": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
            }
            for p in arqs
        ]
    }


@app.get(
    "/painel/relatorios/{nome}",
    include_in_schema=False,
    summary="Baixar um relatório",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/octet-stream": {}}, "description": "O arquivo"},
        404: {"model": Erro, "description": "Arquivo não encontrado"},
    },
)
async def painel_baixar(nome: str, _=Depends(exigir_painel)):
    limpo = Path(nome).name
    alvo = (PASTA_RESULT / limpo).resolve()
    if alvo.parent != PASTA_RESULT.resolve() or not alvo.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(alvo, filename=alvo.name, media_type="application/octet-stream")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(sessao: str = Cookie(None)):
    if not PAINEL_ATIVO:
        return HTMLResponse(
            "<h1>Painel indisponível</h1><p>Gere o hash com "
            "<code>python gerar_credencial.py painel</code> e defina "
            "<code>SENHA_PAINEL_HASH</code> no ambiente do serviço.</p>",
            status_code=503,
        )
    return HTMLResponse(PAINEL if _sessao_valida(sessao) else LOGIN)


# ════════════════════════════════════════════════════════════════
# PÁGINAS
# ════════════════════════════════════════════════════════════════

_ESTILO = """
  * { box-sizing: border-box; }
  body { margin:0; padding:2rem 1rem;
    font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    background:#f4f6f8; color:#1a2027; }
  .wrap { max-width:960px; margin:0 auto; }
  h1 { margin:0 0 .25rem; font-size:1.5rem; color:#1F4E79; }
  section { background:#fff; border:1px solid #e2e8ee; border-radius:10px;
    padding:1.25rem 1.4rem; margin-bottom:1.15rem; }
  h2 { margin:0 0 .9rem; font-size:1.02rem; color:#1F4E79;
    display:flex; align-items:center; gap:.5rem; }
  .num { background:#1F4E79; color:#fff; width:1.5rem; height:1.5rem;
    border-radius:50%; display:inline-flex; align-items:center;
    justify-content:center; font-size:.8rem; flex:none; }
  button { font:inherit; font-weight:600; cursor:pointer; border:0;
    border-radius:7px; padding:.6rem 1.15rem; background:#1F4E79; color:#fff; }
  button:hover:not(:disabled) { background:#163a5b; }
  button:disabled { background:#b6c2cf; cursor:not-allowed; }
  button.ghost { background:#eef2f6; color:#1F4E79; }
  input[type=password], input[type=file] { font:inherit; }
  input[type=password] { padding:.6rem .8rem; border:1px solid #ccd6e0;
    border-radius:7px; width:100%; }
  table { width:100%; border-collapse:collapse; font-size:.9rem; }
  th,td { text-align:left; padding:.5rem .6rem; border-bottom:1px solid #eef1f4; }
  th { color:#5c6b7a; font-weight:600; font-size:.78rem;
    text-transform:uppercase; letter-spacing:.04em; }
  .vazio { color:#8b98a5; font-size:.9rem; padding:.4rem 0; }
  .linha { display:flex; gap:.7rem; align-items:center; flex-wrap:wrap; }
  .badge { display:inline-block; padding:.2rem .6rem; border-radius:20px;
    font-size:.78rem; font-weight:600; }
  .b-fila { background:#eef2f6; color:#5c6b7a; }
  .b-proc { background:#fff4d6; color:#8a6100; }
  .b-ok { background:#dff3e4; color:#1d6b34; }
  .b-alerta { background:#ffeccc; color:#8a4b00; }
  .b-erro { background:#fde5e3; color:#9b2c22; }
  .av-item { font-size:.85rem; padding:.45rem .7rem; border-radius:6px;
    background:#fff5e6; border:1px solid #f2dcb3; color:#7a4b00; margin-top:.3rem; }

  /* Sinal de vida: um lote silencioso por oito minutos é indistinguível de um
     lote travado. O ponto pisca, a barra corre e a última linha do log muda —
     três evidências independentes de que ainda está andando. */
  .pulso { display:inline-block; width:.45rem; height:.45rem; border-radius:50%;
    background:currentColor; margin-right:.4rem; vertical-align:middle;
    animation:pisca 1.3s ease-in-out infinite; }
  @keyframes pisca { 0%,100% { opacity:1 } 50% { opacity:.2 } }
  .barra { height:4px; border-radius:3px; background:#eef2f6;
    overflow:hidden; margin-top:.6rem; }
  .barra i { display:block; height:100%; width:32%; border-radius:3px;
    background:#1F4E79; animation:corre 1.7s ease-in-out infinite; }
  @keyframes corre { 0% { margin-left:-32% } 100% { margin-left:100% } }
  .solta { border:2px dashed #ccd6e0; border-radius:9px; padding:1.3rem 1rem;
    text-align:center; background:#fbfcfd; transition:.15s; }
  .solta.sobre { border-color:#1F4E79; background:#eef4fa; }
  .solta strong { display:block; margin-bottom:.55rem; color:#3d4a57; font-size:.95rem; }
  .arq { display:flex; align-items:center; gap:.6rem; padding:.4rem .1rem;
    border-bottom:1px solid #f1f4f7; font-size:.88rem; }
  .arq:last-child { border-bottom:0; }
  .arq .nome { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .arq .kb { color:#8b98a5; font-size:.8rem; flex:none; }
  .arq .tira { border:0; background:none; color:#9b2c22; cursor:pointer;
    font-size:1.05rem; line-height:1; padding:.1rem .35rem; border-radius:5px; flex:none; }
  .arq .tira:hover { background:#fde5e3; }
  .excedeu { color:#9b2c22; font-weight:600; }
  .baixe { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.65rem; }
  .baixe a { font-size:.83rem; padding:.32rem .7rem; border-radius:6px;
    background:#eef2f6; text-decoration:none; }
  .baixe a:hover { background:#e2eaf2; text-decoration:none; }
  .andando { margin-top:.55rem; font-size:.88rem; color:#3d4a57; }
  .andando b { font-weight:600; }
  .ao-vivo { margin-top:.3rem; font:12px/1.5 ui-monospace,Consolas,monospace;
    color:#7b8794; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  @media (prefers-reduced-motion:reduce) {
    .pulso, .barra i { animation:none }
  }
  a.dl { color:#1F4E79; font-weight:600; text-decoration:none; }
  a.dl:hover { text-decoration:underline; }
  pre.log { background:#10161c; color:#c8d6e2; border-radius:7px; padding:.8rem;
    font:12px/1.5 ui-monospace,Consolas,monospace; max-height:240px;
    overflow:auto; white-space:pre-wrap; margin:.5rem 0 0; }
  details summary { cursor:pointer; color:#1F4E79; font-size:.85rem; font-weight:600; }
  .erro-msg { color:#9b2c22; font-size:.9rem; margin-top:.5rem; }
"""

_JS_ARQUIVOS = """
// ── Coletor de PDFs ──────────────────────────────────────────────
//
// Usado pelo painel e pela página do Swagger. Um <input type=file> descarta a
// escolha anterior a cada nova, e o formulário do Swagger só aceita um arquivo
// por linha. Aqui os arquivos SE SOMAM, venham de escolhas sucessivas, de uma
// pasta inteira ou de arrastar e soltar — inclusive arrastando a pasta, que o
// dataTransfer.files sozinho ignora.

const Arquivos = {
  itens: [],
  aviso: '',

  chave(f) { return f.name + '::' + f.size + '::' + f.lastModified; },
  total()  { return Arquivos.itens.reduce((s, f) => s + f.size, 0); },
  excedeu(){ return Arquivos.total() > MAX_MB * 1048576; },
  limpar() { Arquivos.itens = []; Arquivos.aviso = ''; },
  remover(i) { Arquivos.itens.splice(i, 1); },

  add(lista) {
    const naoPdf = [];
    let repetidos = 0;
    for (const f of lista) {
      if (!/\\.pdf$/i.test(f.name)) { naoPdf.push(f.name); continue; }
      if (Arquivos.itens.some(e => Arquivos.chave(e) === Arquivos.chave(f))) { repetidos++; continue; }
      Arquivos.itens.push(f);
    }
    const partes = [];
    if (naoPdf.length) partes.push(
      naoPdf.length > 3
        ? `${naoPdf.length} arquivo(s) ignorado(s) por não serem PDF`
        : `Ignorado(s) por não ser PDF: ${naoPdf.join(', ')}`);
    if (repetidos) partes.push(`${repetidos} já estavam na lista`);
    Arquivos.aviso = partes.join('. ');
    return Arquivos.itens.length;
  },

  // Arrastar uma PASTA entrega um diretório, não arquivos — é preciso percorrer.
  async doDrop(dt) {
    const entradas = dt.items
      ? [...dt.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean)
      : [];
    if (!entradas.length) return Arquivos.add(dt.files);

    const achados = [];
    for (const e of entradas) await Arquivos._percorrer(e, achados);
    return Arquivos.add(achados);
  },

  async _percorrer(entrada, acc) {
    if (entrada.isFile) {
      acc.push(await new Promise((ok, erro) => entrada.file(ok, erro)));
      return;
    }
    if (!entrada.isDirectory) return;
    const leitor = entrada.createReader();
    // readEntries devolve no máximo 100 por chamada — repetir até vir vazio.
    let bloco;
    do {
      bloco = await new Promise((ok, erro) => leitor.readEntries(ok, erro));
      for (const e of bloco) await Arquivos._percorrer(e, acc);
    } while (bloco.length);
  },
};

const tamanho = b => b >= 1048576
  ? (b / 1048576).toFixed(1) + ' MB'
  : Math.max(1, Math.round(b / 1024)) + ' KB';

const escapar = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ── Envio do lote ────────────────────────────────────────────────
//
// XMLHttpRequest em vez de fetch por um motivo só: fetch não informa o
// progresso do UPLOAD. Num lote de 100 MB a tela ficava muda por minutos e
// quem estava esperando não distinguia "enviando" de "travou".
//
// Devolve sempre {ok, status, dados, erro} — nunca lança. Todo caminho de
// falha tem mensagem que diz o que fazer.
function enviarLote(url, arquivos, cabecalhos, aoProgresso, campos) {
  return new Promise(resolve => {
    const fd = new FormData();
    for (const f of arquivos) fd.append('arquivos', f);
    if (campos) fd.append('campos', campos);   // [Fase 3] etapas selecionadas

    const req = new XMLHttpRequest();
    req.open('POST', url);
    for (const [k, v] of Object.entries(cabecalhos || {})) req.setRequestHeader(k, v);

    if (aoProgresso) {
      req.upload.onprogress = e => {
        if (e.lengthComputable) aoProgresso(e.loaded, e.total);
      };
    }

    req.onload = () => {
      let dados = null;
      try { dados = JSON.parse(req.responseText); } catch (_) { /* não é JSON */ }

      if (req.status >= 200 && req.status < 300) {
        return resolve(dados
          ? {ok: true, status: req.status, dados}
          : {ok: false, status: req.status,
             erro: 'O servidor respondeu num formato inesperado.'});
      }

      // O 413 quase sempre vem do proxy à frente do serviço, não daqui: ele
      // corta o corpo antes de a requisição chegar. Dizer isso poupa horas.
      if (req.status === 413) {
        return resolve({ok: false, status: 413, erro:
          'O envio foi recusado por ser grande demais. Se o limite mostrado ' +
          'acima não foi atingido, quem recusou foi o servidor de entrada ' +
          '(proxy) — peça a quem administra para aumentar o tamanho máximo ' +
          'de requisição, ou envie os PDFs em lotes menores.'});
      }
      if (req.status === 401 || req.status === 403) {
        return resolve({ok: false, status: req.status, erro:
          'Sessão expirada ou token inválido. Entre de novo e repita o envio.'});
      }
      if (req.status === 502 || req.status === 503 || req.status === 504) {
        return resolve({ok: false, status: req.status, erro:
          'O serviço não respondeu. Aguarde um instante e tente de novo.'});
      }
      resolve({ok: false, status: req.status,
               erro: (dados && dados.detail) || ('Falha no envio (HTTP ' + req.status + ').')});
    };

    req.onerror   = () => resolve({ok: false, status: 0, erro:
      'Não foi possível falar com o servidor. Verifique a conexão e tente de novo.'});
    req.ontimeout = () => resolve({ok: false, status: 0, erro:
      'O envio demorou demais e foi interrompido. Tente com menos arquivos.'});
    req.onabort   = () => resolve({ok: false, status: 0, erro: 'Envio cancelado.'});

    req.send(fd);
  });
}

// "42 MB de 100 MB (42%)" — o que quem espera precisa ver.
function textoProgresso(enviado, total) {
  const pct = total ? Math.round(enviado / total * 100) : 0;
  return `Enviando… ${tamanho(enviado)} de ${tamanho(total)} (${pct}%)`;
}

// Rótulos dos artefatos. O Excel do Agente 2 é acumulativo — dizer isso no
// botão evita que alguém o mande para o cartório achando que é só deste lote.
const ROTULO_ARQUIVO = {
  agente1_planilha: 'Planilha do Agente 1',
  agente1_json    : 'JSON do Agente 1',
  agente2_json    : 'JSON do Agente 2',
  agente2_planilha: 'Planilha do Agente 2 (acumulada)',
};
"""


_ENVIO_DOCS = ("""
<style>
  .hera { max-width:1400px; margin:1.4rem auto 0; padding:0 20px;
    font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; color:#1a2027; }
  .hera .cx { background:#fff; border:1px solid #d6e0ea; border-left:4px solid #1F4E79;
    border-radius:9px; padding:1.1rem 1.3rem; }
  .hera h2 { margin:0 0 .3rem; font-size:1.08rem; color:#1F4E79; }
  .hera p { margin:.25rem 0 0; font-size:.88rem; color:#5c6b7a; }
  .hera label { display:block; font-size:.8rem; color:#5c6b7a; margin:.9rem 0 .3rem; }
  .hera input[type=password] { font:inherit; padding:.5rem .7rem; border:1px solid #ccd6e0;
    border-radius:7px; width:100%; max-width:520px; }
  .hera .zona { margin-top:.9rem; border:2px dashed #ccd6e0; border-radius:9px;
    padding:1.1rem; text-align:center; background:#fbfcfd; transition:.15s; }
  .hera .zona.sobre { border-color:#1F4E79; background:#eef4fa; }
  .hera button { font:inherit; font-weight:600; cursor:pointer; border:0; border-radius:7px;
    padding:.55rem 1.05rem; background:#1F4E79; color:#fff; }
  .hera button.g { background:#eef2f6; color:#1F4E79; }
  .hera button:disabled { background:#b6c2cf; cursor:not-allowed; }
  .hera .fila { max-height:190px; overflow:auto; margin-top:.7rem; }
  .hera .it { display:flex; gap:.6rem; align-items:center; font-size:.86rem;
    padding:.32rem .1rem; border-bottom:1px solid #f1f4f7; }
  .hera .it span:first-child { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .hera .it small { color:#8b98a5; }
  .hera .it button { background:none; color:#9b2c22; padding:.1rem .3rem; }
  .hera .barra { height:4px; border-radius:3px; background:#eef2f6; overflow:hidden; margin-top:.7rem; }
  .hera .barra i { display:block; height:100%; width:32%; border-radius:3px; background:#1F4E79;
    animation:heracorre 1.7s ease-in-out infinite; }
  @keyframes heracorre { 0% { margin-left:-32% } 100% { margin-left:100% } }
  .hera .estado { margin-top:.8rem; font-size:.9rem; }
  .hera .ruim { color:#9b2c22; font-weight:600; }
  .hera .av { font-size:.85rem; padding:.4rem .6rem; border-radius:6px; background:#fff5e6;
    border:1px solid #f2dcb3; color:#7a4b00; margin-top:.3rem; }
  @media (prefers-reduced-motion:reduce) { .hera .barra i { animation:none } }
</style>

<div class="hera"><div class="cx">
  <h2>Enviar um lote de teste</h2>
  <p>O formulário do Swagger, mais abaixo, aceita <b>um arquivo por linha</b> — é limitação
     daquela página. Aqui você solta a pasta inteira de uma vez. Mesma rota
     <code>POST /api/v1/lotes</code>, mesmo token.</p>

  <label for="hera-tk">Token do consumidor</label>
  <input type="password" id="hera-tk" placeholder="pgms_live_..." autocomplete="off">

  <input type="file" id="hera-arq" multiple accept="application/pdf,.pdf" hidden>
  <input type="file" id="hera-pasta" webkitdirectory directory multiple hidden>

  <div class="zona" id="hera-zona">
    <strong>Arraste os PDFs — ou a pasta inteira — aqui</strong>
    <div style="margin-top:.6rem;display:flex;gap:.6rem;justify-content:center;flex-wrap:wrap">
      <button type="button" class="g" id="hera-b-arq">Escolher arquivos</button>
      <button type="button" class="g" id="hera-b-pasta">Escolher uma pasta</button>
    </div>
    <p>Só os PDFs entram. Todos formam <b>um único lote</b>. Até __MAX_MB__ MB.</p>
  </div>

  <div class="fila" id="hera-fila"></div>

  <div style="display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;margin-top:.9rem">
    <button id="hera-enviar" disabled>Enviar e processar</button>
    <button class="g" id="hera-limpar" hidden>Limpar</button>
    <span style="font-size:.85rem;color:#8b98a5" id="hera-total"></span>
  </div>

  <div class="estado" id="hera-estado"></div>
</div></div>

<script>
const MAX_MB = __MAX_MB__;
</script>
<script>""" + _JS_ARQUIVOS + """</script>
<script>
(function () {
  const q = s => document.querySelector(s);
  const estado = q('#hera-estado');
  let acompanhando = null;

  function autorizacao() {
    const t = q('#hera-tk').value.trim();
    return t ? {'Authorization': 'Bearer ' + t} : null;
  }

  function desenhar() {
    const itens = Arquivos.itens, excedeu = Arquivos.excedeu();
    q('#hera-fila').innerHTML = itens.map((f, i) => `
      <div class="it"><span>${escapar(f.webkitRelativePath || f.name)}</span>
        <small>${tamanho(f.size)}</small>
        <button data-i="${i}" title="Tirar da lista">&times;</button></div>`).join('');
    q('#hera-enviar').disabled = !itens.length || excedeu;
    q('#hera-enviar').textContent = itens.length
      ? `Enviar ${itens.length} PDF(s) e processar` : 'Enviar e processar';
    q('#hera-limpar').hidden = !itens.length;
    q('#hera-total').innerHTML = itens.length
      ? (excedeu
          ? `<span class="ruim">${tamanho(Arquivos.total())} — acima do limite de ${MAX_MB} MB</span>`
          : `${tamanho(Arquivos.total())} no total`)
      : '';
    if (Arquivos.aviso) { estado.textContent = Arquivos.aviso; Arquivos.aviso = ''; }
  }

  q('#hera-fila').onclick = e => {
    const i = e.target.dataset && e.target.dataset.i;
    if (i === undefined) return;
    Arquivos.remover(Number(i)); desenhar();
  };
  q('#hera-b-arq').onclick   = () => q('#hera-arq').click();
  q('#hera-b-pasta').onclick = () => q('#hera-pasta').click();
  q('#hera-limpar').onclick  = () => { Arquivos.limpar(); desenhar(); };
  for (const id of ['#hera-arq', '#hera-pasta']) {
    q(id).onchange = e => { Arquivos.add(e.target.files); e.target.value = ''; desenhar(); };
  }

  const zona = q('#hera-zona');
  ['dragenter','dragover'].forEach(ev => zona.addEventListener(ev, e => {
    e.preventDefault(); zona.classList.add('sobre');
  }));
  ['dragleave','drop'].forEach(ev => zona.addEventListener(ev, e => {
    e.preventDefault(); zona.classList.remove('sobre');
  }));
  zona.addEventListener('drop', async e => {
    estado.textContent = 'Lendo os arquivos...';
    await Arquivos.doDrop(e.dataTransfer);
    desenhar();
  });

  // Baixar exige o cabeçalho de autenticação, então não dá para usar <a href>.
  async function baixar(url, nome) {
    const r = await fetch(url, {headers: autorizacao()});
    if (!r.ok) { estado.innerHTML += `<div class="ruim">Falha ao baixar (${r.status})</div>`; return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = nome;
    a.click(); URL.revokeObjectURL(a.href);
  }

  function pintar(l) {
    const dur = s => s == null ? '' : (s < 60 ? s + ' s' : Math.floor(s/60) + ' min');
    let html = `<b>${escapar(l.lote_id)}</b> — ${escapar(l.status)}`;
    if (l.status === 'na_fila')
      html += ` · ${l.lotes_na_frente || 0} lote(s) na frente · esperando há ${dur(l.decorrido_s)}`
            + '<div class="barra"><i></i></div>';
    if (l.status === 'processando')
      html += ` · ${escapar(l.etapa || '')} · há ${dur(l.decorrido_s)}`
            + '<div class="barra"><i></i></div>';
    if (l.status === 'concluido') html += ` · processado em ${dur(l.decorrido_s)}`;
    if (l.resumo) html += `<div style="margin-top:.4rem">${escapar(l.resumo)}</div>`;
    if (l.erro)   html += `<div class="ruim">${escapar(l.erro)}</div>`;
    (l.avisos || []).forEach(a => html += `<div class="av">&#9888; ${escapar(a)}</div>`);

    const baixaveis = l.downloads || [];
    if (baixaveis.length)
      html += '<div style="margin-top:.7rem;display:flex;gap:.6rem;flex-wrap:wrap">'
            + baixaveis.map(t => `<button class="g" data-baixar="${t}">&#8681; `
                + `${escapar(ROTULO_ARQUIVO[t] || t)}</button>`).join('')
            + '</div>';
    estado.innerHTML = html;

    estado.querySelectorAll('[data-baixar]').forEach(b => {
      const t = b.dataset.baixar;
      const ext = t.endsWith('planilha') ? 'xlsx' : 'json';
      b.onclick = () => baixar(`/api/v1/lotes/${l.lote_id}/arquivos/${t}`,
                               `${t}_${l.lote_id}.${ext}`);
    });
  }

  async function acompanhar(id) {
    clearInterval(acompanhando);
    const passo = async () => {
      const r = await fetch('/api/v1/lotes/' + id, {headers: autorizacao()});
      if (!r.ok) { clearInterval(acompanhando); return; }
      const l = await r.json();
      pintar(l);
      if (l.status === 'concluido' || l.status === 'erro') clearInterval(acompanhando);
    };
    await passo();
    acompanhando = setInterval(passo, 3000);
  }

  let enviando = false;

  q('#hera-enviar').onclick = async () => {
    if (enviando) return;                  // trava contra o duplo clique
    const cab = autorizacao();
    if (!cab) { estado.innerHTML = '<span class="ruim">Informe o token do consumidor.</span>'; return; }

    const quantos = Arquivos.itens.length;
    enviando = true;
    q('#hera-enviar').disabled = true;
    estado.textContent = `Enviando ${quantos} PDF(s)…`;

    try {
      const r = await enviarLote('/api/v1/lotes', Arquivos.itens, cab,
                                 (env, tot) => { estado.textContent = textoProgresso(env, tot); });
      if (!r.ok) {
        // Antes, um 413 do proxy (que responde HTML) fazia o r.json() estourar
        // e a tela mostrava "Falha de rede: Unexpected token '<'".
        estado.innerHTML = `<span class="ruim">${escapar(r.erro)}</span>`;
        return;
      }
      Arquivos.limpar(); desenhar();
      acompanhar(r.dados.lote_id);
    } finally {
      enviando = false;
      q('#hera-enviar').disabled = !Arquivos.itens.length;
    }
  };

  desenhar();
})();
</script>
""").replace("__MAX_MB__", str(MAX_MB_LOTE))


LOGIN = """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Entrar — Triagem de Execuções Fiscais</title>
<style>""" + _ESTILO + """
  .wrap { max-width:380px; margin-top:12vh; }
</style></head><body><div class="wrap">
  <section>
    <h1>Triagem de Execuções Fiscais</h1>
    <p class="vazio">HERA Tecnologia / PGMS</p>
    <form id="f" style="margin-top:1.2rem">
      <label for="senha" style="font-size:.85rem;color:#5c6b7a">Senha de acesso</label>
      <input type="password" id="senha" name="senha" autofocus required style="margin:.35rem 0 .9rem">
      <button type="submit" style="width:100%">Entrar</button>
    </form>
    <p class="erro-msg" id="erro"></p>
  </section>
</div>
<script>
document.getElementById('f').onsubmit = async e => {
  e.preventDefault();
  const fd = new FormData();
  fd.append('senha', document.getElementById('senha').value);
  const r = await fetch('/painel/login', {method:'POST', body:fd});
  if (r.ok) { location.reload(); }
  else {
    const j = await r.json().catch(() => ({detail:'Falha no login'}));
    document.getElementById('erro').textContent = j.detail;
  }
};
</script></body></html>
"""

PAINEL = ("""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Triagem de Execuções Fiscais — PGMS</title>
<style>""" + _ESTILO + """</style></head><body><div class="wrap">

  <div class="linha" style="justify-content:space-between;margin-bottom:1.5rem">
    <div>
      <h1>Triagem de Execuções Fiscais</h1>
      <p class="vazio" style="padding:0">HERA Tecnologia / PGMS</p>
    </div>
    <button class="ghost" id="sair">Sair</button>
  </div>

  <section>
    <h2><span class="num">1</span> Enviar processos</h2>

    <input type="file" id="files" multiple accept="application/pdf,.pdf" hidden>
    <input type="file" id="pasta" webkitdirectory directory multiple hidden>
    <div id="solta" class="solta">
      <strong>Arraste os PDFs — ou a pasta inteira — aqui</strong>
      <div class="linha" style="justify-content:center">
        <button type="button" class="ghost" id="btn-escolher">Escolher arquivos</button>
        <button type="button" class="ghost" id="btn-pasta">Escolher uma pasta</button>
      </div>
      <p class="vazio" style="padding:.6rem 0 0">Pode escolher várias vezes, de pastas
        diferentes — os arquivos se somam. Só os PDFs entram. Até __MAX_MB__ MB por lote.</p>
    </div>

    <div id="lista"></div>

    <details style="margin-top:.9rem">
      <summary style="cursor:pointer;font-size:.9rem;color:#5c6b7a">
        Etapas a extrair (avançado) — por padrão, todas</summary>
      <div class="linha" id="campos-box" style="flex-wrap:wrap;gap:.8rem;margin-top:.6rem;font-size:.9rem">
        <label><input type="checkbox" class="campo" value="citacao" checked> Citação</label>
        <label><input type="checkbox" class="campo" value="penhora" checked> Penhora</label>
        <label><input type="checkbox" class="campo" value="movimentacao" checked> Movimentação</label>
        <label><input type="checkbox" class="campo" value="sinais" checked> Sinais (extinção/parcelamento/art.40)</label>
        <label><input type="checkbox" class="campo" value="entidades" checked> Entidades (CPF, CDA, valor…)</label>
        <label><input type="checkbox" class="campo" value="tipo" checked> Tipo (execução fiscal?)</label>
      </div>
      <p class="vazio" style="padding:.4rem 0 0">As etapas não marcadas são
        <b>reaproveitadas</b> do que já foi extraído antes (não se perdem). Útil para
        reprocessar só uma etapa (ex.: só penhora) sem refazer o resto.</p>
    </details>

    <div class="linha" style="margin-top:.9rem">
      <button id="btn-up" disabled>Enviar e processar</button>
      <button class="ghost" id="btn-limpar" hidden>Limpar seleção</button>
      <span class="vazio" id="total" style="padding:0"></span>
    </div>

    <p class="vazio" id="up-msg">Cada envio vira um lote independente, processado em
      fila — um por vez. A leitura dos PDFs e a análise levam alguns minutos por
      processo; pode fechar a página e voltar depois.</p>
  </section>

  <section>
    <h2><span class="num">2</span> Lotes <span id="contador" class="vazio"
        style="padding:0;font-weight:400"></span></h2>
    <div id="lotes"><p class="vazio">Carregando...</p></div>
  </section>

  <section>
    <h2><span class="num">3</span> Relatórios acumulados</h2>
    <table>
      <thead><tr><th>Arquivo</th><th style="width:110px">Tamanho</th><th style="width:150px">Gerado em</th></tr></thead>
      <tbody id="relatorios"></tbody>
    </table>
  </section>

</div>
<script>
const MAX_MB = __MAX_MB__;
</script>
<script>""" + _JS_ARQUIVOS + """</script>
<script>
const $ = s => document.querySelector(s);
const esc = escapar;

const CLASSE = {na_fila:'b-fila', processando:'b-proc', concluido:'b-ok', erro:'b-erro'};

// A lista é redesenhada a cada 3 s. Sem isto, o log que o procurador abre para
// acompanhar o lote fecha sozinho na próxima rodada — justamente enquanto ele
// está olhando. 'toggle' não borbulha, daí a captura.
const abertos = new Set();
document.addEventListener('toggle', e => {
  const id = e.target.dataset && e.target.dataset.lote;
  if (!id) return;
  e.target.open ? abertos.add(id) : abertos.delete(id);
}, true);

$('#sair').onclick = async () => { await fetch('/painel/logout',{method:'POST'}); location.reload(); };

// ── Seleção de arquivos ──────────────────────────────────────────
// A lógica de acumular vive em Arquivos (coletor compartilhado); aqui fica só
// o desenho da tela do painel.

function desenharSelecao() {
  const itens = Arquivos.itens, excedeu = Arquivos.excedeu();

  $('#lista').innerHTML = itens.map((f, i) => `
    <div class="arq">
      <span class="nome">${esc(f.webkitRelativePath || f.name)}</span>
      <span class="kb">${tamanho(f.size)}</span>
      <button class="tira" data-i="${i}" title="Tirar da lista">&times;</button>
    </div>`).join('');

  $('#btn-up').disabled = !itens.length || excedeu;
  $('#btn-up').textContent = itens.length
    ? `Enviar ${itens.length} PDF(s) e processar`
    : 'Enviar e processar';
  $('#btn-limpar').hidden = !itens.length;
  $('#total').innerHTML = itens.length
    ? (excedeu
        ? `<span class="excedeu">${tamanho(Arquivos.total())} — acima do limite de ${MAX_MB} MB. Tire alguns arquivos.</span>`
        : `${tamanho(Arquivos.total())} no total`)
    : '';
  if (Arquivos.aviso) { $('#up-msg').textContent = Arquivos.aviso; Arquivos.aviso = ''; }
}

$('#lista').onclick = e => {
  const i = e.target.dataset && e.target.dataset.i;
  if (i === undefined) return;
  Arquivos.remover(Number(i));
  desenharSelecao();
};

$('#btn-escolher').onclick = () => $('#files').click();
$('#btn-pasta').onclick    = () => $('#pasta').click();
$('#btn-limpar').onclick   = () => { Arquivos.limpar(); desenharSelecao(); };

for (const id of ['#files', '#pasta']) {
  $(id).onchange = e => {
    Arquivos.add(e.target.files);
    e.target.value = '';   // libera reescolher o MESMO arquivo depois de removê-lo
    desenharSelecao();
  };
}

const solta = $('#solta');
['dragenter','dragover'].forEach(ev => solta.addEventListener(ev, e => {
  e.preventDefault(); solta.classList.add('sobre');
}));
['dragleave','drop'].forEach(ev => solta.addEventListener(ev, e => {
  e.preventDefault(); solta.classList.remove('sobre');
}));
solta.addEventListener('drop', async e => {
  $('#up-msg').textContent = 'Lendo os arquivos...';
  await Arquivos.doDrop(e.dataTransfer);
  desenharSelecao();
});

let enviando = false;

$('#btn-up').onclick = async () => {
  if (enviando) return;                    // trava contra o duplo clique
  if (!Arquivos.itens.length) { $('#up-msg').textContent = 'Selecione ao menos um PDF.'; return; }

  const quantos = Arquivos.itens.length;
  enviando = true;
  $('#btn-up').disabled = true;
  $('#up-msg').textContent = `Enviando ${quantos} PDF(s)…`;

  // enviarLote nunca lança: o try/finally aqui é só para o estado do botão.
  // Antes não havia catch nenhum, e uma falha de rede deixava a tela parada
  // em "Enviando…" com o botão reabilitado — dois cliques, dois lotes iguais.
  try {
    // [Fase 3] etapas marcadas; se todas (ou nenhuma) -> "" = todas.
    const marcados = Array.from(document.querySelectorAll('.campo:checked')).map(c => c.value);
    const todas = document.querySelectorAll('.campo').length;
    const campos = (marcados.length === 0 || marcados.length === todas) ? '' : marcados.join(',');
    const r = await enviarLote('/painel/lotes', Arquivos.itens, null,
                               (env, tot) => { $('#up-msg').textContent = textoProgresso(env, tot); },
                               campos);
    if (r.ok) {
      $('#up-msg').textContent =
        `Lote ${r.dados.lote_id} criado com ${r.dados.arquivos.length} PDF(s) — acompanhe abaixo.`;
      Arquivos.limpar();
    } else {
      $('#up-msg').textContent = r.erro;
      if (r.status === 401) setTimeout(() => location.reload(), 1500);
    }
    desenharSelecao();
    carregar();
  } finally {
    enviando = false;
    $('#btn-up').disabled = !Arquivos.itens.length;
  }
};

// "há 3 min" diz mais do que "iniciado 14:31:02" para quem está esperando.
function duracao(s) {
  if (s == null) return '';
  if (s < 60) return `${s} s`;
  const min = Math.floor(s / 60), h = Math.floor(min / 60);
  return h ? `${h} h ${String(min % 60).padStart(2, '0')} min` : `${min} min`;
}

// Última linha do log, sem o timestamp que o servidor prefixa.
function ultimaLinha(l) {
  if (!l.log || !l.log.length) return '';
  const bruta = l.log[l.log.length - 1]
    .replace(/^\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d\\s+/, '')
    .replace(/^──\\s*|\\s*──$/g, '');
  return bruta.length > 120 ? bruta.slice(0, 120) + '…' : bruta;
}

function andamento(l) {
  if (l.status === 'na_fila') {
    const frente = l.lotes_na_frente
      ? `${l.lotes_na_frente} lote(s) na frente`
      : 'é o próximo a entrar';
    return `<div class="andando"><b>Na fila</b> — ${frente}.
              Aguardando há ${duracao(l.decorrido_s)}.</div>
            <div class="barra"><i></i></div>`;
  }
  if (l.status === 'processando') {
    const viva = ultimaLinha(l);
    return `<div class="andando"><b>${esc(l.etapa || 'Processando')}</b>
              — há ${duracao(l.decorrido_s)}. Leva alguns minutos por processo.</div>
            <div class="barra"><i></i></div>
            ${viva ? `<div class="ao-vivo">${esc(viva)}</div>` : ''}`;
  }
  if (l.status === 'concluido' && l.decorrido_s != null) {
    return `<div class="andando" style="color:#5c6b7a">Processado em ${duracao(l.decorrido_s)}.</div>`;
  }
  return '';
}

function cartaoLote(l) {
  const comAviso = l.status === 'concluido' && l.avisos.length;
  const emCurso = l.status === 'na_fila' || l.status === 'processando';
  const cls = comAviso ? 'b-alerta' : (CLASSE[l.status] || 'b-fila');
  const rotulo = comAviso ? 'concluído com avisos' : l.status;
  return `
  <div style="border:1px solid #eef1f4;border-radius:8px;padding:.85rem 1rem;margin-bottom:.7rem">
    <div class="linha" style="justify-content:space-between">
      <div>
        <strong style="font-size:.9rem">${esc(l.lote_id)}</strong>
        <span class="vazio" style="padding:0;margin-left:.5rem">
          ${l.arquivos.length} PDF(s) &middot; ${esc(l.origem||'')} &middot; ${esc(l.criado_em||'')}
        </span>
      </div>
      <span class="badge ${cls}">${emCurso ? '<span class="pulso"></span>' : ''}${esc(rotulo)}</span>
    </div>
    ${andamento(l)}
    ${(l.downloads || []).length ? `<div class="baixe">${l.downloads.map(t =>
        `<a class="dl" href="/painel/lotes/${encodeURIComponent(l.lote_id)}/arquivos/${t}"
            download>&#8681; ${esc(ROTULO_ARQUIVO[t] || t)}</a>`).join('')}</div>` : ''}
    ${l.resumo ? `<div style="margin-top:.5rem;font-size:.9rem">${esc(l.resumo)}</div>` : ''}
    ${l.erro ? `<div class="erro-msg">${esc(l.erro)}</div>` : ''}
    ${l.avisos.map(a => `<div class="av-item">&#9888; ${esc(a)}</div>`).join('')}
    ${l.log && l.log.length ? `<details style="margin-top:.6rem" data-lote="${esc(l.lote_id)}"
      ${abertos.has(l.lote_id) ? 'open' : ''}>
      <summary>Ver log</summary><pre class="log">${esc(l.log.join('\\n'))}</pre></details>` : ''}
  </div>`;
}

async function carregar() {
  try {
    const r = await fetch('/painel/lotes');
    if (r.status === 401) { location.reload(); return; }
    const j = await r.json();
    $('#lotes').innerHTML = j.lotes.length
      ? j.lotes.map(cartaoLote).join('')
      : '<p class="vazio">Nenhum lote enviado ainda.</p>';

    // O procurador pode estar com a seção 3 na tela e não ver o cartão do lote.
    const ativos = j.lotes.filter(l => l.status === 'na_fila' || l.status === 'processando').length;
    $('#contador').textContent = ativos ? `— ${ativos} em andamento` : '';
    document.title = ativos
      ? `(${ativos}) Triagem de Execuções Fiscais — PGMS`
      : 'Triagem de Execuções Fiscais — PGMS';

    const rel = await (await fetch('/painel/relatorios')).json();
    $('#relatorios').innerHTML = rel.arquivos.length
      ? rel.arquivos.map(a => `<tr>
          <td><a class="dl" href="/painel/relatorios/${encodeURIComponent(a.nome)}">${esc(a.nome)}</a></td>
          <td>${a.tamanho_kb} KB</td><td>${esc(a.modificado_em)}</td></tr>`).join('')
      : '<tr><td colspan="3" class="vazio">Nenhum relatório gerado ainda.</td></tr>';
  } catch (e) { /* rede instável — proxima rodada tenta de novo */ }
}

desenharSelecao();
carregar();
setInterval(carregar, 3000);
</script></body></html>
""").replace("__MAX_MB__", str(MAX_MB_LOTE))