import os
import re
import pdfplumber
import pandas as pd
from datetime import datetime
import logging
from openai import OpenAI


# Esto silencia los logs internos de la librería que genera esos mensajes
logging.getLogger('pdfminer').setLevel(logging.ERROR)
logging.basicConfig(level=logging.INFO)

# Directorio de entrada y salida
current_dir = os.path.dirname(os.path.abspath(__file__))
input_directory = os.path.join(current_dir, "processos pra analiser")
output_file_prompts = os.path.join(current_dir, "prompts_generadosV3.txt")
output_file_excel = os.path.join(current_dir, "resultados_procesosV3.xlsx")



# Plantilla del prompt
PROMPT_TEMPLATE = """
Objetivo: Determinar automaticamente se um processo judicial está "APTO" ou "NÃO APTO" para prosseguimento com base em critérios específicos de movimentação processual, citação e penhora.
1. Data da última movimentação do processo: {fecha}.
2. Status da citação: {citacion}.
3. Resultado da penhora: {penhora}.

Regras:
- Se a última interação ocorreu há mais de um ano, avalie o status da citação e penhora.
- Se a última interação ocorreu há menos de um ano, classifique como NÃO APTO.
- Se alguma variável não puder ser determinada, retorne "Informação insuficiente".

Decisão:
"""

# 1. Extraer texto de PDF por páginas
def extract_text_by_page(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        return [page.extract_text() for page in pdf.pages]

# 2. Filtrar texto relevante con palabras clave
def filter_text_by_keywords(text, keywords):
    relevant_lines = []
    for line in text.splitlines():
        if any(keyword in line.lower() for keyword in keywords):
            relevant_lines.append(line)
    return " ".join(relevant_lines)

# 3. Obtener la fecha más reciente en el texto
def fecha_mas_reciente(texto):
    patron_fecha = r"(\d{2}) de (janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro) de (\d{4})"
    
    # Diccionario para convertir meses en números
    meses = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
        "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
    }

    # Encontrar todas las fechas en el texto
    fechas_encontradas = re.findall(patron_fecha, texto)
    fechas_convertidas = []

    for dia, mes_texto, anio in fechas_encontradas:
        dia = int(dia)
        mes = meses[mes_texto.lower()]
        anio = int(anio)
        fechas_convertidas.append(datetime(anio, mes, dia))
    
    # Devolver la fecha más reciente o None si no hay fechas
    return max(fechas_convertidas) if fechas_convertidas else None




# 4. Extraer estado de citación
KEYWORDS_CITACION_OK = [
    "certifico, para os devidos fins",
    "embargos à execução",
    "parcelamento de débitos",
    "citação válida"
]
KEYWORDS_CITACION_NAO_OK = [
    "aviso de recebimento negativo",
    "intime-se a fazenda pública para que adote as providências cabíveis em face da ausência de localização do executado"
]
def extract_citacion(text):
    text_lower = text.lower()
    
    if any(k in text_lower for k in KEYWORDS_CITACION_OK):
        return "HOUVE CITAÇÃO"
    elif any(k in text_lower for k in KEYWORDS_CITACION_NAO_OK):
        return "NÃO HOUVE ou TENTATIVA FALHA"
    return "Citación não encontrado"

# 5. Extraer resultado de la penhora
KEYWORDS_PENHORA_BACENJUD = [
    "bloqueio bacenjud", 
    "penhora bacenjud"
    ]
KEYWORDS_TENTATIVA_PENHORA = [
    "termo de penhora", 
    "intimação da penhora", 
    "auto de penhora e avaliação"
    ]
def extract_penhora(text):
    text_lower = text.lower()
    if any(k in text_lower for k in KEYWORDS_PENHORA_BACENJUD):
        return "penhora bacenjud"
    elif any(k in text_lower for k in KEYWORDS_TENTATIVA_PENHORA):
        return "tentativa de penhora"
    elif "penhora não realizada" in text_lower:
        return "No realizada"
    return "Penhora não encontrado"

