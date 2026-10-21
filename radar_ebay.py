"""Radar de relojes v3 - eBay Browse API, busqueda por MARCA.

Flujo (cada 15 min, en bucle):
  1. Por cada marca de marcas.csv busca "<marca> watch" ordenado por mas reciente
     (EE. UU., precio fijo / mejor oferta, precio >= piso) y toma lo nuevo.
  2. Descarta por titulo solo piezas, correas, replicas, lotes. No hay filtro de
     modelo, genero, tamano ni ano: Juan compra cualquier modelo de estas marcas.
  3. Triage barato para decidir si vale la pena abrir la publicacion:
       a) si el titulo trae referencia exacta -> comparables de ESA referencia y
          esa condicion (cache diario). Pasa si total <= UMBRAL_MERCADO * p25.
       b) si no hay referencia o faltan comparables -> Claude (modelo rapido) con
          titulo + miniatura estima el precio justo. Pasa si <= UMBRAL_JUSTO.
  4. Finalistas: getItem (descripcion, specs, fotos, vendedor) -> descarta danados,
     "as is", vendedores flojos -> Claude (modelo completo) con 4 fotos: veredicto,
     riesgo de autenticidad y precio justo. Alerta si <= UMBRAL_JUSTO del precio
     justo y no fue descartado.
  5. Telegram con foto. Nunca avisa dos veces del mismo articulo.

Variables de entorno:
  EBAY_CLIENT_ID, EBAY_CLIENT_SECRET            (obligatorias)
  ANTHROPIC_API_KEY                             (necesaria para triage y revision)
  CLAUDE_MODEL (def. claude-sonnet-5-5), CLAUDE_MODEL_RAPIDO (def. claude-haiku-5-5)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  LOOKBACK_HORAS (def. 2), UMBRAL_MERCADO (def. 0.85), UMBRAL_JUSTO (def. 0.85)
  BARRIDO=1 revisa todo lo activo (no solo lo nuevo), PRUEBA_AVISO=1 reenvia la ultima alerta
"""
import base64
import csv
import datetime as dt
import html
import http.client
import json
import os
import pathlib
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/"
SCOPE = "https://api.ebay.com/oauth/api_scope"
WATCH_CATEGORY = "31387"
PAISES = os.environ.get("PAISES_ORIGEN", "US").split(",")
BARRIDO = bool(os.environ.get("BARRIDO"))

LOOKBACK_HORAS = float(os.environ.get("LOOKBACK_HORAS", "2"))
UMBRAL_MERCADO = float(os.environ.get("UMBRAL_MERCADO", "0.85"))  # vs cuartil bajo de la misma referencia
UMBRAL_JUSTO = float(os.environ.get("UMBRAL_JUSTO", "0.85"))      # vs precio justo estimado por Claude
UMBRAL_SOSPECHA = 0.35
MIN_COMPARABLES = 5
MIN_VALORACIONES = 20
MIN_POSITIVO = 97.0
MODELO_COMPLETO = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
MODELO_RAPIDO = os.environ.get("CLAUDE_MODEL_RAPIDO", "claude-haiku-5-5")

# Titulos que delatan piezas, accesorios o basura (unico filtro de titulo)
JUNK_TITULO = ["replica", "homage", "for parts", "parts only", "not working", " repair", "dial only",
               " dial ", " bezel", "insert", " link", "links", "bracelet only", "strap", " band", "buckle",
               "clasp", "crown", "crystal", "hands", "movement only", "movement ", "case only", "caseback",
               "case back", "plate", "papers only", "booklet", "manual", "box only", "box and papers only",
               "warranty card", "tag only", "hang tag", "fits ", "for rolex", "for cartier", "for omega",
               "for tudor", "for breitling", "for tag", "for iwc", "for panerai", "compatible", "aftermarket",
               "custom", " mod ", "genuine rolex part", "lot of", "wholesale", "display", "catalog",
               "catalogue", "advertisement", "poster", "magazine", "pouch", "travel case",
               "winder", "tool", "spring bar", "gasket", "stem", "rotor", "bulova", "invicta", "seiko",
               "cufflink", "lighter", "sunglasses", "keychain", "necklace", "style", "inspired"]
