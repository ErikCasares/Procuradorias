"""
AGENTE 2 — Raciocínio Jurídico-Fiscal
HERA Tecnologia / PGMS — Contrato nº 01/2026

Responsabilidade:
    Recebe os processos APTO do Agente 1 via JSON e executa análise
    jurídico-fiscal para priorização de cobrança.

Modo de operação:
    File watcher — monitora a pasta de saída do Agente 1 e processa
    automaticamente qualquer arquivo *_agente2.json novo ou modificado.

Uso:
    # Modo watcher (produção) — fica rodando em background
    python agente2.py --watch

    # Modo pontual — processa um JSON específico e encerra
    python agente2.py --arquivo resultados_procesosV7_agente2.json

    # Modo watcher com pasta explícita
    python agente2.py --watch --pasta /caminho/para/outputs/

Dependências:
    Apenas stdlib — sem dependências externas para o watcher.
    (watchdog opcional para produção de alta performance)

Migração a Gemini:
    A função analisar_processo() tem un bloco marcado [GEMINI PLACEHOLDER].
    Quando chegarem as credenciais de SEMIT, só substituir esse bloco.
    A estrutura de entrada/saída não muda.
"""

import os
import re
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Agente2] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agente2")

# ── Configuración ─────────────────────────────────────────────────
CURRENT_DIR       = os.path.dirname(os.path.abspath(__file__))
PASTA_JSON       = os.path.join(CURRENT_DIR, "JSON")         # carpeta donde el Agente 1 escribe los JSON
PASTA_RESULTADOS  = os.path.join(CURRENT_DIR, "resultados")   # carpeta de salida del Agente 2
SUFIJO_INPUT      = "_agente2.json"       # archivos que genera el Agente 1
SUFIJO_OUTPUT     = "_agente2_resultado.json"   # resultado por lote
HISTORIAL_JSONL   = "historial_agente2.jsonl"   # registro acumulativo — una línea por processo
REPORTE_XLSX      = "historial_agente2.xlsx"    # reporte Excel acumulativo para el procurador
INTERVALO_WATCH   = 10                          # segundos entre chequeos del watcher

VERSION_AGENTE2 = "0.1-placeholder"


# ════════════════════════════════════════════════════════════════════
# HISTORIAL JSONL — registro acumulativo sin duplicados
# ════════════════════════════════════════════════════════════════════
#
# El archivo historial_agente2.jsonl vive en la misma carpeta que
# el script. Cada línea es un JSON completo de un processo analizado.
# Formato de cada línea:
# {"numero_processo":"...","id_lote":"...","analise":{...},"processado_em":"..."}
#
# Deduplicación: se usa numero_processo (número CNJ) como clave única.
# Si llega un processo ya registrado, se actualiza su línea en lugar
# de agregar una nueva — esto evita duplicados entre lotes.
#
# Lectura del historial: cada línea es JSON válido independiente,
# se puede leer con: for line in f: record = json.loads(line)
# ════════════════════════════════════════════════════════════════════

def _ruta_historial(pasta: str) -> str:
    """
    Devuelve la ruta absoluta del archivo historial JSONL
    dentro de la carpeta resultados/ (se crea si no existe).
    El parámetro pasta se ignora — siempre usa PASTA_RESULTADOS.
    """
    os.makedirs(PASTA_JSON, exist_ok=True)
    return os.path.join(PASTA_JSON, HISTORIAL_JSONL)


