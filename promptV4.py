import os
import re
import unicodedata
import pdfplumber
import pandas as pd
from datetime import datetime
import logging
from openai import OpenAI


# Función para normalizar texto (remover acentos, convertir a minúsculas)
def normalizar(text):
    if not text:
        return ""
    return unicodedata.normalize('NFKD', text).encode('ASCII', 'ignore').decode('ASCII').lower()

# Función para extraer fechas de un texto normalizado
def _extraer_fechas_de_texto(text_norm):
    # Patrones para extraer fechas (tanto con meses completos como abreviados)
    _MESES_COMPLETOS = {
        "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
        "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
    }
    _MESES_ABREV = {
        "jan": 1, "fev": 2, "mar": 3, "abr": 4,
        "mai": 5, "jun": 6, "jul": 7, "ago": 8,
        "set": 9, "out": 10, "nov": 11, "dez": 12
    }
    _PATRON_COMPLETO = r"(\d{1,2}) de (janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro) de (\d{4})"
    _PATRON_ABREV    = r"(\d{1,2})\s+(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)\.?\s+(\d{4})"
    fechas = []
    for dia, mes_txt, anio in re.findall(_PATRON_COMPLETO, text_norm):
        fechas.append(datetime(int(anio), _MESES_COMPLETOS[mes_txt], int(dia)))
    for dia, mes_txt, anio in re.findall(_PATRON_ABREV, text_norm):
        if mes_txt in _MESES_ABREV:
            fechas.append(datetime(int(anio), _MESES_ABREV[mes_txt], int(dia)))
    return fechas


# Esto silencia los logs internos de la librería que genera esos mensajes
logging.getLogger('pdfminer').setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO)

# Directorio de entrada y salida
current_dir = os.path.dirname(os.path.abspath(__file__))
input_directory = os.path.join(current_dir, "processos pra analiser")
output_file_prompts = os.path.join(current_dir, "prompts_generadosV4.txt")
output_file_excel = os.path.join(current_dir, "resultados_procesosV4.xlsx")



# Plantilla resumida (usada solo para guardar en el .txt)
PROMPT_TEMPLATE = """Você é um assistente jurídico especializado em execuções fiscais brasileiras.
    O sistema automático não conseguiu classificar este processo com certeza. Analise os dados abaixo e tome a decisão final.

    DADOS DO PROCESSO:
    - Última movimentação: {fecha}
    - Status da citação: {citacion}
    - Resultado da penhora: {penhora}

    CRITÉRIOS DE CLASSIFICAÇÃO:

    1. Se a última movimentação ocorreu há MENOS de 1 ano → NÃO APTO
    2. Se a última movimentação ocorreu há MAIS de 1 ano, avalie citação e penhora:
        a) CITAÇÃO AUSENTE ou FALHA → APTO
        b) CITAÇÃO VÁLIDA → avalie penhora:
            - Sem penhora / tentativa sem resultado → APTO
            - Penhora EFETIVADA (bacenjud/sisbajud, imóvel, faturamento, quotas, créditos, RENAJUD, CNIB) → NÃO APTO
    3. Se os dados forem insuficientes → INFORMAÇÃO INSUFICIENTE

    FORMATO DE RESPOSTA (responda APENAS neste formato, sem texto adicional):
    DECISÃO: <APTO | NÃO APTO | INFORMAÇÃO INSUFICIENTE>
    MOTIVO: <justificativa em uma frase>
    """
# Template para GPT com texto completo do PDF   
PROMPT_TEMPLATE_FULL = """Você é um assistente jurídico especializado em execuções fiscais brasileiras.
    O sistema automático não conseguiu classificar este processo com certeza.
    Analise o TEXTO COMPLETO do processo abaixo e determine a classificação.

    CRITÉRIOS:
    1. Última movimentação há MENOS de 1 ano → NÃO APTO
    2. Última movimentação há MAIS de 1 ano:
        a) Executado NÃO foi citado (ou citação falhou) → APTO
        b) Citação VÁLIDA → avalie penhora:
            - Sem penhora / apenas tentativa → APTO
            - Penhora EFETIVADA (BacenJud/SisBajud com saldo bloqueado, imóvel, 
              faturamento, quotas, RENAJUD, CNIB) → NÃO APTO
    3. Dados insuficientes → INFORMAÇÃO INSUFICIENTE

    ⚠️ ATENÇÃO — DISTINÇÃO CRÍTICA SOBRE SISBAJUD/BACENJUD:
    - Uma PETIÇÃO do exequente SOLICITANDO o bloqueio via SISBAJUD/BacenJud NÃO é penhora efetivada.
    - Para ser NÃO APTO, deve existir no processo um DOCUMENTO DE RESULTADO: 
      extrato de bloqueio, comprovante de valores bloqueados, resposta dos bancos, 
      ou despacho confirmando o bloqueio com saldo.
    - Se só há petição solicitando, sem documento de resultado → classifique como APTO.

    DADOS EXTRAÍDOS PELO SISTEMA (use como referência, mas confie no texto original):
    - Última movimentação detectada: {fecha}
    - Status da citação detectado: {citacion}
    - Status da penhora detectado: {penhora}

    TEXTO COMPLETO DO PROCESSO:
    {full_text}

    FORMATO DE RESPOSTA (responda APENAS neste formato, sem texto adicional):
    DECISÃO: <APTO | NÃO APTO | INFORMAÇÃO INSUFICIENTE>
    MOTIVO: <justificativa em uma frase>
    STATUS CITAÇÃO: <resumo do que encontrou sobre citação>
    STATUS PENHORA: <resumo do que encontrou sobre penhora>
    """

