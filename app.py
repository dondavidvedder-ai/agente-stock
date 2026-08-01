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
    "https://www.dropbox.com/scl/fi/t86yz2horo6njo891h8ny/Stock-30-v5.xlsx?rlkey=ayl3u11megwi6ne7f5iilg60o&dl=1"
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

# Permisos por zona. None = admin (ve todo Chile).
# El valor debe coincidir EXACTO con lo que hay en la columna "Zona" del Excel.
PERMISOS_ZONA = {
    "whatsapp:+56926121144": None,                     # admin
    "whatsapp:+56953634351": None,                     # admin
    "whatsapp:+56972494232": None,                     # admin (visual)
    "whatsapp:+56997054149": "Juan Carlos Berrios",
    "whatsapp:+56954077612": "Claudio Berrios",
    "whatsapp:+56972495007": "Victor Flores",
    "whatsapp:+56990674664": "Isabel Garcia",
}


def get_zona_for_sender(sender: str) -> str | None:
    """Devuelve el nombre del supervisor para un sender, o None si es admin
    (o si no está en el mapa)."""
    return PERMISOS_ZONA.get(sender)

CLIENTES_VALIDOS = {"falabella", "ripley", "paris", "jumbo", "tottus", "walmart"}
PALABRAS_IGNORAR = {"stock", "inventario", "consulta", "ver", "buscar", "mostrar", "dame", "hay"}

# Patrón para detectar códigos SKU Mattel.
# Cubre formatos reales del Excel:
#   - Letras+digitos: HLW02, JGK23, C4982, DXV29, HRJ78, W2085  (929 de 947)
#   - Solo digitos:   1806, 54886, 42050                        (6 de 947)
SKU_RE = re.compile(r'\b([A-Z]{1,4}\d{2,6}[A-Z]?\d?|\d{4,6})\b', re.IGNORECASE)

# Palabras extra que pueden acompañar un SKU y que igual deben tratarse como
# consulta "solo SKU" (ej: "info HDX82", "sku HDX82", "mattel HDX82")
PALABRAS_SKU_CONTEXTO = {"sku", "mattel", "info", "informacion", "información", "codigo", "código", "cod"}

_cache = {"data": None, "tiendas": [], "actividades": [], "descuentos": []}

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

    # Tiendas únicas (lowercase, ordenadas por longitud desc para longest-match)
    tiendas = df["Nombre Tienda"].dropna().astype(str).str.strip().str.lower().unique().tolist()
    _cache["tiendas"] = sorted([t for t in tiendas if t], key=len, reverse=True)

    # Actividades únicas (si la columna existe)
    if "Actividad" in df.columns:
        acts = df["Actividad"].dropna().astype(str).str.strip().str.lower().unique().tolist()
        _cache["actividades"] = sorted([a for a in acts if a], key=len, reverse=True)
    else:
        _cache["actividades"] = []

    # Descuentos/promociones únicas (si la columna existe)
    if "descuento" in df.columns:
        descs = df["descuento"].dropna().astype(str).str.strip().str.lower().unique().tolist()
        _cache["descuentos"] = sorted([d for d in descs if d], key=len, reverse=True)
    else:
        _cache["descuentos"] = []

    log.info(f"Tiendas cacheadas: {len(_cache['tiendas'])} | Actividades: {_cache['actividades']} | Descuentos: {_cache['descuentos']}")
    return df

