"""
Comportamentos que só aparecem depois de o serviço rodar semanas: retenção em
disco, freio de login e limites de execução.
"""
from datetime import datetime, timedelta

import pytest


def _lote_falso(app_mod, lote_id, dias_atras, status="concluido"):
    quando = (datetime.now() - timedelta(days=dias_atras)).strftime("%Y-%m-%d %H:%M:%S")
    d = app_mod._dir_lote(lote_id)
    (d / "entrada").mkdir(parents=True, exist_ok=True)
    (d / "entrada" / "processo.pdf").write_bytes(b"%PDF-1.4 conteudo")
    (d / "resultados").mkdir(parents=True, exist_ok=True)
    (d / "resultados" / "resultados_V8.xlsx").write_bytes(b"planilha")
    app_mod._lotes[lote_id] = {
        "id": lote_id, "status": status, "origem": "siap",
        "criado_em": quando, "iniciado_em": quando, "concluido_em": quando,
        "arquivos": ["processo.pdf"], "etapa": "concluído", "log": [],
        "analises": [], "avisos": [], "resumo": None, "erro": None,
        "totais": None, "planilha": "resultados_V8.xlsx",
    }
    return d


# ══════════════════════════════════════════════════════════
# Retenção em disco
# ══════════════════════════════════════════════════════════

def test_pdf_recente_e_preservado(app_mod):
    d = _lote_falso(app_mod, "RET-NOVO", dias_atras=0)
    app_mod._limpar_antigos()
    assert (d / "entrada" / "processo.pdf").exists(), "apagou PDF de lote recente"
    app_mod._lotes.pop("RET-NOVO", None)


def test_pdf_antigo_e_apagado_mas_o_resultado_fica(app_mod):
    """
    O PDF é o que pesa (100 MB no caso do cliente) e o original está com quem
    enviou. O que o serviço produziu tem que sobreviver.
    """
    d = _lote_falso(app_mod, "RET-VELHO", dias_atras=app_mod.RETENCAO_PDF_DIAS + 1)
    app_mod._limpar_antigos()
    assert not (d / "entrada").exists(), "PDF antigo não foi apagado — o disco enche"
    assert (d / "resultados" / "resultados_V8.xlsx").exists(), "apagou o resultado junto"
    assert "RET-VELHO" in app_mod._lotes, "removeu o lote antes da retenção do lote"
    app_mod._lotes.pop("RET-VELHO", None)


def test_lote_muito_antigo_some_por_inteiro(app_mod):
    d = _lote_falso(app_mod, "RET-ANCIAO", dias_atras=app_mod.RETENCAO_LOTE_DIAS + 1)
    app_mod._limpar_antigos()
    assert not d.exists()
    assert "RET-ANCIAO" not in app_mod._lotes


def test_retencao_nao_toca_lote_em_andamento(app_mod):
    """Um lote em processamento é velho no relógio, mas está em uso."""
    d = _lote_falso(app_mod, "RET-ATIVO",
                    dias_atras=app_mod.RETENCAO_LOTE_DIAS + 5, status="processando")
    app_mod._limpar_antigos()
    assert d.exists(), "apagou um lote que ainda estava processando"
    assert "RET-ATIVO" in app_mod._lotes
    app_mod._lotes.pop("RET-ATIVO", None)


# ══════════════════════════════════════════════════════════
# Registro persistido
# ══════════════════════════════════════════════════════════

def test_log_gravado_e_aparado(app_mod):
    """
    O registro é reserializado inteiro a cada mudança de estado; log completo
    por lote vira dezenas de MB reescritos com mil lotes.
    """
    import json
    app_mod._lotes["LOG-GRANDE"] = {
        "id": "LOG-GRANDE", "status": "concluido", "origem": "siap",
        "criado_em": "2026-01-01 00:00:00", "concluido_em": "2026-01-01 00:00:01",
        "arquivos": [], "log": [f"linha {i}" for i in range(2000)],
        "analises": [], "avisos": [], "resumo": None, "erro": None,
        "totais": None, "planilha": None, "etapa": "concluído",
    }
    app_mod._salvar_registro()
    gravado = json.loads(app_mod.ARQ_REGISTRO.read_text(encoding="utf-8"))
    assert len(gravado["LOG-GRANDE"]["log"]) == app_mod.MAX_LINHAS_LOG_DISCO
    assert gravado["LOG-GRANDE"]["log"][-1] == "linha 1999", "aparou as linhas erradas"
    # em memória o log completo continua
    assert len(app_mod._lotes["LOG-GRANDE"]["log"]) == 2000
    app_mod._lotes.pop("LOG-GRANDE", None)


# ══════════════════════════════════════════════════════════
# Freio de login
# ══════════════════════════════════════════════════════════

def test_ataque_de_um_ip_nao_tranca_o_procurador(app_mod):
    """
    O freio era global: dez erros de QUALQUER origem trancavam o painel por
    cinco minutos, inclusive para o procurador que sabe a senha. Num serviço
    publicado isso é negação de serviço de graça.
    """
    from fastapi.testclient import TestClient
    from conftest import SENHA
    app_mod._falhas_login.clear()

    atacante = TestClient(app_mod.app, client=("203.0.113.7", 5555))
    for _ in range(app_mod.MAX_FALHAS_LOGIN + 2):
        atacante.post("/painel/login", data={"senha": "errada"})
    assert atacante.post("/painel/login", data={"senha": "errada"}).status_code == 429

    procurador = TestClient(app_mod.app, client=("198.51.100.4", 6666))
    r = procurador.post("/painel/login", data={"senha": SENHA})
    assert r.status_code == 200, "o ataque de outro IP trancou o painel do procurador"
    app_mod._falhas_login.clear()


def test_o_ip_que_ataca_e_barrado(app_mod):
    from fastapi.testclient import TestClient
    app_mod._falhas_login.clear()
    c = TestClient(app_mod.app, client=("203.0.113.9", 7777))
    for _ in range(app_mod.MAX_FALHAS_LOGIN):
        assert c.post("/painel/login", data={"senha": "errada"}).status_code == 401
    assert c.post("/painel/login", data={"senha": "errada"}).status_code == 429
    app_mod._falhas_login.clear()


def test_acerto_limpa_o_contador(app_mod):
    from fastapi.testclient import TestClient
    from conftest import SENHA
    app_mod._falhas_login.clear()
    c = TestClient(app_mod.app, client=("198.51.100.9", 8888))
    c.post("/painel/login", data={"senha": "errada"})
    c.post("/painel/login", data={"senha": SENHA})
    assert not app_mod._falhas_login, "o contador não zerou depois do acerto"


# ══════════════════════════════════════════════════════════
# Limites de execução
# ══════════════════════════════════════════════════════════

def test_timeout_configurado(app_mod):
    """Sem teto, um agente travado congela a fila serial para sempre."""
    assert app_mod.TIMEOUT_AGENTE_S > 0
    assert app_mod.TIMEOUT_AGENTE_S == 3600, "default divergiu do documentado"


def test_defaults_de_retencao_sao_os_documentados(app_mod):
    assert app_mod.RETENCAO_PDF_DIAS == 7
    assert app_mod.RETENCAO_LOTE_DIAS == 90
