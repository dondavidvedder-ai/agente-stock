"""
Agente WhatsApp - Consulta de Stock
====================================
Servidor Flask que recibe mensajes de WhatsApp via Twilio,
lee los archivos Excel de stock desde Dropbox y responde
con la informacion filtrada.

Autor: generado con Claude
"""

import os
import re
import json
import io
import logging
from datetime import date

import pandas as pd
import anthropic
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

DROPBOX_FILES = {
    "falabella": "https://www.dropbox.com/scl/fi/nm4mpaqbv1z8zefz2et5t/Falabella.xlsx?rlkey=cupt094g92jpbh8uevc2hoirx&st=vaoqhcfa&dl=1",
    "ripley": "https://www.dropbox.com/scl/fi/tp306bb75aym4yrlr9gch/Ripley.xlsx?rlkey=dnjioc8fcjwiapedccvg410sx&st=v2ix91tb&dl=1",
    "walmart": "https://www.dropbox.com/scl/fi/94cobt417zg6ltgz940fl/Walmart.xlsx?rlkey=tu8ig67ktfrjlnlyk75f2dibw&st=4oj4g8kt&dl=1",
    "jumbo": "https://www.dropbox.com/scl/fi/arcuhsmvg3xe8iqwx4llr/Jumbo.xlsx?rlkey=56y14tkk2boiuuwvlj2kexdsx&st=wm6hhrbg&dl=1",
    "tottus": "https://www.dropbox.com/scl/fi/ldfv1i3m5dcrqiy8vfykk/Tottus.xlsx?rlkey=egn9vsr2bnrj2t69vwbu6m7sp&st=v7ydie76&dl=1",
}

NUMEROS_AUTORIZADOS = {
    "whatsapp:+56926121144",
    "whatsapp:+56953634351",
    "whatsapp:+56972494232",
    "whatsapp:+56997054149",
    "whatsapp:+56954077612",
    "whatsapp:+56972495007",
    "whatsapp:+56990674664",
}

def download_file_from_dropbox(cliente):
    try:
        url = DROPBOX_FILES.get(cliente.lower())
        if not url:
            raise ValueError(f"Cliente '{cliente}' no disponible")
        log.info(f"Descargando {cliente} desde Dropbox...")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return io.BytesIO(resp.content)
    except Exception as e:
        log.error(f"Error descargando {cliente}: {e}")
        raise

def get_current_week():
    w = date.today().isocalendar()[1]
    return str(w).zfill(2)

def find_best_week():
    return get_current_week()

def read_falabella(file_bytes, tienda, producto):
    try:
        df = pd.read_excel(file_bytes, sheet_name="Sheet1", header=0)
        log.info(f"Falabella - Columnas: {list(df.columns[:15])}")
        desc_col = None
        marca_col = None
        tienda_col = None
        for col in df.columns:
            cl = str(col).lower().strip()
            if "descripción" in cl or "descripcion" in cl:
                desc_col = col
            elif "marca" in cl:
                marca_col = col
        if len(df.columns) > 12:
            tienda_col = df.columns[12]
        if not desc_col or not marca_col or not tienda_col:
            log.warning("Falabella: Columnas faltantes")
            return []
        tienda_words = tienda.lower().strip().split()
        mask = pd.Series([False] * len(df))
        for word in tienda_words:
            if len(word) > 2:
                mask = mask | df[tienda_col].astype(str).str.lower().str.contains(word, na=False, regex=False)
        filtered = df[mask]
        if len(filtered) == 0:
            log.info(f"Falabella: Sin coincidencia para '{tienda}'")
            return []
        results = []
        for _, row in filtered.iterrows():
            desc_str = str(row.get(desc_col, "")).strip()
            marca = str(row.get(marca_col, "")).strip()
            if producto and producto.upper() not in desc_str.upper():
                continue
            if desc_str:
                results.append({"modelo": "", "descripcion": desc_str[:50], "marca": marca, "stock": 0, "trf": 0})
        return results[:20]
    except Exception as e:
        log.error(f"Error leyendo Falabella: {e}")
        return []

