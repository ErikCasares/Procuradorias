"""
Testes dos agentes e do diagnóstico do lote — sem subir servidor.

Cobrem defeitos que já chegaram ao cliente uma vez: PDF ilegível passando por
processo válido, log em espanhol, planilha duplicando linha, e o resultado do
Agente 2 gravado por cima da entrada.
"""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════
# Diagnóstico do lote (webapp._diagnosticar)
# ══════════════════════════════════════════════════════════

def test_resumo_e_lido_do_log(app_mod):
    """O regex ficou órfão uma vez e 'resumo' passou meses sempre nulo."""
    resumo, _ = app_mod._diagnosticar(["  9 de 12 processo(s) extraído(s) com sucesso."])
    assert resumo == "9 de 12 processo(s) extraído(s) com sucesso"


def test_avisa_quando_nada_foi_extraido(app_mod):
    resumo, avisos = app_mod._diagnosticar(["  0 de 5 processo(s) extraído(s) com sucesso."])
    assert resumo
    assert any("Nenhum processo pôde ser extraído" in a for a in avisos)


def test_avisa_extracao_parcial(app_mod):
    _, avisos = app_mod._diagnosticar(["  3 de 10 processo(s) extraído(s) com sucesso."])
    assert any("7 de 10" in a for a in avisos)


def test_avisa_quando_agente1_morreu_sem_exportar(app_mod):
    """Log sem a linha final = o agente morreu no meio. Não pode passar por sucesso."""
    _, avisos = app_mod._diagnosticar(["INFO: começando", "algo explodiu"])
    assert any("não chegou a exportar" in a for a in avisos)


def test_avisa_pdf_ilegivel(app_mod):
    _, avisos = app_mod._diagnosticar([
        "EXTRACAO FALHOU: x.pdf — PDF sem texto extraível",
        "  0 de 1 processo(s) extraído(s) com sucesso.",
    ])
    assert any("não pôde ser lido" in a for a in avisos)


def test_avisa_falta_de_memoria(app_mod):
    _, avisos = app_mod._diagnosticar(["MemoryError", "  0 de 1 processo(s) extraído(s) com sucesso."])
    assert any("Memória insuficiente" in a for a in avisos)


# ══════════════════════════════════════════════════════════
# Filtro do log devolvido ao consumidor
# ══════════════════════════════════════════════════════════

def test_log_preserva_dado_do_processo(app_mod):
    """
    Uma versão do filtro encurtava qualquer token com '/', e o CNPJ
    13.504.675/0001-10 saía como '0001-10' no log entregue ao cliente.
    """
    f = app_mod._FiltroLog()
    for entrada in (
        "  CPF/CNPJ       : 13.504.675/0001-10",
        "  Valor orig.    : R$ 279.057,24",
        "  Nº processo    : 0046416-67.2007.8.05.0001",
    ):
        assert f(entrada) == entrada


def test_log_colapsa_traceback(app_mod):
    f = app_mod._FiltroLog()
    linhas = [
        "ERROR:root:Erro ao renderizar",
        "Traceback (most recent call last):",
        '  File "/x/y.py", line 5, in z',
        "    proc = Popen(cmd)",
        "           ^^^^^^^^^",
        "FileNotFoundError: sumiu",
        "INFO:root:  Lote 1/7 concluído",
    ]
    saida = [x for x in (f(l) for l in linhas) if x]
    assert "FileNotFoundError: sumiu" in saida, "a exceção útil foi engolida"
    assert not any("Traceback" in s or "Popen(" in s or "^^^" in s for s in saida)


def test_log_esconde_caminho_interno(app_mod):
    f = app_mod._FiltroLog()
    linha = f"Planilha gerada: {app_mod.PASTA_RESULT}/resultados_V8.xlsx"
    saida = f(linha)
    assert str(app_mod.PASTA_RESULT) not in saida
    assert "resultados_V8.xlsx" in saida


# ══════════════════════════════════════════════════════════
# Agente 2 — regras de negócio
# ══════════════════════════════════════════════════════════

def _agente2(sandbox):
    sys.path.insert(0, str(sandbox))
    import agente2
    return agente2


def test_limiares_de_prioridade(sandbox):
    """Os valores que o Swagger e o README publicam têm que ser os do código."""
    a2 = _agente2(sandbox)
    assert a2.LIMIAR_PRIORIDADE_ALTA == 5000.0
    assert a2.LIMIAR_PRIORIDADE_MEDIA == 1000.0


@pytest.mark.parametrize("valor,esperado", [
    ("R$ 12.480,55", 12480.55),
    ("R$ 1.234.567,89", 1234567.89),
    ("R$ 0,00", 0.0),
    ("", 0.0),
    (None, 0.0),
    ("não informado", 0.0),
])
def test_parser_de_valor_brasileiro(sandbox, valor, esperado):
    a2 = _agente2(sandbox)
    assert a2._valor_para_ordenar(valor) == pytest.approx(esperado)


