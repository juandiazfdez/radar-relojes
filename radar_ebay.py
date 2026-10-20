"""Radar de relojes v2 - eBay Browse API.

Cambios frente a la v1:
  * Solo mira publicaciones NUEVAS (ultimas horas) y recuerda las que ya vio.
  * Abre cada candidato (getItem) y lee descripcion, condicion, especificaciones
    (departamento, referencia, caja, ano) e imagenes. Descarta piezas, relojes que
    no funcionan, "as is", de mujer, replicas, etc.
  * Compara contra el MERCADO: mediana recortada de los listados activos en EE. UU.
    de la misma referencia/modelo (no contra el precio de Juan). Solo es alerta si
    esta claramente por debajo de esa mediana con suficientes comparables.
  * Opcional: revision con Claude (texto + fotos) si hay ANTHROPIC_API_KEY.
  * Avisa por Telegram y/o correo si hay credenciales; siempre deja data/alertas.json.

Variables de entorno:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET            (obligatorias)
  ANTHROPIC_API_KEY, CLAUDE_MODEL               (opcional, revision con fotos)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID          (opcional, aviso instantaneo)
  GMAIL_USER, GMAIL_APP_PASSWORD, ALERTA_EMAIL  (opcional, aviso por correo)
  LOOKBACK_HORAS (def. 3), UMBRAL_ALERTA (def. 0.72), MIN_COMPARABLES (def. 5)
"""
import base64
import csv
import datetime as dt
import html
import json
import os
import pathlib
import re
import smtplib
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"
WATCH_CATEGORY = "31387"
PAISES = os.environ.get("PAISES_ORIGEN", "US,JP").split(",")  # donde esta el reloj

LOOKBACK_HORAS = float(os.environ.get("LOOKBACK_HORAS", "3"))
UMBRAL_ALERTA = float(os.environ.get("UMBRAL_ALERTA", "0.72"))   # <= 72 % de la mediana
UMBRAL_SOSPECHA = 0.40                                             # < 40 % casi siempre es estafa o pieza
MIN_COMPARABLES = int(os.environ.get("MIN_COMPARABLES", "5"))
MIN_VALORACIONES = 20
MIN_POSITIVO = 97.0

# Palabras en el TITULO que delatan piezas o accesorios (se descarta sin abrir)
JUNK_TITULO = ["replica", "homage", "for parts", "parts only", "not working", "repair",
               " dial", "bezel", "insert", " link", "links", "bracelet only", "strap",
               " band", "buckle", "clasp", "crown", "crystal", "hands", "movement only",
               "case only", "caseback", "case back", "plate", "papers only", "booklet",
               "manual", "box only", "warranty card", "tag only", "hang tag", "fits ",
               "for rolex", "for cartier", "for omega", "for tudor", "for breitling",
               "compatible", "aftermarket", "custom", " mod ", "genuine rolex part",
               "ladies", "lady", "women", "womens", "women's", "midsize", "mid-size"]
# Palabras en DESCRIPCION / condicion que descartan el reloj aunque el titulo este limpio
ROJAS = ["not working", "doesn't work", "does not work", "not running", "doesn't run",
         "does not run", "non working", "non-working", "as is", "as-is", "for parts",
         "parts only", "parts or repair", "needs service", "needs servicing", "needs repair",
         "needs work", "needs to be serviced", "needs a service", "needs restoration",
         "no movement", "movement missing", "missing movement", "keeps stopping",
         "stops running", "loses time", "losing time", "gains time", "runs fast",
         "runs slow", "cracked crystal", "cracked", "broken", "replica", "franken",
         "aftermarket dial", "aftermarket bezel", "aftermarket movement", "refinished dial",
         "redial", "re-dial", "custom dial", "not authentic", "unknown authenticity",
         "cannot guarantee authenticity", "can't guarantee authenticity",
         "sold as untested", "untested",
         "ladies", "women's", "womens watch", "lady's", "unisex children"]
# Senales que no descartan pero se reportan
AMARILLAS = ["no returns", "all sales final", "scratch", "scratches", "wear", "worn", "polished", "service history unknown",
             "no box", "no papers", "battery", "dent", "hairline", "patina", "faded"]