def read_ripley(file_bytes, tienda, producto):
    try:
        df = pd.read_excel(file_bytes, sheet_name="BASE", header=0)
        log.info(f"Ripley - Columnas: {list(df.columns[:10])}")
        sucursal_col = next((c for c in df.columns if str(c).strip().lower() == "sucursal"), None)
        if not sucursal_col:
            sucursal_col = next((c for c in df.columns if "sucursal" in str(c).lower() and "cod" not in str(c).lower()), None)
        marca_col = next((c for c in df.columns if "marca" in str(c).lower() and "cod" not in str(c).lower()), None)
        if not sucursal_col:
            log.warning("Ripley: No se encontro columna Sucursal")
            return []
        tienda_words = tienda.lower().strip().split()
        mask = pd.Series([False] * len(df))
        for word in tienda_words:
            if len(word) > 2:
                mask = mask | df[sucursal_col].astype(str).str.lower().str.contains(word, na=False, regex=False)
        filtered = df[mask]
        if len(filtered) == 0:
            log.info(f"Ripley: Sin coincidencia para '{tienda}'")
            return []
        desc_col = next((c for c in df.columns if "desc" in str(c).lower() and "art" in str(c).lower()), None)
        stock_col = next((c for c in df.columns if "stock" in str(c).lower() and "disponible" in str(c).lower() and "(u)" in str(c).lower()), None)
        if not stock_col:
            stock_col = next((c for c in df.columns if "stock" in str(c).lower() and "(u)" in str(c).lower()), None)
        results = []
        for _, row in filtered.iterrows():
            desc_str = str(row.get(desc_col, "")).strip() if desc_col else ""
            marca = str(row.get(marca_col, "")).strip() if marca_col else ""
            stock_val = int(float(row.get(stock_col, 0))) if stock_col and pd.notna(row.get(stock_col)) else 0
            if producto and producto.upper() not in desc_str.upper():
                continue
            if desc_str:
                results.append({"modelo": "", "descripcion": desc_str[:50], "marca": marca, "stock": stock_val, "trf": 0})
        return results[:20]
    except Exception as e:
        log.error(f"Error leyendo Ripley: {e}")
        return []

def read_generic(file_bytes, tienda, producto):
    try:
        xl = pd.ExcelFile(file_bytes)
        df = pd.read_excel(file_bytes, sheet_name=xl.sheet_names[0], header=0)
    except Exception:
        return []
    tienda_col = next((c for c in df.columns if any(k in str(c).lower() for k in ["tienda","sala","sucursal","local"])), None)
    if tienda_col:
        df = df[df[tienda_col].astype(str).str.lower().str.contains(tienda.lower(), na=False)]
    stock_col = next((c for c in df.columns if "stock" in str(c).lower()), None)
    desc_col = next((c for c in df.columns if "desc" in str(c).lower() or "nombre" in str(c).lower()), None)
    results = []
    for _, row in df.iterrows():
        stock_val = int(float(row[stock_col])) if stock_col and pd.notna(row[stock_col]) else 0
        desc_str = str(row[desc_col]) if desc_col and pd.notna(row[desc_col]) else ""
        if producto and producto.upper() not in desc_str.upper():
            continue
        if stock_val != 0:
            results.append({"modelo": "", "descripcion": desc_str, "marca": "", "stock": stock_val, "trf": 0})
    return results

READER_MAP = {
    "falabella": read_falabella,
    "ripley":    read_ripley,
    "paris":     read_generic,
    "jumbo":     read_generic,
    "tottus":    read_generic,
}

def format_whatsapp(cliente, tienda, producto, results, week):
    if not results:
        filtro = f" de *{producto}*" if producto else ""
        return f"No encontre stock{filtro} en *{cliente.upper()} {tienda.upper()}* (Semana {week}).\n\nVerifica el nombre de la tienda o el producto."
    lines = [f"*{cliente.upper()} -- {tienda.upper()}*", f"_Semana {week}_ | {len(results)} referencia(s)"]
    if producto:
        lines.append(f"_{producto}_")
    lines.append("")
    for r in results[:20]:
        emoji = "+" if r["stock"] > 0 else "-"
        lines.append(f"{emoji} {r['descripcion'][:35]}")
        lines.append(f"   {r['marca']} | Stock: {r['stock']}")
    if len(results) > 20:
        lines.append(f"\n_...y {len(results) - 20} mas_")
    return "\n".join(lines)