# 1. Extraer texto de PDF por páginas
def extract_text_by_page(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() for page in pdf.pages]

# 2. Filtrar texto relevante con palabras clave
def filter_text_by_keywords(text, keywords):
    relevant_lines = []
    norm_keywords = [normalizar(k) for k in keywords]
    for line in text.splitlines():
        if any(kw in normalizar(line) for kw in norm_keywords):
            relevant_lines.append(line)
    return " ".join(relevant_lines)

# 3. Obtener la fecha más reciente en el texto
# Keywords que indican una movimentación procesal real
KEYWORDS_MOVIMENTACAO_PROCESSUAL = [
    # Despachos y decisiones
    "despacho", "decisao interlocutoria", "decisao",
    "sentenca", "acordao",
    # Certidões processuales
    "certidao de publicacao de relacao",
    "certidao de remessa da intimacao",
    "certidao de intimacao",
    "ciencia da intimacao",
    "certidao de publicacao",
    # Peticiones
    "pede deferimento",
    "pede juntada",
    # Atos cartorários
    "ato ordinatorio",
    "cumpra-se",
    "publique-se. intime-se",
    # Localización de fecha en documentos judiciales
    "salvador (ba),",
    "salvador, ba,",
    # Intimaciones electrónicas
    "data da intimacao",
    "encaminhado para intimacao no portal eletronico",
]

def fecha_ultima_movimentacao(text):
    """
    Reemplaza fecha_mas_reciente().
    Solo considera fechas cercanas a keywords de movimentación processual,
    ignorando fechas de fichas cadastrais, extratos, consultas CNPJ, etc.
    """
    text_norm = normalizar(text)
    fechas = []

    for keyword in KEYWORDS_MOVIMENTACAO_PROCESSUAL:
        keyword_norm = normalizar(keyword)
        for match in re.finditer(re.escape(keyword_norm), text_norm):
            start = max(0, match.start() - 300)
            end   = match.end() + 300
            fragmento = text[start:end]

            fechas_encontradas = _extraer_fechas_de_texto(normalizar(fragmento))
            fechas.extend(fechas_encontradas)

    if not fechas:
        # Fallback: si no encontró nada con keywords,
        # usar el método global pero excluyendo secciones de fichas y extratos
        return _fecha_mas_reciente_fallback(text)

    return max(fechas)


def _fecha_mas_reciente_fallback(text):
    """
    Fallback: escanea todo el texto pero excluye secciones
    de fichas cadastrais, extratos y consultas CNPJ.
    """
    # Marcadores que delimitan secciones no processuales
    MARCADORES_EXCLUSION = [
        "ficha cadastral",
        "extrato fiscal",
        "consulta de dados via cpf",
        "dados do cnpj",
        "dados de empresa via cnpj",
        "posicao de debito",
        "certidao de divida ativa",
        "termo de confissao de divida",
    ]

    text_norm = normalizar(text)
    lineas = text.split("\n")
    lineas_filtradas = []
    excluir = False

    for linea in lineas:
        linea_norm = normalizar(linea)
        # Activar exclusión si encontramos un marcador
        if any(normalizar(m) in linea_norm for m in MARCADORES_EXCLUSION):
            excluir = True
        # Desactivar exclusión al encontrar inicio de documento judicial
        if any(kw in linea_norm for kw in ["poder judiciario", "comarca de salvador", "despacho", "decisao", "certidao"]):
            excluir = False
        if not excluir:
            lineas_filtradas.append(linea)

    texto_filtrado = "\n".join(lineas_filtradas)
    fechas = _extraer_fechas_de_texto(normalizar(texto_filtrado))
    return max(fechas) if fechas else None
    
# Buscador de fechas cercanas a palabras clave específicas (como las relacionadas con citación)
def extraer_fecha_cercana(text, keywords, ventana=500):
    text_norm = normalizar(text)
    fechas = []
    for keyword in keywords:
        kw_norm = normalizar(keyword)
        for match in re.finditer(re.escape(kw_norm), text_norm):
            start = max(0, match.start() - ventana)
            end = match.end() + ventana
            fechas.extend(_extraer_fechas_de_texto(text_norm[start:end]))
    return max(fechas) if fechas else None

