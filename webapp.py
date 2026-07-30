"""
API e painel — Triagem de Execuções Fiscais
HERA Tecnologia / PGMS — Contrato nº 01/2026

Duas superfícies sobre o mesmo pipeline (Agente 1 → Agente 2):

    /api/v1/*   API para sistemas externos (SIAP). Autenticação por token
                Bearer. Assíncrona: o lote é aceito na hora e processado numa
                fila; o consumidor acompanha por polling.

    /painel     Interface do procurador. Autenticação por senha, sessão em
                cookie. Mesmo pipeline, uso manual.

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
import secrets
import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import (
    Cookie, Depends, FastAPI, File, Form, Header, HTTPException,
    Request, UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

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

# Pasta de entrada do uso manual por linha de comando (compatibilidade)
PASTA_ENTRADA_PADRAO = BASE_DIR / "processos pra analiser"

MAX_MB_LOTE    = int(os.getenv("MAX_MB_LOTE", "200"))
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
_falhas_login = deque(maxlen=50)   # timestamps, para frear tentativa em massa


def autenticar_api(authorization: str = Header(None)) -> str:
    """Valida o Bearer token e devolve o rótulo do consumidor."""
    if not TOKENS:
        raise HTTPException(
            503,
            "API sem credenciais configuradas. Defina API_TOKENS no ambiente "
            "do serviço no formato 'rotulo:token'.",
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            401,
            "Envie o token no cabeçalho: Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    enviado = authorization[7:].strip()
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
    """Persiste o registro para os lotes sobreviverem a um restart."""
    try:
        tmp = ARQ_REGISTRO.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_lotes, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ARQ_REGISTRO)
    except Exception as e:
        log.error(f"Não foi possível salvar o registro de lotes: {e}")


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


def _publico(lote: dict, incluir_log=False, incluir_analises=False) -> dict:
    """Projeção do lote para resposta — esconde caminhos internos."""
    saida = {
        "lote_id"      : lote["id"],
        "status"       : lote["status"],
        "origem"       : lote.get("origem"),
        "criado_em"    : lote.get("criado_em"),
        "iniciado_em"  : lote.get("iniciado_em"),
        "concluido_em" : lote.get("concluido_em"),
        "arquivos"     : lote.get("arquivos", []),
        "etapa"        : lote.get("etapa"),
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
# O promptV7.1.py trata OCR quebrado e erro de API internamente e ainda assim
# encerra com código 0 — um lote pode "terminar bem" tendo classificado zero
# processos. Sem isto o consumidor recebe status de sucesso e planilha vazia.

_RE_RESUMO = re.compile(r"(\d+)\s+processo\(s\)\s+APTO\(s\)\s+de\s+(\d+)\s+procesados")

_SINTOMAS = (
    ("poppler",             "Poppler indisponível — o OCR não conseguiu renderizar as páginas escaneadas"),
    ("tesseract",           "Tesseract indisponível — o OCR não rodou"),
    ("401",                 "OpenAI recusou a autenticação (401) — verifique a OPENAI_API_KEY"),
    ("429",                 "OpenAI limitou as chamadas (429) — cota ou rate limit atingido"),
    ("insufficient_quota",  "Cota da OpenAI esgotada"),
    ("sin texto extraíble", "Algum PDF ficou sem texto extraível — confira a qualidade do digitalizado"),
    ("memoryerror",         "Memória insuficiente durante o OCR — reduza OCR_MAX_WORKERS ou OCR_DPI"),
)


def _diagnosticar(linhas: list) -> tuple:
    texto = "\n".join(linhas)
    baixo = texto.lower()

    resumo = None
    m = _RE_RESUMO.search(texto)
    if m:
        resumo = f"{m.group(1)} de {m.group(2)} processo(s) classificados como APTO"

    avisos = [msg for chave, msg in _SINTOMAS if chave.lower() in baixo]
    if m and m.group(1) == "0" and int(m.group(2)) > 0:
        avisos.insert(0, "Nenhum processo foi classificado como APTO — o resultado sairá vazio")

    return resumo, avisos


# ════════════════════════════════════════════════════════════════
# PIPELINE
# ════════════════════════════════════════════════════════════════

async def _executar(cmd: list, ambiente: dict, lote: dict, etapa: str) -> int:
    lote["etapa"] = etapa
    lote["log"].append(f"{_agora()}  ── {etapa} ──")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", **ambiente},
    )
    async for bruto in proc.stdout:
        linha = bruto.decode("utf-8", errors="replace").rstrip()
        if linha:
            lote["log"].append(f"{_agora()}  {linha}")
            if len(lote["log"]) > MAX_LINHAS_LOG:
                del lote["log"][:-MAX_LINHAS_LOG]
    return await proc.wait()


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
            [sys.executable, "promptV7.1.py"],
            {
                "PASTA_ENTRADA"   : str(entrada),
                "PASTA_JSON"      : str(saida_json),
                "PASTA_RESULTADOS": str(saida_result),
            },
            lote,
            "Agente 1 — extração, OCR e classificação",
        )
        if rc != 0:
            raise RuntimeError(f"Agente 1 encerrou com código {rc}")

        traspasse = saida_json / "resultados_procesosV7_1_agente2.json"
        if not traspasse.exists():
            raise RuntimeError("O Agente 1 não gerou o JSON de traspasse")

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

    finally:
        resumo, avisos = _diagnosticar(lote.get("log", []))
        lote["resumo"] = resumo
        lote["avisos"] = avisos
        lote["concluido_em"] = _agora()
        _salvar_registro()


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


# ════════════════════════════════════════════════════════════════
# APLICAÇÃO
# ════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _fila
    _garantir_pastas()
    _carregar_registro()
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


app = FastAPI(
    title="Triagem de Execuções Fiscais — PGMS",
    version="1.0",
    docs_url="/api/docs",
    lifespan=lifespan,
)


def _nome_pdf_seguro(nome: str) -> str:
    limpo = Path(nome or "").name
    if not limpo.lower().endswith(".pdf"):
        raise HTTPException(400, f"Somente arquivos .pdf são aceitos: {limpo!r}")
    if limpo in (".", "..") or not limpo.strip():
        raise HTTPException(400, "Nome de arquivo inválido")
    return limpo


async def _gravar_lote(arquivos: list, origem: str) -> dict:
    """Cria o lote em disco e o enfileira."""
    lote_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    entrada = _dir_lote(lote_id) / "entrada"
    entrada.mkdir(parents=True, exist_ok=True)

    nomes, total_bytes = [], 0
    for arq in arquivos:
        nome = _nome_pdf_seguro(arq.filename)
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

@app.get("/health")
async def health():
    """Healthcheck do Easypanel — sem autenticação, sem dado sensível."""
    return {
        "status"     : "ok",
        "api_ativa"  : bool(TOKENS),
        "na_fila"    : sum(1 for l in _lotes.values() if l["status"] == "na_fila"),
        "processando": sum(1 for l in _lotes.values() if l["status"] == "processando"),
    }


@app.post("/api/v1/lotes", status_code=202)
async def criar_lote(
    arquivos: list[UploadFile] = File(..., description="Um ou mais PDFs de processos"),
    consumidor: str = Depends(autenticar_api),
):
    """
    Envia um lote de processos para triagem.

    Responde 202 na hora — o processamento é assíncrono. Acompanhe em
    GET /api/v1/lotes/{lote_id} até status virar 'concluido' ou 'erro'.
    """
    lote = await _gravar_lote(arquivos, origem=consumidor)
    return _publico(lote)


@app.get("/api/v1/lotes")
async def listar_lotes(consumidor: str = Depends(autenticar_api), limite: int = 50):
    """Lista os lotes enviados por este consumidor, mais recentes primeiro."""
    meus = [l for l in _lotes.values() if l.get("origem") == consumidor]
    meus.sort(key=lambda l: l.get("criado_em") or "", reverse=True)
    return {"lotes": [_publico(l) for l in meus[:limite]]}


@app.get("/api/v1/lotes/{lote_id}")
async def consultar_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """Estado do lote. Enquanto status for 'na_fila' ou 'processando', repita."""
    lote = _lotes.get(lote_id)
    if not lote or lote.get("origem") != consumidor:
        raise HTTPException(404, "Lote não encontrado")
    return _publico(lote, incluir_log=True)


@app.get("/api/v1/lotes/{lote_id}/resultado")
async def resultado_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """Priorização completa do lote — só depois de status 'concluido'."""
    lote = _lotes.get(lote_id)
    if not lote or lote.get("origem") != consumidor:
        raise HTTPException(404, "Lote não encontrado")
    if lote["status"] != "concluido":
        raise HTTPException(409, f"Lote ainda em '{lote['status']}' — aguarde 'concluido'")
    return _publico(lote, incluir_analises=True)


@app.get("/api/v1/lotes/{lote_id}/planilha")
async def planilha_lote(lote_id: str, consumidor: str = Depends(autenticar_api)):
    """Excel de revisão gerado pelo Agente 1 para este lote."""
    lote = _lotes.get(lote_id)
    if not lote or lote.get("origem") != consumidor:
        raise HTTPException(404, "Lote não encontrado")
    if not lote.get("planilha"):
        raise HTTPException(404, "Este lote não gerou planilha")

    caminho = _dir_lote(lote_id) / "resultados" / lote["planilha"]
    if not caminho.exists():
        raise HTTPException(404, "Planilha não encontrada em disco")
    return FileResponse(
        caminho,
        filename=f"lote_{lote_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ════════════════════════════════════════════════════════════════
# PAINEL — uso manual do procurador
# ════════════════════════════════════════════════════════════════

@app.post("/painel/login")
async def login(senha: str = Form(...)):
    if not PAINEL_ATIVO:
        raise HTTPException(503, "Painel sem senha configurada")

    # Freia tentativa em massa sem travar o uso legítimo
    recentes = [t for t in _falhas_login if datetime.now() - t < timedelta(minutes=5)]
    if len(recentes) >= 10:
        raise HTTPException(429, "Tentativas demais. Aguarde alguns minutos.")

    if not _senha_confere(senha):
        _falhas_login.append(datetime.now())
        await asyncio.sleep(1)
        raise HTTPException(401, "Senha incorreta")

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


@app.post("/painel/logout")
async def logout(sessao: str = Cookie(None)):
    _sessoes.pop(sessao or "", None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sessao")
    return resp


@app.post("/painel/lotes")
async def painel_criar(
    arquivos: list[UploadFile] = File(...),
    _=Depends(exigir_painel),
):
    lote = await _gravar_lote(arquivos, origem="painel")
    return _publico(lote)


@app.get("/painel/lotes")
async def painel_listar(_=Depends(exigir_painel), limite: int = 30):
    todos = sorted(_lotes.values(), key=lambda l: l.get("criado_em") or "", reverse=True)
    return {"lotes": [_publico(l, incluir_log=True) for l in todos[:limite]]}


@app.get("/painel/relatorios")
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


@app.get("/painel/relatorios/{nome}")
async def painel_baixar(nome: str, _=Depends(exigir_painel)):
    limpo = Path(nome).name
    alvo = (PASTA_RESULT / limpo).resolve()
    if alvo.parent != PASTA_RESULT.resolve() or not alvo.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(alvo, filename=alvo.name, media_type="application/octet-stream")


@app.get("/", response_class=HTMLResponse)
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
  a.dl { color:#1F4E79; font-weight:600; text-decoration:none; }
  a.dl:hover { text-decoration:underline; }
  pre.log { background:#10161c; color:#c8d6e2; border-radius:7px; padding:.8rem;
    font:12px/1.5 ui-monospace,Consolas,monospace; max-height:240px;
    overflow:auto; white-space:pre-wrap; margin:.5rem 0 0; }
  details summary { cursor:pointer; color:#1F4E79; font-size:.85rem; font-weight:600; }
  .erro-msg { color:#9b2c22; font-size:.9rem; margin-top:.5rem; }
"""

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

