"""
Agente WhatsApp - Consulta de Stock
Un solo archivo Excel con todos los clientes.
Columnas: Cliente, Nombre Tienda, Descripcion producto, Marca, Stock
"""

import os, io, json, logging, re
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
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

# UN solo archivo con todos los clientes
# Actualiza STOCK_URL en Railway cada semana sin tocar el código
DROPBOX_URL = os.environ.get(
    "STOCK_URL",
    "https://www.dropbox.com/scl/fi/aqmm5fxe20il0u06jl0i0/Stock-13.xlsx?rlkey=v3s0ppno2jkkvw1y0mokh3qlv&dl=1"
)

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

# Patrón para detectar códigos SKU Mattel (ej: C4982, DXV29, HRJ78, W2085)
SKU_RE = re.compile(r'\b([A-Z]{1,3}\d{3,6}[A-Z]?\d?)\b', re.IGNORECASE)

_cache = {"data": None}

def get_dataframe():
    """Descarga el archivo de Dropbox (o usa cache)."""
    if _cache["data"] is not None:
        return _cache["data"]
    log.info("Descargando archivo desde Dropbox...")
    resp = requests.get(DROPBOX_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content), sheet_name="base", header=0)
    log.info(f"Archivo cargado: {len(df)} filas")
    _cache["data"] = df
    return df

def consultar_stock(cliente: str, tienda: str, producto: str | None) -> list:
    """Filtra el DataFrame por cliente, tienda y producto opcional."""
    df = get_dataframe()

    # Filtrar por cliente
    mask_c = df["Cliente"].str.lower() == cliente.lower()

    # Filtrar por tienda (cualquier palabra)
    mask_t = pd.Series([False] * len(df), index=df.index)
    for word in tienda.lower().split():
        if len(word) > 2:
            mask_t |= df["Nombre Tienda"].str.lower().str.contains(word, na=False, regex=False)

    filtered = df[mask_c & mask_t]

    if len(filtered) == 0:
        tiendas_disponibles = df[mask_c]["Nombre Tienda"].unique()[:5]
        log.info(f"Sin resultados para {cliente}/{tienda}. Tiendas disponibles: {list(tiendas_disponibles)}")
        return []

    # Filtrar por producto si se especificó
    if producto:
        mask_prod = (
            filtered["Descripcion producto"].str.upper().str.contains(producto.upper(), na=False) |
            filtered["Marca"].str.upper().str.contains(producto.upper(), na=False) |
            filtered["Sku Mattel"].str.upper().str.contains(producto.upper(), na=False) |
            filtered["descuento"].str.upper().str.contains(producto.upper(), na=False)
        )
        if "Actividad" in filtered.columns:
            mask_prod |= filtered["Actividad"].str.upper().str.contains(producto.upper(), na=False) |
            filtered["descuento"].str.upper().str.contains(producto.upper(), na=False)
        )
        if "Actividad" in filtered.columns:
            mask_prod |= filtered["Actividad"].str.upper().str.contains(producto.upper(), na=False)
        filtered = filtered[mask_prod]

    results = []
    for _, row in filtered.iterrows():
        try:
            stock = int(row["Stock"]) if pd.notna(row["Stock"]) else 0
        except (ValueError, TypeError):
            stock = 0
        try:
            venta = int(row["Venta"]) if "Venta" in row.index and pd.notna(row["Venta"]) else 0
        except (ValueError, TypeError):
            venta = 0
        results.append({
            "sku_mattel": str(row.get("Sku Mattel", "")),
            "descripcion": str(row.get("Descripcion producto", ""))[:60],
            "actividad": str(row.get("Actividad", "")) if "Actividad" in row.index else "",
            "stock": stock,
            "venta": venta,
        })

    # Si hay producto específico: mostrar TODOS los resultados
    if producto:
        return results

    # Si NO hay producto: ordenar por stock descendente y devolver TOP 50
    results.sort(key=lambda x: x["stock"], reverse=True)
    return results[:50]


def format_respuesta(cliente, tienda, producto, results) -> str:
    semana = str(date.today().isocalendar()[1]).zfill(2)

    if not results:
        filtro = f" de *{producto}*" if producto else ""
        return (
            f"Sin stock{filtro} en *{cliente.upper()} {tienda.upper()}* (Sem {semana}).\n"
            f"Verifica el nombre de la tienda."
        )

    header = f"\U0001f4e6 *{cliente.upper()} \u2014 {tienda.upper()}* (Sem {semana})\n"
    header += f"_{len(results)} producto(s)_"
    if producto:
        header += f" \u00b7 _{producto}_"
    header += "\n"

    lineas = [header]

    # Mostrar primeros 10 (limite sandbox Twilio ~1600 chars)
    max_display = min(10, len(results))
    for r in results[:max_display]:
        estado = "\u2705" if r["stock"] > 0 else "\u26a0\ufe0f"
        desc = r['descripcion'].strip()
        lineas.append(f"{estado} {desc}")
        lineas.append(f"   SKU: {r['sku_mattel'].strip()} \u00b7 Stock: {r['stock']} \u00b7 Venta: {r['venta']}")

    if len(results) > max_display:
        lineas.append(f"\n_...y {len(results)-max_display} mas. Busca por producto para filtrar._")

    return "\n".join(lineas)