# Patrones de numero de referencia por marca (para buscar comparables exactos)
PATRONES_REF = {
    "rolex": r"\b(1\d{4,5}[A-Z]{0,4}|2\d{5}[A-Z]{0,4}|6\d{4})\b",
    "tudor": r"\b(M?7\d{4}[A-Z]?(?:-\d{4})?|M?79\d{3})\b",
    "cartier": r"\b(W[A-Z]{0,4}\d{4,7})\b",
    "omega": r"\b(\d{3}\.\d{2}\.\d{2}\.\d{2}\.\d{2}\.\d{3}|\d{4}\.\d{2}\.\d{2})\b",
    "breitling": r"\b([A-Z]{1,2}\d{5}|[A-Z]{2}\d{4}|AB\d{6}[A-Z0-9]*)\b",
    "tag": r"\b([A-Z]{2,3}\d{3,4}[A-Z]?(?:\.[A-Z]{2}\d{4})?)\b",
    "panerai": r"\b(PAM\s?0?\d{3,5})\b",
    "jaeger": r"\b(Q?\d{7}|\d{3}\.\d\.\d{2})\b",
}


# ----------------------------------------------------------------- utilidades
def ahora():
    return dt.datetime.now(dt.timezone.utc)


def cargar(nombre, defecto):
    p = DATA / nombre
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return defecto


def guardar(nombre, obj):
    DATA.mkdir(exist_ok=True)
    (DATA / nombre).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def sin_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


