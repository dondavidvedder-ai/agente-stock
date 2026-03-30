import os, json, base64, io, logging
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
ONEDRIVE_SHARE_URL = os.environ["ONEDRIVE_SHARE_URL"]

def _share_token(url):
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"

def _graph_get(path):
    token = _share_token(ONEDRIVE_SHARE_URL)
    resp = requests.get(f"https://graph.microsoft.com/v1.0/shares/{token}/root{path}", timeout=15)
    resp.raise_for_status()
    return resp.json()

def list_week_folder(week):
    try:
        return _graph_get(f":/{week}:/children").get("value", [])
    except Exception as e:
        log.error("Error listando semana %s: %s", week, e)
        return []

def download_file(url):
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return io.BytesIO(resp.content)

def find_client_file(files, cliente):
    for f in files:
        if cliente.lower() in f["name"].lower():
            return f
    return None

def get_current_week():
    return str(date.today().isocalendar()[1]).zfill(2)

def read_falabella(file_bytes, tienda, producto):
    df = pd.read_excel(file_bytes, sheet_name="General", header=None)
    store_row = df.iloc[3, :]
    col_idx = next((i for i, v in store_row.items() if pd.notna(v) and tienda.upper() in str(v).upper()), None)
    if col_idx is None:
        return []
    results = []
    for row_idx in range(5, len(df)):
        modelo, desc, stock = df.iloc[row_idx, 0], df.iloc[row_idx, 1], df.iloc[row_idx, col_idx]
        trf = df.iloc[row_idx, col_idx+1] if col_idx+1 < len(df.columns) else 0
        if not pd.notna(modelo) or not pd.notna(stock):
            continue
        desc_str = str(desc) if pd.notna(desc) else ""
        if producto and producto.upper() not in desc_str.upper() and producto.upper() not in str(modelo).upper():
            continue
        try:
            sv, tv = int(float(stock)), int(float(trf)) if pd.notna(trf) else 0
            if sv != 0 or tv != 0:
                results.append({"modelo": str(modelo), "descripcion": desc_str, "marca": "MATTEL", "stock": sv, "trf": tv})
        except: pass
    return results

def read_ripley(file_bytes, tienda, producto):
    xl = pd.ExcelFile(file_bytes)
    sheet = next((s for s in xl.sheet_names if "MATTEL" in s.upper()), None)
    if not sheet: return []
    df = pd.read_excel(file_bytes, sheet_name=sheet, header=0)
    filtered = df[df["Sucursal"].str.lower().str.contains(tienda.lower(), na=False)]
    if producto:
        p = producto.lower()
        filtered = filtered[filtered["Desc.Art.Ripley"].str.lower().str.contains(p, na=False) | filtered["Desc. Art. Prov. (Case Pack)"].str.lower().str.contains(p, na=False)]
    marca_col = next((c for c in df.columns if "marca" in c.lower()), None)
    cod_col = next((c for c in df.columns if "prov" in c.lower() and "cod" in c.lower()), None)
    desc_col = next((c for c in df.columns if "desc" in c.lower() and "prov" in c.lower()), None)
    results = []
    for _, row in filtered.iterrows():
        sv = int(float(row.get("Stock on Hand Disponible (u)", 0) or 0))
        tv = int(float(row.get("Tranferencias on order (u)", 0) or 0))
        if sv == 0 and tv == 0: continue
        results.append({"modelo": str(row[cod_col]).strip() if cod_col else "", "descripcion": str(row[desc_col]).strip() if desc_col else "", "marca": str(row[marca_col]).strip() if marca_col else "MATTEL", "stock": sv, "trf": tv})
    return results

