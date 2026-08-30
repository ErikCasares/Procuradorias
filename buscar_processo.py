"""
BUSCAR PROCESSO v2 — consulta por número CNJ
HERA Tecnologia / PGMS

Lê os arquivos JSON da pasta JSON/ e mostra as informações de um processo
específico, buscando pelo número do processo (CNJ).

Procura em TRÊS fontes:
  1. JSON do Agente 1  → resultados_*_agente2.json     (dados extraídos — TODOS os processos)
  2. Histórico Agente 2 → historial_agente2.jsonl       (análise e priorização — TODOS os processos)
  3. Auditoria         → historico_extracoes.jsonl      (registro de extração — TODOS os processos)

  A partir da v8.0 não há triagem APTO/NÃO APTO: as fontes 1 e 2 contêm todos
  os processos do lote. A fonte 3 permanece como trilha de auditoria.

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
HISTORICO_EXTRACOES = "historico_extracoes.jsonl"   # auditoria — TODAS as extrações do Agente 1
# Nome usado até a v7.2. Mantido só para ler históricos antigos já em disco.
HISTORICO_EXTRACOES_LEGADO = "historial_classificacoes.jsonl"

# [v3] Contrato de saída (código de retorno) — consumido por webapp.py.
#   0 = processo encontrado (relatório vai no stdout)
#   EXIT_NAO_ENCONTRADO = número válido, mas ausente em TODAS as fontes
#   qualquer outro código = falha real do script (a API traduz em 500)
# Antes a API inferia "não encontrado" farejando a prosa do stdout; isso colidia
# com valores de dado como "Citação não encontrado"/"Penhora não encontrado" e
# devolvia relatório de sucesso com HTTP 404. O código de saída é inequívoco e
# não depende do texto (nem carrega dado do contribuinte na decisão).
EXIT_NAO_ENCONTRADO = 3


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
    # Todos os *_agente2.json, EXCETO os *_agente2_resultado.json (do Agente 2).
    #
    # Da mais NOVA para a mais velha. O nome do lote começa com a data, então a
    # ordem alfabética é cronológica; percorrendo ao contrário, o primeiro achado
    # é o mais recente. Antes o laço ia do mais antigo e devolvia a cópia velha —
    # o relatório mostrava a extração de meses atrás ao lado da priorização de
    # hoje, que vem do histórico e é sempre a mais recente.
    padrao = os.path.join(pasta, f"*{SUFIJO_A1}")
    for caminho in sorted(glob.glob(padrao), reverse=True):
        if caminho.endswith("_resultado.json"):
            continue
        try:
            with open(caminho, encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Arquivo corrompido ou ilegível: pula. Um byte ruim num lote não
            # pode derrubar a consulta de todos os outros processos.
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

    APPEND-ONLY: um processo pode ter várias linhas (foi reanalisado entre
    lotes). Devolve uma tupla:
        (registro_escolhido | None, historico_completo: list)

    O 'escolhido' é o estado vigente para exibição em destaque; o histórico
    completo são TODAS as linhas do processo (na ordem lida), para mostrar a
    evolução das análises — mesmo padrão de _buscar_em_historico_extracoes.
    """
    caminho = os.path.join(pasta, HISTORIAL_A2)
    if not os.path.exists(caminho):
        return None, []

    # [FIX] O historial é APPEND-ONLY: um processo pode ter várias linhas.
    #       Ficar com o registro MAIS RECENTE que tenha análise; se nenhuma
    #       linha tiver análise (ex.: só houve erro), cair no último registro
    #       para poder mostrar o motivo do erro. Nunca devolver o 1º cegamente.
    com_analise    = None   # último registro COM analise não-vazia (preferido)
    ultimo_qualquer = None  # último registro do processo (fallback p/ mostrar erro)
    total_analises = 0
    historico      = []     # [hist] todas as linhas do processo, em ordem de leitura
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
                historico.append(rec)           # [hist]
                if rec.get("analise"):          # dict não-vazio
                    com_analise = rec

    escolhido = com_analise or ultimo_qualquer
    if escolhido is not None:
        escolhido["_origem_arquivo"] = HISTORIAL_A2
        escolhido["_total_analises"] = total_analises
        return escolhido, historico
    return None, []


