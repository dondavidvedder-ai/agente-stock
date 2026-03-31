"""
Agente WhatsApp - Consulta de Stock
====================================
Servidor Flask que recibe mensajes de WhatsApp via Twilio,
lee los archivos Excel de stock desde Google Drive y responde
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
from googleapiclient.discovery import build

# ── Configuracion ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY        = os.environ["ANTHROPIC_API_KEY"]
TWILIO_AUTH_TOKEN        = os.environ["TWILIO_AUTH_TOKEN"]
GOOGLE_DRIVE_API_KEY     = os.environ["GOOGLE_DRIVE_API_KEY"]
GOOGLE_DRIVE_FOLDER_ID   = os.environ["GOOGLE_DRIVE_FOLDER_ID"]  # ID de la carpeta Stock en Google Drive

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

# ── Acceso a Google Drive ─────────────────────────────────────────────────────

def _get_drive_service():
    """Retorna servicio de Google Drive API."""
    return build("drive", "v3", developerKey=GOOGLE_DRIVE_API_KEY)


def _find_week_folder(week: str) -> str | None:
    """Busca la carpeta de semana (ej. '13') dentro de la carpeta principal."""
    try:
        service = _get_drive_service()
        query = (
            f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents "
            f"and name = '{week}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        results = service.files().list(q=query, spaces="drive", fields="files(id, name)").execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except Exception as e:
        log.error("Error buscando carpeta semana %s: %s", week, e)
        return None


def list_week_folder(week: str) -> list:
    """Lista los archivos de la carpeta de la semana indicada (ej. '12')."""
    week_folder_id = _find_week_folder(week)
    if not week_folder_id:
        return []

    try:
        service = _get_drive_service()
        query = (
            f"'{week_folder_id}' in parents "
            f"and mimeType != 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        results = service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name, webContentLink)",
            pageSize=50
        ).execute()
        return results.get("files", [])
    except Exception as e:
        log.error("Error listando carpeta semana %s: %s", week, e)
        return []


def download_file(file_id: str) -> io.BytesIO:
    """Descarga un archivo desde Google Drive."""
    try:
        service = _get_drive_service()
        request = service.files().get_media(fileId=file_id)
        file_content = request.execute()
        return io.BytesIO(file_content)
    except Exception as e:
        log.error("Error descargando archivo %s: %s", file_id, e)
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
    """Busca la carpeta de semana mas reciente disponible en Google Drive (hasta 4 semanas atras)."""
    current = date.today().isocalendar()[1]
    for offset in range(0, 5):
        week = str(current - offset).zfill(2)
        try:
            if _find_week_folder(week):
                return week
        except Exception:
            continue
    return get_current_week()  # fallback


# ── Lectura de archivos por cliente ────────────────────────────────────────────

def read_falabella(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    """
    Lee Falabella.xlsx (formato pivot, hoja 'General').
    Estructura: fila 3 = nombres de tienda, fila 4 = headers, datos desde fila 5.
    """
    df = pd.read_excel(file_bytes, sheet_name="General", header=None)

    # Buscar columna de la tienda
    store_row = df.iloc[3, :]
    col_idx = None
    for i, val in store_row.items():
        if pd.notna(val) and tienda.upper() in str(val).upper():
            col_idx = i
            break

    if col_idx is None:
        return []

    results = []
    for row_idx in range(5, len(df)):
        modelo = df.iloc[row_idx, 0]
        desc   = df.iloc[row_idx, 1]
        stock  = df.iloc[row_idx, col_idx]
        trf    = df.iloc[row_idx, col_idx + 1] if col_idx + 1 < len(df.columns) else 0

        if not pd.notna(modelo) or not pd.notna(stock):
            continue

        desc_str = str(desc) if pd.notna(desc) else ""

        # Filtrar por producto si se especifico
        if producto:
            hay_match = (
                producto.upper() in desc_str.upper()
                or producto.upper() in str(modelo).upper()
            )
            if not hay_match:
                continue

        try:
            stock_val = int(float(stock)) if pd.notna(stock) else 0
            trf_val   = int(float(trf))   if pd.notna(trf)   else 0
            if stock_val != 0 or trf_val != 0:
                results.append({
                    "modelo":      str(modelo),
                    "descripcion": desc_str,
                    "marca":       "MATTEL",
                    "stock":       stock_val,
                    "trf":         trf_val,
                })
        except (ValueError, TypeError):
            pass

    return results


def read_ripley(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    """
    Lee Ripley.xlsx (hoja detallada con columnas: Sucursal, Marca, Desc, Stock...).
    """
    # Nombre de hoja largo con espacios al final
    xl = pd.ExcelFile(file_bytes)
    sheet = next((s for s in xl.sheet_names if "MATTEL" in s.upper()), None)
    if sheet is None:
        return []

    df = pd.read_excel(file_bytes, sheet_name=sheet, header=0)

    # Filtrar por tienda
    mask = df["Sucursal"].str.lower().str.contains(tienda.lower(), na=False)
    filtered = df[mask]

    # Filtrar por producto
    if producto:
        p = producto.lower()
        mask_prod = (
            filtered["Desc.Art.Ripley"]
            .str.lower()
            .str.contains(p, na=False)
            | filtered["Desc. Art. Prov. (Case Pack)"]
            .str.lower()
            .str.contains(p, na=False)
        )
        filtered = filtered[mask_prod]

    results = []
    marca_col = next((c for c in df.columns if "marca" in c.lower()), None)

    for _, row in filtered.iterrows():
        stock = row.get("Stock on Hand Disponible (u)", 0)
        trf   = row.get("Tranferencias on order (u)", 0)
        if not pd.notna(stock):
            continue
        stock_val = int(float(stock))
        trf_val   = int(float(trf)) if pd.notna(trf) else 0
        if stock_val == 0 and trf_val == 0:
            continue

        marca = str(row[marca_col]).strip() if marca_col else "MATTEL"
        cod_col = next((c for c in df.columns if "prov" in c.lower() and "cód" in c.lower()), None)
        desc_col = next((c for c in df.columns if "desc" in c.lower() and "prov" in c.lower()), None)

        results.append({
            "modelo":      str(row[cod_col]).strip()  if cod_col  else "",
            "descripcion": str(row[desc_col]).strip() if desc_col else "",
            "marca":       marca,
            "stock":       stock_val,
            "trf":         trf_val,
        })

    return results


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

    # Buscar archivo en Google Drive
    files = list_week_folder(week)
    if not files:
        resp.message(f"No encontre archivos para la semana {week} en Google Drive.")
        return str(resp)

    client_file = find_client_file(files, cliente)
    if not client_file:
        resp.message(f"No hay archivo de {cliente} para la semana {week}.")
        return str(resp)

    # Descargar y leer
    try:
        file_bytes = download_file(client_file["id"])
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