ROJAS = ["not working", "doesn't work", "does not work", "not running", "doesn't run", "does not run",
         "non working", "non-working", "as is", "as-is", "for parts", "parts only", "parts or repair",
         "needs service", "needs servicing", "needs repair", "needs work", "needs to be serviced",
         "needs a service", "needs restoration", "no movement", "movement missing", "missing movement",
         "keeps stopping", "stops running", "loses time", "losing time", "gains time", "runs fast",
         "runs slow", "cracked crystal", "cracked", "broken", "replica", "franken", "aftermarket dial",
         "aftermarket bezel", "aftermarket movement", "refinished dial", "redial", "re-dial", "custom dial",
         "not authentic", "unknown authenticity", "cannot guarantee authenticity",
         "can't guarantee authenticity", "sold as untested", "untested"]
AMARILLAS = ["no returns", "all sales final", "scratch", "scratches", "wear", "worn", "polished",
             "service history unknown", "no box", "no papers", "battery", "dent", "hairline", "patina", "faded",
             "aftermarket strap", "aftermarket band"]

PATRONES_REF = {
    "rolex": r"\b(1\d{4,5}[A-Z]{0,4}|2\d{5}[A-Z]{0,4}|[56]\d{3,4})\b",
    "tudor": r"\b(M?\d{5}[A-Z]{0,2}(?:-\d{4})?)\b",
    "cartier": r"\b(W[A-Z]{0,4}\d{4,7}[A-Z]?|W\d[A-Z]{2}\d{4}|CRW[A-Z0-9]{5,9}|[1-3]\d{3})\b",
    "omega": r"\b(\d{3}\.\d{2}\.\d{2}\.\d{2}\.\d{2}\.\d{3}|\d{4}\.\d{2}(?:\.\d{2})?|\d{3}\.\d{3}\.\d{3})\b",
    "breitling": r"\b([A-Z]{1,2}\d{5}[A-Z]?|[A-Z]{2}\d{4}|AB\d{6}[A-Z0-9]*|[A-Z]\d{6}[A-Z0-9]*)\b",
    "tag heuer": r"\b([A-Z]{2,3}\d[A-Z0-9]{1,5}(?:\.[A-Z]{2}\d{4})?)\b",
    "panerai": r"\b(PAM\s?0?\d{3,5})\b",
    "jaeger": r"\b(Q\d{7}|\d{3}\.\d\.\d{2}(?:\.[A-Z])?|\d{3}\.\d{4}\.\d{3})\b",
    "iwc": r"\b(IW\s?\d{6})\b",
    "bulgari": r"\b(10\d{4}|[A-Z]{2,4}\s?\d{2}\s?[A-Z]{1,4}(?:\s?\d{2}[A-Z]{1,2})?)\b",
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
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(s)).strip()