# Función específica para extraer fechas relacionadas con citación, usando diferentes conjuntos de keywords para intentar capturar distintos tipos de órdenes y resultados de citación.
def extraer_fechas_citacion(text):
    
    KEYWORDS_ORDEN_ESPECIFICAS = [ #Keywords más específicas que indican una orden clara de citación, con menor riesgo de falso positivo, por eso se priorizan en la búsqueda
        # Órdenes directas del juez
        "determino a citação",
        "ordeno a citação",
        # Órdenes operativas (más específicas primero)
        "expeça-se o competente mandado de citação",
        "expeça-se carta de citação",
        "expeça-se mandado de citação",
        "proceda-se à citação",
        "promova-se a citação",
        "expedir carta de citação",
        "diligencie-se para citação",
        "ao oficial de justiça, cite",
        # Edital (tipo especial, pero sigue siendo orden)
        "cite-se por edital", "citação por edital",
        "converta-se em mandado de citação",
    ]
    KEYWORDS_ORDEN_GENERICAS = [ #Keywords genericas que pueden indicar una orden de citación pero con mayor riesgo de falso positivo, por eso se dejan para el final 
        # Más cortas → mayor riesgo de falso positivo
        "expeça-se citação",
        "seja citado", "sejam citados",
        "citem-se",
        "cite-se",
    ]
    # Para usar en extraer_fechas_citacion:
    KEYWORDS_ORDEN = KEYWORDS_ORDEN_ESPECIFICAS + KEYWORDS_ORDEN_GENERICAS

    KEYWORDS_INTENTO = [#Keywords que indican intento de citación, pero sin certeza de que se haya concretado, por eso se buscan por separado para extraer fechas relacionadas a intentos fallidos o en proceso.
        "carta com ar", "aviso de recebimento", "tentativa"
    ]

    KEYWORDS_EFECTIVA = [#Keywords que indican citación efectiva, con alta probabilidad de que el ejecutado haya sido notificado, por eso se buscan por separado para extraer fechas relacionadas a citaciones exitosas.
        "recebido", "assinado", "assinatura"
    ]
    #Logica de búsqueda: primero se buscan las keywords de intento y efectiva para capturar fechas relacionadas a esos eventos, y luego se buscan las keywords de orden para capturar fechas relacionadas a órdenes de citación, incluso si no hay evidencia clara de intento o efectividad.
    text_norm = normalizar(text)
    keywords_encontradas = [k for k in KEYWORDS_INTENTO if normalizar(k) in text_norm]
    print(f"Keywords de intento encontradas: {keywords_encontradas}")
    keywords_encontradas = [k for k in KEYWORDS_ORDEN if normalizar(k) in text_norm]
    print(f"Keywords de orden encontradas: {keywords_encontradas}")
    keywords_encontradas = [k for k in KEYWORDS_EFECTIVA if normalizar(k) in text_norm]
    print(f"Keywords de efectiva encontradas: {keywords_encontradas}")
    # Extraemos fechas relacionadas a cada tipo de evento (orden, intento, efectiva) usando las keywords correspondientes 
    # y una ventana de texto para capturar fechas cercanas a esos eventos.
    fecha_orden    = extraer_fecha_cercana(text, KEYWORDS_ORDEN, ventana=1500)
    fecha_intento  = extraer_fecha_cercana(text, KEYWORDS_INTENTO, ventana=1500)
    fecha_efectiva = extraer_fecha_cercana(text, KEYWORDS_EFECTIVA, ventana=1500)
    # Imprimimos las fechas encontradas para cada tipo de evento para facilitar la depuración 
    # y ver qué fechas se están capturando en relación a las keywords.
    print(f"  fecha_orden:    {fecha_orden}")
    print(f"  fecha_intento:  {fecha_intento}")
    print(f"  fecha_efectiva: {fecha_efectiva}")

    return fecha_orden, fecha_intento, fecha_efectiva