def test_agente2_nao_sobrescreve_a_entrada(tmp_path, sandbox):
    """
    O nome de saída vinha de str.replace(): com um nome que não terminava em
    '_agente2.json' o resultado era gravado EM CIMA do JSON do Agente 1.
    """
    pasta = tmp_path / "json"
    pasta.mkdir()
    entrada = pasta / "saida_agente1_V8.json"
    payload = {
        "metadata": {"gerado_em": "2026-01-01T00:00:00", "total_processados": 1},
        "processos": [{
            "arquivo": "p.pdf",
            "entidades": {"numero_processo": "1234567-89.2020.8.05.0001",
                          "valor_atualizado": "R$ 50.000,00",
                          "nome_executado": "TESTE LTDA"},
        }],
    }
    entrada.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    antes = entrada.read_bytes()

    r = subprocess.run(
        [sys.executable, "agente2.py", "--arquivo", str(entrada)],
        cwd=str(sandbox), capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ,
             "PASTA_JSON": str(pasta),
             "PASTA_RESULTADOS": str(tmp_path / "res"),
             "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert entrada.read_bytes() == antes, "o Agente 2 destruiu o JSON do Agente 1"
    assert (pasta / "saida_agente1_V8_agente2_resultado.json").exists()


def test_log_do_agente2_em_portugues(tmp_path, sandbox):
    """O log do Agente 2 é devolvido pela API e mostrado no painel."""
    pasta = tmp_path / "json"
    pasta.mkdir()
    entrada = pasta / "lote_T1_agente2.json"
    entrada.write_text(json.dumps({
        "metadata": {"total_processados": 1},
        "processos": [{"arquivo": "p.pdf",
                       "entidades": {"numero_processo": "1111111-11.2011.8.05.0001",
                                     "valor_atualizado": "R$ 9.000,00"}}],
    }, ensure_ascii=False), encoding="utf-8")

    r = subprocess.run(
        [sys.executable, "agente2.py", "--arquivo", str(entrada)],
        cwd=str(sandbox), capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ,
             "PASTA_JSON": str(pasta),
             "PASTA_RESULTADOS": str(tmp_path / "res"),
             "PYTHONIOENCODING": "utf-8"},
    )
    saida = (r.stdout + r.stderr).lower()
    for termo in ("procesando", "resultado del", "nuevos", "actualizados",
                  "reporte excel", "historial:", "archivo no encontrado"):
        assert termo not in saida, f"log do Agente 2 ainda em espanhol: {termo!r}"


def test_json_do_agente1_sem_chaves_em_espanhol(sandbox):
    """O JSON é o contrato com o SIAP — as chaves precisam estar em português."""
    fonte = (sandbox / "agente1.py").read_text(encoding="utf-8")
    i = fonte.index('"metadata": {')
    trecho = fonte[i:i + 600]
    for chave in ('"generado_em"', '"total_procesados"', '"version_agente1"', '"total_aptos"'):
        assert chave not in trecho, f"chave em espanhol no JSON entregue: {chave}"


# ══════════════════════════════════════════════════════════
# Agente 1 — parâmetros de OCR ajustáveis (F7)
# ══════════════════════════════════════════════════════════

def test_ocr_ajustavel_por_ambiente(sandbox, monkeypatch):
    """
    O serviço avisa 'reduza OCR_MAX_WORKERS ou OCR_DPI' quando falta memória.
    O conselho só serve se der para fazer isso sem recompilar a imagem.
    """
    monkeypatch.setenv("OCR_DPI", "120")
    monkeypatch.setenv("OCR_MAX_LOTE", "3")
    monkeypatch.setenv("OCR_MAX_WORKERS", "1")
    sys.modules.pop("agente1", None)
    sys.path.insert(0, str(sandbox))
    import agente1
    assert agente1.OCR_DPI == 120
    assert agente1.OCR_MAX_LOTE == 3
    assert agente1.OCR_MAX_WORKERS == 1
    sys.modules.pop("agente1", None)


def test_ocr_ignora_valor_invalido(sandbox, monkeypatch):
    monkeypatch.setenv("OCR_DPI", "não é número")
    sys.modules.pop("agente1", None)
    sys.path.insert(0, str(sandbox))
    import agente1
    assert agente1.OCR_DPI == 200, "valor inválido no ambiente deve cair no padrão"
    sys.modules.pop("agente1", None)


def test_defaults_de_ocr_sao_os_documentados(sandbox, monkeypatch):
    for v in ("OCR_DPI", "OCR_MAX_LOTE", "OCR_MAX_WORKERS"):
        monkeypatch.delenv(v, raising=False)
    sys.modules.pop("agente1", None)
    sys.path.insert(0, str(sandbox))
    import agente1
    assert (agente1.OCR_DPI, agente1.OCR_MAX_LOTE, agente1.OCR_MAX_WORKERS) == (200, 10, 2)
    sys.modules.pop("agente1", None)