def consultar_stock(cliente: str, tienda: str, producto: str | None, zona: str | None = None) -> list:
    """Filtra el DataFrame por cliente, tienda y producto opcional.
    Si zona se pasa, restringe a filas cuya columna "Zona" coincida."""
    df = get_dataframe()

    # Filtro por zona (permisos): se aplica antes que todo lo demas
    if zona and "Zona" in df.columns:
        df = df[df["Zona"].astype(str).str.strip().str.lower() == zona.lower().strip()]

    # Filtrar por cliente
    mask_c = df["Cliente"].str.lower() == cliente.lower()

    # Filtrar por tienda — 3 niveles para evitar falsos positivos
    # (ej: "puente nuevo" no debe matchear "PUENTE ALTO")
    tienda_low = tienda.lower().strip()
    nombres = df["Nombre Tienda"].str.lower()

    # Nivel 1: substring exacto de la frase completa
    mask_t = nombres.str.contains(tienda_low, na=False, regex=False)

    # Nivel 2: AND — la tienda debe contener TODAS las palabras (>2 chars)
    if not (mask_c & mask_t).any():
        words = [w for w in tienda_low.split() if len(w) > 2]
        if words:
            mask_t = pd.Series([True] * len(df), index=df.index)
            for w in words:
                mask_t &= nombres.str.contains(w, na=False, regex=False)

    # Nivel 3: OR — cualquier palabra (fallback original)
    if not (mask_c & mask_t).any():
        mask_t = pd.Series([False] * len(df), index=df.index)
        for w in tienda_low.split():
            if len(w) > 2:
                mask_t |= nombres.str.contains(w, na=False, regex=False)

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
            "sku_cliente": str(row.get("Sku Cliente", "")) if "Sku Cliente" in row.index else "",
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


def format_respuesta(cliente, tienda, producto, results, zona: str | None = None) -> str:
    semana = str(date.today().isocalendar()[1]).zfill(2)

    if not results:
        filtro = f" de *{producto}*" if producto else ""
        if zona:
            return (
                f"Sin stock{filtro} en *{cliente.upper()} {tienda.upper()}* "
                f"dentro de tu zona ({zona}) (Sem {semana})."
            )
        return (
            f"Sin stock{filtro} en *{cliente.upper()} {tienda.upper()}* (Sem {semana}).\n"
            f"Verifica el nombre de la tienda."
        )

    header = f"\U0001f4e6 *{cliente.upper()} \u2014 {tienda.upper()}* (Sem {semana})\n"
    if zona:
        header += f"Zona: {zona}\n"
    header += f"_{len(results)} producto(s)_"
    if producto:
        header += f" \u00b7 _{producto}_"
    header += "\n"

    # Mostrar hasta 20 productos, pero cortando antes si excede ~1500 chars
    # (limite Twilio sandbox ~1600 \u2014 dejamos margen para footer)
    MAX_ITEMS = 20
    CHAR_BUDGET = 1500
    body_lines = []
    chars_used = len(header)
    shown = 0
    for r in results[:MAX_ITEMS]:
        estado = "\u2705" if r["stock"] > 0 else "\u26a0\ufe0f"
        desc = r['descripcion'].strip()
        l1 = f"{estado} {desc}"
        cod = r.get('sku_cliente', '').strip()
        cod_txt = f" \u00b7 Cod: {cod}" if cod and cod.lower() != 'nan' else ""
        l2 = f"   SKU: {r['sku_mattel'].strip()}{cod_txt} \u00b7 Stock: {r['stock']} \u00b7 Venta: {r['venta']}"
        extra = len(l1) + len(l2) + 2  # +2 por los \n
        if chars_used + extra > CHAR_BUDGET:
            break
        body_lines.append(l1)
        body_lines.append(l2)
        chars_used += extra
        shown += 1

    lineas = [header] + body_lines

    if len(results) > shown:
        lineas.append(f"\n_...y {len(results)-shown} mas. Busca por producto para filtrar._")

    return "\n".join(lineas)


# ── Modo "solo SKU" ────────────────────────────────────────────────────────────

def is_sku_only(msg: str) -> str | None:
    """Si el mensaje es solo un SKU (opcionalmente con palabras de contexto
    como 'sku', 'info', 'mattel' o palabras a ignorar), devuelve el SKU en
    mayusculas. Si no, devuelve None."""
    if not msg:
        return None
    lower = msg.lower().strip()
    # Rechazar si contiene algun cliente (falabella, ripley, etc.)
    for c in CLIENTES_VALIDOS:
        if re.search(rf'\b{re.escape(c)}\b', lower):
            return None
    sku_match = SKU_RE.search(lower)
    if not sku_match:
        return None
    sku = sku_match.group(1).upper()
    # Quitar el SKU y verificar que lo que sobra sean solo palabras permitidas
    resto = (lower[:sku_match.start()] + " " + lower[sku_match.end():]).strip()
    resto_palabras = [w for w in resto.split() if w]
    permitidas = PALABRAS_IGNORAR | PALABRAS_SKU_CONTEXTO
    for w in resto_palabras:
        if w not in permitidas:
            return None
    return sku