# 4. Extraer estado de citación
def extract_citacion(text):
    KEYWORDS_CITACION_OK = [
        "certifico que procedi a citacao",
        "certifico que o executado foi citado",
        "certifico que citei",
        "certifico ter realizado a citacao",
        "fica citado",
        "embargos a execucao",
        "parcelamento de debitos",
        "citacao valida",
        # ← "pagamento do debito" ELIMINADO
    ]

    KEYWORDS_CITACION_NAO_OK = [
        "aviso de recebimento negativo",
        "intime-se a fazenda publica para que adote as providencias cabiveis",
        "o reu nao foi citado",
        "executado nao foi citado",
        "sem citacao do executado",
        "nao houve citacao",
        "ausencia de citacao",
        "nao logrando exito na citacao",
        "nao foi possivel realizar a citacao",
        "a parte executada nao foi citada",   # ← nuevo: cubre sentença do G S Campos
    ]

    # Keywords que indican que el AR fue efectivamente entregado
    # Solo cuenta si hay firma Y fecha de entrega en el mismo fragmento
    KEYWORDS_AR_ENTREGUE = [
        "assinatura do recebedor",
        "nome legivel do recebedor",
    ]

    text_norm = normalizar(text)

    # Primero verificar negativo (tiene prioridad)
    if any(normalizar(k) in text_norm for k in KEYWORDS_CITACION_NAO_OK):
        return "NÃO HOUVE ou TENTATIVA FALHA"

    # Verificar positivo con keywords específicas
    if any(normalizar(k) in text_norm for k in KEYWORDS_CITACION_OK):
        # Guardia extra: si "certifico" está presente pero sin contexto de citação,
        # verificar que haya evidencia adicional
        return "HOUVE CITAÇÃO"

    # Verificar AR entregue: buscar firma cerca de "data de entrega"
    if any(normalizar(k) in text_norm for k in KEYWORDS_AR_ENTREGUE):
        # Verificar que no esté en un AR devuelto (negativo)
        if normalizar("motivos de devolucao") in text_norm:
            # Hay AR pero con sección de devolución — verificar si fue firmado
            # Si tiene "data de entrega" con valor real (no vacío), fue entregado
            if normalizar("data de entrega") in text_norm:
                return "HOUVE CITAÇÃO"
        else:
            return "HOUVE CITAÇÃO"

    return "Citação não encontrado"