def _cargar_processos_registrados(path_jsonl: str) -> dict:
    """
    Lee el historial JSONL y devuelve un dict {numero_processo: número_de_línea}.
    Si el archivo no existe, devuelve dict vacío.
    Líneas corruptas se saltan con warning.
    """
    registrados = {}
    if not os.path.exists(path_jsonl):
        return registrados
    with open(path_jsonl, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                np = rec.get("numero_processo")
                if np:
                    registrados[np] = i
            except json.JSONDecodeError:
                log.warning(f"Línea {i} corrupta en historial — ignorada")
    return registrados


def _escribir_historial(path_jsonl: str, resultados: list, origen: str):
    """
    Agrega al historial JSONL solo los procesos que no están registrados aún.
    Si un processo ya existe (mismo numero_processo), lo actualiza reescribiendo
    el archivo completo con la línea actualizada — operación atómica vía archivo temp.

    Parámetros:
        path_jsonl  — ruta del archivo .jsonl
        resultados  — lista de dicts resultado del analisar_processo()
        origen      — nombre del archivo JSON del Agente 1 (para trazabilidad)

    Devuelve (nuevos, actualizados) — conteo para el log.
    """
    # Leer estado actual del historial
    existentes = {}   # numero_processo → dict completo
    orden = []        # mantener orden de inserción
    if os.path.exists(path_jsonl):
        with open(path_jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    np = rec.get("numero_processo")
                    if np:
                        existentes[np] = rec
                        if np not in orden:
                            orden.append(np)
                except json.JSONDecodeError:
                    pass  # líneas corruptas se descartan en la reescritura

    nuevos      = 0
    actualizados = 0

    for r in resultados:
        np = (r.get("entidades") or {}).get("numero_processo") or r.get("numero_processo")
        if not np:
            # Sin número de processo — siempre agregar (sin deduplicar)
            log.warning(f"  Processo sin numero_processo — se agrega sin deduplicar: {r.get('id_lote','?')}")
            np = f"SIN_NP_{r.get('id_lote','desconocido')}"

        # Construir registro JSONL
        registro = {
            "numero_processo"  : np,
            "id_lote"          : r.get("id_lote"),
            "nome_executado"   : r.get("nome_executado"),
            "analise"          : r.get("analise", {}),
            "processado_em"    : r.get("processado_em"),
            "origem_lote"      : origen,
        }

        if np in existentes:
            existentes[np] = registro   # actualizar en lugar de duplicar
            actualizados += 1
        else:
            existentes[np] = registro
            orden.append(np)
            nuevos += 1

    # Reescribir el archivo completo (operación atómica)
    path_tmp = path_jsonl + ".tmp"
    with open(path_tmp, "w", encoding="utf-8") as f:
        for np in orden:
            f.write(json.dumps(existentes[np], ensure_ascii=False) + "\n")
    os.replace(path_tmp, path_jsonl)  # atómico en todos los OS

    return nuevos, actualizados


# ════════════════════════════════════════════════════════════════════
# NÚCLEO DE ANÁLISIS
# ════════════════════════════════════════════════════════════════════

def analisar_processo(processo: dict) -> dict:
    """
    Analiza un proceso APTO y devuelve recomendaciones jurídico-fiscales.

    Input  — dict con la estructura del JSON del Agente 1.
    Output — dict con análisis, prioridad, acción recomendada y alertas.

    [GEMINI PLACEHOLDER]
    El bloque marcado abajo será reemplazado por la llamada a Gemini
    cuando lleguen las credenciales de SEMIT. La estructura de retorno
    no cambia.
    """
    ent = processo.get("entidades", {})

    prioridad    = _calcular_prioridad(processo)
    accion       = _recomendar_accion(processo)
    alerta_presc = _verificar_prescricao(processo)
    observacoes  = _generar_observacoes(processo)

    # ── [GEMINI PLACEHOLDER] ──────────────────────────────────────
    # Reemplazar este bloque cuando lleguen credenciales SEMIT:
    #
    # import vertexai
    # from vertexai.generative_models import GenerativeModel
    # vertexai.init(project=GEMINI_PROJECT_ID, location=GEMINI_REGION)
    # model = GenerativeModel("gemini-pro")
    # prompt = _construir_prompt_gemini(processo, prioridad, accion)
    # response = model.generate_content(prompt)
    # analise_llm = _parsear_resposta_gemini(response.text)
    # accion      = analise_llm.get("acao_recomendada", accion)
    # observacoes = analise_llm.get("observacoes", observacoes)
    # ──────────────────────────────────────────────────────────────

    return {
        "id_lote"          : processo.get("id_lote"),
        "numero_processo"  : ent.get("numero_processo"),
        "nome_executado"   : ent.get("nome_executado"),
        "analise": {
            "prioridade"        : prioridad,
            "acao_recomendada"  : accion,
            "justificativa"     : _justificativa(prioridad, processo),
            "alerta_prescricao" : alerta_presc,
            "observacoes"       : observacoes,
        },
        "processado_em": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ── Funciones de análisis determinístico ──────────────────────────

def _valor_numerico(valor_str):
    """Convierte 'R$ 228.433,16' a 228433.16."""
    if not valor_str:
        return 0.0
    try:
        limpio = str(valor_str).replace("R$","").replace(".","").replace(",",".").strip()
        return float(limpio)
    except ValueError:
        return 0.0


def _calcular_prioridad(processo: dict) -> str:
    """
    ALTA  → valor >= R$ 10.000 o sin movimentação >= 5 años
    MEDIA → valor entre R$ 1.000 y R$ 10.000
    BAIXA → valor < R$ 1.000
    """
    ent   = processo.get("entidades", {})
    valor = _valor_numerico(ent.get("valor_atualizado")) or \
            _valor_numerico(ent.get("valor_original"))

    anos_parado = 0
    ultima = processo.get("ultima_movimentacao")
    if ultima:
        try:
            dt = datetime.strptime(ultima, "%Y-%m-%d")
            anos_parado = (datetime.now() - dt).days / 365
        except ValueError:
            pass

    if valor >= 10_000 or anos_parado >= 5:
        return "ALTA"
    elif valor >= 1_000:
        return "MEDIA"
    else:
        return "BAIXA"


def _recomendar_accion(processo: dict) -> str:
    """Acción recomendada según estado de citação y motivo del Agente 1."""
    citacao = (processo.get("status_citacao") or "").upper()
    motivo  = (processo.get("motivo_agente1") or "").lower()

    if "não houve citação" in citacao or "citação ausente" in motivo:
        return "Realizar nova tentativa de citação — verificar endereços alternativos (SISBAJUD, RENAJUD, edital)"
    elif "ausência de penhora" in motivo:
        return "Requerer penhora online via SISBAJUD / RENAJUD"
    else:
        return "Analisar histórico completo e definir estratégia de cobrança"


def _verificar_prescricao(processo: dict) -> bool:
    """
    Alerta de prescripción quinquenal (CTN art. 174):
    si han pasado >= 5 años desde el ejercicio fiscal sin citação válida.
    """
    ent      = processo.get("entidades", {})
    exercicio = ent.get("exercicio") or ""
    citacao   = (processo.get("status_citacao") or "").lower()

    # "não houve citação" contiene "houve citação" como substring — verificar negación
    citacao_valida = (
        "houve citação" in citacao
        and "não houve" not in citacao
        and "não ocorreu" not in citacao
    )
    if citacao_valida:
        return False  # citación válida interrumpió prescripción

    try:
        ano_base = int(str(exercicio)[:4])
        return (datetime.now().year - ano_base) >= 5
    except (ValueError, TypeError):
        return False


def _generar_observacoes(processo: dict) -> list:
    """Observaciones relevantes para el procurador."""
    obs = []
    ent = processo.get("entidades", {})

    if not ent.get("cpf_cnpj"):
        obs.append("CPF/CNPJ não identificado — verificar manualmente no SIAP")
    if not ent.get("numero_cda"):
        obs.append("Número de CDA não extraído do PDF — consultar sistema PGMS")
    if not ent.get("valor_atualizado"):
        obs.append("Valor atualizado não disponível — solicitar cálculo atualizado à PGMS")

    conf_ocr = processo.get("confianca_ocr_media")
    if conf_ocr is not None and conf_ocr < 50:
        obs.append(f"Qualidade de OCR baixa ({conf_ocr}%) — dados podem conter erros")

    if _verificar_prescricao(processo):
        obs.append("⚠️  Risco de prescrição quinquenal — verificar interrupção do prazo (CTN art. 174)")

    if not obs:
        obs.append("Processo sem observações adicionais")

    return obs


def _justificativa(prioridad: str, processo: dict) -> str:
    ent   = processo.get("entidades", {})
    valor = ent.get("valor_atualizado") or ent.get("valor_original") or "não informado"
    motivo = processo.get("motivo_agente1") or ""
    return f"Prioridade {prioridad} — valor da dívida: {valor}. Agente 1: {motivo}."


# ════════════════════════════════════════════════════════════════════
# PROCESAMIENTO DE LOTE
# ════════════════════════════════════════════════════════════════════

def processar_lote(path_json: str):
    """
    Lee un JSON del Agente 1, analiza cada proceso y escribe resultado.
    Devuelve la ruta del archivo de resultado, o None si hubo error.
    """
    log.info(f"Procesando lote: {os.path.basename(path_json)}")

    try:
        with open(path_json, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        log.error(f"Error leyendo {path_json}: {e}")
        return None

    processos  = payload.get("processos", [])
    meta_a1    = payload.get("metadata", {})
    resultados = []

    for proc in processos:
        try:
            resultado = analisar_processo(proc)
            resultados.append(resultado)
            analise = resultado["analise"]
            log.info(
                f"  [{analise['prioridade']}] "
                f"{resultado.get('nome_executado','—')} — "
                f"{analise['acao_recomendada'][:55]}..."
            )
        except Exception as e:
            log.error(f"  Error en {proc.get('id_lote','?')}: {e}")
            resultados.append({
                "id_lote"      : proc.get("id_lote"),
                "erro"         : str(e),
                "processado_em": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            })

    # Conteo de prioridades
    prioridades = {"ALTA": 0, "MEDIA": 0, "BAIXA": 0}
    for r in resultados:
        p = r.get("analise", {}).get("prioridade")
        if p in prioridades:
            prioridades[p] += 1

    output_payload = {
        "metadata": {
            "generado_em"      : datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "version_agente2"  : VERSION_AGENTE2,
            "total_analisados" : len(resultados),
            "prioridades"      : prioridades,
            "origem_agente1"   : {
                "arquivo"         : os.path.basename(path_json),
                "generado_em"     : meta_a1.get("generado_em"),
                "total_procesados": meta_a1.get("total_procesados"),
                "total_aptos"     : meta_a1.get("total_aptos"),
                "version_agente1" : meta_a1.get("version_agente1"),
            }
        },
        "analises": resultados,
    }

    # Asegurar que la carpeta resultados/ existe
    os.makedirs(PASTA_RESULTADOS, exist_ok=True)

    # Resultado por lote → resultados/*_agente2_resultado.json
    nombre_output = os.path.basename(path_json).replace(SUFIJO_INPUT, SUFIJO_OUTPUT)
    path_output   = os.path.join(PASTA_JSON, nombre_output)

    try:
        # Resultado por lote (JSON único por lote)
        with open(path_output, "w", encoding="utf-8") as f:
            json.dump(output_payload, f, ensure_ascii=False, indent=2)
        log.info(f"  Resultado del lote: resultados/{os.path.basename(path_output)}")

        # Historial JSONL acumulativo → resultados/historial_agente2.jsonl
        pasta = os.path.dirname(os.path.abspath(path_json))
        path_jsonl = _ruta_historial(pasta)
        nuevos, actualizados = _escribir_historial(
            path_jsonl, resultados, os.path.basename(path_json)
        )
        log.info(
            f"  Historial: {os.path.basename(path_jsonl)} — "
            f"+{nuevos} nuevos, {actualizados} actualizados"
        )

        # Generar reporte Excel acumulativo a partir del historial
        path_xlsx = os.path.join(PASTA_RESULTADOS, REPORTE_XLSX)
        gerar_reporte_xlsx(path_jsonl, path_xlsx)

        log.info(
            f"  Prioridades — "
            f"ALTA={prioridades['ALTA']} "
            f"MEDIA={prioridades['MEDIA']} "
            f"BAIXA={prioridades['BAIXA']}"
        )
        return path_output
    except Exception as e:
        log.error(f"Error escribiendo resultado: {e}")
        return None


# ════════════════════════════════════════════════════════════════════
# REPORTE EXCEL — acumulativo, para el procurador
# ════════════════════════════════════════════════════════════════════

def _valor_para_ordenar(valor_str):
    """Convierte 'R$ 228.433,16' a float para ordenar. Vacío → 0."""
    if not valor_str:
        return 0.0
    try:
        limpio = str(valor_str).replace("R$", "").replace(".", "").replace(",", ".").strip()
        return float(limpio)
    except (ValueError, AttributeError):
        return 0.0


def gerar_reporte_xlsx(path_jsonl: str, path_xlsx: str):
    """
    Genera un reporte Excel acumulativo a partir del historial JSONL.

    El reporte lee TODOS los procesos registrados en el historial (no solo
    el último lote), los ordena por prioridade (ALTA → MEDIA → BAIXA) y
    dentro de cada nivel por valor de deuda descendente, y aplica formato
    para lectura rápida por el procurador.

    Requiere: pandas, openpyxl.
    """
    try:
        import pandas as pd
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        log.error(f"Falta dependencia para el reporte Excel: {e}")
        log.error("Instalar con: pip install pandas openpyxl")
        return None

    if not os.path.exists(path_jsonl):
        log.warning(f"No hay historial JSONL para generar el reporte: {path_jsonl}")
        return None

    # ── Leer todos los procesos del historial ──
    filas = []
    with open(path_jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            analise = rec.get("analise", {})
            filas.append({
                "Prioridade"       : analise.get("prioridade", ""),
                "Nº Processo"      : rec.get("numero_processo", ""),
                "Executado"        : rec.get("nome_executado", ""),
                "Ação Recomendada" : analise.get("acao_recomendada", ""),
                "Alerta Prescrição": "SIM" if analise.get("alerta_prescricao") else "",
                "Justificativa"    : analise.get("justificativa", ""),
                "Observações"      : " | ".join(analise.get("observacoes", [])),
                "Origem (lote)"    : rec.get("origem_lote", ""),
                "Processado em"    : rec.get("processado_em", ""),
                "id_lote"          : rec.get("id_lote", ""),
            })

    if not filas:
        log.warning("Historial vacío — no se genera reporte Excel")
        return None

    df = pd.DataFrame(filas)

    # ── Ordenar: prioridade (ALTA→MEDIA→BAIXA) y valor desc ──
    # El valor no está en el JSONL directamente; extraerlo de la justificativa
    _ORDEN_PRIORIDADE = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2, "": 3}
    df["_ord_prio"] = df["Prioridade"].map(lambda p: _ORDEN_PRIORIDADE.get(p, 3))
    # Extraer valor de la justificativa ("valor da dívida: R$ X")
    def _extraer_valor_justif(j):
        m = re.search(r"valor da d[ií]vida:\s*(R\$[\d.,]+)", str(j))
        return _valor_para_ordenar(m.group(1)) if m else 0.0
    df["_ord_valor"] = df["Justificativa"].map(_extraer_valor_justif)

    df = df.sort_values(
        by=["_ord_prio", "_ord_valor"],
        ascending=[True, False]
    ).drop(columns=["_ord_prio", "_ord_valor"])

    # ── Escribir con formato ──
    os.makedirs(os.path.dirname(path_xlsx), exist_ok=True)

    with pd.ExcelWriter(path_xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Priorização")
        ws = writer.sheets["Priorização"]

        # Estilos
        font_header = Font(name="Arial", bold=True, color="FFFFFF", size=11)
        fill_header = PatternFill("solid", fgColor="1F4E79")
        align_header = Alignment(horizontal="center", vertical="center", wrap_text=True)
        borde = Border(*[Side(style="thin", color="D0D0D0")] * 4)

        fills_prioridade = {
            "ALTA" : PatternFill("solid", fgColor="F8CBAD"),   # rojo suave
            "MEDIA": PatternFill("solid", fgColor="FFE699"),   # amarillo suave
            "BAIXA": PatternFill("solid", fgColor="C6E0B4"),   # verde suave
        }

        # Header
        for col_idx, col_name in enumerate(df.columns, start=1):
            c = ws.cell(row=1, column=col_idx)
            c.font = font_header
            c.fill = fill_header
            c.alignment = align_header
            c.border = borde

        # Filas de datos
        col_prioridade = list(df.columns).index("Prioridade") + 1
        for row_idx in range(2, len(df) + 2):
            prioridade = ws.cell(row=row_idx, column=col_prioridade).value
            fill = fills_prioridade.get(prioridade)
            for col_idx in range(1, len(df.columns) + 1):
                c = ws.cell(row=row_idx, column=col_idx)
                c.font = Font(name="Arial", size=10)
                c.border = borde
                c.alignment = Alignment(vertical="top", wrap_text=True)
                if fill and col_idx == col_prioridade:
                    c.fill = fill

        # Anchos de columna
        anchos = {
            "Prioridade": 11, "Nº Processo": 22, "Executado": 30,
            "Ação Recomendada": 45, "Alerta Prescrição": 12,
            "Justificativa": 40, "Observações": 45,
            "Origem (lote)": 22, "Processado em": 20, "id_lote": 24,
        }
        for col_idx, col_name in enumerate(df.columns, start=1):
            letra = get_column_letter(col_idx)
            ws.column_dimensions[letra].width = anchos.get(col_name, 18)

        # Congelar header y activar autofiltro
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(df.columns))}{len(df)+1}"

    log.info(f"  Reporte Excel: resultados/{os.path.basename(path_xlsx)} ({len(df)} processos)")
    return path_xlsx


# ════════════════════════════════════════════════════════════════════
# FILE WATCHER (stdlib pura — sin watchdog)
# ════════════════════════════════════════════════════════════════════

def _ya_procesado(path_json: str) -> bool:
    """True si el resultado ya existe en resultados/ y es más nuevo que el input."""
    nombre = os.path.basename(path_json).replace(SUFIJO_INPUT, SUFIJO_OUTPUT)
    path_resultado = os.path.join(PASTA_JSON, nombre)
    if not os.path.exists(path_resultado):
        return False
    return os.path.getmtime(path_resultado) >= os.path.getmtime(path_json)


def watcher(pasta: str, intervalo: int = INTERVALO_WATCH):
    """
    Monitorea la carpeta y procesa automáticamente cualquier
    *_agente2.json nuevo o modificado. Termina con Ctrl+C.
    """
    log.info(f"=== Agente 2 iniciado — v{VERSION_AGENTE2} ===")
    log.info(f"Monitoreando: {pasta}")
    log.info(f"Sufijo buscado: *{SUFIJO_INPUT}")
    log.info(f"Intervalo: {intervalo}s — Ctrl+C para detener\n")

    procesados = set()  # evitar reprocesar en el mismo ciclo

    try:
        while True:
            jsons = sorted(Path(pasta).glob(f"*{SUFIJO_INPUT}"))

            for path_json in jsons:
                path_str = str(path_json)
                if not _ya_procesado(path_str):
                    if path_str not in procesados:
                        log.info(f"Nuevo archivo detectado: {path_json.name}")
                    processar_lote(path_str)
                    procesados.add(path_str)
                elif path_str in procesados:
                    # Archivo ya procesado en esta sesión — ignorar silencioso
                    pass

            time.sleep(intervalo)

    except KeyboardInterrupt:
        log.info("Agente 2 detenido.")


# ════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Agente 2 — Raciocínio Jurídico-Fiscal (PGMS/HERA)"
    )
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument(
        "--watch", action="store_true",
        help="Modo watcher: monitorea la carpeta continuamente"
    )
    grupo.add_argument(
        "--arquivo", metavar="PATH",
        help="Modo puntual: procesa un JSON específico y termina"
    )
    parser.add_argument(
        "--pasta", metavar="DIR", default=PASTA_JSON,
        help=f"Carpeta a monitorear (default: directorio del script)"
    )
    parser.add_argument(
        "--intervalo", type=int, default=INTERVALO_WATCH,
        help=f"Segundos entre chequeos en modo watcher (default: {INTERVALO_WATCH})"
    )

    args = parser.parse_args()

    if args.watch:
        watcher(args.pasta, args.intervalo)
    else:
        if not os.path.exists(args.arquivo):
            log.error(f"Archivo no encontrado: {args.arquivo}")
            sys.exit(1)
        resultado = processar_lote(args.arquivo)
        sys.exit(0 if resultado else 1)


if __name__ == "__main__":
    main()