"""
Cobertura de TODOS os endpoints da API v1 e do painel.
Auth, autorização entre consumidores, validação de entrada, códigos de erro.
"""
import io
import json
import time
import pytest

PDF_MINIMO = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def pdf(nome="p.pdf", conteudo=PDF_MINIMO):
    return ("arquivos", (nome, io.BytesIO(conteudo), "application/pdf"))


# ══════════════════════════════════════════════════════════
# /health — sem autenticação
# ══════════════════════════════════════════════════════════

def test_health_sem_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["api_ativa"] is True
    assert set(j) == {"status", "api_ativa", "na_fila", "processando"}


# ══════════════════════════════════════════════════════════
# Documentação pública
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("rota", ["/api/docs", "/api/redoc", "/api/openapi.json"])
def test_docs_publicas(client, rota):
    r = client.get(rota)
    assert r.status_code == 200, f"{rota} → {r.status_code}"


def test_openapi_valido(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"]
    caminhos = set(spec["paths"])
    esperadas = {
        "/health",
        "/api/v1/lotes",
        "/api/v1/lotes/{lote_id}",
        "/api/v1/lotes/{lote_id}/resultado",
        "/api/v1/lotes/{lote_id}/arquivos",
        "/api/v1/lotes/{lote_id}/arquivos/{tipo}",
        "/api/v1/lotes/{lote_id}/planilha/agente1",
        "/api/v1/lotes/{lote_id}/planilha/agente2",
        "/api/v1/processos",
    }
    faltando = esperadas - caminhos
    assert not faltando, f"rotas ausentes do OpenAPI: {faltando}"


def test_openapi_nao_vaza_rotas_do_painel(client):
    spec = client.get("/api/openapi.json").json()
    assert not [p for p in spec["paths"] if p.startswith("/painel")]


# ══════════════════════════════════════════════════════════
# Autenticação da API
# ══════════════════════════════════════════════════════════

ROTAS_PROTEGIDAS = [
    ("GET", "/api/v1/lotes"),
    ("GET", "/api/v1/lotes/qualquer"),
    ("GET", "/api/v1/lotes/qualquer/resultado"),
    ("GET", "/api/v1/lotes/qualquer/arquivos"),
    ("GET", "/api/v1/lotes/qualquer/arquivos/agente1_json"),
    ("GET", "/api/v1/lotes/qualquer/planilha/agente1"),
    ("GET", "/api/v1/lotes/qualquer/planilha/agente2"),
    ("GET", "/api/v1/processos?numero=1"),
]


@pytest.mark.parametrize("metodo,rota", ROTAS_PROTEGIDAS)
def test_sem_token_401(client, metodo, rota):
    r = client.request(metodo, rota)
    assert r.status_code == 401, f"{rota} → {r.status_code}"


@pytest.mark.parametrize("metodo,rota", ROTAS_PROTEGIDAS)
def test_token_invalido_401(client, metodo, rota):
    r = client.request(metodo, rota, headers={"Authorization": "Bearer errado"})
    assert r.status_code == 401


def test_post_lote_sem_token_401(client):
    r = client.post("/api/v1/lotes", files=[pdf()])
    assert r.status_code == 401


def test_esquema_errado_401(client):
    r = client.get("/api/v1/lotes", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401


def test_token_com_espacos_aceito(client):
    from conftest import TOKEN
    r = client.get("/api/v1/lotes", headers={"Authorization": f"Bearer  {TOKEN} "})
    assert r.status_code == 200


# ══════════════════════════════════════════════════════════
# POST /api/v1/lotes — validação de entrada
# ══════════════════════════════════════════════════════════

def test_criar_lote_ok(client, auth):
    r = client.post("/api/v1/lotes", files=[pdf("proc-a.pdf")], headers=auth)
    assert r.status_code == 202, r.text
    j = r.json()
    assert j["status"] == "na_fila"
    assert j["origem"] == "siap"
    assert j["arquivos"] == ["proc-a.pdf"]
    assert j["lote_id"]
    return j["lote_id"]


def test_rejeita_nao_pdf(client, auth):
    r = client.post("/api/v1/lotes",
                    files=[("arquivos", ("x.txt", io.BytesIO(b"nao sou pdf"), "text/plain"))],
                    headers=auth)
    assert r.status_code == 400
    assert "pdf" in r.json()["detail"].lower()


def test_rejeita_sem_arquivo(client, auth):
    r = client.post("/api/v1/lotes", headers=auth)
    assert r.status_code == 422


def test_nomes_duplicados_desambiguados(client, auth):
    r = client.post("/api/v1/lotes",
                    files=[pdf("igual.pdf"), pdf("igual.pdf"), pdf("igual.pdf")],
                    headers=auth)
    assert r.status_code == 202
    assert r.json()["arquivos"] == ["igual.pdf", "igual-2.pdf", "igual-3.pdf"]


def test_path_traversal_no_nome(client, auth):
    r = client.post("/api/v1/lotes",
                    files=[("arquivos", ("../../../evil.pdf", io.BytesIO(PDF_MINIMO), "application/pdf"))],
                    headers=auth)
    assert r.status_code == 202
    assert r.json()["arquivos"] == ["evil.pdf"], "path traversal não sanitizado"


def test_nome_com_barra_invertida(client, auth):
    r = client.post("/api/v1/lotes",
                    files=[("arquivos", (r"..\..\evil2.pdf", io.BytesIO(PDF_MINIMO), "application/pdf"))],
                    headers=auth)
    assert r.status_code == 202
    nome = r.json()["arquivos"][0]
    assert "/" not in nome and "\\" not in nome, f"nome não sanitizado: {nome!r}"


def test_extensao_maiuscula_aceita(client, auth):
    r = client.post("/api/v1/lotes",
                    files=[("arquivos", ("P.PDF", io.BytesIO(PDF_MINIMO), "application/pdf"))],
                    headers=auth)
    assert r.status_code == 202


def test_lote_unico_processo(client, auth):
    """Cliente quer rodar lote de 1 processo só."""
    r = client.post("/api/v1/lotes", files=[pdf("solo.pdf")], headers=auth)
    assert r.status_code == 202
    assert len(r.json()["arquivos"]) == 1


# ══════════════════════════════════════════════════════════
# Isolamento entre consumidores (IDOR)
# ══════════════════════════════════════════════════════════

def test_lote_de_outro_consumidor_404(client, auth, auth2):
    lote_id = client.post("/api/v1/lotes", files=[pdf("meu.pdf")], headers=auth).json()["lote_id"]
    for rota in (
        f"/api/v1/lotes/{lote_id}",
        f"/api/v1/lotes/{lote_id}/resultado",
        f"/api/v1/lotes/{lote_id}/arquivos",
        f"/api/v1/lotes/{lote_id}/arquivos/agente1_json",
        f"/api/v1/lotes/{lote_id}/planilha/agente1",
        f"/api/v1/lotes/{lote_id}/planilha/agente2",
    ):
        r = client.get(rota, headers=auth2)
        assert r.status_code == 404, f"IDOR em {rota} → {r.status_code}"


def test_listar_so_meus_lotes(client, auth, auth2):
    client.post("/api/v1/lotes", files=[pdf("a.pdf")], headers=auth)
    client.post("/api/v1/lotes", files=[pdf("b.pdf")], headers=auth2)
    meus = client.get("/api/v1/lotes", headers=auth).json()["lotes"]
    assert meus and all(l["origem"] == "siap" for l in meus)
    outros = client.get("/api/v1/lotes", headers=auth2).json()["lotes"]
    assert outros and all(l["origem"] == "outro" for l in outros)


def test_lote_inexistente_404(client, auth):
    r = client.get("/api/v1/lotes/nao-existe-123", headers=auth)
    assert r.status_code == 404


def test_limite_paginacao(client, auth):
    assert client.get("/api/v1/lotes?limite=0", headers=auth).status_code == 422
    assert client.get("/api/v1/lotes?limite=501", headers=auth).status_code == 422
    assert client.get("/api/v1/lotes?limite=1", headers=auth).status_code == 200


# ══════════════════════════════════════════════════════════
# Resultado / arquivos
# ══════════════════════════════════════════════════════════

def test_resultado_antes_de_concluir_409(client, auth):
    lote_id = client.post("/api/v1/lotes", files=[pdf("x.pdf")], headers=auth).json()["lote_id"]
    r = client.get(f"/api/v1/lotes/{lote_id}/resultado", headers=auth)
    assert r.status_code in (409, 200)
    if r.status_code == 409:
        assert "aguarde" in r.json()["detail"].lower() or "concluido" in r.json()["detail"]


def test_tipo_arquivo_invalido_422(client, auth):
    lote_id = client.post("/api/v1/lotes", files=[pdf("y.pdf")], headers=auth).json()["lote_id"]
    r = client.get(f"/api/v1/lotes/{lote_id}/arquivos/../../etc/passwd", headers=auth)
    assert r.status_code in (404, 422)


def test_consultar_lote_traz_log(client, auth):
    lote_id = client.post("/api/v1/lotes", files=[pdf("z.pdf")], headers=auth).json()["lote_id"]
    j = client.get(f"/api/v1/lotes/{lote_id}", headers=auth).json()
    assert "log" in j
    assert "downloads" in j


# ══════════════════════════════════════════════════════════
# GET /api/v1/processos
# ══════════════════════════════════════════════════════════

def test_processo_inexistente_404(client, auth):
    r = client.get("/api/v1/processos?numero=9999999-99.9999.8.05.9999", headers=auth)
    assert r.status_code == 404, r.text


def test_processo_numero_vazio_422(client, auth):
    r = client.get("/api/v1/processos?numero=", headers=auth)
    assert r.status_code == 422


def test_processo_sem_parametro_422(client, auth):
    r = client.get("/api/v1/processos", headers=auth)
    assert r.status_code == 422


@pytest.mark.parametrize("numero", [
    "--pasta", "-h", "--help", "--pasta=C:/", "-x", "--", "-",
])
def test_processo_argumento_tipo_flag_rejeitado(client, auth, numero):
    """Valor começando com '-' não pode virar opção do argparse no subprocess."""
    r = client.get("/api/v1/processos", params={"numero": numero}, headers=auth)
    assert r.status_code == 400, f"{numero!r} → {r.status_code}: {r.text[:200]}"
    assert "usage" not in r.text.lower()


@pytest.mark.parametrize("numero", [
    "abc", "0752821'; DROP TABLE", "../../etc/passwd", "<script>", "0752821 68",
])
def test_processo_numero_malformado_400(client, auth, numero):
    r = client.get("/api/v1/processos", params={"numero": numero}, headers=auth)
    assert r.status_code == 400, f"{numero!r} → {r.status_code}"


def test_processo_500_nao_vaza_interno(client, auth):
    """Se o subprocess falhar, o corpo não pode trazer stderr/caminho interno."""
    r = client.get("/api/v1/processos?numero=0000000-00.0000.0.00.0000", headers=auth)
    corpo = r.text.lower()
    for vazamento in ("traceback", "site-packages", "users" + chr(92) + "bruno", "/app/", "buscar_processo.py"):
        assert vazamento not in corpo, f"vazou {vazamento!r} no corpo: {r.text[:300]}"


def test_processo_sem_ansi(client, auth):
    """A saída da API não pode carregar códigos de escape ANSI."""
    r = client.get("/api/v1/processos?numero=1234567-89.2020.8.05.0001", headers=auth)
    assert "\x1b[" not in r.text, "vazou ANSI na resposta da API"


# ══════════════════════════════════════════════════════════
# Painel
# ══════════════════════════════════════════════════════════

def test_index_mostra_login(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "senha" in r.text.lower()


def test_login_senha_errada(client):
    r = client.post("/painel/login", data={"senha": "errada"})
    assert r.status_code == 401


def test_login_ok_e_cookie(client):
    from conftest import SENHA
    r = client.post("/painel/login", data={"senha": SENHA})
    assert r.status_code == 200
    assert "sessao" in r.cookies
    client.cookies.clear()


@pytest.mark.parametrize("rota", [
    "/painel/lotes",
    "/painel/relatorios",
    "/painel/relatorios/x.xlsx",
    "/painel/lotes/abc/arquivos/agente1_json",
])
def test_painel_sem_sessao_401(client, rota):
    client.cookies.clear()
    r = client.get(rota)
    assert r.status_code == 401, f"{rota} → {r.status_code}"


def test_painel_ve_todos_os_lotes(client, sessao):
    r = client.get("/painel/lotes", cookies=sessao)
    assert r.status_code == 200
    origens = {l["origem"] for l in r.json()["lotes"]}
    assert len(origens) >= 1
    client.cookies.clear()


def test_painel_relatorios_traversal(client, sessao):
    for nome in ["../webapp.py", "..%2Fwebapp.py", "....//webapp.py", "/etc/passwd"]:
        r = client.get(f"/painel/relatorios/{nome}", cookies=sessao)
        assert r.status_code == 404, f"traversal aceito: {nome} → {r.status_code}"
    client.cookies.clear()


def test_painel_upload_com_sessao(client, sessao):
    r = client.post("/painel/lotes", files=[pdf("painel.pdf")], cookies=sessao)
    assert r.status_code == 200
    assert r.json()["origem"] == "painel"
    client.cookies.clear()


def test_logout_invalida_sessao(client):
    from conftest import SENHA
    r = client.post("/painel/login", data={"senha": SENHA})
    ck = r.cookies
    assert client.get("/painel/lotes", cookies=ck).status_code == 200
    client.post("/painel/logout", cookies=ck)
    client.cookies.clear()
    assert client.get("/painel/lotes", cookies=ck).status_code == 401
    client.cookies.clear()


def test_cookie_httponly(client):
    from conftest import SENHA
    client.cookies.clear()
    r = client.post("/painel/login", data={"senha": SENHA})
    sc = r.headers.get("set-cookie", "")
    assert "httponly" in sc.lower(), sc
    assert "samesite" in sc.lower(), sc
    client.cookies.clear()
