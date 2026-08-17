"""
BUSCAR PROCESSO v3 — consulta por número CNJ (vista consolidada, sem duplicados)
HERA Tecnologia / PGMS

Lê os arquivos JSON da pasta JSON/ e mostra as informações de um processo
específico, buscando pelo número do processo (CNJ).

Procura em TRÊS fontes:
  1. JSON do Agente 1  → resultados_*_agente2.json     (dados extraídos + decisão — SÓ APTO)
  2. Histórico Agente 2 → historial_agente2.jsonl       (análise e priorização — SÓ APTO)
  3. Auditoria (V7.2)  → historial_classificacoes.jsonl (TODAS as decisões, inclusive NÃO APTO)

  A fonte 3 é a que faz um processo NÃO APTO aparecer nesta consulta. As fontes
  1 e 2 só contêm processos APTO, por desenho do pipeline.

Uso:
    # Modo interativo — pergunta o número e mostra
    python buscar_processo_v2.py

    # Modo direto — passa o número como argumento
    python buscar_processo_v2.py 0752821-68.2013.8.05.0001

    # Apontar para outra pasta JSON
    python buscar_processo_v2.py --pasta /caminho/JSON 0752821-68.2013.8.05.0001

Observação:
    A busca é flexível quanto à formatação do número: ignora pontos, traços e
    espaços. Então "0752821-68.2013.8.05.0001" e "07528216820138050001"
    encontram o mesmo processo.

Nota de compatibilidade:
    Esta é a versão CLI. O webapp.py continua usando buscar_processo.py até que
    a rota de API seja cabeada. A auditoria (fonte 3) é COMPARTILHADA entre
    consumidores e os registros NÃO trazem campo 'consumidor', então esta versão
    NÃO deve ser exposta por API sem antes estampar o consumidor no append do
    webapp — do contrário quebraria o isolamento entre consumidores.
"""

import os
import re
import sys
import json
import glob
import argparse


# ── Configuração ──────────────────────────────────────────────────
CURRENT_DIR       = os.path.dirname(os.path.abspath(__file__))
PASTA_JSON        = os.path.join(CURRENT_DIR, "JSON")
SUFIJO_A1         = "_agente2.json"                  # JSON do Agente 1 (não confundir com _resultado)
HISTORIAL_A2      = "historial_agente2.jsonl"
HISTORIAL_CLASSIF = "historial_classificacoes.jsonl"  # [v2] auditoria V7.2 — TODAS as decisões
# [v3.1] O Agente 1 real grava a auditoria com OUTRO nome. Lemos os dois para
#        não perder o histórico já existente. Ordem: nomes conhecidos da auditoria.
HISTORIAL_CLASSIF_ALIASES = [
    "historial_classificacoes.jsonl",   # nome esperado pela busca (v2)
    "historico_extracoes.jsonl",        # nome real gravado pelo Agente 1 (v8)
]


# ── Utilidades ────────────────────────────────────────────────────

def _normalizar_numero(numero: str) -> str:
    """Remove tudo que não é dígito, para comparar números CNJ com formatações diferentes."""
    return re.sub(r"\D", "", str(numero or ""))


def _cor(texto, codigo):
    """Aplica cor ANSI se o terminal suportar; senão, texto puro."""
    if sys.stdout.isatty():
        return f"\033[{codigo}m{texto}\033[0m"
    return texto

def _titulo(t):   return _cor(t, "1;36")   # ciano negrito
def _label(t):    return _cor(t, "1;37")   # branco negrito
def _ok(t):       return _cor(t, "1;32")   # verde
def _alerta(t):   return _cor(t, "1;31")   # vermelho
def _dim(t):      return _cor(t, "2")      # apagado


# ── Leitura das fontes ────────────────────────────────────────────

