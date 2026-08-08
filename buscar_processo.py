"""
BUSCAR PROCESSO — consulta por número CNJ
HERA Tecnologia / PGMS
 
Lê os arquivos JSON da pasta JSON/ e mostra as informações de um processo
específico, buscando pelo número do processo (CNJ).
 
Procura em duas fontes:
  1. JSON do Agente 1  → resultados_*_agente2.json  (dados extraídos + decisão)
  2. Histórico Agente 2 → historial_agente2.jsonl    (análise e priorização)
 
Uso:
    # Modo interativo — pergunta o número e mostra
    python buscar_processo.py
 
    # Modo direto — passa o número como argumento
    python buscar_processo.py 0752821-68.2013.8.05.0001
 
    # Apontar para outra pasta JSON
    python buscar_processo.py --pasta /caminho/JSON 0752821-68.2013.8.05.0001
 
Observação:
    A busca é flexível quanto à formatação do número: ignora pontos, traços e
    espaços. Então "0752821-68.2013.8.05.0001" e "07528216820138050001"
    encontram o mesmo processo.
"""
 
import os
import re
import sys
import json
import glob
import argparse
 
 
# ── Configuração ──────────────────────────────────────────────────
CURRENT_DIR   = os.path.dirname(os.path.abspath(__file__))
PASTA_JSON    = os.path.join(CURRENT_DIR, "JSON")
SUFIJO_A1     = "_agente2.json"        # JSON do Agente 1 (não confundir com _resultado)
HISTORIAL_A2  = "historial_agente2.jsonl"
 
 
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
    decisao = proc.get("decisao_agente1", "")
    dec_fmt = _ok(decisao) if decisao.upper() == "APTO" else decisao
    print(_linha("Decisão Agente 1", dec_fmt))
    print(_linha("Motivo",          proc.get("motivo_agente1")))
    print(_linha("Status citação",  proc.get("status_citacao")))
    print(_linha("Resultado penhora",proc.get("resultado_penhora")))
    print(_linha("Última movimentação", proc.get("ultima_movimentacao")))
    conf = proc.get("confianca_ocr_media")
    if conf is not None:
        print(_linha("Confiança OCR", f"{conf}%"))
 
 
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
        }

    Esta función también conserva compatibilidad con el uso anterior desde
    consola: main() se encarga de presentar los resultados.
    """
    numero_norm = _normalizar_numero(numero)

    if not numero_norm:
        return {"agente1": None, "agente2": None}

    if not os.path.isdir(pasta):
        return {"agente1": None, "agente2": None}

    # Las funciones de búsqueda originales reciben la carpeta y el número.
    # El filtro se aplica aquí, cuando es posible, para mantener compatible
    # la interfaz usada por webapp.py.
    proc_a1 = _buscar_em_json_agente1(pasta, numero_norm)
    rec_a2 = _buscar_em_historial_agente2(pasta, numero_norm)

    if filtro is not None:
        proc_a1 = _aplicar_filtro(proc_a1, filtro)
        rec_a2 = _aplicar_filtro(rec_a2, filtro)

    return {
        "agente1": proc_a1,
        "agente2": rec_a2,
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
        description="Busca um processo por número CNJ nos JSON dos agentes."
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
    proc_a1 = dados.get("agente1")
    rec_a2 = dados.get("agente2")

    if not proc_a1 and not rec_a2:
        print(_alerta(f"\nProcesso não encontrado: {numero}"))
        print(_dim("Possíveis motivos:"))
        print(_dim("  • O número está errado ou com dígitos faltando"))
        print(_dim("  • O processo foi marcado NÃO APTO (não entra no JSON do Agente 1)"))
        print(_dim("  • Os agentes ainda não foram executados sobre esse lote"))
        return

    print(_ok(f"\n═══ Processo {numero} encontrado ═══"))

    if proc_a1:
        _mostrar_agente1(proc_a1)
    else:
        proc_a1_hist = _agente1_desde_historial(rec_a2) if rec_a2 else None
        if proc_a1_hist:
            _mostrar_agente1(proc_a1_hist)
        else:
            print(_dim("\n(sem registro no JSON do Agente 1 — pode ter sido NÃO APTO)"))

    if rec_a2:
        _mostrar_agente2(rec_a2)
    else:
        print(_dim("\n(sem análise do Agente 2 — o Agente 2 ainda não processou este processo)"))

    print()


if __name__ == "__main__":
    main()