def is_sku_plus_cliente(msg: str) -> tuple[str, str] | None:
    """Si el mensaje es SKU + exactamente un cliente (sin tienda), devuelve
    (sku, cliente). Si no, devuelve None."""
    if not msg:
        return None
    lower = msg.lower().strip()
    # Detectar exactamente un cliente
    clientes_encontrados = [c for c in CLIENTES_VALIDOS if re.search(rf'\b{re.escape(c)}\b', lower)]
    if len(clientes_encontrados) != 1:
        return None
    cliente = clientes_encontrados[0]
    lower_sin_cliente = re.sub(rf'\b{re.escape(cliente)}\b', ' ', lower)
    lower_sin_cliente = " ".join(lower_sin_cliente.split())
    # Buscar SKU
    sku_match = SKU_RE.search(lower_sin_cliente)
    if not sku_match:
        return None
    sku = sku_match.group(1).upper()
    # Lo que sobra tiene que ser solo palabras permitidas
    resto = (lower_sin_cliente[:sku_match.start()] + " " + lower_sin_cliente[sku_match.end():]).strip()
    resto_palabras = [w for w in resto.split() if w]
    permitidas = PALABRAS_IGNORAR | PALABRAS_SKU_CONTEXTO
    for w in resto_palabras:
        if w not in permitidas:
            return None
    return sku, cliente


def consultar_por_sku(sku: str, cliente: str | None = None, zona: str | None = None) -> dict:
    """Busca un SKU en TODO el Excel (o filtrado por cliente si se pasa).
    Si zona se pasa, restringe a filas cuya columna "Zona" coincida.
    Devuelve {descripcion, marca, cliente_filtro, filas: [...]} donde cada
    fila es {cliente, sala, stock, venta, sku_cliente}. Ordenado por stock desc."""
    df = get_dataframe()
    mask = df["Sku Mattel"].astype(str).str.upper().str.strip() == sku.upper().strip()
    if cliente:
        mask &= df["Cliente"].astype(str).str.lower().str.strip() == cliente.lower().strip()
    if zona and "Zona" in df.columns:
        mask &= df["Zona"].astype(str).str.strip().str.lower() == zona.lower().strip()
    matched = df[mask]
    if len(matched) == 0:
        return {"descripcion": "", "marca": "", "cliente_filtro": cliente or "", "filas": []}

    descripcion = str(matched.iloc[0].get("Descripcion producto", "")).strip()
    marca = str(matched.iloc[0].get("Marca", "")).strip()

    filas = []
    for _, row in matched.iterrows():
        try:
            stock = int(row["Stock"]) if pd.notna(row["Stock"]) else 0
        except (ValueError, TypeError):
            stock = 0
        try:
            venta = int(row["Venta"]) if "Venta" in row.index and pd.notna(row["Venta"]) else 0
        except (ValueError, TypeError):
            venta = 0
        sku_cliente = str(row.get("Sku Cliente", "")).strip() if "Sku Cliente" in row.index else ""
        if sku_cliente.lower() == "nan":
            sku_cliente = ""
        filas.append({
            "cliente": str(row.get("Cliente", "")).strip(),
            "sala": str(row.get("Nombre Tienda", "")).strip(),
            "sku_cliente": sku_cliente,
            "stock": stock,
            "venta": venta,
        })

    filas.sort(key=lambda x: x["stock"], reverse=True)
    return {"descripcion": descripcion, "marca": marca, "cliente_filtro": cliente or "", "filas": filas}