SYSTEM_PARSE = """
Eres un asistente que extrae informacion de consultas de stock.
Del mensaje del usuario extrae:
- cliente: uno de [Falabella, Ripley, Paris, Jumbo, Tottus]
- tienda: nombre de la tienda o sala (ej. "Parque Arauco", "Costanera", "Vespucio")
- producto: nombre o codigo del producto (opcional, puede ser null)

IMPORTANTE: la palabra "stock" NO es un producto. Si el usuario dice "stock Ripley Los Dominicos", el producto es null.
Palabras como "stock", "inventario", "consulta", "ver" NO son productos.

Responde SOLO con JSON valido:
{"cliente": "...", "tienda": "...", "producto": "..." }
o {"error": "no entendi"}
"""

PALABRAS_IGNORAR = {"stock", "inventario", "consulta", "ver", "buscar", "mostrar", "en", "de", "el", "la"}

def _parse_simple(msg):
    msg_lower = msg.lower()
    cliente = None
    for c in READER_MAP:
        if c in msg_lower:
            cliente = c.capitalize()
            break
    if not cliente:
        return {"error": "no entendi"}
    resto = msg_lower.replace(cliente.lower(), "").strip()
    for palabra in PALABRAS_IGNORAR:
        resto = resto.replace(palabra, "").strip()
    TIENDAS = [
        "parque arauco", "alto las condes", "costanera center", "costanera",
        "los dominicos", "plaza vespucio", "vespucio", "florida center", "florida",
        "plaza oeste", "plaza egana", "egana", "maipu",
        "quilicura", "la reina", "san bernardo", "rancagua",
        "concepcion", "la serena", "antofagasta",
        "iquique", "temuco", "valdivia", "puerto montt",
    ]
    tienda = None
    for t in TIENDAS:
        if t in resto:
            tienda = t.title()
            resto = resto.replace(t, "").strip()
            break
    if not tienda:
        palabras = resto.split()
        if palabras:
            tienda = " ".join(palabras[:2]).title()
            resto = " ".join(palabras[2:])
        else:
            return {"error": "no entendi"}
    producto = resto.strip() if resto.strip() else None
    return {"cliente": cliente, "tienda": tienda, "producto": producto}

def parse_query(msg):
    try:
        ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM_PARSE,
            messages=[{"role": "user", "content": msg}],
        )
        text = resp.content[0].text.strip()
        return json.loads(text)
    except Exception as e:
        log.warning("Claude API fallo (%s), usando parseo simple.", e)
        return _parse_simple(msg)

HELP_MSG = (
    "Hola! Soy el asistente de stock\n\n"
    "Ejemplos de consulta:\n"
    "- _Stock Falabella Parque Arauco_\n"
    "- _Mario Kart en Ripley Costanera_\n"
    "- _Jumbo Maipu_\n\n"
    "Clientes disponibles: Falabella, Ripley, Paris, Jumbo, Tottus"
)

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender = request.form.get("From", "")
    incoming = request.form.get("Body", "").strip()
    log.info("Mensaje de %s: %s", sender, incoming)
    resp = MessagingResponse()
    if sender not in NUMEROS_AUTORIZADOS:
        return str(resp)
    if incoming.lower() in ("hola", "help", "ayuda", "?", ""):
        resp.message(HELP_MSG)
        return str(resp)
    try:
        parsed = parse_query(incoming)
    except Exception:
        resp.message("No pude entender tu consulta. Escribe *ayuda* para ver ejemplos.")
        return str(resp)
    if "error" in parsed:
        resp.message("No entendi tu consulta\n\nEscribe algo como:\n_Stock Falabella Parque Arauco_\n_Ripley Los Dominicos_")
        return str(resp)
    cliente = parsed.get("cliente", "").strip()
    tienda = parsed.get("tienda", "").strip()
    producto = parsed.get("producto")
    if producto and producto.lower().strip() in PALABRAS_IGNORAR:
        producto = None
    week = find_best_week()
    reader_fn = READER_MAP.get(cliente.lower())
    if not reader_fn:
        resp.message(f"Cliente '{cliente}' no reconocido.\n\nDisponibles: {', '.join(READER_MAP)}")
        return str(resp)
    try:
        file_bytes = download_file_from_dropbox(cliente)
        results = reader_fn(file_bytes, tienda, producto)
    except Exception as e:
        log.error("Error leyendo archivo: %s", e)
        resp.message("Ocurrio un error leyendo el archivo. Intentalo de nuevo.")
        return str(resp)
    msg = format_whatsapp(cliente, tienda, producto, results, week)
    resp.message(msg)
    return str(resp)

@app.route("/health")
def health():
    return {"status": "ok", "week": get_current_week()}, 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