# 5. Extraer resultado de la penhora
def extract_penhora(text):
    # Adicionamos muitas variações aos filtros.
    # --- BACENJUD / SISBAJUD (bloqueio bancário) ---
    # Penhora bancária confirmada (despacho autorizando OU comprovante de bloqueio)
    KEYWORDS_BACENJUD_POSITIVO = [
        "bloqueio bacenjud", "penhora bacenjud",
        "bloqueio sisbajud", "penhora sisbajud",
        "penhora on-line", "penhora online",
    ]
    # Solicited via petition — result not yet confirmed in the document
    KEYWORDS_BACENJUD_SOLICITADO = [
        "via sistema sisbajud",
        "via sistema bacenjud",
        "via bacenjud",
        "via sisbajud",
        "sistema sisbajud",
        "sistema bacenjud",
        "bloqueio de dinheiro/ativos financeiros",
        "bloqueio de ativos financeiros",
        "requer o bloqueio de dinheiro",
    ]
    # Confirmed successful bloqueio (result document present)
    KEYWORDS_BACENJUD_CONFIRMADO = [
        "valores bloqueados",
        "bloqueio efetuado",
        "bloqueio realizado",
        "dinheiro bloqueado",
        "penhora on-line efetuada",
        "penhora online efetuada",
        "extrato de bloqueio",
        "certidao de bloqueio",
        "comprovante de bloqueio",
    ]
    KEYWORDS_BACENJUD_NEGATIVO = [
        "resultado negativo da diligencia bacenjud",
        "resultado negativo da diligencia sisbajud",
        "suspensao pelo art. 40 da lef",
        "sem saldo positivo",
        "nao ha saldo",
        "bacenjud sem exito",
        "sisbajud sem exito",
        "diligencia bacenjud restou infrutifera",
        "diligencia sisbajud restou infrutifera",
        "bloqueio desbloqueado",
        "desbloqueio do valor",
    ]

    # --- RENAJUD (bloqueio veicular) ---
    KEYWORDS_RENAJUD_ATIVO = [
        "comprovante de inclusao de restricao veicular",
        "insercao de restricao veicular",
        "bloqueio renajud",
        "restricao renajud",
        "penhora de veiculo",
        "auto de penhora de veiculo",
    ]
    KEYWORDS_RENAJUD_NEGATIVO = [
        "renajud sem exito",
        "restricao renajud cancelada",
        "nao foram localizados veiculos",
        "pesquisa renajud negativa",
    ]

    # --- Penhora de imóvel ---
    KEYWORDS_PENHORA_IMOVEL = [
        "penhora de imovel",
        "penhora do imovel",
        "registro de penhora",
        "matricula do imovel penhorado",
        "imovel penhorado",
        "termo de penhora de imovel",
        "penhora sobre imovel",
    ]

    # --- Penhora de faturamento ---
    KEYWORDS_PENHORA_FATURAMENTO = [
        "penhora de faturamento",
        "penhora sobre faturamento",
        "penhora sobre o faturamento",
        "deposito de percentual do faturamento",
    ]

    # --- Indisponibilidade de bens (CNIB) ---
    KEYWORDS_INDISPONIBILIDADE = [
        "indisponibilidade de bens",
        "decretada a indisponibilidade",
        "cnib",
        "cadastro nacional de indisponibilidade",
        "bloqueio de bens",
    ]

    # --- Penhora de quotas / participação societária ---
    KEYWORDS_PENHORA_QUOTAS = [
        "penhora de quotas",
        "penhora de cotas",
        "penhora de participacao societaria",
        "penhora de acoes",
    ]

    # --- Penhora de créditos / precatório / direitos ---
    KEYWORDS_PENHORA_CREDITOS = [
        "penhora de creditos",
        "penhora de precatorio",
        "penhora de direitos",
        "penhora de direitos hereditarios",
        "penhora de aplicacao financeira",
    ]

    # --- Tentativas genéricas (sem resultado confirmado) ---
    # REMOVED "intimacao da penhora" — aparece como boilerplate em cartas de citação:
    #   "...ou da intimação da penhora (art. 16 da Lei n. 6.830/80)"
    # Use "intimado da penhora" (particípio passado = notificação já ocorrida) em vez disso.
    KEYWORDS_TENTATIVA_PENHORA = [
        "termo de penhora",
        "auto de penhora e avaliacao",
        "diligencia de penhora",
        "tentativa de penhora",
        "intimado da penhora",
        "intimados da penhora",
        "oficial de justica nao localizou bens",
        "nao foram localizados bens",
        "nao encontrou bens",
        "sem bens a penhorar",
        "bens insuficientes",
    ]
    text_norm = normalizar(text)

    # Indisponibilidade CNIB — bloqueio administrativo amplo
    if any(normalizar(k) in text_norm for k in KEYWORDS_INDISPONIBILIDADE):
        return "indisponibilidade de bens (CNIB)"

    # RENAJUD — verifica positivo antes de negativo
    if any(normalizar(k) in text_norm for k in KEYWORDS_RENAJUD_ATIVO):
        if any(normalizar(k) in text_norm for k in KEYWORDS_RENAJUD_NEGATIVO):
            return "tentativa de penhora renajud (negativa)"
        return "bloqueio renajud ativo"

    # Penhora de imóvel
    if any(normalizar(k) in text_norm for k in KEYWORDS_PENHORA_IMOVEL):
        return "penhora de imóvel"

    # Penhora de faturamento
    if any(normalizar(k) in text_norm for k in KEYWORDS_PENHORA_FATURAMENTO):
        return "penhora de faturamento"

    # Penhora de quotas societárias
    if any(normalizar(k) in text_norm for k in KEYWORDS_PENHORA_QUOTAS):
        return "penhora de quotas/ações"

    # Penhora de créditos / precatório / direitos
    if any(normalizar(k) in text_norm for k in KEYWORDS_PENHORA_CREDITOS):
        return "penhora de créditos/direitos"

    # BACENJUD / SISBAJUD — confirmed positive keywords (comprovante/despacho de bloqueio)
    if any(normalizar(k) in text_norm for k in KEYWORDS_BACENJUD_POSITIVO):
        if any(normalizar(k) in text_norm for k in KEYWORDS_BACENJUD_NEGATIVO):
            return "tentativa de penhora bacenjud (negativa)"
        return "penhora bacenjud/sisbajud"

    # BACENJUD / SISBAJUD — only a petition requesting it; result not in document
    if any(normalizar(k) in text_norm for k in KEYWORDS_BACENJUD_SOLICITADO):
        if any(normalizar(k) in text_norm for k in KEYWORDS_BACENJUD_NEGATIVO):
            return "tentativa de penhora bacenjud (negativa)"
        if any(normalizar(k) in text_norm for k in KEYWORDS_BACENJUD_CONFIRMADO):
            return "penhora bacenjud/sisbajud"
        return "sisbajud/bacenjud solicitado (resultado desconhecido)"

    # Tentativas genéricas sem penhora efetivada
    if any(normalizar(k) in text_norm for k in KEYWORDS_TENTATIVA_PENHORA):
        return "tentativa de penhora (sem resultado)"

    if "penhora nao realizada" in text_norm:
        return "penhora não realizada"

    return "Penhora não encontrado"
# Nueva función para detectar suspensión por parcelamento
KEYWORDS_PARCELAMENTO_ATIVO = [
    "suspensão do feito",
    "suspendo o curso do feito",
    "suspendo/mantenho suspenso",
    "parcelamento do débito exequendo",
    "adesão ao pad",
    "parcelamento administrativo de débitos",
    "pad homologado",
    "bloq pad",
    "suspensão pelo parcelamento",
    "art. 151, vi, do ctn",   # artigo que fundamenta suspensão por parcelamento
    "art. 313, ii, do cpc",   # artigo processual da suspensão
]

KEYWORDS_SUSPENSAO_ART40 = [
    "resultado negativo da diligencia bacenjud",
    "resultado negativo da diligencia sisbajud",
    "suspendo a presente execucao fiscal",
    "art. 40, caput, da lei n. 6.830",
    "arquive-se o feito nos termos do art. 40",
    "suspensao da execucao fiscal pelo prazo de 01",
]

