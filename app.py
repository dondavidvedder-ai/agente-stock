"""
Agente WhatsApp - Consulta de Stock
====================================
Servidor Flask que recibe mensajes de WhatsApp via Twilio,
lee los archivos Excel de stock desde OneDrive y responde
con la informacion filtrada.

Autor: generado con Claude
"""

import os
import re
import json
import base64
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
ONEDRIVE_SHARE_URL = os.environ["ONEDRIVE_SHARE_URL"]   # URL publica de la carpeta Stock en OneDrive

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

# ── Acceso a OneDrive (enlace publico) ─────────────────────────────────────────

def _share_token(url: str) -> str:
    """Convierte la URL de compartir OneDrive al token que usa la API."""
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _graph_get(path: str) -> dict:
    token = _share_token(ONEDRIVE_SHARE_URL)
    full_url = f"https://graph.microsoft.com/v1.0/shares/{token}/root{path}"
    resp = requests.get(full_url, timeout=15)
    resp.raise_for_status()
    return resp.json()


def list_week_folder(week: str) -> list:
    """Lista los archivos de la carpeta de la semana indicada (ej. '12')."""
    try:
        data = _graph_get(f":/{week}:/children")
        return data.get("value", [])
    except Exception as e:
        log.error("Error listando carpeta semana %s: %s", week, e)
        return []


def download_file(download_url: str) -> io.BytesIO:
    """Descarga un archivo desde su URL directa de OneDrive."""
    resp = requests.get(download_url, timeout=30)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


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


# ── Lectura de archivos por cliente ────────────────────────────────────────────

def read_falabella(file_bytes: io.BytesIO, tienda: str, producto: str | None) -> list:
    df = pd.read_excel(file_bytes, sheet_name="General", header=None)
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
    xl = pd.ExcelFile(file_bytes)
    sheet = next((s for s in xl.sheet_names if "MATTEL" in s.upper()), None)
    if sheet is None:
        return []
    df = pd.read_excel(file_bytes, sheet_name=sheet, header=0)
    mask = df["Sucursal"].str.lower().str.contains(tienda.lower(), na=False)
    filtered = df[mask]
    if producto:
        p = producto.lower()
        mask_prod = (
            filtered["Desc.Art.Ripley"].str.lower().str.contains(p, na=False)
            | filtered["Desc. Art. Prov. (Case Pack)"].str.lower().str.contains(p, na=False)
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
        cod_col  = next((c for c in df.columns if "prov" in c.lower() and "cod" in c.lower()), None)
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
    try:
        xl = pd.ExcelFile(file_bytes)
        df = pd.read_excel(file_bytes, sheet_name=xl.sheet_names[0], header=0)
    except Exception:
        return []
    tienda_col = next(
        (c for c in df.columns if "tienda" in str(c).lower() or "sala" in str(c).lower()
         or "sucursal" in str(c).lower() or "local" in str(c).lower()),
        None,
    )
    if tienda_col:
        df = df[df[tienda_col].str.lower().str.contains(tienda.lower(), na=False)]
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
            results.append({"modelo": "", "descripcion": desc_str, "marca": "", "stock": stock_val, "trf": 0})
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

def parse_query(msg: str) -> dict:
    ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = ac.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM_PARSE,
        messages=[{"role": "user", "content": msg}],
    )
    text = resp.content[0].text.strip()
    return json.loads(text)


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
    sender   = request.form.get("From", "")
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
    week     = get_current_week()

    reader_fn = READER_MAP.get(cliente.lower())
    if not reader_fn:
        resp.message(f"Cliente '{cliente}' no reconocido.\n\nDisponibles: {', '.join(READER_MAP)}")
        return str(resp)

    files = list_week_folder(week)
    if not files:
        resp.message(f"No encontre archivos para la semana {week} en OneDrive.")
        return str(resp)

    client_file = find_client_file(files, cliente)
    if not client_file:
        resp.message(f"No hay archivo de {cliente} para la semana {week}.")
        return str(resp)

    try:
        file_bytes = download_file(client_file["@microsoft.graph.downloadUrl"])
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
