"""
Caminho de OCR do Agente 1.

O OCR foi reescrito para renderizar as páginas em ARQUIVOS e abrir uma por vez
— antes recebia o lote inteiro de imagens na memória, e era essa a causa do
estouro em PDF grande. Como Tesseract e Poppler não existem na máquina de
desenvolvimento, aqui eles são substituídos por dublês: o que se testa é a
LÓGICA (quais páginas entram, o que sai, como a falha é tratada), não a
qualidade do reconhecimento.
"""
import sys
import types

import pytest


@pytest.fixture
def agente1(sandbox, monkeypatch):
    for v in ("OCR_DPI", "OCR_MAX_LOTE", "OCR_MAX_WORKERS"):
        monkeypatch.delenv(v, raising=False)
    sys.modules.pop("agente1", None)
    sys.path.insert(0, str(sandbox))
    import agente1 as mod
    yield mod
    sys.modules.pop("agente1", None)


def _dubles(monkeypatch, textos_por_pagina, confianca=88.0, erro_render=None):
    """
    Instala dublês de pdf2image, pytesseract e PIL.Image no sys.modules.
    `textos_por_pagina` mapeia número de página → texto que o OCR "leria".
    """
    renderizadas = []

    def convert_from_path(pdf_path, dpi=None, first_page=None, last_page=None,
                          output_folder=None, paths_only=False, fmt=None):
        if erro_render:
            raise erro_render
        assert paths_only, "o OCR deve pedir caminhos, não imagens na memória"
        assert output_folder, "as páginas devem ser renderizadas em disco"
        renderizadas.append((first_page, last_page, dpi))
        return [f"{output_folder}/pag-{n}.png" for n in range(first_page, last_page + 1)]

    abertas, fechadas = [], []

    class _Img:
        def __init__(self, caminho):
            self.caminho = caminho
            self.pagina = int(caminho.rsplit("pag-", 1)[1].split(".")[0])

        def __enter__(self):
            abertas.append(self.pagina)
            return self

        def __exit__(self, *a):
            fechadas.append(self.pagina)
            return False

    pil = types.ModuleType("PIL")
    imagem_mod = types.ModuleType("PIL.Image")
    imagem_mod.open = _Img
    pil.Image = imagem_mod

    def image_to_data(img, lang=None, output_type=None):
        texto = textos_por_pagina.get(img.pagina, "")
        palavras = texto.split() or [""]
        return {"text": palavras, "conf": [confianca] * len(palavras)}

    pytesseract = types.ModuleType("pytesseract")
    pytesseract.image_to_data = image_to_data
    pytesseract.Output = types.SimpleNamespace(DICT="dict")

    pdf2image = types.ModuleType("pdf2image")
    pdf2image.convert_from_path = convert_from_path

    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", imagem_mod)
    return renderizadas, abertas, fechadas


def test_ocr_le_so_as_paginas_pedidas(agente1, monkeypatch):
    """Uma página do intervalo que já tinha texto digital não deve ser lida."""
    _, abertas, _ = _dubles(monkeypatch, {3: "texto da tres", 5: "texto da cinco"})
    r = agente1._ocr_lote("x.pdf", 1, 5, {3, 5})
    assert set(r) == {3, 5}
    assert r[3][0] == "texto da tres"
    assert abertas == [3, 5], "abriu páginas que não precisavam de OCR"


def test_ocr_fecha_cada_imagem(agente1, monkeypatch):
    """
    Se a imagem não for fechada, o pico de memória volta a crescer com o
    tamanho do lote — que é o bug do PDF de 100 MB.
    """
    _, abertas, fechadas = _dubles(monkeypatch, {n: f"p{n}" for n in range(1, 11)})
    agente1._ocr_lote("x.pdf", 1, 10, set(range(1, 11)))
    assert abertas == fechadas, "alguma imagem ficou aberta"
    assert len(fechadas) == 10


def test_ocr_renderiza_para_disco_no_dpi_configurado(agente1, monkeypatch):
    renderizadas, _, _ = _dubles(monkeypatch, {2: "ok"})
    agente1._ocr_lote("x.pdf", 1, 3, {2})
    assert renderizadas == [(1, 3, agente1.OCR_DPI)]


def test_ocr_calcula_confianca_media(agente1, monkeypatch):
    _dubles(monkeypatch, {1: "uma duas tres"}, confianca=75.0)
    r = agente1._ocr_lote("x.pdf", 1, 1, {1})
    assert r[1][1] == pytest.approx(75.0)


def test_ocr_ignora_confianca_negativa(agente1, monkeypatch):
    """O tesseract usa -1 quando não calcula confiança; não pode virar média."""
    _dubles(monkeypatch, {1: "palavra"}, confianca=-1)
    r = agente1._ocr_lote("x.pdf", 1, 1, {1})
    assert r[1][1] == 0.0


def test_falha_de_render_nao_derruba_o_lote(agente1, monkeypatch):
    """
    Poppler ausente ou PDF corrompido: as páginas saem vazias e o processamento
    continua. Um lote inteiro não pode morrer por causa de um intervalo.
    """
    _dubles(monkeypatch, {}, erro_render=RuntimeError("poppler sumiu"))
    r = agente1._ocr_lote("x.pdf", 1, 5, {2, 4})
    assert set(r) == {2, 4}
    assert all(texto == "" and conf == 0.0 for texto, conf in r.values())


def test_dependencia_ausente_nao_derruba_o_lote(agente1, monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    r = agente1._ocr_lote("x.pdf", 1, 3, {1, 2})
    assert set(r) == {1, 2}
    assert all(texto == "" for texto, _ in r.values())


def test_pagina_nao_entregue_pelo_poppler_entra_vazia(agente1, monkeypatch):
    """
    O chamador indexa o resultado por página. Um dicionário incompleto viraria
    KeyError no meio do lote.
    """
    def convert_curto(pdf_path, dpi=None, first_page=None, last_page=None,
                      output_folder=None, paths_only=False, fmt=None):
        return [f"{output_folder}/pag-{first_page}.png"]   # devolve 1 de 3

    _dubles(monkeypatch, {1: "so a primeira"})
    monkeypatch.setattr(sys.modules["pdf2image"], "convert_from_path", convert_curto)
    r = agente1._ocr_lote("x.pdf", 1, 3, {1, 2, 3})
    assert set(r) == {1, 2, 3}
    assert r[1][0] == "so a primeira"
    assert r[2] == ("", 0.0) and r[3] == ("", 0.0)


# ── Agrupamento das páginas em lotes (governa o pico de memória) ──────

def test_paginas_proximas_viram_um_lote(agente1):
    assert agente1._agrupar_paginas_en_lotes([1, 2, 3]) == [(1, 3)]


def test_paginas_distantes_viram_lotes_separados(agente1):
    lotes = agente1._agrupar_paginas_en_lotes([1, 2, 50, 51])
    assert lotes == [(1, 2), (50, 51)]


def test_lote_respeita_o_tamanho_maximo(agente1):
    """É este teto que limita quantas imagens existem ao mesmo tempo."""
    lotes = agente1._agrupar_paginas_en_lotes(list(range(1, 31)))
    assert all(fim - ini + 1 <= agente1.OCR_MAX_LOTE for ini, fim in lotes)
    assert len(lotes) == 3, f"esperado 30/{agente1.OCR_MAX_LOTE} lotes, veio {lotes}"


def test_sem_paginas_nao_gera_lote(agente1):
    assert agente1._agrupar_paginas_en_lotes([]) == []