def extract_suspensao_art40(text):
    text_norm = normalizar(text)
    if any(normalizar(k) in text_norm for k in KEYWORDS_SUSPENSAO_ART40):
        return "processo suspenso — art. 40 LEF (bens não localizados)"
    return None

def extract_parcelamento(text):
    text_norm = normalizar(text)
    if any(normalizar(k) in text_norm for k in KEYWORDS_PARCELAMENTO_ATIVO):
        return "processo suspenso por parcelamento (PAD)"
    return None
# 6. Crear el prompt
def create_prompt(fecha_reciente, citacion, penhora):
    return PROMPT_TEMPLATE.format(
        fecha    = fecha_reciente.strftime("%d/%m/%Y") if fecha_reciente else "Não especificado",
        citacion=citacion or "Não especificado",
        penhora=penhora or "Não especificado"
    )

# 7. Generar prompts para todos los archivos PDF
def generate_prompts(input_dir):
    pdf_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.pdf')]
    prompts = []

    for pdf_file in pdf_files:
        pdf_path = os.path.join(input_dir, pdf_file)

        # Inicializar ANTES del try
        full_text      = ""
        fecha_reciente = None
        citacion       = None
        penhora        = None
        fecha_orden    = None
        fecha_intento  = None
        fecha_efectiva = None
        prompt         = "Error"
        respuesta_gpt  = None

        try:
            pages_text = extract_text_by_page(pdf_path)
            full_text  = " ".join(p for p in pages_text if p)

            if not full_text.strip():
                logging.warning(f"PDF sin texto extraíble: {pdf_file}")
                prompts.append((pdf_file, None, None, None, None, None, None, "Error - PDF sin texto", "Error", ""))
                continue

            fecha_reciente = fecha_ultima_movimentacao(full_text)  # ← corregido
            citacion       = extract_citacion(full_text)
            penhora        = extract_penhora(full_text)
            fecha_orden, fecha_intento, fecha_efectiva = extraer_fechas_citacion(full_text)

            # Regla: fecha_citacion_efectiva debe ser None siempre que status_citacao != "HOUVE CITAÇÃO"
            if not citacion or normalizar(citacion) != normalizar("HOUVE CITAÇÃO"):
                fecha_efectiva = None

            prompt         = create_prompt(fecha_reciente, citacion, penhora)

            print(f"\n{'='*60}")
            print(f"ARQUIVO : {pdf_file}")
            print(f"  Última fecha   : {fecha_reciente.strftime('%Y-%m-%d') if fecha_reciente else 'NO ENCONTRADA'}")
            print(f"  Citação        : {citacion}")
            print(f"  Penhora        : {penhora}")
            print(f"{'='*60}")

            prompts.append((
                pdf_file, fecha_reciente, citacion,
                fecha_orden, fecha_intento, fecha_efectiva,
                penhora, prompt, respuesta_gpt, full_text
            ))

        except Exception as e:
            logging.error(f"Error al procesar {pdf_file}: {e}", exc_info=True)
            prompts.append((
                pdf_file, None, None, None, None, None, None, "Error", "Error", full_text
            ))

    return prompts
client = OpenAI()
MAX_CHARS_FILTRADO = 40_000   # ~10k tokens — intento rápido y barato
MAX_CHARS_FULL     = 400_000  # fallback con texto completo
MAX_CHARS_GPT = 400_000  # ~100k tokens, seguro para gpt-4o-mini (128k ctx)
# Secciones relevantes para filtrar el texto antes de enviarlo a GPT
KEYWORDS_FILTRO_GPT = [
    # Citación
    "citação", "citacao", "cite-se", "citar", "carta", "aviso de recebimento",
    "oficial de justiça", "mandado",
    # Penhora
    "penhora", "bacenjud", "sisbajud", "renajud", "cnib", "bloqueio",
    "indisponibilidade", "arresto", "constrição",
    # Decisiones clave
    "despacho", "decisão", "sentença", "determino", "defiro",
    # Movimentación
    "suspensão", "arquivamento", "extinção", "prescrição"
]


def _filtrar_texto_relevante(full_text):
    """Extrae solo los párrafos que contienen keywords relevantes."""
    keywords_norm = [normalizar(k) for k in KEYWORDS_FILTRO_GPT]
    parrafos = []
    for parrafo in full_text.split("\n\n"):
        parrafo_norm = normalizar(parrafo)
        if any(kw in parrafo_norm for kw in keywords_norm):
            parrafos.append(parrafo.strip())
    return "\n\n".join(parrafos)

def _parsear_respuesta_gpt(respuesta):
    """Extrae decisión y motivo de la respuesta estructurada del GPT."""
    decision = motivo = None
    for linea in respuesta.splitlines():
        if linea.startswith("DECISÃO:"):
            decision = linea.split(":", 1)[1].strip()
        elif linea.startswith("MOTIVO:"):
            motivo = linea.split(":", 1)[1].strip()
    return decision, motivo

