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

# ── Configuracion ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

# URLs directas de Dropbox para cada cliente
DROPBOX_FILES = {
    "falabella": "https://www.dropbox.com/scl/fi/nm4mpaqbv1z8zefz2et5t/Falabella.xlsx?rlkey=cupt094g92jpbh8uevc2hoirx&st=vaoqhcfa&dl=1",
    "ripley": "https://www.dropbox.com/scl/fi/tp306bb75aym4yrlr9gch/Ripley.xlsx?rlkey=dnjioc8fcjwiapedccvg410sx&st=v2ix91tb&dl=1",
    "walmart": "https://www.dropbox.com/scl/fi/94cobt417zg6ltgz940fl/Walmart.xlsx?rlkey=tu8ig67ktfrjlnlyk75f2dibw&st=4oj4g8kt&dl=1",
    "jumbo": "https://www.dropbox.com/scl/fi/arcuhsmvg3xe8iqwx4llr/Jumbo.xlsx?rlkey=56y14tkk2boiuuwvlj2kexdsx&st=wm6hhrbg&dl=1",
    "tottus": "https://www.dropbox.com/scl/fi/ldfv1i3m5dcrqiy8vfykk/Tottus.xlsx?rlkey=egn9vsr2bnrj2t69vwbu6m7sp&st=v7ydie76&dl=1",
}

# ── Lista blanca de numeros autorizados ───────────────────────────────────────
# Solo estos numeros pueden consultar el stock. Formato: whatsapp:+56XXXXXXXXX

NUMEROS_AUTORIZADOS = {
    "whatsapp:+56926121144",
    "whatsapp:+56953634351",
    "whatsapp:+56972494232",
    "whatsapp:+56997054149",
    "whatsapp:+56954077612",
    "whatsapp:+56972495007",
    "whatsapp:+56990674664",
}

# ── Acceso a Dropbox ──────────────────────────────────────────────────────────

def download_file_from_dropbox(cliente: str) -> io.BytesIO:
    """Descarga un archivo Excel desde Dropbox usando URL directa."""
    try:
        cliente_lower = cliente.lower()
        url = DROPBOX_FILES.get(cliente_lower)

        if not url:
            log.error(f"No hay URL de Dropbox para cliente: {cliente}")
            raise ValueError(f"Cliente '{cliente}' no disponible en Dropbox")

        log.info(f"Descargando {cliente} desde Dropbox: {url[:50]}...")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()

        return io.BytesIO(resp.content)

    except Exception as e:
        log.error(f"Error descargando {cliente} de Dropbox: {e}")
        raise


def find_client_file(files: list, cliente: str) -> dict | None:
    """Busca el archivo correspondiente al cliente dentro de los archivos listados."""
    cliente_lower = cliente.lower()
    for f in files:
        if cliente_lower in f["name"].lower():
            return f
    return None


def get_current_week() -> str:
    """Retorna el numero de semana actual (con cero a la izquierda si < 10)."""
    w = date.today().isocalendar()[1]
    return str(w).zfill(2)


def find_best_week() -> str:
    """Retorna la semana actual (en Dropbox usamos siempre la semana 13 por ahora)."""
    # En una versión mejorada, podríamos tener URLs para diferentes semanas
    # Por ahora, asumimos que siempre están actualizados en la semana actual
    return get_current_week()


# ── Lectura de archivos por cliente ────────────────────────────────────────────