def read_generic(file_bytes, tienda, producto):
    try:
        xl = pd.ExcelFile(file_bytes)
        df = pd.read_excel(file_bytes, sheet_name=xl.sheet_names[0], header=0)
    except: return []
    tienda_col = next((c for c in df.columns if any(k in str(c).lower() for k in ["tienda","sala","sucursal","local"])), None)
    if tienda_col:
        df = df[df[tienda_col].str.lower().str.contains(tienda.lower(), na=False)]
    stock_col = next((c for c in df.columns if "stock" in str(c).lower()), None)
    desc_col = next((c for c in df.columns if "desc" in str(c).lower() or "nombre" in str(c).lower()), None)
    results = []
    for _, row in df.iterrows():
        sv = int(float(row[stock_col])) if stock_col and pd.notna(row[stock_col]) else 0
        desc_str = str(row[desc_col]) if desc_col and pd.notna(row[desc_col]) else ""
        if producto and producto.upper() not in desc_str.upper(): continue
        if sv != 0:
            results.append({"modelo": "", "descripcion": desc_str, "marca": "", "stock": sv, "trf": 0})
    return results

READER_MAP = {"falabella": read_falabella, "ripley": read_ripley, "paris": read_generic, "jumbo": read_generic, "tottus": read_generic}

def format_whatsapp(cliente, tienda, producto, results, week):
    if not results:
        return f"No encontre stock{'de *'+producto+'*' if producto else ''} en *{cliente.upper()} {tienda.upper()}* (Semana {week})."
    lines = [f"📦 *{cliente.upper()} — {tienda.upper()}*", f"_Semana {week}_ | {len(results)} referencia(s)"]
    if producto: lines.append(f"🔍 _{producto}_")
    lines.append("")
    for r in results[:20]:
        lines.append(f"{'✅' if r['stock']>0 else '⚠️'} *{r['modelo']}* | {r['descripcion'][:35]}")
        lines.append(f"   {r['marca']} | Stock: {r['stock']} | TRF: {r['trf']}")
    if len(results) > 20: lines.append(f"_...y {len(results)-20} mas_")
    return "\n".join(lines)

SYSTEM_PARSE = "Extrae del mensaje: cliente (Falabella/Ripley/Paris/Jumbo/Tottus), tienda, producto (opcional). Responde SOLO JSON: {cliente, tienda, producto} o {error}"

def parse_query(msg):
    ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = ac.messages.create(model="claude-haiku-4-5-20251001", max_tokens=150, system=SYSTEM_PARSE, messages=[{"role":"user","content":msg}])
    return json.loads(resp.content[0].text.strip())

HELP_MSG = "Hola! Soy el asistente de stock 📦\n\nEjemplos:\n- Stock Falabella Parque Arauco\n- Mario Kart Ripley Costanera\n- Jumbo Maipu\n\nClientes: Falabella, Ripley, Paris, Jumbo, Tottus"

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming = request.form.get("Body", "").strip()
    resp = MessagingResponse()
    if incoming.lower() in ("hola","help","ayuda","?",""):
        resp.message(HELP_MSG); return str(resp)
    try:
        parsed = parse_query(incoming)
    except Exception as e:
        log.error("Error: %s", e)
        resp.message("No pude entender. Escribe ayuda."); return str(resp)
    if "error" in parsed:
        resp.message("No entendi 😅 Ejemplo: Stock Falabella Parque Arauco"); return str(resp)
    cliente, tienda, producto, week = parsed.get("cliente","").strip(), parsed.get("tienda","").strip(), parsed.get("producto"), get_current_week()
    reader_fn = READER_MAP.get(cliente.lower())
    if not reader_fn:
        resp.message(f"Cliente '{cliente}' no reconocido."); return str(resp)
    files = list_week_folder(week)
    if not files:
        resp.message(f"No hay archivos para semana {week}."); return str(resp)
    client_file = find_client_file(files, cliente)
    if not client_file:
        resp.message(f"No hay archivo de {cliente} semana {week}."); return str(resp)
    try:
        results = reader_fn(download_file(client_file["@microsoft.graph.downloadUrl"]), tienda, producto)
    except Exception as e:
        log.error("Error leyendo: %s", e)
        resp.message("Error leyendo archivo. Intentalo de nuevo."); return str(resp)
    resp.message(format_whatsapp(cliente, tienda, producto, results, week))
    return str(resp)

@app.route("/health")
def health():
    return {"status": "ok", "week": get_current_week()}, 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