def _buscar_em_json_agente1(pasta: str, numero_norm: str):
    """
    Procura o processo nos arquivos *_agente2.json do Agente 1.
    Retorna o dict do processo (com decisão e entidades) ou None.
    """
    # Todos os *_agente2.json, EXCETO os *_agente2_resultado.json (que são do Agente 2)
    padrao = os.path.join(pasta, f"*{SUFIJO_A1}")
    for caminho in sorted(glob.glob(padrao)):
        if caminho.endswith("_resultado.json"):
            continue
        try:
            with open(caminho, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for proc in payload.get("processos", []):
            np = (proc.get("entidades") or {}).get("numero_processo")
            if np and _normalizar_numero(np) == numero_norm:
                proc["_origem_arquivo"] = os.path.basename(caminho)
                return proc
    return None


def _buscar_em_historial_agente2(pasta: str, numero_norm: str):
    """
    Procura o processo no historial_agente2.jsonl do Agente 2.
    Retorna o dict do registro (com análise) ou None.
    """
    caminho = os.path.join(pasta, HISTORIAL_A2)
    if not os.path.exists(caminho):
        return None

    # [FIX] O historial é APPEND-ONLY: um processo pode ter várias linhas.
    #       Ficar com o registro MAIS RECENTE que tenha análise; se nenhuma
    #       linha tiver análise (ex.: só houve erro), cair no último registro
    #       para poder mostrar o motivo do erro. Nunca devolver o 1º cegamente.
    com_analise    = None   # último registro COM analise não-vazia (preferido)
    ultimo_qualquer = None  # último registro do processo (fallback p/ mostrar erro)
    total_analises = 0
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            try:
                rec = json.loads(linha)
            except json.JSONDecodeError:
                continue
            np = rec.get("numero_processo")
            if np and _normalizar_numero(np) == numero_norm:
                total_analises += 1
                ultimo_qualquer = rec
                if rec.get("analise"):          # dict não-vazio
                    com_analise = rec

    escolhido = com_analise or ultimo_qualquer
    if escolhido is not None:
        escolhido["_origem_arquivo"] = HISTORIAL_A2
        escolhido["_total_analises"] = total_analises
        return escolhido
    return None


def _buscar_em_historial_classificacoes(pasta: str, numero_norm: str):
    """
    [v2] Procura no historial_classificacoes.jsonl (auditoria V7.2 — TODAS as
    decisões, inclusive NÃO APTO). É esta fonte que faz o NÃO APTO aparecer.

    APPEND-ONLY: um processo pode ter várias linhas (foi reclassificado entre
    lotes). Devolve uma tupla:
        (registro_mais_recente | None, historico_completo: list)
    O 'mais recente' representa o estado vigente; o histórico completo é o que
    dá valor de auditoria (mostra a evolução da decisão).
    """
    # [v3.1] Ler TODOS os aliases conhecidos da auditoria e unir os registros.
    #        Assim funciona tanto com o histórico já existente ("historico_
    #        extracoes.jsonl") quanto com o nome esperado ("historial_
    #        classificacoes.jsonl"), sem perder dados.
    caminhos = [os.path.join(pasta, nome) for nome in HISTORIAL_CLASSIF_ALIASES]
    caminhos = [c for c in caminhos if os.path.exists(c)]
    if not caminhos:
        return None, []

    historico = []
    for caminho in caminhos:
        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    rec = json.loads(linha)
                except json.JSONDecodeError:
                    # Linha corrompida no histórico — pula, não derruba a busca
                    continue
                if _normalizar_numero(rec.get("numero_processo")) == numero_norm:
                    rec["_origem_auditoria"] = os.path.basename(caminho)
                    historico.append(rec)

    if not historico:
        return None, []

    # "estado atual" = registro mais recente. v8.0 grava 'extraido_em' (extração);
    # versões antigas gravavam 'classificado_em' (classificação). Usa os dois.
    recente = max(historico, key=lambda r: r.get("classificado_em") or r.get("extraido_em") or "")
    recente["_origem_arquivo"] = HISTORIAL_CLASSIF
    recente["_total_classificacoes"] = len(historico)
    return recente, historico


def _agente1_desde_historial(rec: dict):
    """
    Reconstrói a visão do Agente 1 a partir do snapshot que o Agente 2 arrasta
    para o historial (campo 'agente1'). Usado quando o processo não está mais
    no JSON do Agente 1 (ex.: o JSON foi sobrescrito por uma corrida posterior).
    Devolve um dict no formato que _mostrar_agente1 espera, ou None.
    """
    a1 = rec.get("agente1")
    if not a1:
        return None
    proc = dict(a1)
    if not proc.get("decisao_agente1"):      # _mostrar_agente1 faz .upper() nisso
        proc["decisao_agente1"] = ""
    proc["_origem_arquivo"] = "historial_agente2.jsonl (dados do Agente 1 arrastados)"
    return proc


# ── Consolidação (v3) ─────────────────────────────────────────────

# Ordem de precedência das fontes para o bloco de IDENTIDADE/ENTIDADES.
# O JSON do Agente 1 é a fonte primária; o snapshot arrastado pelo Agente 2
# e a auditoria são fallbacks quando o JSON do Agente 1 já foi sobrescrito.
def _entidades_consolidadas(proc_a1, rec_a2, auditoria):
    """
    [v3] Junta as três fontes num ÚNICO bloco de identidade/entidades, sem
    repetir. Precedência: JSON Agente 1 → snapshot do Agente 2 → auditoria.
    Cada campo é preenchido pela primeira fonte que o tiver.
    Devolve um dict plano com os campos de identidade do processo.
    """
    ent_a1    = (proc_a1 or {}).get("entidades") or {}
    snap_a2   = (rec_a2 or {}).get("agente1") or {}   # snapshot arrastado pelo A2
    ent_a2    = snap_a2.get("entidades") or {}
    aud       = auditoria or {}

    def pick(*vals):
        for v in vals:
            if v is not None and str(v).strip() != "":
                return v
        return None

    return {
        "numero_processo" : pick(ent_a1.get("numero_processo"),
                                 ent_a2.get("numero_processo"),
                                 aud.get("numero_processo")),
        "nome_executado"  : pick(ent_a1.get("nome_executado"),
                                 ent_a2.get("nome_executado"),
                                 aud.get("nome_executado")),
        "nome_exequente"  : pick(ent_a1.get("nome_exequente"),
                                 ent_a2.get("nome_exequente")),
        "cpf_cnpj"        : pick(ent_a1.get("cpf_cnpj"),
                                 ent_a2.get("cpf_cnpj"),
                                 aud.get("cpf_cnpj")),
        "tipo_tributo"    : pick(ent_a1.get("tipo_tributo"), ent_a2.get("tipo_tributo")),
        "exercicio"       : pick(ent_a1.get("exercicio"), ent_a2.get("exercicio")),
        "numero_cda"      : pick(ent_a1.get("numero_cda"), ent_a2.get("numero_cda")),
        "data_inscricao"  : pick(ent_a1.get("data_inscricao"), ent_a2.get("data_inscricao")),
        "valor_original"  : pick(ent_a1.get("valor_original"), ent_a2.get("valor_original")),
        "valor_atualizado": pick(ent_a1.get("valor_atualizado"), ent_a2.get("valor_atualizado")),
        "vara"            : pick(ent_a1.get("vara"), ent_a2.get("vara")),
        # Estado processual: JSON A1 → snapshot A2 [v0.3-B] → auditoria
        "status_citacao"     : pick((proc_a1 or {}).get("status_citacao"),
                                    snap_a2.get("status_citacao"),
                                    aud.get("status_citacao")),
        "resultado_penhora"  : pick((proc_a1 or {}).get("resultado_penhora"),
                                    snap_a2.get("resultado_penhora"),
                                    aud.get("resultado_penhora")),
        "ultima_movimentacao": pick((proc_a1 or {}).get("ultima_movimentacao"),
                                    snap_a2.get("ultima_movimentacao"),
                                    aud.get("ultima_movimentacao")),
    }


# ── Apresentação ──────────────────────────────────────────────────

def _linha(label, valor):
    """Formata 'Label: valor', mostrando '—' se vazio."""
    v = valor if (valor is not None and str(valor).strip() != "") else _dim("—")
    return f"  {_label(label + ':'):<28} {v}"


def _fmt_decisao(decisao: str):
    """Colore a decisão conforme o tipo: APTO verde, NÃO APTO vermelho, resto amarelo."""
    d = (decisao or "").upper()
    if d == "APTO":
        return _ok(decisao)
    if "NÃO APTO" in d or "NAO APTO" in d:
        return _alerta(decisao)
    return _cor(decisao, "1;33")   # amarelo: INFORMAÇÃO INSUFICIENTE / FORA DE ESCOPO


def _mostrar_agente1(proc: dict):
    ent = proc.get("entidades", {})
    print(_titulo("\n┌─ AGENTE 1 — Triagem e dados extraídos"))
    print(_dim(f"│  fonte: {proc.get('_origem_arquivo','?')}"))
    print("│")
    print(_linha("Nº do processo",  ent.get("numero_processo")))
    print(_linha("Executado",       ent.get("nome_executado")))
    print(_linha("Exequente",       ent.get("nome_exequente")))
    print(_linha("CPF/CNPJ",        ent.get("cpf_cnpj")))
    print(_linha("Tipo de tributo", ent.get("tipo_tributo")))
    print(_linha("Exercício",       ent.get("exercicio")))
    print(_linha("Nº CDA",          ent.get("numero_cda")))
    print(_linha("Data inscrição",  ent.get("data_inscricao")))
    print(_linha("Valor original",  ent.get("valor_original")))
    print(_linha("Valor atualizado",ent.get("valor_atualizado")))
    print(_linha("Vara",            ent.get("vara")))
    print("│")
    decisao = proc.get("decisao_agente1") or ""
    if decisao:  # só versões antigas (v7) trazem decisão
        dec_fmt = _ok(decisao) if decisao.upper() == "APTO" else decisao
        print(_linha("Decisão Agente 1", dec_fmt))
        print(_linha("Motivo",          proc.get("motivo_agente1")))
    # v8.0: sinais processuais no lugar da decisão
    sinais = proc.get("sinais_processuais") or {}
    if sinais.get("extincao"):
        print(_linha("Sinal extinção", sinais.get("extincao")))
    if sinais.get("parcelamento"):
        print(_linha("Sinal parcelamento", sinais.get("parcelamento")))
    if sinais.get("suspensao_art40_lef"):
        print(_linha("Sinal art.40 LEF", sinais.get("suspensao_art40_lef")))
    print(_linha("Status citação",  proc.get("status_citacao")))
    print(_linha("Resultado penhora",proc.get("resultado_penhora")))
    print(_linha("Última movimentação", proc.get("ultima_movimentacao")))
    # OCR: v8.0 aninha em 'ocr'; v7 usava 'confianca_ocr_media' no topo.
    conf = (proc.get("ocr") or {}).get("confianca_media")
    if conf is None:
        conf = proc.get("confianca_ocr_media")
    if conf is not None:
        print(_linha("Confiança OCR", f"{conf}%"))


def _mostrar_auditoria(rec: dict, historico=None):
    """
    [v2] Mostra a classificação de auditoria (fonte 3). O registro é PLANO
    (usa 'decisao'/'motivo', não 'entidades'), então tem apresentação própria.
    É aqui que um processo NÃO APTO fica visível na consulta.
    """
    print(_titulo("\n┌─ AUDITORIA — Triagem/extração (todos os processos)"))
    print(_dim(f"│  fonte: {rec.get('_origem_arquivo','?')}"))
    print("│")
    # v8.0: registro de EXTRAÇÃO (sem 'decisao'). Versões antigas traziam decisão.
    if rec.get("decisao"):
        print(_linha("Decisão",          _fmt_decisao(rec.get("decisao"))))
        print(_linha("Motivo",           rec.get("motivo")))
        print(_linha("Fonte da decisão", rec.get("fonte_decisao")))
    else:
        print(_dim("│  (registro de extração — Agente 1 v8.0 não emite APTO/NÃO APTO)"))
        if rec.get("extincao"):
            print(_linha("Sinal extinção",     rec.get("extincao")))
        if rec.get("parcelamento"):
            print(_linha("Sinal parcelamento", rec.get("parcelamento")))
        if rec.get("suspensao_art40_lef"):
            print(_linha("Sinal art.40 LEF",   rec.get("suspensao_art40_lef")))
    print(_linha("Registrado em",       rec.get("classificado_em") or rec.get("extraido_em")))
    print(_linha("Executado",           rec.get("nome_executado")))
    print(_linha("CPF/CNPJ",            rec.get("cpf_cnpj")))
    print(_linha("Status citação",      rec.get("status_citacao")))
    print(_linha("Resultado penhora",   rec.get("resultado_penhora")))
    print(_linha("Última movimentação", rec.get("ultima_movimentacao")))
    print(_linha("Lote (origem)",       rec.get("id_lote")))

    total = rec.get("_total_classificacoes")
    if historico and total and total > 1:
        print("│")
        print(f"  {_label('Histórico de classificações:')} {_dim(f'({total} registros)')}")
        # do mais antigo ao mais recente, para ler a evolução da decisão
        for h in sorted(historico, key=lambda r: r.get("classificado_em") or ""):
            quando = h.get("classificado_em") or "—"
            dec    = h.get("decisao") or "—"
            mot    = h.get("motivo") or ""
            sufixo = f"  —  {mot}" if mot else ""
            print(f"    • {_dim(quando)}  {_fmt_decisao(dec)}{sufixo}")


def _mostrar_agente2(rec: dict):
    an = rec.get("analise", {}) or {}
    print(_titulo("\n┌─ AGENTE 2 — Análise jurídico-fiscal"))
    print(_dim(f"│  fonte: {rec.get('_origem_arquivo','?')}"))
    print("│")

    total = rec.get("_total_analises")
    if total and total > 1:
        print(_linha("Vezes analisado", total))

    # [FIX] Se o registro é de ERRO (analisar_processo falhou), mostrar o
    #       motivo em vez de deixar tudo em "—" sem explicação.
    erro = rec.get("erro")
    if erro:
        print(_linha("Status", _alerta("FALHA NA ANÁLISE")))
        print(_linha("Erro",   _alerta(erro)))
        print(_dim("│  (o Agente 2 não conseguiu analisar este processo;"))
        print(_dim("│   os campos abaixo ficam vazios por isso)"))
        print("│")

    # [7.x] NÃO APTO: mostrar a triagem do Agente 1, não uma priorização.
    status_triagem = an.get("status_triagem")
    if status_triagem and status_triagem.upper() != "APTO":
        print(_linha("Triagem Agente 1", _cor(status_triagem, "1;33")))
        print(_dim("│  (processo NÃO APTO na triagem — sem análise jurídico-fiscal no Agente 2)"))
        print("│")

    prioridade = an.get("prioridade", "")
    prio_fmt = {
        "ALTA":  _alerta("ALTA"),
        "MEDIA": _cor("MÉDIA", "1;33"),
        "BAIXA": _ok("BAIXA"),
    }.get(prioridade, prioridade)
    print(_linha("Prioridade",       prio_fmt))
    print(_linha("Ação recomendada", an.get("acao_recomendada")))
    print(_linha("Justificativa",    an.get("justificativa")))
    alerta = an.get("alerta_prescricao")
    print(_linha("Alerta prescrição", _alerta("SIM") if alerta else "não"))
    obs = an.get("observacoes", [])
    if obs:
        print(f"  {_label('Observações:')}")
        for o in obs:
            print(f"    • {o}")
    print(_linha("Processado em",    rec.get("processado_em")))
    print(_linha("Origem (lote)",    rec.get("origem_lote")))


def _mostrar_identidade(ident: dict):
    """
    [v3] Bloco ÚNICO com a identidade e os dados do processo. Substitui a
    repetição de 'entidades' que antes aparecia em Agente 1, Agente 2 e
    Auditoria. Aqui cada campo é mostrado uma só vez.
    """
    print(_titulo("\n┌─ PROCESSO — identificação e dados"))
    print("│")
    print(_linha("Nº do processo",   ident.get("numero_processo")))
    print(_linha("Executado",        ident.get("nome_executado")))
    print(_linha("Exequente",        ident.get("nome_exequente")))
    print(_linha("CPF/CNPJ",         ident.get("cpf_cnpj")))
    print(_linha("Tipo de tributo",  ident.get("tipo_tributo")))
    print(_linha("Exercício",        ident.get("exercicio")))
    print(_linha("Nº CDA",           ident.get("numero_cda")))
    print(_linha("Data inscrição",   ident.get("data_inscricao")))
    print(_linha("Valor original",   ident.get("valor_original")))
    print(_linha("Valor atualizado", ident.get("valor_atualizado")))
    print(_linha("Vara",             ident.get("vara")))
    print("│")
    print(_linha("Status citação",      ident.get("status_citacao")))
    print(_linha("Resultado penhora",   ident.get("resultado_penhora")))
    print(_linha("Última movimentação", ident.get("ultima_movimentacao")))


def _mostrar_agente1_triagem(proc_a1, auditoria):
    """
    [v3] Só o que é PRÓPRIO da triagem do Agente 1 — decisão (versões v7),
    sinais processuais (v8) e confiança OCR. NÃO repete entidades nem estado
    processual (isso já saiu no bloco de identidade).
    """
    proc = proc_a1 or {}
    aud  = auditoria or {}

    # Fonte dos dados de triagem: preferir o JSON/snapshot do Agente 1; se não
    # houver, cair na auditoria (registro de extração v8).
    decisao = (proc.get("decisao_agente1") or aud.get("decisao") or "").strip()
    motivo  = proc.get("motivo_agente1") or aud.get("motivo")
    sinais  = proc.get("sinais_processuais") or {
        "extincao"           : aud.get("extincao"),
        "parcelamento"       : aud.get("parcelamento"),
        "suspensao_art40_lef": aud.get("suspensao_art40_lef"),
    }
    conf = (proc.get("ocr") or {}).get("confianca_media")
    if conf is None:
        conf = proc.get("confianca_ocr_media")

    tem_algo = decisao or any(sinais.values()) or conf is not None
    if not tem_algo:
        return

    print(_titulo("\n┌─ AGENTE 1 — Triagem"))
    print("│")
    if decisao:
        print(_linha("Decisão Agente 1", _fmt_decisao(decisao)))
        if motivo:
            print(_linha("Motivo", motivo))
    else:
        print(_dim("│  (registro de extração v8 — Agente 1 não emite APTO/NÃO APTO)"))
    if sinais.get("extincao"):
        print(_linha("Sinal extinção", sinais.get("extincao")))
    if sinais.get("parcelamento"):
        print(_linha("Sinal parcelamento", sinais.get("parcelamento")))
    if sinais.get("suspensao_art40_lef"):
        print(_linha("Sinal art.40 LEF", sinais.get("suspensao_art40_lef")))
    if conf is not None:
        print(_linha("Confiança OCR", f"{conf}%"))


def _mostrar_historico_auditoria(historico: list):
    """
    [v3] Histórico COMPLETO da auditoria (append-only). Mostra a evolução das
    decisões/extrações do mais antigo ao mais recente. Não repete entidades —
    só o eixo temporal (quando, decisão/estado, motivo).
    """
    def _quando(h):
        return h.get("classificado_em") or h.get("extraido_em") or ""

    ordenado = sorted(historico, key=_quando)
    print(_titulo("\n┌─ AUDITORIA — histórico de registros"))
    print(_dim(f"│  {len(ordenado)} registro(s) — append-only, do mais antigo ao mais recente"))
    print("│")
    for h in ordenado:
        quando = _quando(h) or "—"
        dec    = h.get("decisao")
        if dec:
            estado = _fmt_decisao(dec)
            mot    = h.get("motivo") or ""
            sufixo = f"  —  {mot}" if mot else ""
        else:
            # registro de extração v8 (sem decisão): mostrar sinais se houver
            sinais = [k for k in ("extincao","parcelamento","suspensao_art40_lef") if h.get(k)]
            estado = _dim("extração") + (f"  [{', '.join(sinais)}]" if sinais else "")
            sufixo = ""
        lote = h.get("id_lote") or ""
        print(f"    • {_dim(quando)}  {estado}{sufixo}  {_dim(lote)}")


# ── Fluxo principal ───────────────────────────────────────────────

def buscar_dados(
    numero: str,
    pasta: str = PASTA_JSON,
    filtro=None,
):
    """
    Busca un proceso y devuelve los datos estructurados para ser consumidos
    por webapp.py.

    Args:
        numero: número CNJ del proceso.
        pasta: carpeta donde están los JSON.
        filtro: función opcional que recibe un registro/objeto y devuelve
                True si pertenece al consumidor solicitado.

    Returns:
        {
            "agente1": <registro o None>,
            "agente2": <registro o None>,
            "auditoria": <registro más reciente o None>,   # [v2]
            "auditoria_historico": <lista de registros>,    # [v2]
        }

    Esta función también conserva compatibilidad con el uso anterior desde
    consola: main() se encarga de presentar los resultados.
    """
    numero_norm = _normalizar_numero(numero)

    if not numero_norm:
        return {"agente1": None, "agente2": None, "auditoria": None, "auditoria_historico": []}

    if not os.path.isdir(pasta):
        return {"agente1": None, "agente2": None, "auditoria": None, "auditoria_historico": []}

    # Las funciones de búsqueda originales reciben la carpeta y el número.
    # El filtro se aplica aquí, cuando es posible, para mantener compatible
    # la interfaz usada por webapp.py.
    proc_a1 = _buscar_em_json_agente1(pasta, numero_norm)
    rec_a2  = _buscar_em_historial_agente2(pasta, numero_norm)
    audit_recente, audit_historico = _buscar_em_historial_classificacoes(pasta, numero_norm)

    if filtro is not None:
        proc_a1       = _aplicar_filtro(proc_a1, filtro)
        rec_a2        = _aplicar_filtro(rec_a2, filtro)
        audit_recente = _aplicar_filtro(audit_recente, filtro)
        # Se o filtro rejeitou o registro mais recente, some com o histórico
        # também — todas as linhas são do mesmo processo/consumidor.
        if audit_recente is None:
            audit_historico = []

    return {
        "agente1": proc_a1,
        "agente2": rec_a2,
        "auditoria": audit_recente,
        "auditoria_historico": audit_historico,
    }


def _aplicar_filtro(dados, filtro):
    """
    Aplica el filtro de consumidor de forma tolerante.

    Si no hay datos, devuelve None. Si el filtro acepta el objeto encontrado,
    lo conserva; si lo rechaza, devuelve None.

    Se mantiene separado para no modificar las funciones de búsqueda existentes.
    """
    if dados is None or filtro is None:
        return dados

    try:
        return dados if filtro(dados) else None
    except (TypeError, AttributeError, KeyError):
        # Algunos filtros pueden estar diseñados para recibir un lote/estructura
        # distinta. En ese caso no descartamos silenciosamente datos válidos.
        return dados


def main():
    parser = argparse.ArgumentParser(
        description="Busca um processo por número CNJ nos JSON dos agentes (v2 — com auditoria)."
    )
    parser.add_argument(
        "numero", nargs="?",
        help="Número do processo (CNJ). Se omitido, o script pergunta."
    )
    parser.add_argument(
        "--pasta", default=PASTA_JSON,
        help="Pasta onde estão os arquivos JSON (padrão: ./JSON)"
    )
    args = parser.parse_args()

    numero = args.numero
    if not numero:
        try:
            numero = input("Digite o número do processo (CNJ): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return

    dados = buscar_dados(numero, args.pasta)
    proc_a1        = dados.get("agente1")
    rec_a2         = dados.get("agente2")
    auditoria      = dados.get("auditoria")
    auditoria_hist = dados.get("auditoria_historico", [])

    if not proc_a1 and not rec_a2 and not auditoria:
        print(_alerta(f"\nProcesso não encontrado: {numero}"))
        print(_dim("Possíveis motivos:"))
        print(_dim("  • O número está errado ou com dígitos faltando"))
        print(_dim("  • O lote foi processado antes da V7.2 (sem historial_classificacoes.jsonl)"))
        print(_dim("  • Os agentes ainda não foram executados sobre esse lote"))
        return

    print(_ok(f"\n═══ Processo {numero} encontrado ═══"))

    # [v3] Reconstruir a visão do Agente 1 a partir do snapshot, se o JSON
    #      do Agente 1 já não estiver em disco.
    if not proc_a1 and rec_a2:
        proc_a1 = _agente1_desde_historial(rec_a2)

    # [v3] BLOCO ÚNICO de identidade/entidades — consolidado das três fontes.
    #      Cada dado aparece UMA vez; nada de entidades repetidas por agente.
    ident = _entidades_consolidadas(proc_a1, rec_a2, auditoria)
    _mostrar_identidade(ident)

    # Agente 1 — só o que é PRÓPRIO da triagem (sem repetir entidades)
    _mostrar_agente1_triagem(proc_a1, auditoria)

    # Agente 2 — análise jurídico-fiscal (só o que é próprio da análise)
    if rec_a2:
        _mostrar_agente2(rec_a2)
    else:
        dec = ((auditoria or {}).get("decisao") or "").upper()
        if dec and "APTO" in dec and "NÃO" not in dec and "NAO" not in dec:
            print(_dim("\n(sem análise do Agente 2 — ainda não processado)"))
        elif dec:
            print(_dim("\n(sem análise do Agente 2 — processo não é APTO na triagem, esperado)"))
        else:
            print(_dim("\n(sem análise do Agente 2)"))

    # Auditoria — histórico COMPLETO sempre (append-only, valor de auditoria)
    if auditoria_hist:
        _mostrar_historico_auditoria(auditoria_hist)

    print()


if __name__ == "__main__":
    main()