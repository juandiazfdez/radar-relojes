"""Radar diario de relojes - eBay Browse API.

Lee guia_precios.csv, busca en eBay listados de precio fijo ubicados en EE. UU.
y guarda los que estan a <=65% (oportunidad real) o 65-80% (otras opciones)
del precio de venta de Juan. Escribe data/YYYY-MM-DD.json y data/latest.json.

Variables de entorno: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET.
"""
import base64
import csv
import datetime as dt
import json
import os
import pathlib
import sys
import urllib.parse
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"
WATCH_CATEGORY = "31387"  # Wristwatches
UMBRAL_OPORTUNIDAD = 0.65
UMBRAL_OTRAS = 0.80
JUNK = ["replica", "homage", "for parts", "not working", "repair", "dial only",
        "band only", "bracelet only", "case only", "movement only", "box only"]


def token():
    cid, secret = os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"]
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Error de token eBay {e.code}: {e.read().decode(errors='replace')[:300]}")


def buscar(tok, q, precio_max):
    params = {
        "q": q,
        "category_ids": WATCH_CATEGORY,
        "filter": ",".join([
            "itemLocationCountry:US",
            "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
            f"price:[100..{int(precio_max)}]",
            "priceCurrency:USD",
        ]),
        "sort": "price",
        "limit": "200",
    }
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", headers={
        "Authorization": f"Bearer {tok}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("itemSummaries", [])


def envio(item):
    for op in item.get("shippingOptions", []) or []:
        cost = (op.get("shippingCost") or {}).get("value")
        if cost is not None:
            return float(cost)
    return None


def coincide(titulo, debe, excluir):
    t = titulo.lower()
    if any(j in t for j in JUNK):
        return False
    if any(x and x in t for x in excluir):
        return False
    return all(d in t for d in debe if d)


def main():
    tok = token()
    hoy = dt.date.today().isoformat()
    resultados = []
    with open(ROOT / "guia_precios.csv", newline="", encoding="utf-8") as f:
        guia = list(csv.DictReader(f))
    for fila in guia:
        tu_precio = float(fila["tu_precio_usd"])
        debe = [s.strip().lower() for s in fila["debe_contener"].split("|")]
        excluir = [s.strip().lower() for s in fila["excluir"].split("|")]
        try:
            items = buscar(tok, fila["busqueda_ebay"], tu_precio * UMBRAL_OTRAS)
        except Exception as e:  # sigue con el siguiente modelo
            print(f"Error en {fila['modelo']}: {e}", file=sys.stderr)
            continue
        for it in items:
            titulo = it.get("title", "")
            if not coincide(titulo, debe, excluir):
                continue
            try:
                precio = float(it["price"]["value"])
            except (KeyError, TypeError, ValueError):
                continue
            env = envio(it)
            total = precio + (env or 0)
            pct = total / tu_precio
            vendedor = it.get("seller", {}) or {}
            resultados.append({
                "fecha": hoy,
                "modelo_guia": fila["modelo"],
                "tu_precio": tu_precio,
                "titulo": titulo,
                "precio": precio,
                "envio": env,
                "pct_de_tu_precio": round(pct * 100, 1),
                "margen_bruto": round(tu_precio - total),
                "grupo": "oportunidad" if pct <= UMBRAL_OPORTUNIDAD else "otras",
                "condicion": it.get("condition"),
                "vendedor": vendedor.get("username"),
                "valoraciones": vendedor.get("feedbackScore"),
                "pct_positivo": vendedor.get("feedbackPercentage"),
                "opciones": it.get("buyingOptions"),
                "url": it.get("itemWebUrl"),
                "item_id": it.get("itemId"),
                "nota_guia": fila.get("nota", ""),
            })
    resultados.sort(key=lambda r: (r["grupo"] != "oportunidad", r["pct_de_tu_precio"]))
    salida = {"fecha": hoy, "modelos": len(guia), "resultados": resultados}
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    for nombre in (f"{hoy}.json", "latest.json"):
        (data / nombre).write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    op = sum(r["grupo"] == "oportunidad" for r in resultados)
    print(f"{hoy}: {op} oportunidades, {len(resultados) - op} otras opciones")


if __name__ == "__main__":
    main()
