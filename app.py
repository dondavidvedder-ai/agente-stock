"""
Agente WhatsApp - Consulta de Stock
Un solo archivo Excel con todos los clientes.
Columnas: Cliente, Nombre Tienda, Descripcion producto, Marca, Stock
"""

import os, io, json, logging
from datetime import date

import pandas as pd
import anthropic
import requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]

DROPBOX_URL = "https://www.dropbox.com/scl/fi/aqmm5fxe20il0u06jl0i0/Stock-13.xlsx?rlkey=v3s0ppno2jkkvw1y0mokh3qlv&dl=1"

NUMEROS_AUTORIZADOS = {
    "whatsapp:+56926121144",
    "whatsapp:+56953634351",
    "whatsapp:+56972494232",
    "whatsapp:+56997054149",
    "whatsapp:+56954077612",
    "whatsapp:+56972495007",
    "whatsapp:+56990674664",
}

CLIENTES_VALIDOS = {"falabella", "ripley", "paris", "jumbo", "tottus", "walmart"}
PALABRAS_IGNORAR = {"stock", "inventario", "consulta", "ver", "buscar", "mostrar", "dame", "hay"}

_cache = {"data": None}

def get_dataframe():
    if _cache["data"] is not None:
        return _cache["data"]
    log.info("Descargando archivo desde Dropbox...")
    resp = requests.get(DROPBOX_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), sheet_name="base", header=0)
    log.info(f"Archivo cargado: {len(df)} filas")
    _cache["data"] = df
    return df

def consultar_stock(cliente, tienda, producto):
    df = get_dataframe()
    mask_c = df["Cliente"].str.lower() == cliente.lower()
    mask_t = pd.Series([False] * len(df), index=df.index)
    for word in tienda.lower().split():
        if len(word) > 2:
            mask_t |= df["Nombre Tienda"].str.lower().str.contains(word, na=False, regex=False)
    filtered = df[mask_c & mask_t]
    if len(filtered) == 0:
        disponibles = df[mask_c]["Nombre Tienda"].unique()[:5]
        log.info(f"Sin resultados para {cliente}/{tienda}. Disponibles: {list(disponibles)}")
        return []
    if producto:
        filtered = filtered[
            filtered["Descripcion producto"].str.upper().str.contains(producto.upper(), na=False) |
            filtered["Marca"].str.upper().str.contains(producto.upper(), na=False)
        ]
    results = []
    for _, row in filtered.iterrows():
        stock = int(row["Stock"]) if pd.notna(row["Stock"]) else 0
        results.append({
            "tienda":      str(row.get("Nombre Tienda", "")),
            "descripcion": str(row.get("Descripcion producto", ""))[:50],
            "marca":       str(row.get("Marca", "")),
            "stock":       stock,
        })
    return results[:25]

def format_respuesta(cliente, tienda, producto, results):
    semana = str(date.today().isocalendar()[1]).zfill(2)
    if not results:
        filtro = f" de *{producto}*" if producto else ""
        return f"Sin stock{filtro} en *{cliente.upper()} {tienda.upper()}* (Sem {semana}).\nVerifica el nombre de la tienda."
    header = f"*{cliente.upper()} - {tienda.upper()}* (Sem {semana})\n_{len(results)} producto(s)_"
    if producto:
        header += f" - _{producto}_"
    lineas = [header, ""]
    for r in results[:20]:
        emoji = "+" if r["stock"] > 0 else "-"
        lineas.append(f"{emoji} {r['descripcion'][:40]}")
        lineas.append(f"   {r['marca']} | Stock: {r['stock']}")
    if len(results) > 20:
        lineas.append(f"\n...y {len(results)-20} mas")
    return "\n".join(lineas)

SYSTEM_PARSE = """
Extrae del mensaje:
- cliente: uno de [falabella, ripley, paris, jumbo, tottus, walmart]
- tienda: nombre de tienda
- producto: marca o producto (null si no se menciona)
"stock" NO es un producto.
JSON: {"cliente":"...","tienda":"...","producto":null}
o {"error":"no entendi"}
"""