def format_respuesta_sku(sku: str, data: dict, zona: str | None = None) -> str:
    semana = str(date.today().isocalendar()[1]).zfill(2)
    filas = data.get("filas", [])
    cliente_filtro = data.get("cliente_filtro", "")

    if not filas:
        filtro_txt = f" en *{cliente_filtro.upper()}*" if cliente_filtro else ""
        if zona:
            return f"Sin stock de *{sku}*{filtro_txt} en tu zona ({zona}) (Sem {semana})."
        return f"SKU *{sku}* no encontrado{filtro_txt} (Sem {semana})."

    desc = data.get("descripcion", "")
    marca = data.get("marca", "")
    stock_total = sum(f["stock"] for f in filas)
    venta_total = sum(f["venta"] for f in filas)

    scope = f" — {cliente_filtro.upper()}" if cliente_filtro else ""
    header = f"\U0001f4e6 SKU *{sku}*{scope} (Sem {semana})\n"
    if zona:
        header += f"Zona: {zona}\n"
    if desc:
        header += f"{desc[:60]}\n"
    if marca:
        header += f"Marca: {marca}\n"
    header += f"Total: Stock {stock_total} · Venta {venta_total}\n"
    header += f"_{len(filas)} sala(s)_\n"

    # Agrupar por cliente, ordenar clientes por stock desc
    por_cliente = {}
    for f in filas:
        por_cliente.setdefault(f["cliente"], []).append(f)
    clientes_ordenados = sorted(
        por_cliente.items(),
        key=lambda kv: sum(x["stock"] for x in kv[1]),
        reverse=True,
    )

    CHAR_BUDGET = 1500
    body_lines = []
    chars_used = len(header)
    shown = 0
    total_disponibles = len(filas)
    truncado = False

    for cliente, lista in clientes_ordenados:
        if truncado:
            break
        subtotal_stock = sum(x["stock"] for x in lista)
        subtotal_venta = sum(x["venta"] for x in lista)
        cods_unicos = sorted({x.get("sku_cliente", "") for x in lista if x.get("sku_cliente")})
        cod_txt = f"Cod: {', '.join(cods_unicos)} · " if cods_unicos else ""
        h = f"\n*{cliente.upper()}* ({cod_txt}Stock {subtotal_stock} · Venta {subtotal_venta})"
        if chars_used + len(h) + 1 > CHAR_BUDGET:
            truncado = True
            break
        body_lines.append(h)
        chars_used += len(h) + 1
        for x in lista:
            estado = "✅" if x["stock"] > 0 else "⚠️"
            linea = f"{estado} {x['sala']} · Stock: {x['stock']} · Venta: {x['venta']}"
            if chars_used + len(linea) + 1 > CHAR_BUDGET:
                truncado = True
                break
            body_lines.append(linea)
            chars_used += len(linea) + 1
            shown += 1

    lineas = [header] + body_lines
    if shown < total_disponibles:
        lineas.append(f"\n_...y {total_disponibles - shown} sala(s) mas._")

    return "\n".join(lineas)


# ── Parser con Claude ──────────────────────────────────────────────────────────