def _buscar_em_historico_extracoes(pasta: str, numero_norm: str):
    """
    Procura no historico_extracoes.jsonl — a trilha de auditoria que o Agente 1
    grava para TODOS os processos, tenham eles entrado ou não no resultado.

    Lê também o nome antigo (historial_classificacoes.jsonl) para que um
    histórico já gravado por uma versão anterior continue aparecendo.

    APPEND-ONLY: um processo pode ter várias linhas (foi reprocessado entre
    lotes). Devolve uma tupla:
        (registro_mais_recente | None, historico_completo: list)
    O 'mais recente' representa o estado vigente; o histórico completo é o que
    dá valor de auditoria (mostra a evolução do processo entre lotes).
    """
    historico = []
    lido_de = None
    for nome in (HISTORICO_EXTRACOES, HISTORICO_EXTRACOES_LEGADO):
        caminho = os.path.join(pasta, nome)
        if not os.path.exists(caminho):
            continue
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
                    historico.append(rec)
                    lido_de = lido_de or nome

    if not historico:
        return None, []

    # "estado atual" = registro mais recente. A v8.0 grava 'extraido_em';
    # versões antigas gravavam 'classificado_em'. Usa os dois.
    recente = max(historico, key=lambda r: r.get("classificado_em") or r.get("extraido_em") or "")
    recente["_origem_arquivo"] = lido_de or HISTORICO_EXTRACOES
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


def _fmt_prioridade(prioridade: str):
    """Colore a prioridade: ALTA vermelho, MÉDIA amarelo, BAIXA verde."""
    return {
        "ALTA":  _alerta("ALTA"),
        "MEDIA": _cor("MÉDIA", "1;33"),
        "BAIXA": _ok("BAIXA"),
    }.get(prioridade, prioridade or "")