def parse_query(msg):
    try:
        ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=SYSTEM_PARSE,
            messages=[{"role": "user", "content": msg}],
        )
        return json.loads(resp.content[0].text.strip())
    except Exception as e:
        log.warning("Claude API fallo: %s", e)
        return parse_simple(msg)

def parse_simple(msg):
    lower = msg.lower()
    for w in PALABRAS_IGNORAR:
        lower = lower.replace(w, " ")
    lower = " ".join(lower.split())
    cliente = None
    for c in CLIENTES_VALIDOS:
        if c in lower:
            cliente = c.capitalize()
            lower = lower.replace(c, " ").strip()
            break
    if not cliente:
        return {"error": "no entendi"}
    TIENDAS = [
        "los dominicos", "parque arauco", "alto las condes", "costanera center",
        "costanera", "plaza vespucio", "vespucio", "florida center", "florida",
        "plaza oeste", "plaza egana", "egana", "maipu", "quilicura",
        "la reina", "san bernardo", "rancagua", "antofagasta", "concepcion",
        "la serena", "iquique", "temuco", "valdivia", "puerto montt",
        "huerfanos", "astor", "arica", "chillan", "copiapo", "coquimbo",
    ]
    tienda = None
    for t in TIENDAS:
        if t in lower:
            tienda = t.title()
            lower = lower.replace(t, " ").strip()
            break
    if not tienda:
        palabras = lower.split()
        tienda = " ".join(palabras[:3]).title() if palabras else None
        lower = " ".join(palabras[3:]) if len(palabras) > 3 else ""
    if not tienda:
        return {"error": "no entendi"}
    producto = lower.strip() if lower.strip() and lower.strip() not in PALABRAS_IGNORAR else None
    return {"cliente": cliente, "tienda": tienda, "producto": producto}

HELP_MSG = "Hola! Soy el asistente de stock\n\nEjemplos:\n- Ripley Los Dominicos\n- Falabella Parque Arauco\n- Barbie Ripley Costanera\n\nClientes: Falabella, Ripley, Jumbo, Tottus, Walmart"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender   = request.form.get("From", "")
    incoming = request.form.get("Body", "").strip()
    log.info("De %s: %s", sender, incoming)
    resp = MessagingResponse()
    if sender not in NUMEROS_AUTORIZADOS:
        return str(resp)
    if incoming.lower() in ("hola", "help", "ayuda", "?", ""):
        resp.message(HELP_MSG)
        return str(resp)
    parsed = parse_query(incoming)
    log.info("Parsed: %s", parsed)
    if "error" in parsed:
        resp.message("No entendi\nEscribe por ejemplo: Ripley Los Dominicos")
        return str(resp)
    cliente  = parsed.get("cliente", "").strip()
    tienda   = parsed.get("tienda", "").strip()
    producto = parsed.get("producto")
    if producto and producto.lower() in PALABRAS_IGNORAR:
        producto = None
    try:
        results = consultar_stock(cliente, tienda, producto)
    except Exception as e:
        log.error("Error: %s", e)
        resp.message("Error leyendo el archivo. Intenta de nuevo.")
        return str(resp)
    resp.message(format_respuesta(cliente, tienda, producto, results))
    return str(resp)

@app.route("/test")
def test():
    msg = request.args.get("msg", "Ripley Los Dominicos")
    parsed = parse_query(msg)
    if "error" in parsed:
        return {"error": "No se pudo parsear", "msg": msg}
    cliente  = parsed.get("cliente", "")
    tienda   = parsed.get("tienda", "")
    producto = parsed.get("producto")
    if producto and producto.lower() in PALABRAS_IGNORAR:
        producto = None
    try:
        results = consultar_stock(cliente, tienda, producto)
    except Exception as e:
        return {"error": str(e)}
    return {
        "parsed": parsed,
        "producto_final": producto,
        "resultados": len(results),
        "muestra": results[:3],
        "respuesta": format_respuesta(cliente, tienda, producto, results),
    }

@app.route("/health")
def health():
    return {"status": "ok", "semana": str(date.today().isocalendar()[1]).zfill(2)}, 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