def get_system_parse() -> str:
    """Construye el system prompt inyectando actividades y descuentos vigentes del Excel."""
    actividades = _cache.get("actividades", [])
    descuentos = _cache.get("descuentos", [])
    activs_str = ", ".join(sorted(actividades)) if actividades else "(no disponibles)"
    descs_str = ", ".join(sorted(descuentos)) if descuentos else "(no disponibles)"
    return f"""
Extrae del mensaje del usuario:
- cliente: uno de {sorted(CLIENTES_VALIDOS)} (obligatorio)
- tienda: nombre de tienda (obligatorio, puede ser compuesto como "Puente Nuevo", "La Serena", "Plaza Vespucio")
- producto: marca, nombre de producto, código SKU Mattel, tipo de ACTIVIDAD, o tipo de PROMOCIÓN/DESCUENTO (opcional, null si no se menciona)

ACTIVIDADES válidas (si aparecen en el mensaje van en `producto`): {activs_str}

PROMOCIONES/DESCUENTOS válidos (si aparecen en el mensaje van en `producto`): {descs_str}

IMPORTANTE:
- Los códigos SKU Mattel son combinaciones cortas de letras y números como C4982, DXV29, HRJ78, W2085, K5904. Son PRODUCTOS, NO tiendas.
- La palabra "stock" NO es un producto. Es solo una palabra de solicitud.
- Si el mensaje contiene una palabra de la lista de ACTIVIDADES o PROMOCIONES (ej: "collector", "motu", "venta insolita"), ESA palabra/frase va en `producto`.

Ejemplos:
- "C4982 Walmart Vitacura" → cliente=walmart, tienda=vitacura, producto=C4982
- "Barbie Ripley Los Dominicos" → cliente=ripley, tienda=los dominicos, producto=barbie
- "Falabella Parque Arauco" → cliente=falabella, tienda=parque arauco, producto=null
- "Collector Walmart Puente Nuevo" → cliente=walmart, tienda=puente nuevo, producto=collector
- "Mario Kart Walmart Vitacura" → cliente=walmart, tienda=vitacura, producto=mario kart
- "Venta Insolita Jumbo Concha y Toro" → cliente=jumbo, tienda=concha y toro, producto=venta insolita
- "Ripley Plaza" → cliente=ripley, tienda=plaza, producto=null

Responde SOLO con JSON:
{{"cliente":"...","tienda":"...","producto":null}}
o {{"error":"no entendi"}}
"""

def _normalize_categoria(parsed: dict) -> dict:
    """Si Haiku puso una actividad/descuento adentro del campo `tienda`, moverla a `producto`.
    Ejemplo: tienda='Venta Insolita Concha' → tienda='Concha', producto='venta insolita'."""
    if "error" in parsed:
        return parsed
    producto = parsed.get("producto")
    tienda = (parsed.get("tienda") or "").lower()
    if producto or not tienda:
        return parsed
    categorias = sorted(
        set(_cache.get("actividades", [])) | set(_cache.get("descuentos", [])),
        key=len, reverse=True
    )
    for cat in categorias:
        if re.search(rf'\b{re.escape(cat)}\b', tienda):
            new_tienda = re.sub(rf'\b{re.escape(cat)}\b', ' ', tienda)
            new_tienda = " ".join(new_tienda.split()).title()
            parsed["tienda"] = new_tienda
            parsed["producto"] = cat
            log.info("Post-normalizado: movido '%s' de tienda a producto", cat)
            break
    return parsed


def parse_query(msg: str) -> dict:
    try:
        get_dataframe()  # asegura que el cache de actividades esté poblado
        ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=get_system_parse(),
            messages=[{"role": "user", "content": msg}],
        )
        result = json.loads(resp.content[0].text.strip())
        if "error" in result:
            log.warning("Claude devolvio error para '%s', intentando parseo simple", msg)
            return parse_simple(msg)
        return _normalize_categoria(result)
    except Exception as e:
        log.warning("Claude API fallo: %s — usando parseo simple", e)
        return parse_simple(msg)