def call_chatgpt(full_text, fecha, citacion, penhora):
    """
    Estrategia en dos pasos:
      1. Intenta con texto filtrado (rápido y barato).
      2. Si GPT responde INFORMAÇÃO INSUFICIENTE, reintenta con texto completo.
    """
    fecha_str    = fecha    or "Não especificado"
    citacion_str = citacion or "Não especificado"
    penhora_str  = penhora  or "Não especificado"

    # Nota de advertencia contextual según lo que detectó el sistema
    nota_extra = ""
    if "solicitado" in penhora_str.lower():
        nota_extra = (
            "\n⚠️ NOTA DO SISTEMA: O status da penhora indica que o SISBAJUD/BacenJud "
            "foi APENAS SOLICITADO em petição. Não foi encontrado documento de resultado "
            "(extrato, comprovante de bloqueio ou resposta dos bancos). "
            "Classifique como APTO salvo se encontrar evidência contrária no texto.\n"
        )
    elif "tentativa" in penhora_str.lower():
        nota_extra = (
            "\n⚠️ NOTA DO SISTEMA: O sistema detectou apenas TENTATIVA de penhora, "
            "sem resultado confirmado. Classifique como APTO salvo evidência contrária.\n"
        )
    elif "não encontrado" in penhora_str.lower():
        nota_extra = (
            "\n⚠️ NOTA DO SISTEMA: O sistema não encontrou informação de penhora no processo. "
            "Verifique cuidadosamente o texto antes de classificar como NÃO APTO.\n"
        )

    # --- Paso 1: texto filtrado ---
    texto_filtrado = _filtrar_texto_relevante(full_text)

    if len(texto_filtrado) < 200:
        texto_filtrado = None

    if texto_filtrado:
        texto_truncado = texto_filtrado[:MAX_CHARS_FILTRADO]
        prompt_paso1 = PROMPT_TEMPLATE_FULL.format(
            fecha=fecha_str,
            citacion=citacion_str,
            penhora=penhora_str,
            full_text=texto_truncado,
        ) + nota_extra

        try:
            resp1 = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Você é um assistente jurídico especializado em execuções fiscais brasileiras. Responda sempre em português, de forma objetiva e estruturada."},
                    {"role": "user",   "content": prompt_paso1}
                ],
                temperature=0
            )
            respuesta1 = resp1.choices[0].message.content.strip()
            decision1, _ = _parsear_respuesta_gpt(respuesta1)

            if decision1 and "INSUFICIENTE" not in decision1.upper():
                print(f"  [GPT] Decidido en paso 1 (texto filtrado, {len(texto_truncado)} chars)")
                return respuesta1

        except Exception as e:
            print(f"  [GPT] Error en paso 1: {e}")

    # --- Paso 2: texto completo (fallback) ---
    print(f"  [GPT] Escalando a paso 2 (texto completo)")
    texto_completo_truncado = full_text[:MAX_CHARS_GPT]
    prompt_paso2 = PROMPT_TEMPLATE_FULL.format(
        fecha=fecha_str,
        citacion=citacion_str,
        penhora=penhora_str,
        full_text=texto_completo_truncado,
    )
    try:
        resp2 = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Você é um assistente jurídico especializado em execuções fiscais brasileiras. Responda sempre em português, de forma objetiva e estruturada."},
                {"role": "user",   "content": prompt_paso2}
            ],
            temperature=0
        )
        return resp2.choices[0].message.content.strip()
    except Exception as e:
        print(f"  [GPT] Error en paso 2: {e}")
        return "Error en API"
# 8. Guardar prompts en un archivo de texto
def save_prompts_to_file(prompts, output_file):
    with open(output_file, 'w', encoding='utf-8') as file:
        for pdf_file, _, _, _, _, _, _, prompt, respuesta, _ in prompts:
            file.write(f"Archivo: {pdf_file}\nPrompt:\n{prompt}\nRespuesta GPT:\n{respuesta}\n{'-'*50}\n")
    print(f"Prompts guardados en {output_file}")