def read_falabella(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    """
    Lee Falabella.xlsx (Sheet1).
    Columnas: FECHA, EAN ID, SKU ID, ID Estilo, DESCRIPCIÓN, SUBCLASE, DESC SUBCLASE, MARCA, ..., M=Tienda
    """
    try:
        df = pd.read_excel(file_bytes, sheet_name="Sheet1", header=0)

        log.info(f"Falabella - Columnas: {list(df.columns[:15])}")
        log.info(f"Falabella - Shape: {df.shape}")

        # Encontrar columnas relevantes
        desc_col = None
        marca_col = None
        tienda_col = None

        for col in df.columns:
            col_lower = str(col).lower().strip()
            if "descripción" in col_lower or "descripcion" in col_lower:
                desc_col = col
            elif "marca" in col_lower:
                marca_col = col

        # Columna M debería ser tienda (13ava columna, índice 12)
        if len(df.columns) > 12:
            tienda_col = df.columns[12]  # Columna M (índice 12)

        log.info(f"Falabella - Columnas detectadas: desc={desc_col}, marca={marca_col}, tienda={tienda_col}")

        if not desc_col or not marca_col or not tienda_col:
            log.warning(f"Falabella: Columnas faltantes")
            return []

        # Filtrar por tienda (búsqueda flexible: case-insensitive, parcial)
        tienda_lower = tienda.lower().strip()
        mask = df[tienda_col].astype(str).str.lower().str.contains(tienda_lower, na=False, regex=False)
        filtered = df[mask]

        if len(filtered) == 0:
            log.info(f"Falabella: No hay tiendas que coincidan con '{tienda}'. Disponibles: {df[tienda_col].unique()[:5]}")
            return []

        results = []
        for _, row in filtered.iterrows():
            desc_str = str(row.get(desc_col, "")).strip()
            marca = str(row.get(marca_col, "")).strip()

            # Filtrar por producto
            if producto and producto.upper() not in desc_str.upper():
                continue

            if desc_str:
                results.append({
                    "modelo": "",
                    "descripcion": desc_str[:50],
                    "marca": marca,
                    "stock": 0,
                    "trf": 0,
                })

        return results[:20]

    except Exception as e:
        log.error(f"Error leyendo Falabella: {e}")
        import traceback
        log.error(traceback.format_exc())
        return []


def read_ripley(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    """
    Lee Ripley.xlsx (hoja "base").
    Columnas: Cod. Sucursal, Sucursal, Cod. Marca, Marca, ...
    """
    try:
        df = pd.read_excel(file_bytes, sheet_name="BASE", header=0)

        log.info(f"Ripley - Columnas: {list(df.columns[:10])}")
        log.info(f"Ripley - Shape: {df.shape}")

        # Filtrar por tienda (columna "Sucursal")
        sucursal_col = next((c for c in df.columns if "sucursal" in str(c).lower()), None)
        marca_col = next((c for c in df.columns if "marca" in str(c).lower() and "cod" not in str(c).lower()), None)

        if not sucursal_col:
            log.warning("Ripley: No se encontró columna Sucursal")
            return []

        # Búsqueda flexible: case-insensitive, parcial
        tienda_lower = tienda.lower().strip()
        mask = df[sucursal_col].astype(str).str.lower().str.contains(tienda_lower, na=False, regex=False)
        filtered = df[mask]

        if len(filtered) == 0:
            log.info(f"Ripley: No hay sucursales que coincidan con '{tienda}'. Disponibles: {df[sucursal_col].unique()[:5]}")
            return []

        results = []
        for _, row in filtered.iterrows():
            desc = str(row.get(sucursal_col, "")).strip()
            marca = str(row.get(marca_col, "")).strip() if marca_col else ""

            # Filtrar por producto
            if producto and producto.upper() not in desc.upper():
                continue

            if desc:
                results.append({
                    "modelo": "",
                    "descripcion": desc[:50],
                    "marca": marca,
                    "stock": 0,
                    "trf": 0,
                })

        return results[:20]

    except Exception as e:
        log.error(f"Error leyendo Ripley: {e}")
        return []


def read_generic(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    """
    Lector generico para archivos con columnas de tienda/producto/stock.
    Intenta detectar automaticamente las columnas relevantes.
    """
    try:
        xl = pd.ExcelFile(file_bytes)
        df = pd.read_excel(file_bytes, sheet_name=xl.sheet_names[0], header=0)
    except Exception:
        return []

    # Buscar columna de tienda
    tienda_col = next(
        (c for c in df.columns if "tienda" in str(c).lower() or "sala" in str(c).lower()
         or "sucursal" in str(c).lower() or "local" in str(c).lower()),
        None,
    )
    if tienda_col:
        df = df[df[tienda_col].str.lower().str.contains(tienda.lower(), na=False)]

    # Buscar columna de stock
    stock_col = next(
        (c for c in df.columns if "stock" in str(c).lower() and "disponible" in str(c).lower()),
        next((c for c in df.columns if "stock" in str(c).lower()), None),
    )
    desc_col = next(
        (c for c in df.columns if "desc" in str(c).lower() or "nombre" in str(c).lower()),
        None,
    )

    results = []
    for _, row in df.iterrows():
        stock_val = int(float(row[stock_col])) if stock_col and pd.notna(row[stock_col]) else 0
        desc_str  = str(row[desc_col]) if desc_col and pd.notna(row[desc_col]) else ""

        if producto and producto.upper() not in desc_str.upper():
            continue
        if stock_val != 0:
            results.append({
                "modelo": "",
                "descripcion": desc_str,
                "marca": "",
                "stock": stock_val,
                "trf": 0,
            })

    return results


# ── Mapa de clientes a funciones lectoras ──────────────────────────────────────

READER_MAP = {
    "falabella": read_falabella,
    "ripley":    read_ripley,
    "paris":     read_generic,
    "jumbo":     read_generic,
    "tottus":    read_generic,
}


# ── Formateo de respuesta ──────────────────────────────────────────────────────

def format_whatsapp(cliente, tienda, producto, results, week) -> str:
    if not results:
        filtro = f" de *{producto}*" if producto else ""
        return (
            f"No encontre stock{filtro} en *{cliente.upper()} {tienda.upper()}* "
            f"(Semana {week}).\n\nVerifica el nombre de la tienda o el producto."
        )

    lines = [
        f"📦 *{cliente.upper()} — {tienda.upper()}*",
        f"_Semana {week}_ | {len(results)} referencia(s)",
    ]
    if producto:
        lines.append(f"🔍 _{producto}_")
    lines.append("")

    for r in results[:20]:
        emoji = "✅" if r["stock"] > 0 else "⚠️"
        desc  = r["descripcion"][:35]
        lines.append(f"{emoji} *{r['modelo']}* | {desc}")
        lines.append(f"   {r['marca']} | Stock: {r['stock']} | TRF: {r['trf']}")

    if len(results) > 20:
        lines.append(f"\n_...y {len(results) - 20} referencias mas_")

    return "\n".join(lines)


# ── Parseo inteligente con Claude ──────────────────────────────────────────────

SYSTEM_PARSE = """
Eres un asistente que extrae informacion de consultas de stock.
Del mensaje del usuario extrae:
- cliente: uno de [Falabella, Ripley, Paris, Jumbo, Tottus]
- tienda: nombre de la tienda o sala (ej. "Parque Arauco", "Costanera", "Vespucio")
- producto: nombre o codigo del producto (opcional, puede ser null)

Responde SOLO con JSON valido, sin texto adicional:
{"cliente": "...", "tienda": "...", "producto": "..." }
o si no puedes identificar cliente/tienda:
{"error": "no entendi"}
"""

def _parse_simple(msg: str) -> dict:
    """Parseo de respaldo sin API: detecta cliente y tienda por palabras clave."""
    msg_lower = msg.lower()

    # Detectar cliente
    cliente = None
    for c in READER_MAP:
        if c in msg_lower:
            cliente = c.capitalize()
            break

    if not cliente:
        return {"error": "no entendi"}

    # Quitar el nombre del cliente del mensaje para extraer tienda/producto
    resto = msg_lower.replace(cliente.lower(), "").strip()

    # Tiendas conocidas (orden de mayor a menor especificidad)
    TIENDAS = [
        "parque arauco", "alto las condes", "costanera center", "costanera",
        "vespucio", "plaza vespucio", "florida center", "florida",
        "plaza oeste", "plaza egana", "egana", "maipu", "maipú",
        "quilicura", "la reina", "san bernardo", "rancagua",
        "concepcion", "concepción", "la serena", "antofagasta",
        "iquique", "temuco", "valdivia", "puerto montt",
    ]
    tienda = None
    for t in TIENDAS:
        if t in resto:
            tienda = t.title()
            resto = resto.replace(t, "").strip()
            break

    # Si no se encontró tienda conocida, tomar las primeras palabras restantes
    if not tienda:
        palabras = resto.split()
        if palabras:
            tienda = " ".join(palabras[:2]).title()
            resto = " ".join(palabras[2:])
        else:
            return {"error": "no entendi"}

    producto = resto.strip() if resto.strip() else None

    return {"cliente": cliente, "tienda": tienda, "producto": producto}


def parse_query(msg: str) -> dict:
    """Intenta con Claude API; si falla usa parseo simple de respaldo."""
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


# ── Endpoint principal WhatsApp ────────────────────────────────────────────────

HELP_MSG = (
    "Hola! Soy el asistente de stock 📦\n\n"
    "Ejemplos de consulta:\n"
    "• _Stock Falabella Parque Arauco_\n"
    "• _Mario Kart en Ripley Costanera_\n"
    "• _Jumbo Maipu_\n\n"
    "Clientes disponibles: Falabella, Ripley, Paris, Jumbo, Tottus"
)


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender  = request.form.get("From", "")
    incoming = request.form.get("Body", "").strip()
    log.info("Mensaje de %s: %s", sender, incoming)

    resp = MessagingResponse()

    # Verificar numero autorizado
    if sender not in NUMEROS_AUTORIZADOS:
        log.warning("Numero no autorizado: %s", sender)
        return str(resp)   # Silencio total — no responde nada

    # Ayuda
    if incoming.lower() in ("hola", "help", "ayuda", "?", ""):
        resp.message(HELP_MSG)
        return str(resp)

    try:
        parsed = parse_query(incoming)
    except Exception as e:
        log.error("Error parseando query: %s", e)
        resp.message("No pude entender tu consulta. Escribe *ayuda* para ver ejemplos.")
        return str(resp)

    if "error" in parsed:
        resp.message(
            "No entendi tu consulta 😅\n\n"
            "Escribe algo como:\n_Stock Falabella Parque Arauco_\n_Mario Kart Ripley Costanera_"
        )
        return str(resp)

    cliente  = parsed.get("cliente", "").strip()
    tienda   = parsed.get("tienda", "").strip()
    producto = parsed.get("producto")
    week     = find_best_week()

    # Validar cliente
    reader_fn = READER_MAP.get(cliente.lower())
    if not reader_fn:
        resp.message(f"Cliente '{cliente}' no reconocido.\n\nDisponibles: {', '.join(READER_MAP)}")
        return str(resp)

    # Descargar archivo de Dropbox
    try:
        file_bytes = download_file_from_dropbox(cliente)
        results    = reader_fn(file_bytes, tienda, producto)
    except Exception as e:
        log.error("Error leyendo archivo: %s", e)
        resp.message("Ocurrio un error leyendo el archivo ⚠️. Intentalo de nuevo.")
        return str(resp)

    msg = format_whatsapp(cliente, tienda, producto, results, week)
    resp.message(msg)
    return str(resp)


@app.route("/health")
def health():
    return {"status": "ok", "week": get_current_week()}, 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