# \u2500\u2500 Parser con Claude \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

SYSTEM_PARSE = f"""
Extrae del mensaje del usuario:
- cliente: uno de {sorted(CLIENTES_VALIDOS)} (obligatorio)
- tienda: nombre de tienda (obligatorio)
- producto: marca, nombre de producto, o código SKU Mattel (opcional, null si no se menciona)

IMPORTANTE: Los códigos SKU Mattel son combinaciones cortas de letras y números como C4982, DXV29, HRJ78, W2085, K5904. Son PRODUCTOS, NO tiendas.
La palabra "stock" NO es un producto. Es solo una palabra de solicitud.

Ejemplos:
- "C4982 Walmart Vitacura" \u2192 clienente=walmart, tienda=vitacura, producto=C4982
- "Barbie Ripley Los Dominicos" → cliente=ripley, tienda=los dominicos, producto=barbie
- "Falabella Parque Arauco" → cliente=falabella, tienda=parque arauco, producto=null
- "Ripley Plaza" → cliente=ripley, tienda=plaza, producto=null
- "Ripley Oeste" → cliente=ripley, tienda=oeste, producto=null

Responde SOLO con JSON:
{{"cliente":"...","tienda":"...","producto":null}}
o {{"error":"no entendi"}}
"""

def parse_query(msg: str) -> dict:
    try:
        ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=SYSTEM_PARSE,
            messages=[{"role": "user", "content": msg}],
        )
        result = json.loads(resp.content[0].text.strip())
        if "error" in result:
            log.warning("Claude devolvio error para '%s', intentando parseo simple", msg)
            return parse_simple(msg)
        return result
    except Exception as e:
        log.warning("Claude API fallo: %s — usando parseo simple", e)
        return parse_simple(msg)

def parse_simple(msg: str) -> dict:
    """Parseo de respaldo sin API."""
    lower = msg.lower()

    # Eliminar palabras a ignorar
    for w in PALABRAS_IGNORAR:
        lower = lower.replace(w, " ")
    lower = " ".join(lower.split())  # normalizar espacios

    # Detectar cliente
    cliente = None
    for c in CLIENTES_VALIDOS:
        if c in lower:
            cliente = c.capitalize()
            lower = lower.replace(c, " ").strip()
            break

    if not cliente:
        return {"error": "no entendi"}

    # Detectar SKU Mattel antes de todo (ej: C4982, DXV29, HRJ78)
    sku_candidate = None
    sku_match = SKU_RE.search(lower)
    if sku_match:
        sku_candidate = sku_match.group(1).upper()
        lower = lower[:sku_match.start()] + " " + lower[sku_match.end():]
        lower = " ".join(lower.split())

    # Detectar tiendas conocidas (frases multi-palabra primero, luego palabras sueltas)
    TIENDAS = [
        # frases multi-palabra (van primero para que no se partan)
        "los dominicos", "parque arauco", "alto las condes", "costanera center",
        "plaza vespucio", "florida center", "plaza oeste", "plaza egana",
        "san bernardo", "puerto montt", "puente alto", "la florida",
        "la reina", "las condes", "la serena", "barros arana",
        "marina arauco", "arauco maipu", "paseo estacion", "plaza trebol",
        "portal belloto", "portal osorno", "portal temuco", "portal nunoa",
        "el llano", "el roble", "plaza vespucio",
        # palabras sueltas (ciudades, barrios, sectores)
        "costanera", "vespucio", "florida", "egana", "maipu", "quilicura",
        "rancagua", "antofagasta", "concepcion", "iquique", "temuco",
        "valdivia", "valparaiso", "huerfanos", "astor", "arica", "chillan",
        "copiapo", "coquimbo", "vitacura", "providencia", "nunoa", "recoleta",
        "pudahuel", "cerrillos", "bandera", "lyon", "huechuraba", "quilin",
        "independencia", "quilpue", "quillota", "talcahuano", "coronel",
        "curico", "melipilla", "ovalle", "calama", "renca", "dehesa",
        "barnechea", "macul", "tobalaba", "maipú", "ñuñoa", "concon",
        "linares", "talca", "osorno", "angol", "villarrica", "frutillar",
        "punta arenas", "buin", "talagante", "penaflor", "colina", "lampa",
        "alameda", "vicuna", "mackenna", "apoquindo", "irarrazaval",
        "kennedy", "grecia", "vivaceta", "carrascal", "quinta normal",
        "cisterna", "peñalolen", "peñaflor", "centro", "oeste", "oriente", "norte", "sur", "plaza",
    ]
    tienda = None
    for t in TIENDAS:
        if t in lower:
            tienda = t.title()
            lower = lower.replace(t, " ").strip()
            break

    if not tienda:
        # Heuristica: estructura tipica es [producto] [tienda]
        # Si el texto no contiene palabras de marca/producto → todo es tienda
        MARCAS = {
            "barbie", "reco", "hot", "wheels", "thomas", "train", "fisher",
            "price", "mega", "uno", "mario", "kart", "disney", "pixar",
            "polly", "pocket", "enchantimals", "monster", "high", "ever",
            "after", "imaginext", "matchbox", "hotwheels", "mattel",
        }
        palabras = lower.split()
        if not palabras:
            return {"error": "no entendi"}

        tiene_marca = any(p in MARCAS for p in palabras)

        if not tiene_marca:
            # Sin marca = todo el texto restante es el nombre de la tienda
            tienda = " ".join(palabras).title()
            lower = ""
        elif len(palabras) == 1:
            tienda = palabras[0].title()
            lower = ""
        elif len(palabras) == 2:
            tienda = palabras[-1].title()
            lower = " ".join(palabras[:-1])
        else:
            # Con marca: producto al inicio, tienda al final
            if len(palabras[-1]) < 4:
                tienda = " ".join(palabras[-2:]).title()
                lower = " ".join(palabras[:-2])
            else:
                tienda = palabras[-1].title()
                lower = " ".join(palabras[:-1])

    producto = lower.strip() if lower.strip() else None
    if producto and producto in PALABRAS_IGNORAR:
        producto = None
    # Si no hay otro producto pero si habia un SKU, usarlo como producto
    if not producto and sku_candidate:
        producto = sku_candidate

    return {"cliente": cliente, "tienda": tienda, "producto": producto}