def parse_simple(msg: str) -> dict:
    """Parseo de respaldo sin API."""
    # Cargar cache para tener tiendas y actividades disponibles
    try:
        get_dataframe()
    except Exception as e:
        log.warning("parse_simple: no se pudo precargar Excel (%s) — sigo sin listas dinámicas", e)
    tiendas_dyn = _cache.get("tiendas", [])
    actividades = _cache.get("actividades", [])
    descuentos = _cache.get("descuentos", [])
    # Combinamos actividades y descuentos: ambos se matchean como "producto categoría"
    categorias = sorted(set(actividades) | set(descuentos), key=len, reverse=True)

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

    # Detectar actividad o descuento (collector, motu, ts5, venta insolita, etc.)
    # → van en `producto`. Longest-match primero para frases multi-palabra.
    actividad_match = None
    for a in categorias:
        if re.search(rf'\b{re.escape(a)}\b', lower):
            actividad_match = a
            lower = re.sub(rf'\b{re.escape(a)}\b', ' ', lower)
            lower = " ".join(lower.split())
            break

    # Detectar SKU Mattel antes de todo (ej: C4982, DXV29, HRJ78)
    sku_candidate = None
    sku_match = SKU_RE.search(lower)
    if sku_match:
        sku_candidate = sku_match.group(1).upper()
        lower = lower[:sku_match.start()] + " " + lower[sku_match.end():]
        lower = " ".join(lower.split())

    # Detectar tienda usando lista dinámica del Excel (longest-match primero)
    tienda = None
    for t in tiendas_dyn:
        if t and t in lower:
            tienda = t.title()
            lower = lower.replace(t, " ").strip()
            lower = " ".join(lower.split())
            break

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
    if not tienda:
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
    # Prioridad: actividad detectada > SKU > resto del texto
    if actividad_match:
        producto = actividad_match
    elif not producto and sku_candidate:
        producto = sku_candidate

    return {"cliente": cliente, "tienda": tienda, "producto": producto}


# ── Endpoints ─────────────────────────────────────────────────────────────────

