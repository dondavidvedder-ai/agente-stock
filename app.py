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

# UN solo archivo con todos los clientes
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

# Cache del archivo en memoria para no descargarlo en cada consulta
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
                filtered = filtered[
                    filtered["Descripcion producto"].str.upper().str.contains(producto.upper(), na=False) |
                    filtered["Marca"].str.upper().str.contains(producto.upper(), na=False) |
                    filtered["Sku Mattel"].str.upper().str.contains(producto.upper(), na=False) |
                    filtered["descuento"].str.upper().str.contains(producto.upper(), na=False)
        ]

    results = []
    for _, row in filtered.iterrows():
                stock = int(row["Stock"]) if pd.notna(row["Stock"]) else 0
                results.append({
                    "sku_mattel":  str(row.get("Sku Mattel", "")),
                    "descripcion": str(row.get("Descripcion producto", ""))[:60],
                    "stock":       stock,
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

    header = f"📦 *{cliente.upper()} — {tienda.upper()}* (Sem {semana})\n"
    header += f"_{len(results)} producto(s)_"
    if producto:
                header += f" · _{producto}_"
            header += "\n"

    lineas = [header]

    # Mostrar primeros 20 o todos si son pocos
    max_display = min(20, len(results))
    for r in results[:max_display]:
                emoji = "✅" if r["stock"] > 0 else "⚠️"
                lineas.append(f"{emoji} {r['descripcion']}")
                lineas.append(f"   SKU: {r['sku_mattel']} · Stock: {r['stock']}")

    if len(results) > max_display:
                lineas.append(f"\n_...y {len(results)-max_display} más_")

    return "\n".join(lineas)


# ── Parser con Claude ──────────────────────────────────────────────────────────

SYSTEM_PARSE = f"""
Extrae del mensaje del usuario:
- cliente: uno de {sorted(CLIENTES_VALIDOS)} (obligatorio)
- tienda: nombre de tienda (obligatorio)
- producto: marca o nombre de producto (opcional, null si no se menciona)

La palabra "stock" NO es un producto. Es solo una palabra de solicitud.

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
                    return json.loads(resp.content[0].text.strip())
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

    # Detectar tiendas conocidas
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
                        if palabras:
                                        tienda = " ".join(palabras[:3]).title()
                                        lower = " ".join(palabras[3:])
            else:
            return {"error": "no entendi"}

                    producto = lower.strip() if lower.strip() else None
    if producto and producto in PALABRAS_IGNORAR:
                producto = None

    return {"cliente": cliente, "tienda": tienda, "producto": producto}


# ── Endpoints ─────────────────────────────────────────────────────────────────

HELP_MSG = (
        "Hola! Soy el asistente de stock 📦\n\n"
        "Escribe algo como:\n"
        "• _Ripley Los Dominicos_\n"
        "• _Falabella Parque Arauco_\n"
        "• _Barbie Ripley Costanera_\n\n"
        "Clientes: Falabella, Ripley, Jumbo, Tottus, Walmart"
)

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
                resp.message("No entendí 😅\nEscribe por ejemplo:\n_Ripley Los Dominicos_")
                return str(resp)

    cliente  = parsed.get("cliente", "").strip()
    tienda   = parsed.get("tienda", "").strip()
    producto = parsed.get("producto")
    if producto and producto.lower() in PALABRAS_IGNORAR:
                producto = None

    try:
                results = consultar_stock(cliente, tienda, producto)
    except Exception as e:
        log.error("Error consultando stock: %s", e)
        resp.message("⚠️ Error leyendo el archivo. Intenta de nuevo.")
        return str(resp)

    resp.message(format_respuesta(cliente, tienda, producto, results))
    return str(resp)


@app.route("/test")
def test():
        """Endpoint para probar sin WhatsApp. Ej: /test?msg=Ripley+Los+Dominicos"""
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
                "parsed":    parsed,
                "producto_final": producto,
                "resultados": len(results),
                "muestra":   results[:5],
                "respuesta": format_respuesta(cliente, tienda, producto, results),
    }


@app.route("/health")
def health():
        semana = str(date.today().isocalendar()[1]).zfill(2)
    return {"status": "ok", "semana": semana}, 200


if __name__ == "__main__":
        app.run(debug=True, port=5000)