# ── Endpoints ─────────────────────────────────────────────────────────────────

HELP_MSG = (
    "Hola! Soy el asistente de stock.\n\n"
    "Escribe algo como:\n"
    "- _Ripley Los Dominicos_\n"
    "- _Falabella Parque Arauco_\n"
    "- _Barbie Ripley Costanera_\n\n"
    "Clientes: Falabella, Ripley, Jumbo, Tottus, Walmart"
)

def twiml(resp):
    """Retorna TwiML con charset UTF-8 explícito para evitar encoding roto."""
    return str(resp), 200, {'Content-Type': 'text/xml; charset=utf-8'}


@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender = request.form.get("From", "")
    incoming = request.form.get("Body", "").strip()
    log.info("De %s: %s", sender, incoming)

    resp = MessagingResponse()

    if sender not in NUMEROS_AUTORIZADOS:
        return twiml(resp)

    if incoming.lower() in ("hola", "help", "ayuda", "?", ""):
        resp.message(HELP_MSG)
        return twiml(resp)

    parsed = parse_query(incoming)
    log.info("Parsed: %s", parsed)

    if "error" in parsed:
        resp.message("No entendi\nEscribe por ejemplo:\nRipley Los Dominicos")
        return twiml(resp)

    cliente = parsed.get("cliente", "").strip()
    tienda = parsed.get("tienda", "").strip()
    producto = parsed.get("producto")
    if producto and producto.lower() in PALABRAS_IGNORAR:
        producto = None

    try:
        results = consultar_stock(cliente, tienda, producto)
    except Exception as e:
        log.error("Error consultando stock: %s", e)
        resp.message("Error leyendo el archivo. Intenta de nuevo.")
        return twiml(resp)

    resp.message(format_respuesta(cliente, tienda, producto, results))
    return twiml(resp)


@app.route("/test")
def test():
    """Endpoint para probar sin WhatsApp. Ej: /test?msg=Ripley+Los+Dominicos"""
    msg = request.args.get("msg", "Ripley Los Dominicos")
    parsed = parse_query(msg)
    if "error" in parsed:
        return {"error": "No se pudo parsear", "msg": msg}

    cliente = parsed.get("cliente", "")
    tienda = parsed.get("tienda", "")
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
        "muestra": results[:5],
        "respuesta": format_respuesta(cliente, tienda, producto, results),
    }


@app.route("/reload")
def reload_data():
    """Limpia el cache para forzar descarga del archivo actualizado desde Dropbox."""
    _cache["data"] = None
    url_activa = DROPBOX_URL[:60] + "..."
    log.info("Cache limpiado. Proxima consulta descargara el archivo nuevo.")
    return {"status": "ok", "mensaje": "Cache limpiado. El archivo se descargara en la proxima consulta.", "url": url_activa}, 200


@app.route("/health")
def health():
    semana = str(date.today().isocalendar()[1]).zfill(2)
    return {"status": "ok", "semana": semana}, 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