HELP_MSG = (
    "Hola! Soy el asistente de stock.\n\n"
    "Escribe algo como:\n"
    "- _Ripley Los Dominicos_\n"
    "- _Falabella Parque Arauco_\n"
    "- _Barbie Ripley Costanera_\n"
    "- _HDX82_ (solo el SKU → info a nivel Chile)\n"
    "- _HDX82 Walmart_ (SKU en una cadena)\n\n"
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

    zona = get_zona_for_sender(sender)  # None = admin (sin filtro)

    if incoming.lower() in ("hola", "help", "ayuda", "?", ""):
        resp.message(HELP_MSG)
        return twiml(resp)

    # Modo "SKU + cliente": consulta un SKU filtrado por una cadena
    sku_cli = is_sku_plus_cliente(incoming)
    if sku_cli:
        sku, cliente = sku_cli
        try:
            data = consultar_por_sku(sku, cliente=cliente, zona=zona)
        except Exception as e:
            log.error("Error consultando por SKU+cliente: %s", e)
            resp.message("Error leyendo el archivo. Intenta de nuevo.")
            return twiml(resp)
        resp.message(format_respuesta_sku(sku, data, zona=zona))
        return twiml(resp)

    # Modo "solo SKU": consulta a nivel Chile por Sku Mattel
    sku_solo = is_sku_only(incoming)
    if sku_solo:
        try:
            data = consultar_por_sku(sku_solo, zona=zona)
        except Exception as e:
            log.error("Error consultando por SKU: %s", e)
            resp.message("Error leyendo el archivo. Intenta de nuevo.")
            return twiml(resp)
        resp.message(format_respuesta_sku(sku_solo, data, zona=zona))
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
        results = consultar_stock(cliente, tienda, producto, zona=zona)
    except Exception as e:
        log.error("Error consultando stock: %s", e)
        resp.message("Error leyendo el archivo. Intenta de nuevo.")
        return twiml(resp)

    resp.message(format_respuesta(cliente, tienda, producto, results, zona=zona))
    return twiml(resp)


@app.route("/test")
def test():
    """Endpoint para probar sin WhatsApp. Ej: /test?msg=Ripley+Los+Dominicos"""
    msg = request.args.get("msg", "Ripley Los Dominicos")
    # Sender opcional para simular permisos por zona en las pruebas.
    # Ej: /test?msg=HDX82&sender=whatsapp:+56997054149
    sender = request.args.get("sender", "")
    zona = get_zona_for_sender(sender) if sender else None

    # Modo "SKU + cliente"
    sku_cli = is_sku_plus_cliente(msg)
    if sku_cli:
        sku, cliente = sku_cli
        try:
            data = consultar_por_sku(sku, cliente=cliente, zona=zona)
        except Exception as e:
            return {"error": str(e)}
        return {
            "modo": "sku+cliente",
            "sku": sku,
            "cliente": cliente,
            "zona": zona,
            "descripcion": data.get("descripcion", ""),
            "marca": data.get("marca", ""),
            "salas": len(data.get("filas", [])),
            "muestra": data.get("filas", [])[:5],
            "respuesta": format_respuesta_sku(sku, data, zona=zona),
        }

    # Modo "solo SKU"
    sku_solo = is_sku_only(msg)
    if sku_solo:
        try:
            data = consultar_por_sku(sku_solo, zona=zona)
        except Exception as e:
            return {"error": str(e)}
        return {
            "modo": "sku",
            "sku": sku_solo,
            "zona": zona,
            "descripcion": data.get("descripcion", ""),
            "marca": data.get("marca", ""),
            "salas": len(data.get("filas", [])),
            "muestra": data.get("filas", [])[:5],
            "respuesta": format_respuesta_sku(sku_solo, data, zona=zona),
        }

    parsed = parse_query(msg)
    if "error" in parsed:
        return {"error": "No se pudo parsear", "msg": msg}

    cliente = parsed.get("cliente", "")
    tienda = parsed.get("tienda", "")
    producto = parsed.get("producto")
    if producto and producto.lower() in PALABRAS_IGNORAR:
        producto = None

    try:
        results = consultar_stock(cliente, tienda, producto, zona=zona)
    except Exception as e:
        return {"error": str(e)}

    return {
        "parsed": parsed,
        "producto_final": producto,
        "zona": zona,
        "resultados": len(results),
        "muestra": results[:5],
        "respuesta": format_respuesta(cliente, tienda, producto, results, zona=zona),
    }


@app.route("/reload")
def reload_data():
    """Limpia el cache para forzar descarga del archivo actualizado desde Dropbox."""
    _cache["data"] = None
    _cache["tiendas"] = []
    _cache["actividades"] = []
    _cache["descuentos"] = []
    url_activa = DROPBOX_URL[:60] + "..."
    log.info("Cache limpiado. Proxima consulta descargara el archivo nuevo.")
    return {"status": "ok", "mensaje": "Cache limpiado. El archivo se descargara en la proxima consulta.", "url": url_activa}, 200


@app.route("/health")
def health():
    semana = str(date.today().isocalendar()[1]).zfill(2)
    return {"status": "ok", "semana": semana}, 200


@app.route("/actividades")
def actividades():
    """Devuelve las actividades únicas del Excel cargado (diagnóstico)."""
    try:
        get_dataframe()
    except Exception as e:
        return {"error": str(e)}, 500
    return {
        "actividades": _cache.get("actividades", []),
        "descuentos": _cache.get("descuentos", []),
        "total_tiendas": len(_cache.get("tiendas", [])),
    }, 200


@app.route("/debug")
def debug():
    """Diagnóstico del Excel: hojas disponibles, columnas, y valores únicos de columnas no-estándar."""
    try:
        resp = requests.get(DROPBOX_URL, timeout=30)
        resp.raise_for_status()
        xlsx = pd.ExcelFile(io.BytesIO(resp.content))
        sheets = xlsx.sheet_names
        df = get_dataframe()
        cols = list(df.columns)
        # Para cada columna que NO sea estándar, muestra valores únicos (max 20)
        STANDARD = {"Cliente", "Cod Tienda", "Nombre Tienda", "Sku Cliente",
                    "Sku Mattel", "Descripcion producto", "Marca", "Stock",
                    "Venta", "descuento", "porcentaje descuento", "Actividad"}
        extra_cols = {}
        for c in cols:
            if c not in STANDARD:
                vals = df[c].dropna().astype(str).str.strip().unique().tolist()
                extra_cols[c] = vals[:20]
        return {
            "hojas": sheets,
            "hoja_actual": "base",
            "columnas": cols,
            "columnas_extra": extra_cols,
            "filas": len(df),
        }, 200
    except Exception as e:
        return {"error": str(e)}, 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
