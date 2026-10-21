"""Consulta puntual a eBay (misma API del radar).

Uso: CONSULTA="bvlgari serpenti" PRECIO_MIN=1500 python consulta_ebay.py
Escribe data/consulta.json con todos los listados activos de precio fijo en EE. UU.
ordenados por precio, con vendedor, condicion, especificaciones basicas y enlace.
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import radar_ebay as r  # noqa: E402

consulta = os.environ.get("CONSULTA", "bvlgari serpenti")
precio_min = int(float(os.environ.get("PRECIO_MIN", "1500")))
abrir = int(os.environ.get("ABRIR", "60"))  # cuantos abrir con getItem para leer specs
eb = r.Ebay()
filtros = ["itemLocationCountry:US", "buyingOptions:{FIXED_PRICE|BEST_OFFER}",
           f"price:[{precio_min}..]", "priceCurrency:USD"]
items = eb.buscar(consulta, filtros, sort="price", limit=200)
salida = []
for it in items:
    t = it.get("title", "")
    tl = t.lower()
    if any(j in tl for j in ["strap", "band only", "bracelet only", "box only", "replica", "for parts", "links", "buckle"]):
        continue
    total = r.precio_total(it)
    if total is None:
        continue
    v = it.get("seller") or {}
    fila = {"titulo": t, "precio": float(it["price"]["value"]), "envio": r.envio(it), "total": round(total, 2),
            "condicion": it.get("condition"), "vendedor": v.get("username"), "valoraciones": v.get("feedbackScore"),
            "pct_positivo": v.get("feedbackPercentage"), "url": (it.get("itemWebUrl") or "").split("?")[0],
            "item_id": it.get("itemId")}
    salida.append(fila)
for fila in salida[:abrir]:
    det = eb.item(fila["item_id"])
    if not det:
        continue
    asp = r.aspectos(det)
    fila.update({"referencia": asp.get("reference number") or asp.get("model number"),
                 "modelo": asp.get("model"), "caja_mm": asp.get("case size"), "material": asp.get("case material"),
                 "departamento": asp.get("department"), "ano": asp.get("year manufactured"),
                 "caja_original": asp.get("with original box/packaging"), "papeles": asp.get("with papers"),
                 "garantia_autenticidad": bool(det.get("authenticityVerification")),
                 "devoluciones": (det.get("returnTerms") or {}).get("returnsAccepted"),
                 "condicion_desc": (det.get("conditionDescription") or "")[:200],
                 "rojas": [x for x in r.ROJAS if x in r.sin_html(det.get("description", "")).lower()][:4],
                 "foto": (det.get("image") or {}).get("imageUrl")})
r.guardar("consulta.json", {"consulta": consulta, "precio_min": precio_min, "n": len(salida), "items": salida})
print(f"{consulta}: {len(salida)} listados (llamadas {eb.llamadas})")
for f in salida:
    print(f"{f['total']:>8.0f} | {f.get('condicion','')[:18]:18} | {f.get('referencia') or '':12} | {f.get('caja_mm') or '':6} | "
          f"{f.get('material') or '':14} | {f['titulo'][:70]} | {f['vendedor']} {f['valoraciones']} {f['pct_positivo']} | {f['url']}")