PAINEL = """<!doctype html>
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
    <div class="linha">
      <input type="file" id="files" multiple accept="application/pdf,.pdf">
      <button id="btn-up">Enviar e processar</button>
    </div>
    <p class="vazio" id="up-msg">Cada envio vira um lote independente, processado em fila.</p>
  </section>

  <section>
    <h2><span class="num">2</span> Lotes</h2>
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
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const CLASSE = {na_fila:'b-fila', processando:'b-proc', concluido:'b-ok', erro:'b-erro'};

$('#sair').onclick = async () => { await fetch('/painel/logout',{method:'POST'}); location.reload(); };

$('#btn-up').onclick = async () => {
  const inp = $('#files');
  if (!inp.files.length) { $('#up-msg').textContent = 'Selecione ao menos um PDF.'; return; }
  const fd = new FormData();
  for (const f of inp.files) fd.append('arquivos', f);
  $('#btn-up').disabled = true;
  $('#up-msg').textContent = 'Enviando...';
  try {
    const r = await fetch('/painel/lotes', {method:'POST', body:fd});
    const j = await r.json();
    $('#up-msg').textContent = r.ok
      ? `Lote ${j.lote_id} criado com ${j.arquivos.length} PDF(s).`
      : 'Falha: ' + (j.detail || 'erro desconhecido');
    inp.value = '';
    carregar();
  } finally { $('#btn-up').disabled = false; }
};

function cartaoLote(l) {
  const comAviso = l.status === 'concluido' && l.avisos.length;
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
      <span class="badge ${cls}">${esc(rotulo)}</span>
    </div>
    ${l.resumo ? `<div style="margin-top:.5rem;font-size:.9rem">${esc(l.resumo)}</div>` : ''}
    ${l.erro ? `<div class="erro-msg">${esc(l.erro)}</div>` : ''}
    ${l.avisos.map(a => `<div class="av-item">&#9888; ${esc(a)}</div>`).join('')}
    ${l.log && l.log.length ? `<details style="margin-top:.6rem">
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

    const rel = await (await fetch('/painel/relatorios')).json();
    $('#relatorios').innerHTML = rel.arquivos.length
      ? rel.arquivos.map(a => `<tr>
          <td><a class="dl" href="/painel/relatorios/${encodeURIComponent(a.nome)}">${esc(a.nome)}</a></td>
          <td>${a.tamanho_kb} KB</td><td>${esc(a.modificado_em)}</td></tr>`).join('')
      : '<tr><td colspan="3" class="vazio">Nenhum relatório gerado ainda.</td></tr>';
  } catch (e) { /* rede instável — proxima rodada tenta de novo */ }
}

carregar();
setInterval(carregar, 3000);
</script></body></html>
"""
