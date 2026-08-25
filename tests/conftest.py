"""
Suíte de testes — Triagem de Execuções Fiscais (HERA / PGMS)

Roda com:
    pip install pytest httpx
    python -m pytest

Cada sessão copia os .py para uma sandbox temporária e sobe a aplicação a
partir dela. As pastas de trabalho reais (JSON/, resultados/, dados/) nunca são
tocadas, então os testes podem rodar num ambiente com dados de produção.
"""
import hashlib
import importlib
import os
import secrets
import shutil
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
FONTES = ["webapp.py", "agente1.py", "agente2.py", "buscar_processo.py", "gerar_credencial.py"]

# Credenciais geradas por execução — nada fixo no repositório.
TOKEN = "pgms_live_" + secrets.token_urlsafe(32)
TOKEN2 = "pgms_live_" + secrets.token_urlsafe(32)
SENHA = "senha-de-teste-" + secrets.token_hex(4)


def _sha256(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _hash_senha(senha: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(senha.encode(), salt=salt, n=16384, r=8, p=1,
                        maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


@pytest.fixture(scope="session")
def sandbox(tmp_path_factory):
    d = tmp_path_factory.mktemp("procuradorias")
    for f in FONTES:
        shutil.copy2(RAIZ / f, d / f)
    (d / "processos pra analiser").mkdir(exist_ok=True)
    return d


@pytest.fixture(scope="session")
def app_mod(sandbox):
    """Importa o webapp já configurado, a partir da sandbox."""
    os.environ["API_TOKENS"] = f"siap:sha256:{_sha256(TOKEN)},outro:sha256:{_sha256(TOKEN2)}"
    os.environ["SENHA_PAINEL_HASH"] = _hash_senha(SENHA)
    os.environ["MAX_MB_LOTE"] = "500"
    os.environ["COOKIE_SEGURO"] = "0"
    os.environ["HORAS_SESSAO"] = "12"
    os.environ.pop("SENHA_PAINEL", None)

    sys.path.insert(0, str(sandbox))
    for m in ("webapp", "agente1", "agente2", "buscar_processo"):
        sys.modules.pop(m, None)
    return importlib.import_module("webapp")


@pytest.fixture(scope="session")
def client(app_mod):
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def auth():
    """Consumidor 'siap'."""
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def auth2():
    """Consumidor 'outro' — usado para provar o isolamento entre consumidores."""
    return {"Authorization": f"Bearer {TOKEN2}"}


@pytest.fixture
def sessao(client):
    r = client.post("/painel/login", data={"senha": SENHA})
    assert r.status_code == 200, r.text
    return r.cookies
