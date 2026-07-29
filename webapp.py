"""
Interface web — Triagem de Execuções Fiscais
HERA Tecnologia / PGMS — Contrato nº 01/2026

Camada HTTP sobre os dois agentes de linha de comando:
    - upload dos PDFs para a pasta de entrada
    - disparo do pipeline (Agente 1 → Agente 2) em segundo plano
    - acompanhamento do progresso pelo log, em tempo real
    - download dos relatórios Excel gerados

Os agentes rodam como SUBPROCESSOS, não como import. Isso mantém o
promptV7.1.py intocado — ele monta os caminhos de saída e o timestamp no
momento do import, então importá-lo congelaria o nome do arquivo Excel na
primeira execução e reaproveitaria o mesmo em todos os lotes seguintes.

Uso:
    uvicorn webapp:app --host 0.0.0.0 --port 3000
"""

import os
import re
import sys
import asyncio
import logging
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [web] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("webapp")

# ── Caminhos — espelham os dos dois agentes ──────────────────────
BASE_DIR       = Path(__file__).resolve().parent
PASTA_ENTRADA  = BASE_DIR / "processos pra analiser"
PASTA_JSON     = BASE_DIR / "JSON"
PASTA_RESULT   = BASE_DIR / "resultados"

# Nome fixo definido em promptV7.1.py — é o JSON de repasse ao Agente 2
JSON_AGENTE2   = PASTA_JSON / "resultados_procesosV7_1_agente2.json"

MAX_LINHAS_LOG = 500

app = FastAPI(title="Triagem de Execuções Fiscais — PGMS", docs_url="/api/docs")


# ════════════════════════════════════════════════════════════════
# ESTADO DO LOTE EM EXECUÇÃO
# ════════════════════════════════════════════════════════════════
#
# Só um lote roda por vez — o OCR já satura a CPU da VPS, e dois
# Agentes 1 simultâneos escreveriam no mesmo JSON de repasse (nome
# fixo), com o segundo sobrescrevendo o primeiro.

_estado = {
    "rodando"     : False,
    "etapa"       : "ocioso",
    "iniciado_em" : None,
    "terminado_em": None,
    "erro"        : None,
    "resumo"      : None,   # "0 de 12 processos classificados como APTO"
    "avisos"      : [],     # falhas que o Agente 1 engoliu sem derrubar o processo
}
_log_lote = deque(maxlen=MAX_LINHAS_LOG)
_lock = asyncio.Lock()

# O promptV7.1.py trata OCR quebrado e erro de API internamente e ainda assim
# encerra com código 0 — um lote pode "terminar bem" tendo classificado zero
# processos. Estes padrões extraem o desfecho real do log para que o procurador
# não baixe uma planilha vazia achando que deu tudo certo.
_RE_RESUMO = re.compile(r"(\d+)\s+processo\(s\)\s+APTO\(s\)\s+de\s+(\d+)\s+procesados")

_SINTOMAS = (
    ("poppler",     "Poppler indisponível — o OCR não conseguiu renderizar as páginas escaneadas"),
    ("tesseract",   "Tesseract indisponível — o OCR não rodou"),
    ("401",         "OpenAI recusou a autenticação (401) — verifique a OPENAI_API_KEY"),
    ("429",         "OpenAI limitou as chamadas (429) — cota ou rate limit atingido"),
    ("insufficient_quota", "Cota da OpenAI esgotada"),
    ("sin texto extraíble", "Algum PDF ficou sem texto extraível — confira a qualidade do digitalizado"),
    ("MemoryError", "Memória insuficiente durante o OCR — reduza OCR_MAX_WORKERS ou OCR_DPI"),
)


def _diagnosticar() -> tuple:
    """
    Varre o log do lote e devolve (resumo, avisos) — o desfecho real,
    já que o código de saída dos agentes não o reflete.
    """
    texto = "\n".join(_log_lote)

    resumo = None
    m = _RE_RESUMO.search(texto)
    if m:
        aptos, total = m.group(1), m.group(2)
        resumo = f"{aptos} de {total} processo(s) classificados como APTO"

    baixo = texto.lower()
    avisos = [msg for chave, msg in _SINTOMAS if chave.lower() in baixo]

    if m and m.group(1) == "0" and int(m.group(2)) > 0:
        avisos.insert(0, "Nenhum processo foi classificado como APTO — a planilha sairá vazia")

    return resumo, avisos


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _registrar(linha: str):
    _log_lote.append(f"{_agora()}  {linha}")


