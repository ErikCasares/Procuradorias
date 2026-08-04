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
 
def _buscar_em_json_agente1(pasta: str, numero_norm: str, filtro_arquivo=None):
    """
    Procura o processo nos arquivos *_agente2.json do Agente 1.
    Retorna o dict do processo (com decisão e entidades) ou None.

    filtro_arquivo — função que recebe o nome do arquivo e devolve se ele pode
    ser lido. É o que a API usa para restringir a busca aos lotes do próprio
    consumidor; na linha de comando fica None e lê tudo.
    """
    # Todos os *_agente2.json, EXCETO os *_agente2_resultado.json (que são do Agente 2)
    padrao = os.path.join(pasta, f"*{SUFIJO_A1}")
    for caminho in sorted(glob.glob(padrao)):
        if caminho.endswith("_resultado.json"):
            continue
        if filtro_arquivo and not filtro_arquivo(os.path.basename(caminho)):
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
 
 
def _buscar_em_historial_agente2(pasta: str, numero_norm: str, filtro_arquivo=None):
    """
    Procura o processo no historial_agente2.jsonl do Agente 2.
    Retorna o dict do registro (com análise) ou None.

    filtro_arquivo recebe aqui o 'origem_lote' do registro — o nome do JSON do
    Agente 1 que originou a análise —, para o mesmo recorte por lote da busca
    no Agente 1.
    """
    caminho = os.path.join(pasta, HISTORIAL_A2)
    if not os.path.exists(caminho):
        return None

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
            if not np or _normalizar_numero(np) != numero_norm:
                continue
            if filtro_arquivo and not filtro_arquivo(rec.get("origem_lote") or ""):
                continue
            rec["_origem_arquivo"] = HISTORIAL_A2
            return rec
    return None
 
 
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
    an = rec.get("analise", {})
    print(_titulo("\n┌─ AGENTE 2 — Análise jurídico-fiscal"))
    print(_dim(f"│  fonte: {rec.get('_origem_arquivo','?')}"))
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

def buscar_dados(numero: str, pasta: str = PASTA_JSON, filtro_arquivo=None) -> dict:
    """
    O núcleo da busca, sem imprimir nada — é o que a API consome.

    Devolve sempre o mesmo formato; None em 'agente1'/'agente2' significa que
    aquela fonte não tem o processo:

        {
          "numero"            : "0752821-68.2013.8.05.0001",
          "numero_normalizado": "07528216820138050001",
          "agente1"           : {...} | None,
          "agente2"           : {...} | None,
        }

    Número vazio ou pasta inexistente devolvem as duas fontes vazias em vez de
    estourar — quem chama decide se isso é 404 ou mensagem no terminal.
    """
    numero_norm = _normalizar_numero(numero)
    achados = {"agente1": None, "agente2": None}

    if numero_norm and os.path.isdir(pasta):
        achados["agente1"] = _buscar_em_json_agente1(pasta, numero_norm, filtro_arquivo)
        achados["agente2"] = _buscar_em_historial_agente2(pasta, numero_norm, filtro_arquivo)

    return {"numero": numero, "numero_normalizado": numero_norm, **achados}


def buscar(numero: str, pasta: str = PASTA_JSON):
    numero_norm = _normalizar_numero(numero)
 
    if not numero_norm:
        print(_alerta("Número de processo inválido ou vazio."))
        return
 
    if not os.path.isdir(pasta):
        print(_alerta(f"Pasta não encontrada: {pasta}"))
        print(_dim("Verifique se você está rodando o script na pasta correta,"))
        print(_dim("ou use --pasta para apontar para a pasta JSON."))
        return
 
    print(_dim(f"\nBuscando processo {numero} em {pasta} ..."))

    dados   = buscar_dados(numero, pasta)
    proc_a1 = dados["agente1"]
    rec_a2  = dados["agente2"]

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
        print(_dim("\n(sem registro no JSON do Agente 1 — pode ter sido NÃO APTO)"))
 
    if rec_a2:
        _mostrar_agente2(rec_a2)
    else:
        print(_dim("\n(sem análise do Agente 2 — o Agente 2 ainda não processou este processo)"))
 
    print()
 
 
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
 
    buscar(numero, args.pasta)
 
 
if __name__ == "__main__":
    main()
 