def mediana_recortada(valores):
    v = sorted(valores)
    if len(v) >= 8:
        corte = max(1, len(v) // 10)
        v = v[corte:-corte]
    return statistics.median(v), v[len(v) // 4], v[(3 * len(v)) // 4]


def cuenta(d, k):
    d[k] = d.get(k, 0) + 1


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
        for intento in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.agotado = True
                    print("Cuota diaria de eBay agotada; se detiene la corrida.", file=sys.stderr)
                    return None
                if e.code >= 500 and intento < 2:
                    time.sleep(2 + 3 * intento)
                    continue
                print(f"HTTP {e.code} en {url[:120]}: {e.read().decode(errors='replace')[:200]}", file=sys.stderr)
                return None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as e:
                if intento < 2:
                    time.sleep(2 + 3 * intento)
                    continue
                print(f"Sin respuesta de eBay en {url[:120]}: {e}", file=sys.stderr)
                return None

    def buscar(self, q, filtros, sort=None, limit=200, offset=0):
        params = {"q": q, "category_ids": WATCH_CATEGORY, "filter": ",".join(filtros),
                  "limit": str(limit), "offset": str(offset)}
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


def titulo_ok(titulo, marca):
    t = f" {titulo.lower()} "
    if any(j in t for j in JUNK_TITULO):
        return False
    m = marca.lower()
    nombres = {"bulgari": ["bulgari", "bvlgari"], "tag heuer": ["tag heuer", "tag-heuer", "heuer"],
               "jaeger-lecoultre": ["jaeger", "lecoultre", "jlc"]}.get(m, [m])
    return any(n in t for n in nombres)


def extraer_ref(marca, textos):
    patron = next((p for k, p in PATRONES_REF.items() if k in marca.lower()), None)
    if not patron:
        return None
    for t in textos:
        for m in re.finditer(patron, t or "", flags=re.I):
            ref = m.group(1).upper().replace(" ", "")
            if re.fullmatch(r"(19|20)\d{2}", ref):  # un ano no es una referencia
                continue
            return ref
    return None


def aspectos(detalle):
    return {(a.get("name") or "").strip().lower(): (a.get("value") or "").strip()
            for a in detalle.get("localizedAspects", []) or []}


# ---------------------------------------------------------------- mercado
MERCADO_BARRIDO = {}
try:
    MERCADO_BARRIDO = json.loads((DATA / "mercado_barrido.json").read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    pass


def comparables(eb, marca, ref, condicion_id, excluir_id, cache):
    """Cuartil bajo / mediana de los listados activos en EE. UU. de la MISMA referencia
    y misma condicion (nuevo/usado). Cache diario por referencia."""
    usada = str(condicion_id or "") not in ("1000", "1500", "1750")
    q = f"{marca} {ref}"
    clave = f"{q}|{'usado' if usada else 'nuevo'}"
    hoy = dt.date.today().isoformat()
    if clave in cache and cache[clave].get("fecha") == hoy:
        return cache[clave]
    # Mercado calculado por el ultimo barrido completo (vale 3 dias, no gasta cuota)
    mb = MERCADO_BARRIDO.get(f"{marca}|{ref.split('-')[0]}|{'usado' if usada else 'nuevo'}")
    if mb and mb.get("n", 0) >= MIN_COMPARABLES and mb.get("fecha", "") >= (dt.date.today() - dt.timedelta(days=3)).isoformat():
        return mb
    filtros = ["itemLocationCountry:US", "buyingOptions:{FIXED_PRICE|BEST_OFFER}", "priceCurrency:USD",
               "conditions:{USED}" if usada else "conditions:{NEW}"]
    precios = []
    for it in eb.buscar(q, filtros, sort="price", limit=200):
        if it.get("itemId") == excluir_id:
            continue
        t = it.get("title", "")
        if not titulo_ok(t, marca) or ref.lower() not in t.lower().replace(" ", ""):
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


# ------------------------------------------------------------------ Claude
def claude(modelo, contenido, max_tokens=6000):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({"model": modelo, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": contenido}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    texto = claude_texto(req)
    if texto is None:
        return None
    m = re.search(r"\{.*\}", texto, flags=re.S)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    print(f"Claude ({modelo}) sin JSON: {texto[:200]!r}", file=sys.stderr)
    return None


def claude_texto(req, intentos=5):
    """Llama a la API con reintentos ante 429/5xx/sobrecarga; devuelve el texto de la respuesta."""
    for intento in range(intentos):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.load(r)
            return "".join(b.get("text", "") for b in resp.get("content", []))
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 502, 503, 529) and intento < intentos - 1:
                espera = int(e.headers.get("retry-after") or 0) or (5 * 2 ** intento)
                time.sleep(min(espera, 90))
                continue
            print(f"Claude HTTP {e.code}: {cuerpo}", file=sys.stderr)
            return None
        except Exception as e:
            if intento < intentos - 1:
                time.sleep(5 * 2 ** intento)
                continue
            print(f"Claude no disponible: {e}", file=sys.stderr)
            return None


CONTEXTO = ("Eres un comprador experto de relojes de lujo usados para reventa (Crono Watches, Republica Dominicana). "
            "Compra cualquier modelo de Rolex, Cartier, Tudor, Panerai, Omega, Breitling, TAG Heuer, "
            "Jaeger-LeCoultre, Bulgari e IWC, de hombre o de mujer, moderno o vintage, si el precio es claramente "
            "bueno para la referencia exacta, el ano y el estado. "
            "precio_justo_usd = lo que ESE reloj concreto (referencia, generacion/ano, material, tamano, estado, "
            "con o sin caja y papeles) se vende realmente hoy en EE. UU. entre particulares y dealers (precios de "
            "venta cerrada, no precios pedidos). Un Datejust de 1985 no vale lo que uno de 2022; un Reverso "
            "Classic de acero no vale lo que uno de oro con complicaciones. Se conservador. ")


def triage_claude(marca, titulo, total, condicion, miniatura):
    """Clasificacion barata: solo titulo, precio, condicion y miniatura."""
    contenido = []
    if miniatura:
        contenido.append({"type": "image", "source": {"type": "url", "url": miniatura}})
    contenido.append({"type": "text", "text": CONTEXTO + (
        f"\n\nPublicacion nueva en eBay EE. UU.\nMarca buscada: {marca}\nTitulo: {titulo}\n"
        f"Precio total con envio: US${total:.0f}\nCondicion eBay: {condicion}\n\n"
        "Identifica el modelo/referencia/generacion con lo que se ve y estima el precio justo. "
        "Responde SOLO JSON: {\"modelo\": \"...\", \"referencia\": \"... o null\", "
        "\"precio_justo_usd\": numero, \"interesante\": true|false, \"nota\": \"frase corta\"}. "
        "interesante=true solo si el precio pedido esta claramente por debajo (>=15 %) de lo que ese reloj "
        "se vende realmente, y no parece pieza, replica ni listado enganoso.")})
    return claude(MODELO_RAPIDO, contenido, max_tokens=3000)


def revision_claude(cand):
    info = cand["info"]
    contenido = [{"type": "image", "source": {"type": "url", "url": u}} for u in info["imagenes"][:4]]
    resumen = {k: info.get(k) for k in ("referencia", "modelo_esp", "departamento", "caja_mm", "ano", "material",
                                         "movimiento", "caja_original", "papeles", "condicion", "condicion_desc",
                                         "garantia_autenticidad", "devoluciones")}
    m = cand.get("mercado") or {}
    contenido.append({"type": "text", "text": CONTEXTO + (
        "\n\nEvalua esta publicacion. Mira las fotos: estado real de caja, bisel, cristal, esfera, brazalete; "
        "senales de replica, piezas no originales, dial repintado, fotos de catalogo o robadas; si falta foto del "
        "fondo de caja o del movimiento dilo. Lee la descripcion buscando defectos o condiciones ocultas.\n\n"
        f"Marca: {cand['marca']}\nTitulo: {cand['titulo']}\nPrecio total: US${cand['total']:.0f}\n"
        + (f"Comparables de la misma referencia en eBay EE. UU. ({m.get('n')}): cuartil bajo {m.get('p25')}, "
           f"mediana {m.get('mediana')} (son precios pedidos, suelen estar inflados)\n" if m.get("n") else "")
        + (f"Triage previo: {json.dumps(cand.get('triage'), ensure_ascii=False)}\n" if cand.get("triage") else "")
        + f"Especificaciones: {json.dumps(resumen, ensure_ascii=False)}\n"
        f"Vendedor: {cand['vendedor']} ({cand['valoraciones']} valoraciones, {cand['pct_positivo']} % positivo)\n"
        f"Descripcion: {(info.get('descripcion') or '')[:1800]}\n\n"
        "Responde SOLO JSON: {\"veredicto\": \"alerta\"|\"dudoso\"|\"descartar\", \"modelo\": \"...\", "
        "\"estado\": \"frase corta\", \"riesgo_autenticidad\": \"bajo\"|\"medio\"|\"alto\", "
        "\"completo\": true|false, \"razones\": [\"...\", \"...\"], \"precio_justo_usd\": numero, "
        "\"precio_reventa_rd_usd\": numero}")})
    return claude(MODELO_COMPLETO, contenido)


# ------------------------------------------------------------- analisis item
def analizar(detalle, marca):
    asp = aspectos(detalle)
    titulo = detalle.get("title", "")
    desc = sin_html(detalle.get("description", ""))
    cond_desc = detalle.get("conditionDescription") or ""
    texto = f"{titulo}\n{cond_desc}\n{desc}".lower()
    info = {
        "departamento": asp.get("department") or asp.get("gender"),
        "referencia": asp.get("reference number") or asp.get("model number") or asp.get("manufacturer part number"),
        "modelo_esp": asp.get("model"), "caja_mm": asp.get("case size"), "material": asp.get("case material"),
        "ano": asp.get("year manufactured") or asp.get("year of manufacture"), "movimiento": asp.get("movement"),
        "caja_original": asp.get("with original box/packaging") or asp.get("with original box"),
        "papeles": asp.get("with papers") or asp.get("with original papers"),
        "condicion": detalle.get("condition"), "condicion_id": detalle.get("conditionId"),
        "condicion_desc": cond_desc[:400],
        "garantia_autenticidad": bool(detalle.get("authenticityVerification")),
        "devoluciones": (detalle.get("returnTerms") or {}).get("returnsAccepted"),
        "amarillas": sorted({a for a in AMARILLAS if a in texto}),
        "descripcion": desc[:1800],
        "imagenes": [u for u in [(detalle.get("image") or {}).get("imageUrl")] +
                     [i.get("imageUrl") for i in detalle.get("additionalImages", []) or []] if u][:6],
    }
    if not info["referencia"]:
        info["referencia"] = extraer_ref(marca, [titulo, asp.get("model", "")])
    rojas = [r for r in ROJAS if r in texto]
    if rojas:
        return "descripcion: " + ", ".join(rojas[:3]), info
    if str(info["condicion_id"] or "") == "7000" or "parts" in (info["condicion"] or "").lower():
        return "condicion: piezas/reparacion", info
    vend = detalle.get("seller") or {}
    try:
        if int(vend.get("feedbackScore") or 0) < MIN_VALORACIONES:
            return f"vendedor con {vend.get('feedbackScore')} valoraciones", info
        if float(vend.get("feedbackPercentage") or 0) < MIN_POSITIVO:
            return f"vendedor {vend.get('feedbackPercentage')} % positivo", info
    except ValueError:
        pass
    return None, info


# ---------------------------------------------------------------- avisos
def texto_alerta(c):
    cl = c.get("claude") or {}
    m = c.get("mercado") or {}
    lineas = [f"<b>{html.escape(c['marca'])} — {html.escape(str(cl.get('modelo') or c['info'].get('modelo_esp') or ''))}</b>",
              html.escape(c["titulo"][:120]),
              f"Precio: <b>US${c['total']:,.0f}</b> (envio {c['envio'] if c['envio'] is not None else '?'})"]
    if cl.get("precio_justo_usd"):
        lineas.append(f"Precio justo estimado: <b>US${float(cl['precio_justo_usd']):,.0f}</b> → esta al "
                      f"{round(100 * c['total'] / float(cl['precio_justo_usd']))} %"
                      + (f" · reventa RD ~US${float(cl['precio_reventa_rd_usd']):,.0f}" if cl.get("precio_reventa_rd_usd") else ""))
    if m.get("n"):
        lineas.append(f"Misma referencia en eBay: {m['n']} listados, cuartil bajo US${m['p25']:,}, mediana US${m['mediana']:,}")
    lineas.append(f"Ref. {c['info'].get('referencia') or '?'} · {c['info'].get('caja_mm') or ''} · "
                  f"{c['info'].get('ano') or 'ano ?'} · {c['info'].get('condicion')}"
                  + " · caja/papeles: " + "/".join(str(c['info'].get(k) or '?') for k in ("caja_original", "papeles"))
                  + (" · Authenticity Guarantee" if c['info'].get('garantia_autenticidad') else ""))
    lineas.append(f"Vendedor: {c['vendedor']} ({c['valoraciones']} val., {c['pct_positivo']} %)")
    if c["info"].get("amarillas"):
        lineas.append("Ojo: " + ", ".join(c["info"]["amarillas"]))
    if cl:
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
                body = urllib.parse.urlencode({"chat_id": chat, "photo": foto, "caption": texto, "parse_mode": "HTML"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendPhoto", data=body, timeout=30).read()
            else:
                body = urllib.parse.urlencode({"chat_id": chat, "text": texto, "parse_mode": "HTML"}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, timeout=30).read()
        except Exception as e:
            print(f"Telegram fallo: {e}", file=sys.stderr)
        time.sleep(1.2)  # Telegram limita ~1 mensaje/seg por chat
    return True


# ------------------------------------------------------------------ main
def procesar_marca(eb, fila, t0, desde, estado):
    marca = fila["marca"]
    piso = float(fila["piso_usd"])
    vistos, mercado, revisados, candidatos, descartes = (estado[k] for k in ("vistos", "mercado", "revisados", "candidatos", "descartes"))
    items = []
    for pais in PAISES:
        filtros = [f"itemLocationCountry:{pais}", "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
                   f"price:[{int(piso)}..]", "priceCurrency:USD"]
        if BARRIDO:
            for off in (0, 200, 400, 600, 800):
                lote = eb.buscar(fila["busqueda_ebay"], filtros, sort="price", limit=200, offset=off)
                items += lote
                if len(lote) < 200:
                    break
        else:
            filtros.append(f"itemStartDate:[{desde}..]")
            for off in (0, 200):
                lote = eb.buscar(fila["busqueda_ebay"], filtros, sort="newlyListed", limit=200, offset=off)
                items += lote
                if len(lote) < 200:
                    break
    for it in items:
        if eb.agotado:
            return
        iid = it.get("itemId")
        if not iid or iid in vistos:
            continue
        vistos[iid] = t0.isoformat()
        estado["nuevos"] += 1
        titulo = it.get("title", "")
        total = precio_total(it)
        if total is None or not titulo_ok(titulo, marca):
            cuenta(descartes, "titulo")
            continue
        condicion = it.get("condition")
        registro = {"fecha": t0.isoformat(), "marca": marca, "titulo": titulo, "total": round(total, 2),
                    "condicion": condicion, "url": (it.get("itemWebUrl") or "").split("?")[0], "item_id": iid}
        # --- triage: referencia + comparables, o Claude rapido
        ref = extraer_ref(marca, [titulo])
        m, triage = None, None
        if ref:
            m = comparables(eb, marca, ref, it.get("conditionId"), iid, mercado)
            registro["referencia"] = ref
            registro["mercado"] = {k: m.get(k) for k in ("n", "p25", "mediana")}
        if m and m.get("n", 0) >= MIN_COMPARABLES:
            ratio_prev = total / m["p25"]
            registro["ratio_mercado"] = round(ratio_prev, 3)
            if ratio_prev < UMBRAL_SOSPECHA:
                registro["motivo"] = f"demasiado barato ({round(ratio_prev*100)} % del cuartil bajo): sospechoso"
                cuenta(descartes, "sospechoso"); revisados.append(registro); continue
            if ratio_prev > UMBRAL_MERCADO:
                registro["motivo"] = f"precio normal ({round(ratio_prev*100)} % del cuartil bajo de {ref})"
                cuenta(descartes, "precio_normal"); revisados.append(registro); continue
        else:
            miniatura = (it.get("image") or {}).get("imageUrl")
            triage = triage_claude(marca, titulo, total, condicion, miniatura)
            registro["triage"] = triage
            if triage is None:
                registro["motivo"] = "triage no disponible"
                cuenta(descartes, "triage_error"); revisados.append(registro); continue
            try:
                justo = float(triage.get("precio_justo_usd") or 0)
            except (TypeError, ValueError):
                justo = 0
            if not (triage.get("interesante") and justo and total <= UMBRAL_JUSTO * justo):
                registro["motivo"] = f"triage: {triage.get('nota', '')} (justo {justo:.0f})"
                cuenta(descartes, "triage"); revisados.append(registro); continue
        # --- finalista: abrir, leer, revisar con fotos
        det = eb.item(iid)
        if not det:
            continue
        motivo, info = analizar(det, marca)
        registro["referencia"] = info.get("referencia") or ref
        if motivo:
            registro["motivo"] = motivo
            cuenta(descartes, motivo.split(":")[0].split(" ")[0]); revisados.append(registro); continue
        if ref is None and info.get("referencia"):
            m = comparables(eb, marca, info["referencia"], info.get("condicion_id"), iid, mercado)
            registro["mercado"] = {k: m.get(k) for k in ("n", "p25", "mediana")}
        vend = det.get("seller") or {}
        cand = {"fecha": t0.isoformat(), "marca": marca, "titulo": titulo, "precio": float(det["price"]["value"]),
                "envio": envio(det), "total": round(total, 2), "mercado": m, "triage": triage,
                "vendedor": vend.get("username"), "valoraciones": vend.get("feedbackScore"),
                "pct_positivo": vend.get("feedbackPercentage"), "url": registro["url"], "item_id": iid, "info": info}
        cl = revision_claude(cand)
        cand["claude"] = cl
        registro["claude"] = {k: (cl or {}).get(k) for k in ("veredicto", "precio_justo_usd", "riesgo_autenticidad", "estado")}
        if not cl:
            registro["motivo"] = "revision no disponible"
            cuenta(descartes, "revision_error"); revisados.append(registro); continue
        if cl.get("veredicto") == "descartar":
            registro["motivo"] = "Claude descarto: " + "; ".join(map(str, cl.get("razones", [])[:2]))
            cuenta(descartes, "claude"); revisados.append(registro); continue
        try:
            justo = float(cl.get("precio_justo_usd") or 0)
        except (TypeError, ValueError):
            justo = 0
        if justo:
            cand["ratio_justo"] = round(total / justo, 3)
            registro["ratio_justo"] = cand["ratio_justo"]
            if cand["ratio_justo"] > UMBRAL_JUSTO:
                registro["motivo"] = f"precio normal segun revision ({round(cand['ratio_justo']*100)} % del justo {justo:.0f})"
                cuenta(descartes, "precio_justo"); revisados.append(registro); continue
        registro["motivo"] = "ALERTA"
        revisados.append(registro)
        if iid not in estado["ids_alertados"]:
            candidatos.append(cand)


def main():
    if os.environ.get("PRUEBA_AVISO"):
        ultimas = cargar("alertas.json", [])[-1:]
        print("Prueba de aviso:", avisar_telegram(ultimas) if ultimas else "sin alertas guardadas")
    eb = Ebay()
    t0 = ahora()
    desde = (t0 - dt.timedelta(hours=LOOKBACK_HORAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    alertas = cargar("alertas.json", [])
    estado = {"vistos": cargar("vistos.json", {}), "mercado": cargar("mercado.json", {}),
              "revisados": cargar("revisados_ultimos.json", []), "candidatos": [], "descartes": {},
              "nuevos": 0, "ids_alertados": {a["item_id"] for a in alertas + cargar("barrido_alertas.json", [])}}
    with open(ROOT / "marcas.csv", newline="", encoding="utf-8") as f:
        marcas = list(csv.DictReader(f))
    for fila in marcas:
        if eb.agotado:
            break
        try:
            procesar_marca(eb, fila, t0, desde, estado)
        except Exception as e:
            print(f"Error inesperado en {fila['marca']}: {e!r}", file=sys.stderr)
    candidatos = sorted(estado["candidatos"], key=lambda c: c.get("ratio_justo") or 1)
    if candidatos:
        print(f"Avisos: telegram={avisar_telegram(candidatos)}")
    for c in candidatos:
        c["info"] = {k: v for k, v in c["info"].items() if k != "descripcion"}
    alertas = [a for a in alertas + candidatos if a["fecha"] >= (t0 - dt.timedelta(days=90)).isoformat()]
    limite_v = (t0 - dt.timedelta(days=30)).isoformat()
    guardar("vistos.json", {k: v for k, v in estado["vistos"].items() if v >= limite_v})
    guardar("mercado.json", estado["mercado"])
    guardar("alertas.json", alertas)
    guardar("revisados_ultimos.json", estado["revisados"][-600:])
    hoy = t0.date().isoformat()
    guardar("latest.json", {
        "actualizado": t0.isoformat(), "ventana_horas": LOOKBACK_HORAS, "marcas": [m["marca"] for m in marcas],
        "corrida": {"nuevos_vistos": estado["nuevos"], "descartes": estado["descartes"],
                    "alertas_nuevas": len(candidatos), "llamadas_ebay": eb.llamadas, "cuota_agotada": eb.agotado},
        "alertas_hoy": [a for a in alertas if a["fecha"][:10] == hoy],
    })
    print(f"{t0:%Y-%m-%d %H:%M}Z  nuevos={estado['nuevos']}  descartes={estado['descartes']}  "
          f"ALERTAS={len(candidatos)}  llamadas eBay={eb.llamadas}")
    for c in candidatos:
        cl = c.get("claude") or {}
        print(f"  {round((c.get('ratio_justo') or 0)*100)} % del justo | {c['marca']} | {c['titulo'][:70]} | "
              f"US${c['total']:.0f} vs justo {cl.get('precio_justo_usd')} | {cl.get('veredicto')} | {c['url']}")


if __name__ == "__main__":
    main()