def mediana_recortada(valores):
    v = sorted(valores)
    if len(v) >= 8:
        corte = max(1, len(v) // 10)
        v = v[corte:-corte]
    return statistics.median(v), (v[len(v) // 4] if v else None), (v[(3 * len(v)) // 4] if v else None)


# ---------------------------------------------------------------------- eBay
class Ebay:
    def __init__(self):
        cid, secret = os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, headers={
            "Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self.tok = json.load(r)["access_token"]
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Error de token eBay {e.code}: {e.read().decode(errors='replace')[:300]}")
        self.llamadas = 0
        self.agotado = False

    def _get(self, url):
        if self.agotado:
            return None
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.tok}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        self.llamadas += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.agotado = True
                print("Cuota diaria de eBay agotada; se detiene la corrida.", file=sys.stderr)
                return None
            print(f"HTTP {e.code} en {url[:120]}: {e.read().decode(errors='replace')[:200]}", file=sys.stderr)
            return None

    def buscar(self, q, filtros, sort=None, limit=200):
        params = {"q": q, "category_ids": WATCH_CATEGORY, "filter": ",".join(filtros), "limit": str(limit)}
        if sort:
            params["sort"] = sort
        r = self._get(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
        return (r or {}).get("itemSummaries", []) or []

    def item(self, item_id):
        return self._get(f"{ITEM_URL}{urllib.parse.quote(item_id, safe='')}?fieldgroups=PRODUCT")


def envio(item):
    for op in item.get("shippingOptions", []) or []:
        cost = (op.get("shippingCost") or {}).get("value")
        if cost is not None:
            return float(cost)
    return None


def precio_total(item):
    try:
        return float(item["price"]["value"]) + (envio(item) or 0.0)
    except (KeyError, TypeError, ValueError):
        return None


def titulo_ok(titulo, debe, excluir):
    t = f" {titulo.lower()} "
    if any(j in t for j in JUNK_TITULO):
        return False
    if any(x and x in t for x in excluir):
        return False
    return all(d in t for d in debe if d)


# ------------------------------------------------------------- analisis item
def aspectos(detalle):
    out = {}
    for a in detalle.get("localizedAspects", []) or []:
        out[(a.get("name") or "").strip().lower()] = (a.get("value") or "").strip()
    return out


def extraer_ref(marca, textos):
    patron = next((p for k, p in PATRONES_REF.items() if k in marca.lower()), None)
    if not patron:
        return None
    for t in textos:
        m = re.search(patron, t or "", flags=re.I)
        if m:
            return m.group(1).upper().replace(" ", "")
    return None


def analizar(detalle, fila):
    """Devuelve (motivo_descarte | None, info)."""
    asp = aspectos(detalle)
    titulo = detalle.get("title", "")
    desc = sin_html(detalle.get("description", ""))
    cond_desc = detalle.get("conditionDescription") or ""
    texto = f"{titulo}\n{cond_desc}\n{desc}".lower()
    info = {
        "departamento": asp.get("department") or asp.get("gender"),
        "referencia": asp.get("reference number") or asp.get("model number") or asp.get("manufacturer part number"),
        "modelo_esp": asp.get("model"),
        "caja_mm": asp.get("case size"),
        "ano": asp.get("year manufactured") or asp.get("year of manufacture"),
        "movimiento": asp.get("movement"),
        "caja_original": asp.get("with original box/packaging") or asp.get("with original box"),
        "papeles": asp.get("with papers") or asp.get("with original papers"),
        "condicion": detalle.get("condition"),
        "condicion_id": detalle.get("conditionId"),
        "condicion_desc": cond_desc[:400],
        "garantia_autenticidad": bool(detalle.get("authenticityVerification")),
        "devoluciones": (detalle.get("returnTerms") or {}).get("returnsAccepted"),
        "amarillas": sorted({a for a in AMARILLAS if a in texto}),
        "descripcion": desc[:1500],
        "imagenes": [detalle.get("image", {}).get("imageUrl")] +
                    [i.get("imageUrl") for i in detalle.get("additionalImages", []) or []],
    }
    info["imagenes"] = [u for u in info["imagenes"] if u][:6]
    if not info["referencia"]:
        info["referencia"] = extraer_ref(fila["marca"], [titulo, asp.get("model", ""), desc[:800]])

    dep = (info["departamento"] or "").lower()
    if dep and ("women" in dep or "ladies" in dep or "girl" in dep or "children" in dep):
        return f"departamento {info['departamento']}", info
    rojas = [r for r in ROJAS if r in texto]
    if rojas:
        return "descripcion: " + ", ".join(rojas[:3]), info
    cid = str(info["condicion_id"] or "")
    if cid in ("7000",) or "parts" in (info["condicion"] or "").lower():
        return "condicion: piezas/reparacion", info
    try:
        mm = float(re.search(r"\d+(\.\d+)?", info["caja_mm"] or "").group())
        if fila.get("caja_min_mm") and mm < float(fila["caja_min_mm"]):
            return f"caja {mm} mm (minimo {fila['caja_min_mm']})", info
    except (AttributeError, ValueError):
        pass
    try:
        if fila.get("ano_min") and info["ano"] and int(re.search(r"\d{4}", info["ano"]).group()) < int(fila["ano_min"]):
            return f"ano {info['ano']} (minimo {fila['ano_min']})", info
    except (AttributeError, ValueError):
        pass
    vend = detalle.get("seller") or {}
    try:
        if int(vend.get("feedbackScore") or 0) < MIN_VALORACIONES:
            return f"vendedor con {vend.get('feedbackScore')} valoraciones", info
        if float(vend.get("feedbackPercentage") or 0) < MIN_POSITIVO:
            return f"vendedor {vend.get('feedbackPercentage')} % positivo", info
    except ValueError:
        pass
    return None, info


# ---------------------------------------------------------------- mercado
def comparables(eb, fila, ref, condicion_id, excluir_id, cache):
    """Mediana recortada del precio de los listados activos (EE. UU., precio fijo)
    de la misma referencia (o del mismo modelo si no hay referencia)."""
    usada = str(condicion_id or "") not in ("1000", "1500", "1750")
    q = f"{fila['marca']} {ref}" if ref else fila["busqueda_ebay"]
    clave = f"{q}|{'usado' if usada else 'nuevo'}"
    hoy = dt.date.today().isoformat()
    if clave in cache and cache[clave].get("fecha") == hoy:
        return cache[clave]
    debe = [s.strip().lower() for s in fila["debe_contener"].split("|")]
    excluir = [s.strip().lower() for s in fila["excluir"].split("|")]
    piso = float(fila["tu_precio_usd"]) * 0.25
    filtros = ["itemLocationCountry:US", "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
               f"price:[{int(piso)}..]", "priceCurrency:USD",
               "conditions:{USED}" if usada else "conditions:{NEW}"]
    items = eb.buscar(q, filtros, sort="price", limit=200)
    precios = []
    for it in items:
        if it.get("itemId") == excluir_id:
            continue
        t = it.get("title", "")
        if not titulo_ok(t, debe if not ref else [], excluir):
            continue
        if ref and ref.lower() not in t.lower():
            continue
        p = precio_total(it)
        if p:
            precios.append(p)
    res = {"fecha": hoy, "consulta": q, "n": len(precios)}
    if precios:
        med, p25, p75 = mediana_recortada(precios)
        res.update({"mediana": round(med), "p25": round(p25), "p75": round(p75),
                    "min": round(min(precios)), "max": round(max(precios))})
    cache[clave] = res
    return res


# ------------------------------------------------------------- Claude (opc.)
def revisar_con_claude(cand):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    modelo = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
    contenido = []
    for u in cand["info"]["imagenes"][:4]:
        contenido.append({"type": "image", "source": {"type": "url", "url": u}})
    resumen = {k: cand["info"].get(k) for k in ("departamento", "referencia", "modelo_esp", "caja_mm",
                                                 "ano", "movimiento", "caja_original", "papeles",
                                                 "condicion", "condicion_desc", "garantia_autenticidad",
                                                 "devoluciones")}
    contenido.append({"type": "text", "text": (
        "Eres un comprador experto de relojes de lujo usados para reventa en Republica Dominicana. "
        "Evalua esta publicacion de eBay. Mira las fotos: estado real de caja, bisel, cristal, esfera, "
        "brazalete; senales de replica, piezas no originales, dial repintado, fotos de catalogo/stock, "
        "fotos robadas o sin el reloj real. Lee la descripcion buscando defectos o condiciones ocultas.\n\n"
        f"Modelo buscado: {cand['modelo_guia']}\nTitulo: {cand['titulo']}\n"
        f"Precio total: US${cand['total']:.0f}  |  Mediana de mercado (listados activos EE. UU.): "
        f"US${cand['mercado'].get('mediana')} con {cand['mercado'].get('n')} comparables "
        f"(p25 {cand['mercado'].get('p25')}, p75 {cand['mercado'].get('p75')})\n"
        f"Especificaciones: {json.dumps(resumen, ensure_ascii=False)}\n"
        f"Vendedor: {cand['vendedor']} ({cand['valoraciones']} valoraciones, {cand['pct_positivo']} % positivo)\n"
        f"Descripcion: {(cand['info'].get('descripcion') or '')[:1500]}\n\n"
        "Responde SOLO con JSON: {\"veredicto\": \"alerta\"|\"dudoso\"|\"descartar\", "
        "\"estado\": \"frase corta\", \"riesgo_autenticidad\": \"bajo\"|\"medio\"|\"alto\", "
        "\"completo\": true|false, \"razones\": [\"...\", \"...\"], \"precio_justo_usd\": numero}")})
    body = json.dumps({"model": modelo, "max_tokens": 6000,
                       "messages": [{"role": "user", "content": contenido}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        texto = "".join(b.get("text", "") for b in resp.get("content", []))
        m = re.search(r"\{.*\}", texto, flags=re.S)
        if m:
            return json.loads(m.group())
        print(f"Claude respondio sin JSON: {json.dumps(resp)[:600]}", file=sys.stderr)
        return {"veredicto": "dudoso", "razones": [texto[:200] or "respuesta vacia"]}
    except urllib.error.HTTPError as e:
        print(f"Claude HTTP {e.code}: {e.read().decode(errors='replace')[:400]}", file=sys.stderr)
        return None
    except Exception as e:  # la revision es opcional; no tumba la corrida
        print(f"Claude no disponible: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- avisos
def texto_alerta(c):
    m = c["mercado"]
    pct = round(100 * c["total"] / m["mediana"])
    lineas = [f"<b>{html.escape(c['modelo_guia'])}</b> — {pct} % de la mediana de mercado",
              html.escape(c["titulo"][:120]),
              f"Precio: <b>US${c['total']:,.0f}</b> (envio {c['envio'] if c['envio'] is not None else '?'}) · "
              f"Mercado: mediana US${m['mediana']:,} · rango tipico {m['p25']:,}–{m['p75']:,} · {m['n']} comparables",
              f"Ref. {c['info'].get('referencia') or '?'} · {c['info'].get('caja_mm') or '?'} · "
              f"{c['info'].get('ano') or 'ano ?'} · {c['info'].get('condicion')}"
              + (" · Garantia de autenticidad eBay" if c['info'].get('garantia_autenticidad') else ""),
              f"Vendedor: {c['vendedor']} ({c['valoraciones']} val., {c['pct_positivo']} %) · ubicado en {c.get('pais') or '?'}"]
    if c["info"].get("amarillas"):
        lineas.append("Ojo: " + ", ".join(c["info"]["amarillas"]))
    if c.get("claude"):
        cl = c["claude"]
        lineas.append(f"Claude: <b>{cl.get('veredicto')}</b> · {html.escape(str(cl.get('estado', '')))} · "
                      f"riesgo autenticidad {cl.get('riesgo_autenticidad')} · "
                      + "; ".join(html.escape(str(r)) for r in cl.get("razones", [])[:3]))
    lineas.append(c["url"])
    return "\n".join(lineas)


def avisar_telegram(cands):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    for c in cands:
        texto = texto_alerta(c)
        foto = (c["info"].get("imagenes") or [None])[0]
        try:
            if foto and len(texto) < 1000:
                body = urllib.parse.urlencode({"chat_id": chat, "photo": foto, "caption": texto,
                                               "parse_mode": "HTML"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendPhoto", data=body, timeout=30).read()
            else:
                body = urllib.parse.urlencode({"chat_id": chat, "text": texto, "parse_mode": "HTML"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, timeout=30).read()
        except Exception as e:
            print(f"Telegram fallo: {e}", file=sys.stderr)
    return True


def avisar_correo(cands):
    user, pw, dest = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD"), os.environ.get("ALERTA_EMAIL")
    if not (user and pw and dest):
        return False
    msg = EmailMessage()
    msg["Subject"] = f"Radar relojes: {len(cands)} oportunidad(es) nueva(s) en eBay"
    msg["From"], msg["To"] = user, dest
    cuerpo = "<br><br>".join(texto_alerta(c).replace("\n", "<br>") +
                             (f'<br><img src="{c["info"]["imagenes"][0]}" width="320">' if c["info"].get("imagenes") else "")
                             for c in cands)
    msg.set_content("Abre este correo en HTML.")
    msg.add_alternative(f"<html><body style='font-family:sans-serif'>{cuerpo}</body></html>", subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"Correo fallo: {e}", file=sys.stderr)
        return False


# ------------------------------------------------------------------ main
def main():
    if os.environ.get("PRUEBA_AVISO"):
        ultimas = cargar("alertas.json", [])[-1:]
        if ultimas:
            print("Prueba de revision con Claude:", json.dumps(revisar_con_claude(ultimas[0]), ensure_ascii=False))
            print("Prueba de aviso:", avisar_telegram(ultimas), avisar_correo(ultimas))
        else:
            print("No hay alertas guardadas para la prueba")
    eb = Ebay()
    t0 = ahora()
    desde = (t0 - dt.timedelta(hours=LOOKBACK_HORAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    vistos = cargar("vistos.json", {})
    mercado = cargar("mercado.json", {})
    alertas = cargar("alertas.json", [])
    revisados = cargar("revisados_ultimos.json", [])
    with open(ROOT / "guia_precios.csv", newline="", encoding="utf-8") as f:
        guia = list(csv.DictReader(f))

    nuevos, descartes, candidatos = 0, {}, []
    ids_alertados = {a["item_id"] for a in alertas}
    for fila in guia:
        if eb.agotado:
            break
        debe = [s.strip().lower() for s in fila["debe_contener"].split("|")]
        excluir = [s.strip().lower() for s in fila["excluir"].split("|")]
        piso = float(fila["tu_precio_usd"]) * 0.25
        nuevos_items = []
        for pais in PAISES:
            filtros = [f"itemLocationCountry:{pais}", "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
                       f"price:[{int(piso)}..]", "priceCurrency:USD", f"itemStartDate:[{desde}..]"]
            nuevos_items += eb.buscar(fila["busqueda_ebay"], filtros, sort="newlyListed", limit=100)
        for it in nuevos_items:
            iid = it.get("itemId")
            if not iid or iid in vistos:
                continue
            vistos[iid] = t0.isoformat()
            nuevos += 1
            titulo = it.get("title", "")
            total = precio_total(it)
            if not titulo_ok(titulo, debe, excluir) or total is None:
                descartes["titulo"] = descartes.get("titulo", 0) + 1
                continue
            det = eb.item(iid)
            if not det:
                continue
            motivo, info = analizar(det, fila)
            registro = {"fecha": t0.isoformat(), "modelo_guia": fila["modelo"], "titulo": titulo,
                        "total": round(total, 2), "url": it.get("itemWebUrl"), "item_id": iid,
                        "referencia": info.get("referencia"), "motivo": motivo}
            if motivo:
                clave = motivo.split(":")[0].split(" ")[0]
                descartes[clave] = descartes.get(clave, 0) + 1
                revisados.append(registro)
                continue
            m = comparables(eb, fila, info.get("referencia"), info.get("condicion_id"), iid, mercado)
            if m.get("n", 0) < MIN_COMPARABLES and info.get("referencia"):
                m = comparables(eb, fila, None, info.get("condicion_id"), iid, mercado)
            registro["mercado"] = {k: m.get(k) for k in ("consulta", "n", "mediana", "p25", "p75")}
            if m.get("n", 0) < MIN_COMPARABLES or not m.get("mediana"):
                registro["motivo"] = f"sin comparables suficientes ({m.get('n', 0)})"
                descartes["sin_comparables"] = descartes.get("sin_comparables", 0) + 1
                revisados.append(registro)
                continue
            ratio = total / m["mediana"]
            registro["ratio_mercado"] = round(ratio, 3)
            if ratio > UMBRAL_ALERTA:
                registro["motivo"] = f"precio normal ({round(ratio*100)} % de la mediana)"
                descartes["precio_normal"] = descartes.get("precio_normal", 0) + 1
                revisados.append(registro)
                continue
            if ratio < UMBRAL_SOSPECHA:
                registro["motivo"] = f"demasiado barato ({round(ratio*100)} % de la mediana): sospechoso"
                descartes["sospechoso"] = descartes.get("sospechoso", 0) + 1
                revisados.append(registro)
                continue
            vend = det.get("seller") or {}
            cand = {"fecha": t0.isoformat(), "modelo_guia": fila["modelo"], "tu_precio": float(fila["tu_precio_usd"]),
                    "titulo": titulo, "precio": float(det["price"]["value"]), "envio": envio(det),
                    "total": round(total, 2), "ratio_mercado": round(ratio, 3), "mercado": m,
                    "vendedor": vend.get("username"), "valoraciones": vend.get("feedbackScore"),
                    "pct_positivo": vend.get("feedbackPercentage"), "url": it.get("itemWebUrl"),
                    "item_id": iid, "pais": (det.get("itemLocation") or {}).get("country"), "info": info}
            cand["claude"] = revisar_con_claude(cand)
            if cand["claude"] and cand["claude"].get("veredicto") == "descartar":
                registro["motivo"] = "Claude descarto: " + "; ".join(map(str, cand["claude"].get("razones", [])[:2]))
                descartes["claude"] = descartes.get("claude", 0) + 1
                revisados.append(registro)
                continue
            registro["motivo"] = "ALERTA"
            revisados.append(registro)
            if iid not in ids_alertados:
                candidatos.append(cand)

    candidatos.sort(key=lambda c: c["ratio_mercado"])
    if candidatos:
        tg = avisar_telegram(candidatos)
        mail = avisar_correo(candidatos)
        print(f"Avisos: telegram={tg} correo={mail}")
    for c in candidatos:
        c["info"] = {k: v for k, v in c["info"].items() if k != "descripcion"}
    alertas.extend(candidatos)
    limite = (t0 - dt.timedelta(days=90)).isoformat()
    alertas = [a for a in alertas if a["fecha"] >= limite]
    limite_v = (t0 - dt.timedelta(days=30)).isoformat()
    vistos = {k: v for k, v in vistos.items() if v >= limite_v}
    revisados = revisados[-400:]

    guardar("vistos.json", vistos)
    guardar("mercado.json", mercado)
    guardar("alertas.json", alertas)
    guardar("revisados_ultimos.json", revisados)
    hoy = t0.date().isoformat()
    guardar("latest.json", {
        "actualizado": t0.isoformat(), "ventana_horas": LOOKBACK_HORAS,
        "corrida": {"nuevos_vistos": nuevos, "descartes": descartes, "alertas_nuevas": len(candidatos),
                    "llamadas_ebay": eb.llamadas, "cuota_agotada": eb.agotado},
        "alertas_hoy": [a for a in alertas if a["fecha"][:10] == hoy],
        "mercado": mercado,
    })
    print(f"{t0:%Y-%m-%d %H:%M}Z  nuevos={nuevos}  descartes={descartes}  "
          f"ALERTAS={len(candidatos)}  llamadas eBay={eb.llamadas}")
    for c in candidatos:
        print(f"  {round(c['ratio_mercado']*100)} % | {c['modelo_guia']} | {c['titulo'][:70]} | "
              f"US${c['total']:.0f} vs mediana {c['mercado']['mediana']} (n={c['mercado']['n']}) | {c['url']}")


if __name__ == "__main__":
    main()