def _valor_hist(v):
    """
    Normaliza um valor para comparação/exibição no histórico.
    Devolve None para vazios (para não poluir a linha do tempo) e converte
    booleanos em texto ('SIM'/'não'), que é como o procurador lê.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "SIM" if v else "não"
    s = str(v).strip()
    return s or None


def _mostrar_evolucao(historico, campos, key_tempo, headline=None, indent="      "):
    """
    Imprime, em ordem cronológica, o que MUDOU entre um ponto e o anterior —
    a ideia é dar ao procurador "o que aconteceu nesse tempo" sem ruído.

    historico : lista de registros (dicts) do mesmo processo
    campos    : lista de (label, extrator(rec) -> valor) — os dados a acompanhar
    key_tempo : extrator(rec) -> str usado para ordenar e rotular cada ponto
    headline  : opcional, extrator(rec) -> texto já colorido, mostrado ao lado
                da data (ex.: a decisão/prioridade daquele ponto)

    Semântica de "último valor conhecido": se um registro NÃO traz um campo
    (ex.: uma corrida que só registrou erro), esse campo é tratado como
    inalterado — mantém-se o último valor visto. Só se reporta mudança quando
    chega um valor novo, não-vazio, diferente do anterior. Isso evita falsos
    "sumiu e voltou" e mostra a evolução real do processo.

    Regras:
      • um campo só entra se tiver valor em ALGUM ponto (evita parede de "—");
      • o 1º ponto mostra o estado inicial (campos preenchidos);
      • os pontos seguintes mostram só os campos que mudaram de fato.
    """
    ordenado = sorted(historico, key=lambda r: key_tempo(r) or "")

    # descarta campos que nunca têm valor em nenhum registro
    campos_uteis = [
        (label, ext) for label, ext in campos
        if any(_valor_hist(ext(r)) is not None for r in ordenado)
    ]

    conhecido = {}      # último valor NÃO-vazio visto por campo
    primeiro  = True
    for r in ordenado:
        quando = key_tempo(r) or "—"
        cabeca = (headline(r) if headline else "") or ""
        bullet = indent[:-2] + "• "
        atual  = {label: _valor_hist(ext(r)) for label, ext in campos_uteis}

        if primeiro:
            print(f"{bullet}{_dim(quando)}  {cabeca}  {_dim('(estado inicial)')}".rstrip())
            for label, _ in campos_uteis:
                if atual[label] is not None:
                    print(f"{indent}{_label(label + ':')} {atual[label]}")
                    conhecido[label] = atual[label]
            primeiro = False
            continue

        print(f"{bullet}{_dim(quando)}  {cabeca}".rstrip())
        mudou = False
        for label, _ in campos_uteis:
            novo = atual[label]
            if novo is None:            # ausente neste registro → mantém o conhecido
                continue
            antigo = conhecido.get(label)
            if novo != antigo:
                de_txt = antigo if antigo is not None else _dim("—")
                print(f"{indent}{_label(label + ':')} {de_txt} {_dim('→')} {novo}")
                conhecido[label] = novo
                mudou = True
        if not mudou:
            print(f"{indent}{_dim('sem mudanças nos campos acompanhados')}")


def _a1_snapshot(rec, campo):
    """
    Lê um campo do snapshot do Agente 1 arrastado para o registro do Agente 2.
    Procura primeiro em 'entidades' (onde ficam valores/CDA etc.) e depois no
    topo do snapshot (onde ficam status_citacao/penhora/movimentação).
    """
    a1  = rec.get("agente1") or {}
    ent = a1.get("entidades") or {}
    return ent.get(campo) if ent.get(campo) is not None else a1.get(campo)


def _mostrar_agente1(proc: dict):
    ent = proc.get("entidades", {})
    # [Fase 1] página de origem por entidade (se disponível no JSON)
    _ent_evid = ((proc.get("evidencias") or {}).get("entidades") or {})
    def _v(valor, campo):
        b = _ent_evid.get(campo) or {}
        p = b.get("encontrado_em_pagina")
        if valor is None or p is None:
            return valor
        via = " OCR" if b.get("via_ocr") else ""
        return f"{valor}  (pág. {p}{via})"
    print(_titulo("\n┌─ AGENTE 1 — Triagem e dados extraídos"))
    print(_dim(f"│  fonte: {proc.get('_origem_arquivo','?')}"))
    print("│")
    print(_linha("Arquivo",  proc.get("arquivo")))    
    print(_linha("Nº do processo",  _v(ent.get("numero_processo"), "numero_processo")))
    print(_linha("Executado",       _v(ent.get("nome_executado"), "nome_executado")))
    print(_linha("Exequente",       _v(ent.get("nome_exequente"), "nome_exequente")))
    print(_linha("CPF/CNPJ",        _v(ent.get("cpf_cnpj"), "cpf_cnpj")))
    print(_linha("Tipo de tributo", _v(ent.get("tipo_tributo"), "tipo_tributo")))
    print(_linha("Exercício",       _v(ent.get("exercicio"), "exercicio")))
    print(_linha("Nº CDA",          _v(ent.get("numero_cda"), "numero_cda")))
    print(_linha("Data inscrição",  _v(ent.get("data_inscricao"), "data_inscricao")))
    print(_linha("Valor original",  _v(ent.get("valor_original"), "valor_original")))
    print(_linha("Valor atualizado",_v(ent.get("valor_atualizado"), "valor_atualizado")))
    print(_linha("Vara",            _v(ent.get("vara"), "vara")))
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
    _evid = proc.get("evidencias") or {}
    _bc = _evid.get("citacao") or {}
    if _bc.get("encontrado_em_pagina"):
        print(_linha("  ↳ citação na", f"pág. {_bc['encontrado_em_pagina']}" + (" (OCR)" if _bc.get('via_ocr') else "")))
    print(_linha("Resultado penhora",proc.get("resultado_penhora")))
    _bp = _evid.get("penhora") or {}
    if _bp.get("encontrado_em_pagina"):
        print(_linha("  ↳ penhora na", f"pág. {_bp['encontrado_em_pagina']}" + (" (OCR)" if _bp.get('via_ocr') else "")))
    print(_linha("Última movimentação", proc.get("ultima_movimentacao")))
    # OCR: v8.0 aninha em 'ocr'; v7 usava 'confianca_ocr_media' no topo.
    conf = (proc.get("ocr") or {}).get("confianca_media")
    if conf is None:
        conf = proc.get("confianca_ocr_media")
    if conf is not None:
        print(_linha("Confiança OCR", f"{conf}%"))
    # [Fase 4] Conflitos de merge — mesmo PDF deu valor diferente do guardado.
    conflitos = proc.get("conflitos") or []
    if conflitos:
        print("│")
        print(_linha("⚠ CONFLITOS", _alerta(f"{len(conflitos)} campo(s) a revisar")))
        for c in conflitos:
            print(_linha(f"  • {c.get('campo')}",
                         f"guardado: {c.get('valor_anterior')!r}  ×  novo: {c.get('valor_novo')!r}"
                         f"  ({c.get('motivo')})"))


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
        print(_dim("  (cada ponto mostra o que mudou desde o anterior)"))
        # campos que costumam evoluir entre lotes — o que interessa ao procurador
        campos = [
            ("Motivo",              lambda r: r.get("motivo")),
            ("Status citação",      lambda r: r.get("status_citacao")),
            ("Resultado penhora",   lambda r: r.get("resultado_penhora")),
            ("Última movimentação", lambda r: r.get("ultima_movimentacao")),
            ("Sinal extinção",      lambda r: r.get("extincao")),
            ("Sinal parcelamento",  lambda r: r.get("parcelamento")),
            ("Sinal art.40 LEF",    lambda r: r.get("suspensao_art40_lef")),
            ("Lote (origem)",       lambda r: r.get("id_lote")),
        ]
        _mostrar_evolucao(
            historico,
            campos,
            key_tempo=lambda r: r.get("classificado_em") or r.get("extraido_em"),
            headline=lambda r: _fmt_decisao(r.get("decisao")) if r.get("decisao") else "",
        )


def _mostrar_agente2(rec: dict, historico=None):
    an = rec.get("analise", {}) or {}

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
    print(_linha("Prioridade",       _fmt_prioridade(an.get("prioridade", ""))))
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

    # [hist] Histórico de análises (append-only): quando o processo foi
    #        analisado mais de uma vez, mostra a EVOLUÇÃO — não só a prioridade,
    #        mas os dados que mudaram (citação, penhora, movimentação, valores),
    #        para o procurador enxergar o que aconteceu entre um lote e outro.
    if historico and total and total > 1:
        print("│")
        print(f"  {_label('Histórico de análises:')} {_dim(f'({total} registros)')}")
        print(_dim("  (cada ponto mostra o que mudou desde o anterior)"))
        campos = [
            ("Ação recomendada",    lambda r: (r.get("analise") or {}).get("acao_recomendada")),
            ("Justificativa",       lambda r: (r.get("analise") or {}).get("justificativa")),
            ("Alerta prescrição",   lambda r: (r.get("analise") or {}).get("alerta_prescricao")),
            # dados do processo (snapshot do Agente 1) que evoluem no tempo:
            ("Valor atualizado",    lambda r: _a1_snapshot(r, "valor_atualizado")),
            ("Status citação",      lambda r: _a1_snapshot(r, "status_citacao")),
            ("Resultado penhora",   lambda r: _a1_snapshot(r, "resultado_penhora")),
            ("Última movimentação", lambda r: _a1_snapshot(r, "ultima_movimentacao")),
            ("Origem (lote)",       lambda r: r.get("origem_lote")),
        ]
        _mostrar_evolucao(
            historico,
            campos,
            key_tempo=lambda r: r.get("processado_em"),
            headline=lambda r: (_alerta("FALHA NA ANÁLISE") if r.get("erro")
                                else _fmt_prioridade((r.get("analise") or {}).get("prioridade") or "—")),
        )


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
            "agente2_historico": <lista de registros>,      # [hist] evolución
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
    rec_a2, hist_a2 = _buscar_em_historial_agente2(pasta, numero_norm)
    audit_recente, audit_historico = _buscar_em_historico_extracoes(pasta, numero_norm)

    if filtro is not None:
        proc_a1       = _aplicar_filtro(proc_a1, filtro)
        rec_a2        = _aplicar_filtro(rec_a2, filtro)
        audit_recente = _aplicar_filtro(audit_recente, filtro)
        # Se o filtro rejeitou o registro vigente, some com o histórico também —
        # todas as linhas são do mesmo processo/consumidor.
        if rec_a2 is None:
            hist_a2 = []
        if audit_recente is None:
            audit_historico = []

    return {
        "agente1": proc_a1,
        "agente2": rec_a2,
        "agente2_historico": hist_a2,        # [hist] evolução das análises
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
        # Estrutura no mesmo formato da API, para exibição amigável (e reuso).
        nao_encontrado = {
            "mensagem": f"Não encontramos nenhum processo com o número {numero}.",
            "possiveis_motivos": [
                "O número pode estar incompleto ou ter algum dígito trocado",
                "O lote pode ter sido processado antes da versão 7.2",
                "Os agentes ainda não foram executados sobre esse lote",
            ],
            "sugestao": "Verifique o número e tente novamente.",
        }

        print(_alerta(f"\n{nao_encontrado['mensagem']}"))
        print(_dim("\nIsso pode acontecer por alguns motivos:"))
        for motivo in nao_encontrado["possiveis_motivos"]:
            print(_dim(f"  • {motivo}"))
        print(_dim(f"\n{nao_encontrado['sugestao']}"))
        # [v3] Sinaliza ausência pelo código de saída — a API depende disto para
        #      responder 404 sem farejar o texto (ver EXIT_NAO_ENCONTRADO).
        sys.exit(EXIT_NAO_ENCONTRADO)

    print(_ok(f"\n═══ Processo {numero} encontrado ═══"))

    # Agente 1 — dados extraídos (v8.0: contém TODOS os processos; pode faltar
    # se o JSON foi sobrescrito, aí reconstrói-se do snapshot do histórico do A2)
    if proc_a1:
        _mostrar_agente1(proc_a1)
    else:
        proc_a1_hist = _agente1_desde_historial(rec_a2) if rec_a2 else None
        if proc_a1_hist:
            _mostrar_agente1(proc_a1_hist)
        elif not auditoria:
            print(_dim("\n(sem registro no JSON do Agente 1)"))

    # [v2] Auditoria — é aqui que o NÃO APTO fica visível e, com o histórico,
    #      é onde sai a EVOLUÇÃO das classificações entre lotes.
    if auditoria:
        _mostrar_auditoria(auditoria, auditoria_hist)

    # Agente 2 — análise jurídico-fiscal (agora roda para TODOS os processos)
    if rec_a2:
        _mostrar_agente2(rec_a2, dados.get("agente2_historico"))
    else:
        # v0.3: sem triagem APTO/NÃO APTO. Todo processo do Agente 1 deveria
        # receber análise do Agente 2. Se não há registro no historial, o
        # Agente 2 simplesmente ainda não processou este lote — ou o
        # historial_agente2.jsonl não está na pasta consultada.
        print(_dim("\n(sem análise do Agente 2 — este processo ainda não foi processado"))
        print(_dim(f" pelo Agente 2, ou o {HISTORIAL_A2} não está em {args.pasta})"))

    print()


if __name__ == "__main__":
    main()