# 9. Procesar prompts y generar Excel
def process_prompts_to_excel(prompts, output_excel):
    resultados = []
    hoy = datetime.now()

    for pdf_file, fecha_reciente, citacion, fecha_orden, fecha_intento, fecha_efectiva, penhora, prompt, respuesta_gpt, full_text in prompts:
        parcelamento = extract_parcelamento(full_text)
        suspensao_art40 = extract_suspensao_art40(full_text)
        # Inicializar siempre antes del bloque de decisión
        respuesta_gpt = None
        fonte = "Sistema automático"

        if fecha_reciente is None:
            decision = "Informação insuficiente"
            motivo = "Data da última movimentação não informada"
        elif (hoy - fecha_reciente).days < 365:
            decision = "NO APTO"
            motivo = "Movimentação recente (< 1 ano)"
        elif parcelamento:                          # ← nuevo bloque
            decision = "NO APTO"
            motivo = f"Processo suspenso — {parcelamento}. Verificar status atual do PAD."
        elif suspensao_art40:
            decision = "APTO"
            motivo   = f"{suspensao_art40} — sem bens localizados para penhora"
        else:
            citacao_ausente = (
                not citacion
                or "não encontrado" in citacion.lower()
                or "nao houve" in normalizar(citacion)
                or "tentativa falha" in normalizar(citacion)
            )
            if citacao_ausente:
                decision = "APTO"
                motivo = "Citação ausente ou não localizada"
            else:
                _p = penhora.lower() if penhora else ""
                if not penhora or "não encontrado" in _p or "nao encontrado" in _p:
                    decision = "APTO"
                    motivo = "Ausência de penhora"
                elif "penhora não realizada" in _p or "penhora nao realizada" in _p:
                    decision = "APTO"
                    motivo = "Penhora não realizada"
                elif "tentativa" in _p:
                    decision = "APTO"
                    motivo = f"Apenas tentativa de penhora: {penhora}"
                elif "indisponibilidade de bens" in _p:
                    decision = "NO APTO"
                    motivo = "Indisponibilidade de bens (CNIB) decretada"
                elif "bloqueio renajud ativo" in _p:
                    decision = "NO APTO"
                    motivo = "Bloqueio RENAJUD ativo — penhora de veículo em andamento"
                elif "penhora de imóvel" in _p or "penhora de imovel" in _p:
                    decision = "NO APTO"
                    motivo = "Penhora de imóvel efetivada"
                elif "penhora de faturamento" in _p:
                    decision = "NO APTO"
                    motivo = "Penhora de faturamento efetivada"
                elif "penhora de quotas" in _p or "penhora de acoes" in _p:
                    decision = "NO APTO"
                    motivo = "Penhora de quotas/ações efetivada"
                elif "penhora de créditos" in _p or "penhora de creditos" in _p:
                    decision = "NO APTO"
                    motivo = "Penhora de créditos/direitos efetivada"
                elif "penhora bacenjud" in _p or "penhora sisbajud" in _p:
                    decision = "NO APTO"
                    motivo = "Penhora efetivada via BacenJud/SisBajud"
                elif "solicitado (resultado desconhecido)" in _p:
                    decision = "APTO"
                    motivo = "SISBAJUD/BacenJud apenas solicitado em petição — sem resultado confirmado no processo"
                else:
                    decision = "Informação insuficiente"
                    motivo = "Não foi possível determinar regra aplicável"

        # Llamar a GPT solo si el sistema no pudo decidir
        if decision == "Informação insuficiente":
            fecha_str = fecha_reciente.strftime("%Y-%m-%d") if fecha_reciente else "Não especificado"
            try:
                respuesta_gpt = call_chatgpt(
                    full_text,
                    fecha_str,
                    citacion or "Não especificado",
                    penhora  or "Não especificado"
                )
                # Parsear la respuesta para actualizar decisión y motivo
                decision_gpt, motivo_gpt = _parsear_respuesta_gpt(respuesta_gpt)
                if decision_gpt:
                    decision = decision_gpt
                    motivo   = motivo_gpt or motivo
                    fonte    = "GPT (paso 1 - filtrado)" if "paso 1" in respuesta_gpt else "GPT (paso 2 - completo)"

            except Exception as e:
                logging.error(f"Error GPT para {pdf_file}: {e}")
                respuesta_gpt = "Error en API"

        resultados.append({
            "CASO"                    : pdf_file,
            "Última data de interação": fecha_reciente.strftime("%Y-%m-%d") if fecha_reciente else "Não especificado",
            "Status da citação"       : citacion or "Não especificado",
            "Fecha orden citación"    : fecha_orden.strftime("%Y-%m-%d")   if fecha_orden    else "Não especificado",
            "Fecha intento citación"  : fecha_intento.strftime("%Y-%m-%d") if fecha_intento  else "Não especificado",
            "Fecha citación efectiva" : fecha_efectiva.strftime("%Y-%m-%d") if fecha_efectiva else "Não especificado",
            "Resultado da penhora"    : penhora or "Não especificado",
            "Decisión"                : decision,
            "Motivo"                  : motivo,
            "Fonte da decisão"        : fonte,
            "Respuesta GPT"           : respuesta_gpt or "",
        })

    df = pd.DataFrame(resultados)
    df.to_excel(output_excel, index=False)
    print(f"Planilla Excel generada: {output_excel}")
# 10. Ejecutar el flujo
if __name__ == "__main__":
    prompts = generate_prompts(input_directory)
    save_prompts_to_file(prompts, output_file_prompts)
    process_prompts_to_excel(prompts, output_file_excel)