# 6. Crear el prompt
def create_prompt(fecha_reciente, citacion, penhora):
    return PROMPT_TEMPLATE.format(
        fecha=fecha_reciente.strftime("%d/%m/%Y") if fecha_reciente else "Não especificado",
        citacion=citacion or "Não especificado",
        penhora=penhora or "Não especificado"
    )

# 7. Generar prompts para todos los archivos PDF
def generate_prompts(input_dir):
    pdf_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.pdf')]
    prompts = []
    for pdf_file in pdf_files:
        pdf_path = os.path.join(input_dir, pdf_file)
        try:
            pages_text = extract_text_by_page(pdf_path)
            full_text = " ".join(pages_text)

            fecha_reciente = fecha_mas_reciente(full_text)
            citacion = extract_citacion(full_text)
            penhora = extract_penhora(full_text)

            prompt = create_prompt(fecha_reciente, citacion, penhora)

            # No llamar a la API aquí; se llamará sólo si la decisión final es "Informação insuficiente"
            respuesta_gpt = None

            prompts.append((pdf_file, fecha_reciente, citacion, penhora, prompt, respuesta_gpt))

            print(f"Archivo: {pdf_file}")
            print(f"Prompt generado (sin llamada a la API).\n")

        except Exception as e:
            logging.error(f"Error al procesar {pdf_file}: {e}")
            prompts.append((pdf_file, None, None, None, "Error", "Error"))

    return prompts

client = OpenAI()
def call_chatgpt(prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # modelo actual
            messages=[
                {"role": "system", "content": "Eres un asistente legal que analiza procesos judiciales."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Error al llamar a ChatGPT: {e}")
        return "Error en API"



# 8. Guardar prompts en un archivo de texto
def save_prompts_to_file(prompts, output_file):
    with open(output_file, 'w', encoding='utf-8') as file:
        for pdf_file, _, _, _, prompt, respuesta in prompts:
            file.write(f"Archivo: {pdf_file}\nPrompt:\n{prompt}\nRespuesta GPT:\n{respuesta}\n{'-'*50}\n")
    print(f"Prompts guardados en {output_file}")

# 9. Procesar prompts y generar Excel
def process_prompts_to_excel(prompts, output_excel):
    resultados = []
    hoy = datetime.now()

    for pdf_file, fecha_reciente, citacion, penhora, prompt, respuesta_gpt in prompts:
        if fecha_reciente is None:
            decision = "Informação insuficiente"
            motivo = "Data da última movimentação não informada"
        elif (hoy - fecha_reciente).days < 365:
            decision = "NO APTO"
            motivo = "Movimentação recente (< 1 ano)"
        else:
            if not citacion or "não encontrado" in citacion.lower():
                decision = "APTO"
                motivo = "Citação ausente ou não localizada"
            else:
                if not penhora or "não encontrado" in penhora.lower():
                    decision = "APTO"
                    motivo = "Ausência de penhora"
                elif "tentativa" in penhora.lower():
                    decision = "APTO"
                    motivo = "Apenas tentativa de penhora"
                elif "penhora bacenjud" in penhora.lower():
                    decision = "NO APTO"
                    motivo = "Penhora efetivada via BacenJud"
                else:
                    decision = "Informação insuficiente"
                    motivo = "Não foi possível determinar regra aplicável"

        # Llamar a la API solo si la decisión es Información insuficiente
        if decision == "Informação insuficiente":
            try:
                respuesta_gpt = call_chatgpt(prompt)
            except Exception:
                respuesta_gpt = "Error en API"

        resultados.append({
            "CASO": pdf_file,
            "Última data de interação": fecha_reciente.strftime("%Y-%m-%d") if fecha_reciente else "Não especificado",
            "Status da citação": citacion or "Não especificado",
            "Resultado da penhora": penhora or "Não especificado",
            "Decisión": decision,
            "Motivo": motivo,
            "Respuesta GPT": respuesta_gpt,
        })

    df = pd.DataFrame(resultados)
    df.to_excel(output_excel, index=False)
    print(f"Planilla Excel generada: {output_excel}")

# 10. Ejecutar el flujo
if __name__ == "__main__":
    prompts = generate_prompts(input_directory)
    save_prompts_to_file(prompts, output_file_prompts)
    process_prompts_to_excel(prompts, output_file_excel)