def _garantir_pastas():
    for p in (PASTA_ENTRADA, PASTA_JSON, PASTA_RESULT):
        p.mkdir(parents=True, exist_ok=True)


def _caminho_seguro(pasta: Path, nome: str) -> Path:
    """
    Resolve `nome` dentro de `pasta`, barrando path traversal
    (../, caminhos absolutos, separadores embutidos no nome).
    """
    limpo = Path(nome).name
    if not limpo or limpo in (".", ".."):
        raise HTTPException(400, "Nome de arquivo inválido")
    alvo = (pasta / limpo).resolve()
    if alvo.parent != pasta.resolve():
        raise HTTPException(400, "Nome de arquivo inválido")
    return alvo


# ════════════════════════════════════════════════════════════════
# PIPELINE — Agente 1 seguido do Agente 2
# ════════════════════════════════════════════════════════════════

async def _executar(cmd: list, etapa: str) -> int:
    """Roda um subprocesso e vai empurrando a saída dele para o log do lote."""
    _estado["etapa"] = etapa
    _registrar(f"── {etapa} ──")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(BASE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async for bruto in proc.stdout:
        linha = bruto.decode("utf-8", errors="replace").rstrip()
        if linha:
            _registrar(linha)
    return await proc.wait()


async def _pipeline():
    """Executa Agente 1 → Agente 2 e guarda o desfecho no estado global."""
    try:
        rc = await _executar(
            [sys.executable, "promptV7.1.py"],
            "Agente 1 — extração, OCR e classificação",
        )
        if rc != 0:
            raise RuntimeError(
                f"Agente 1 encerrou com código {rc}. Verifique o log acima — "
                "causas comuns: OPENAI_API_KEY inválida, PDF corrompido ou memória insuficiente no OCR."
            )

        if not JSON_AGENTE2.exists():
            raise RuntimeError(
                "O Agente 1 terminou mas não gerou o JSON de repasse. "
                "Provavelmente nenhum processo foi classificado como APTO."
            )

        rc = await _executar(
            [sys.executable, "agente2.py", "--arquivo", str(JSON_AGENTE2)],
            "Agente 2 — priorização jurídico-fiscal",
        )
        if rc != 0:
            raise RuntimeError(f"Agente 2 encerrou com código {rc}")

        _estado["etapa"] = "concluído"
        _registrar("── Lote concluído. Relatórios disponíveis para download. ──")

    except Exception as e:
        _estado["erro"]  = str(e)
        _estado["etapa"] = "erro"
        _registrar(f"ERRO: {e}")
        log.exception("Falha no pipeline")

    finally:
        resumo, avisos = _diagnosticar()
        _estado["resumo"] = resumo
        _estado["avisos"] = avisos
        if avisos:
            _registrar("── Avisos: " + " | ".join(avisos) + " ──")
        _estado["rodando"]      = False
        _estado["terminado_em"] = _agora()


# ════════════════════════════════════════════════════════════════
# API
# ════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    """Healthcheck para o Easypanel."""
    return {"status": "ok", "rodando": _estado["rodando"], "etapa": _estado["etapa"]}


@app.get("/api/status")
async def status():
    return {
        **_estado,
        "log": list(_log_lote),
        "tem_chave_openai": bool(os.getenv("OPENAI_API_KEY")),
        "pdfs_na_fila": len(_listar_pdfs()),
    }


def _listar_pdfs() -> list:
    _garantir_pastas()
    return sorted(
        (p for p in PASTA_ENTRADA.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
        key=lambda p: p.name.lower(),
    )


@app.get("/api/arquivos")
async def listar_arquivos():
    return {
        "arquivos": [
            {"nome": p.name, "tamanho_kb": round(p.stat().st_size / 1024, 1)}
            for p in _listar_pdfs()
        ]
    }


@app.post("/api/upload")
async def upload(arquivos: list[UploadFile] = File(...)):
    _garantir_pastas()
    salvos, ignorados = [], []

    for arq in arquivos:
        nome = Path(arq.filename or "").name
        if not nome.lower().endswith(".pdf"):
            ignorados.append(nome or "(sem nome)")
            continue

        destino = _caminho_seguro(PASTA_ENTRADA, nome)
        with open(destino, "wb") as f:
            while chunk := await arq.read(1024 * 1024):
                f.write(chunk)
        salvos.append(nome)

    log.info(f"Upload: {len(salvos)} PDF(s) salvos, {len(ignorados)} ignorado(s)")
    return {"salvos": salvos, "ignorados": ignorados}


@app.delete("/api/arquivos/{nome}")
async def remover_arquivo(nome: str):
    alvo = _caminho_seguro(PASTA_ENTRADA, nome)
    if not alvo.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    alvo.unlink()
    return {"removido": alvo.name}


@app.post("/api/processar")
async def processar():
    async with _lock:
        if _estado["rodando"]:
            raise HTTPException(409, "Já existe um lote em processamento")

        if not _listar_pdfs():
            raise HTTPException(400, "Nenhum PDF na fila — envie os processos primeiro")

        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(
                500,
                "OPENAI_API_KEY não configurada. O Agente 1 cria o cliente OpenAI "
                "já no import e falha sem ela.",
            )

        _log_lote.clear()
        _estado.update(
            rodando=True,
            etapa="iniciando",
            iniciado_em=_agora(),
            terminado_em=None,
            erro=None,
            resumo=None,
            avisos=[],
        )

    asyncio.create_task(_pipeline())
    return {"iniciado": True}


@app.get("/api/resultados")
async def listar_resultados():
    _garantir_pastas()
    arquivos = [p for p in PASTA_RESULT.iterdir() if p.is_file() and not p.name.startswith(".")]
    arquivos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "arquivos": [
            {
                "nome": p.name,
                "tamanho_kb": round(p.stat().st_size / 1024, 1),
                "modificado_em": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
            }
            for p in arquivos
        ]
    }


@app.get("/api/resultados/{nome}")
async def baixar_resultado(nome: str):
    alvo = _caminho_seguro(PASTA_RESULT, nome)
    if not alvo.exists():
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(alvo, filename=alvo.name, media_type="application/octet-stream")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(PAGINA)


# ════════════════════════════════════════════════════════════════
# PÁGINA
# ════════════════════════════════════════════════════════════════

PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Triagem de Execuções Fiscais — PGMS</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 1rem;
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #f4f6f8; color: #1a2027;
  }
  .wrap { max-width: 940px; margin: 0 auto; }
  header { margin-bottom: 1.75rem; }
  h1 { margin: 0 0 .25rem; font-size: 1.5rem; color: #1F4E79; }
  header p { margin: 0; color: #5c6b7a; font-size: .9rem; }
  section {
    background: #fff; border: 1px solid #e2e8ee; border-radius: 10px;
    padding: 1.25rem 1.4rem; margin-bottom: 1.15rem;
  }
  h2 { margin: 0 0 .9rem; font-size: 1.02rem; color: #1F4E79;
       display: flex; align-items: center; gap: .5rem; }
  .num { background: #1F4E79; color: #fff; width: 1.5rem; height: 1.5rem;
         border-radius: 50%; display: inline-flex; align-items: center;
         justify-content: center; font-size: .8rem; flex: none; }
  button {
    font: inherit; font-weight: 600; cursor: pointer;
    border: 0; border-radius: 7px; padding: .6rem 1.15rem;
    background: #1F4E79; color: #fff; transition: background .15s;
  }
  button:hover:not(:disabled) { background: #163a5b; }
  button:disabled { background: #b6c2cf; cursor: not-allowed; }
  button.ghost { background: #eef2f6; color: #1F4E79; }
  button.ghost:hover:not(:disabled) { background: #dee6ee; }
  input[type=file] { font: inherit; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #eef1f4; }
  th { color: #5c6b7a; font-weight: 600; font-size: .78rem;
       text-transform: uppercase; letter-spacing: .04em; }
  tr:last-child td { border-bottom: 0; }
  .vazio { color: #8b98a5; font-size: .9rem; padding: .4rem 0; }
  .linha { display: flex; gap: .7rem; align-items: center; flex-wrap: wrap; }
  #log {
    background: #10161c; color: #c8d6e2; border-radius: 7px;
    padding: .9rem 1rem; font: 12.5px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace;
    height: 300px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
  }
  .badge { display: inline-block; padding: .2rem .6rem; border-radius: 20px;
           font-size: .78rem; font-weight: 600; }
  .b-ocioso { background: #eef2f6; color: #5c6b7a; }
  .b-rodando { background: #fff4d6; color: #8a6100; }
  .b-ok { background: #dff3e4; color: #1d6b34; }
  .b-alerta { background: #ffeccc; color: #8a4b00; }
  .b-erro { background: #fde5e3; color: #9b2c22; }
  #resumo { margin: .85rem 0; }
  .res-linha { font-size: .95rem; padding: .55rem .8rem; border-radius: 7px;
               background: #eef4fa; color: #1F4E79; margin-bottom: .4rem; }
  .av-item { font-size: .87rem; padding: .5rem .8rem; border-radius: 7px;
             background: #fff5e6; border: 1px solid #f2dcb3; color: #7a4b00;
             margin-bottom: .35rem; }
  .aviso { background: #fff8e6; border: 1px solid #f0dca8; color: #7a5a00;
           padding: .7rem .9rem; border-radius: 7px; font-size: .87rem; margin-bottom: .9rem; }
  a.dl { color: #1F4E79; font-weight: 600; text-decoration: none; }
  a.dl:hover { text-decoration: underline; }
  .rm { background: none; border: 0; color: #b03a2e; cursor: pointer;
        font-size: 1.05rem; padding: 0 .3rem; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <h1>Triagem de Execuções Fiscais</h1>
    <p>HERA Tecnologia / PGMS — envie os processos, execute a análise e baixe a priorização.</p>
  </header>

  <div id="aviso-chave" class="aviso" hidden>
    <strong>OPENAI_API_KEY não configurada.</strong>
    O Agente 1 não roda sem ela — defina a variável de ambiente no painel e reinicie o serviço.
  </div>

  <section>
    <h2><span class="num">1</span> Enviar processos</h2>
    <div class="linha">
      <input type="file" id="files" multiple accept="application/pdf,.pdf">
      <button id="btn-up" class="ghost">Enviar PDFs</button>
    </div>
    <p class="vazio" id="up-msg"></p>
    <table>
      <thead><tr><th>Arquivo na fila</th><th style="width:110px">Tamanho</th><th style="width:50px"></th></tr></thead>
      <tbody id="fila"></tbody>
    </table>
  </section>

  <section>
    <h2><span class="num">2</span> Executar análise</h2>
    <div class="linha">
      <button id="btn-run">Processar lote</button>
      <span class="badge b-ocioso" id="estado">ocioso</span>
      <span class="vazio" id="tempo"></span>
    </div>
    <p class="vazio">O Agente 1 faz OCR e classifica; em seguida o Agente 2 gera a priorização. Pode levar vários minutos.</p>
    <div id="resumo" hidden></div>
    <div id="log">Aguardando execução...</div>
  </section>

  <section>
    <h2><span class="num">3</span> Relatórios</h2>
    <table>
      <thead><tr><th>Arquivo</th><th style="width:110px">Tamanho</th><th style="width:150px">Gerado em</th></tr></thead>
      <tbody id="resultados"></tbody>
    </table>
  </section>

</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function carregarFila() {
  const r = await (await fetch('/api/arquivos')).json();
  const tb = $('#fila');
  if (!r.arquivos.length) {
    tb.innerHTML = '<tr><td colspan="3" class="vazio">Nenhum PDF na fila.</td></tr>';
    return;
  }
  tb.innerHTML = r.arquivos.map(a => `
    <tr>
      <td>${esc(a.nome)}</td>
      <td>${a.tamanho_kb} KB</td>
      <td><button class="rm" title="Remover" data-nome="${esc(a.nome)}">&times;</button></td>
    </tr>`).join('');
  tb.querySelectorAll('.rm').forEach(b => b.onclick = async () => {
    await fetch('/api/arquivos/' + encodeURIComponent(b.dataset.nome), {method: 'DELETE'});
    carregarFila();
  });
}

async function carregarResultados() {
  const r = await (await fetch('/api/resultados')).json();
  const tb = $('#resultados');
  if (!r.arquivos.length) {
    tb.innerHTML = '<tr><td colspan="3" class="vazio">Nenhum relatório gerado ainda.</td></tr>';
    return;
  }
  tb.innerHTML = r.arquivos.map(a => `
    <tr>
      <td><a class="dl" href="/api/resultados/${encodeURIComponent(a.nome)}">${esc(a.nome)}</a></td>
      <td>${a.tamanho_kb} KB</td>
      <td>${esc(a.modificado_em)}</td>
    </tr>`).join('');
}

$('#btn-up').onclick = async () => {
  const inp = $('#files');
  if (!inp.files.length) { $('#up-msg').textContent = 'Selecione ao menos um PDF.'; return; }
  const fd = new FormData();
  for (const f of inp.files) fd.append('arquivos', f);
  $('#btn-up').disabled = true;
  $('#up-msg').textContent = 'Enviando...';
  try {
    const r = await (await fetch('/api/upload', {method: 'POST', body: fd})).json();
    let msg = `${r.salvos.length} PDF(s) enviados.`;
    if (r.ignorados.length) msg += ` ${r.ignorados.length} ignorado(s) — apenas .pdf é aceito.`;
    $('#up-msg').textContent = msg;
    inp.value = '';
    carregarFila();
  } catch (e) {
    $('#up-msg').textContent = 'Falha no envio: ' + e;
  } finally {
    $('#btn-up').disabled = false;
  }
};

$('#btn-run').onclick = async () => {
  $('#btn-run').disabled = true;
  const resp = await fetch('/api/processar', {method: 'POST'});
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({detail: 'Erro desconhecido'}));
    $('#log').textContent = 'Não foi possível iniciar: ' + err.detail;
    $('#btn-run').disabled = false;
    return;
  }
  atualizar();
};

let ultimoLog = '';
async function atualizar() {
  let s;
  try { s = await (await fetch('/api/status')).json(); } catch { return; }

  $('#aviso-chave').hidden = s.tem_chave_openai;
  $('#btn-run').disabled = s.rodando || s.pdfs_na_fila === 0;

  const avisos = s.avisos || [];
  const badge = $('#estado');
  const comAviso = !s.rodando && s.etapa === 'concluído' && avisos.length > 0;
  badge.textContent = comAviso ? 'concluído com avisos' : s.etapa;
  badge.className = 'badge ' + (
    s.rodando ? 'b-rodando' :
    comAviso ? 'b-alerta' :
    s.etapa === 'concluído' ? 'b-ok' :
    s.etapa === 'erro' ? 'b-erro' : 'b-ocioso');

  const box = $('#resumo');
  if (!s.rodando && (s.resumo || avisos.length)) {
    box.innerHTML =
      (s.resumo ? `<div class="res-linha"><strong>${esc(s.resumo)}</strong></div>` : '') +
      avisos.map(a => `<div class="av-item">&#9888; ${esc(a)}</div>`).join('');
    box.hidden = false;
  } else {
    box.hidden = true;
  }

  $('#tempo').textContent = s.iniciado_em
    ? (s.terminado_em ? `${s.iniciado_em} → ${s.terminado_em}` : `iniciado ${s.iniciado_em}`)
    : '';

  const texto = s.log.length ? s.log.join('\\n') : 'Aguardando execução...';
  if (texto !== ultimoLog) {
    const el = $('#log');
    const colado = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = texto;
    if (colado) el.scrollTop = el.scrollHeight;
    ultimoLog = texto;
  }

  if (!s.rodando && (s.etapa === 'concluído' || s.etapa === 'erro')) {
    carregarResultados();
    carregarFila();
  }
}

carregarFila();
carregarResultados();
atualizar();
setInterval(atualizar, 2000);
</script>
</body>
</html>
"""
