"""Barrido completo del mercado actual (eBay EE. UU.) con el criterio v3.

1. Descarga TODO el inventario activo (precio fijo / mejor oferta, EE. UU.) de cada
   marca de marcas.csv, partiendo por rangos de precio para saltar el tope de 10 000
   resultados por busqueda.
2. Comparables locales: agrupa por marca + referencia exacta + nuevo/usado y saca el
   cuartil bajo con todo el mercado descargado (sin gastar mas cuota).
3. Sin referencia (o con pocos comparables): Claude evalua por lotes de titulos y
   marca los que esten >=15 % por debajo del precio justo.
4. Finalistas (los mas baratos de ambos caminos): se abren completos y Claude los
   revisa con fotos. Alerta si <= UMBRAL_JUSTO del precio justo y no se descarta.
5. Telegram: resumen + alertas con foto. Guarda data/barrido_reporte.json,
   data/barrido_alertas.json y data/mercado_barrido.json (lo reutiliza el radar).
"""
import concurrent.futures as cf
import csv
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import radar_ebay as r

FINALISTAS_MAX = int(os.environ.get("FINALISTAS_MAX", "600"))
LOTE = int(os.environ.get("LOTE_TRIAGE", "40"))
HILOS_CLAUDE = int(os.environ.get("HILOS_CLAUDE", "6"))
UMBRAL_MERCADO = r.UMBRAL_MERCADO
UMBRAL_JUSTO = r.UMBRAL_JUSTO
LIMITE_TRIAGE_SEG = float(os.environ.get("LIMITE_TRIAGE_HORAS", "3.3")) * 3600
LIMITE_TOTAL_SEG = float(os.environ.get("LIMITE_TOTAL_HORAS", "5.2")) * 3600
BANDAS = [0, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 5000, 6000, 7000, 8000, 9000, 10000,
          12000, 14000, 16000, 20000, 25000, 30000, 40000, 60000, 100000, None]
T0 = time.time()
lock = threading.Lock()


def log(*a):
    print(f"[{(time.time() - T0) / 60:6.1f} min]", *a, flush=True)


# ------------------------------------------------------------ 1. descarga
def pagina(eb, q, filtros, offset):
    params = {"q": q, "category_ids": r.WATCH_CATEGORY, "filter": ",".join(filtros),
              "limit": "200", "offset": str(offset), "sort": "price"}
    resp = eb._get(f"{r.SEARCH_URL}?{urllib.parse.urlencode(params)}") or {}
    return resp.get("itemSummaries", []) or [], int(resp.get("total") or 0)


def descargar_rango(eb, q, base, lo, hi, salida, prof=0):
    filtros = base + [f"price:[{lo}..{hi if hi is not None else ''}]"]
    items, total = pagina(eb, q, filtros, 0)
    if total > 9800 and hi is not None and hi - lo > 20 and prof < 8:
        mid = (lo + hi) // 2
        descargar_rango(eb, q, base, lo, mid, salida, prof + 1)
        descargar_rango(eb, q, base, mid, hi, salida, prof + 1)
        return
    for it in items:
        salida[it.get("itemId")] = it
    off = 200
    while off < min(total, 10000) and not eb.agotado:
        lote, _ = pagina(eb, q, filtros, off)
        if not lote:
            break
        for it in lote:
            salida[it.get("itemId")] = it
        off += 200


def descargar_marca(eb, fila):
    q = fila.get("busqueda_barrido") or fila["busqueda_ebay"].replace(" watch", "")
    piso = int(float(fila["piso_usd"]))
    base = ["itemLocationCountry:US", "buyingOptions:{FIXED_PRICE|BEST_OFFER}", "priceCurrency:USD"]
    salida = {}
    bandas = [b for b in BANDAS if b is None or b > piso]
    cortes = [piso] + bandas
    for lo, hi in zip(cortes[:-1], cortes[1:]):
        if eb.agotado:
            break
        descargar_rango(eb, q, base, lo, hi, salida)
    return salida


# ------------------------------------------------------- 3. triage por lotes
def triage_lote(marca, lote):
    lineas = [f"{i}. {it['titulo']} | US${it['total']:.0f} | {it.get('condicion') or '?'}" for i, it in enumerate(lote)]
    texto = r.CONTEXTO + (
        f"\n\nAbajo hay {len(lote)} publicaciones activas de {marca} en eBay EE. UU. "
        "(formato: n. titulo | precio total con envio | condicion eBay). Para cada una identifica mentalmente "
        "modelo, referencia, generacion, material y tamano probables, y estima lo que ese reloj concreto se vende "
        "realmente hoy. Devuelve SOLO las que esten al menos 15 % por debajo de ese precio justo y no parezcan "
        "pieza, accesorio, replica, lote, reloj de otra marca ni listado enganoso.\n\n"
        + "\n".join(lineas) +
        "\n\nResponde SOLO un JSON array (vacio [] si ninguna): "
        "[{\"i\": n, \"modelo\": \"...\", \"justo\": numero, \"nota\": \"max 12 palabras\"}]")
    body = json.dumps({"model": r.MODELO_COMPLETO, "max_tokens": 16000,
                       "messages": [{"role": "user", "content": texto}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "content-type": "application/json"})
    resp = r.claude_texto(req)
    if resp is None:
        return None
    m = re.search(r"\[.*\]", resp, flags=re.S)
    try:
        return json.loads(m.group()) if m else []
    except json.JSONDecodeError:
        return []


# ------------------------------------------------------- 4. finalistas
def evaluar_finalista(eb, f):
    det = eb.item(f["item_id"])
    if not det:
        f["motivo"] = "no se pudo abrir (cuota o retirada)"
        return f, None
    motivo, info = r.analizar(det, f["marca"])
    f["referencia"] = info.get("referencia") or f.get("referencia")
    if motivo:
        f["motivo"] = motivo
        return f, None
    vend = det.get("seller") or {}
    cand = {"fecha": r.ahora().isoformat(), "origen": "barrido", "marca": f["marca"], "titulo": f["titulo"],
            "precio": float(det["price"]["value"]), "envio": r.envio(det), "total": f["total"],
            "mercado": f.get("mercado"), "triage": f.get("triage"), "vendedor": vend.get("username"),
            "valoraciones": vend.get("feedbackScore"), "pct_positivo": vend.get("feedbackPercentage"),
            "url": f["url"], "item_id": f["item_id"], "info": info}
    cl = r.revision_claude(cand)
    cand["claude"] = cl
    f["claude"] = {k: (cl or {}).get(k) for k in ("veredicto", "modelo", "precio_justo_usd", "riesgo_autenticidad", "estado")}
    if not cl:
        f["motivo"] = "revision no disponible"
        return f, None
    if cl.get("veredicto") == "descartar":
        f["motivo"] = "Claude descarto: " + "; ".join(map(str, cl.get("razones", [])[:2]))
        return f, None
    try:
        justo = float(cl.get("precio_justo_usd") or 0)
    except (TypeError, ValueError):
        justo = 0
    if not justo:
        f["motivo"] = "sin precio justo"
        return f, None
    ref = (info.get("referencia") or "").upper().replace(" ", "")
    m = f.get("mercado") if f.get("mercado", {}).get("n") else None
    if not m and ref:
        m = r.comparables(eb, f["marca"], ref, info.get("condicion_id"), f["item_id"], {})
        cand["mercado"] = f["mercado"] = {k: m.get(k) for k in ("n", "p25", "mediana")}
    if m and m.get("n", 0) >= r.MIN_COMPARABLES and f["total"] > r.UMBRAL_ANCLA * m["p25"]:
        f["motivo"] = f"el mercado de su referencia no lo confirma ({round(100 * f['total'] / m['p25'])} % del cuartil bajo)"
        return f, None
    cand["ratio_justo"] = f["ratio_justo"] = round(f["total"] / justo, 3)
    if cand["ratio_justo"] > UMBRAL_JUSTO:
        f["motivo"] = f"precio normal segun revision ({round(cand['ratio_justo'] * 100)} % de {justo:.0f})"
        return f, None
    f["motivo"] = "ALERTA"
    return f, cand


def telegram_texto(texto):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    for i in range(0, len(texto), 3800):
        body = urllib.parse.urlencode({"chat_id": chat, "text": texto[i:i + 3800], "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, timeout=30).read()
        except Exception as e:
            print(f"Telegram fallo: {e}", file=sys.stderr)
        time.sleep(1.2)


def cuota_num(eb):
    """(restantes, segundos_hasta_reinicio) o (None, None)."""
    try:
        url = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/?api_context=buy&api_name=browse"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {eb.tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.load(resp)
        for rl in d.get("rateLimits", []):
            for res in rl.get("resources", []):
                rate = (res.get("rates") or [{}])[0]
                if rate.get("remaining") is not None:
                    reset = dt.datetime.fromisoformat(rate["reset"].replace("Z", "+00:00"))
                    return int(rate["remaining"]), (reset - r.ahora()).total_seconds()
    except Exception as e:
        print(f"cuota_num: {e}", file=sys.stderr)
    return None, None


def cuota(eb):
    try:
        url = "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/?api_context=buy&api_name=browse"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {eb.tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.load(resp)
        for rl in d.get("rateLimits", []):
            for res in rl.get("resources", []):
                if res.get("name", "").endswith("item_summary.search") or res.get("name") == "buy.browse":
                    rate = (res.get("rates") or [{}])[0]
                    return f"{res.get('name')}: quedan {rate.get('remaining')} de {rate.get('limit')} (reinicia {rate.get('reset')})"
        return json.dumps(d)[:300]
    except Exception as e:
        return f"no disponible ({e})"


# ------------------------------------------------------------------ main
def main():
    eb = r.Ebay()
    log("Cuota eBay al inicio:", cuota(eb))
    restantes, hasta = cuota_num(eb)
    minimo = int(os.environ.get("CUOTA_MINIMA", "1800"))
    if restantes is not None and restantes < minimo and hasta and 0 < hasta < 3 * 3600:
        log(f"Quedan {restantes} llamadas; espero el reinicio de cuota en {hasta / 60:.0f} min.")
        time.sleep(hasta + 120)
        eb = r.Ebay()
        log("Cuota eBay tras reinicio:", cuota(eb))
    global T0
    T0 = time.time()
    with open(r.ROOT / "marcas.csv", newline="", encoding="utf-8") as fh:
        marcas = list(csv.DictReader(fh))
    ya_alertados = {a["item_id"] for a in r.cargar("alertas.json", []) + r.cargar("barrido_alertas.json", [])}

    # 1. descarga
    todos, por_marca = {}, {}
    for fila in marcas:
        if eb.agotado:
            break
        crudo = descargar_marca(eb, fila)
        n_ok = 0
        piso = float(fila["piso_usd"])
        for iid, it in crudo.items():
            if iid in todos:
                continue
            total = r.precio_total(it)
            titulo = it.get("title", "")
            if total is None or total < piso or not r.titulo_ok(titulo, fila["marca"]):
                continue
            todos[iid] = {"item_id": iid, "marca": fila["marca"], "titulo": titulo, "total": round(total, 2),
                          "condicion": it.get("condition"), "condicion_id": it.get("conditionId"),
                          "url": (it.get("itemWebUrl") or "").split("?")[0],
                          "vendedor": (it.get("seller") or {}).get("username"),
                          "valoraciones": (it.get("seller") or {}).get("feedbackScore"),
                          "ref": r.extraer_ref(fila["marca"], [titulo])}
            n_ok += 1
        por_marca[fila["marca"]] = {"descargados": len(crudo), "relojes": n_ok}
        log(f"{fila['marca']}: {len(crudo)} descargados, {n_ok} relojes validos · llamadas {eb.llamadas}")
    log(f"Total relojes: {len(todos)} · llamadas eBay {eb.llamadas} · cuota: {cuota(eb)}")

    # 2. comparables locales
    grupos = {}
    for it in todos.values():
        if it["ref"]:
            nuevo = str(it["condicion_id"] or "") in ("1000", "1500", "1750")
            it["clave"] = f"{it['marca']}|{r.ref_base(it['marca'], it['ref'])}|{'nuevo' if nuevo else 'usado'}"
            grupos.setdefault(it["clave"], []).append(it["total"])
    hoy = dt.date.today().isoformat()
    mercado = {}
    for k, precios in grupos.items():
        if len(precios) >= r.MIN_COMPARABLES:
            med, p25, p75 = r.mediana_recortada(precios)
            mercado[k] = {"fecha": hoy, "consulta": k, "n": len(precios), "mediana": round(med),
                          "p25": round(p25), "p75": round(p75), "min": round(min(precios)), "max": round(max(precios))}
    r.guardar("mercado_barrido.json", mercado)
    log(f"Referencias con >= {r.MIN_COMPARABLES} comparables: {len(mercado)}")

    finalistas, para_triage, stats = [], {}, {"sospechoso": 0, "precio_normal": 0}
    for it in todos.values():
        m = mercado.get(it.get("clave"))
        if m:
            ratio = it["total"] / m["p25"]
            it["mercado"] = {k: m[k] for k in ("n", "p25", "mediana")}
            if ratio < r.UMBRAL_SOSPECHA:
                stats["sospechoso"] += 1
            elif ratio <= UMBRAL_MERCADO:
                it["score"] = ratio
                it["via"] = "referencia"
                finalistas.append(it)
            else:
                stats["precio_normal"] += 1
        else:
            para_triage.setdefault(it["marca"], []).append(it)
    n_triage = sum(len(v) for v in para_triage.values())
    log(f"Por referencia: {len(finalistas)} finalistas, {stats['precio_normal']} precio normal, "
        f"{stats['sospechoso']} sospechosos · a triage con Claude: {n_triage}")

    # 3. triage por lotes
    lotes = [(marca, lst[i:i + LOTE]) for marca, lst in para_triage.items() for i in range(0, len(lst), LOTE)]
    hechos, fallidos, marcados = 0, 0, 0
    with cf.ThreadPoolExecutor(HILOS_CLAUDE) as ex:
        futuros = {}
        for marca, lote in lotes:
            futuros[ex.submit(triage_lote, marca, lote)] = (marca, lote)
        for fut in cf.as_completed(futuros):
            marca, lote = futuros[fut]
            res = fut.result()
            hechos += 1
            if res is None:
                fallidos += 1
                continue
            for x in res:
                try:
                    it = lote[int(x["i"])]
                    justo = float(x.get("justo") or 0)
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                if justo and it["total"] <= UMBRAL_JUSTO * justo:
                    it["score"] = it["total"] / justo
                    it["via"] = "triage"
                    it["triage"] = {"modelo": x.get("modelo"), "precio_justo_usd": justo, "nota": x.get("nota")}
                    with lock:
                        finalistas.append(it)
                        marcados += 1
            if hechos % 50 == 0:
                log(f"Triage {hechos}/{len(lotes)} lotes · marcados {marcados} · fallidos {fallidos}")
            if time.time() - T0 > LIMITE_TRIAGE_SEG:
                log("Limite de tiempo del triage alcanzado; se pasa a finalistas con lo hecho.")
                for f2 in futuros:
                    f2.cancel()
                break
    log(f"Triage terminado: {hechos}/{len(lotes)} lotes, {marcados} marcados, {fallidos} fallidos")

    # 4. finalistas
    finalistas = [f for f in finalistas if f["item_id"] not in ya_alertados]
    finalistas.sort(key=lambda f: f["score"])
    elegidos = finalistas[:FINALISTAS_MAX]
    log(f"Finalistas: {len(finalistas)} (se revisan {len(elegidos)})")
    evaluados, alertas = [], []
    with cf.ThreadPoolExecutor(HILOS_CLAUDE) as ex:
        futuros = [ex.submit(evaluar_finalista, eb, f) for f in elegidos]
        for i, fut in enumerate(cf.as_completed(futuros), 1):
            f, cand = fut.result()
            evaluados.append(f)
            if cand:
                alertas.append(cand)
            if i % 25 == 0:
                log(f"Revisados {i}/{len(elegidos)} · alertas {len(alertas)} · llamadas eBay {eb.llamadas}")
            if time.time() - T0 > LIMITE_TOTAL_SEG:
                log("Limite de tiempo total alcanzado.")
                for f2 in futuros:
                    f2.cancel()
                break
    alertas.sort(key=lambda c: c.get("ratio_justo") or 1)
    motivos = {}
    for f in evaluados:
        k = (f.get("motivo") or "?").split(":")[0].split(" (")[0]
        motivos[k] = motivos.get(k, 0) + 1
    log(f"Alertas: {len(alertas)} · motivos: {motivos} · cuota: {cuota(eb)}")

    # 5. avisos y guardado
    resumen = (f"<b>Barrido completo eBay EE. UU. — {dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4):%d %b %H:%M}</b>\n"
               f"Relojes activos revisados: <b>{len(todos):,}</b> de 10 marcas\n"
               + "\n".join(f"· {m}: {v['relojes']:,}" for m, v in por_marca.items()) +
               f"\nReferencias con comparables: {len(mercado):,}\n"
               f"Finalistas abiertos y revisados con fotos: {len(evaluados)}\n"
               f"<b>Oportunidades: {len(alertas)}</b> (van a continuación, de la mas barata a la menos)")
    telegram_texto(resumen)
    con_foto, resto = alertas[:60], alertas[60:]
    r.avisar_telegram(con_foto)
    if resto:
        telegram_texto("<b>Mas oportunidades (sin foto)</b>\n" + "\n".join(
            f"{round(c['ratio_justo'] * 100)} % · {c['marca']} · US${c['total']:,.0f} vs {float(c['claude']['precio_justo_usd']):,.0f} · "
            f"{c['titulo'][:60]}\n{c['url']}" for c in resto))
    for c in alertas:
        c["info"] = {k: v for k, v in c["info"].items() if k != "descripcion"}
    r.guardar("barrido_alertas.json", r.cargar("barrido_alertas.json", []) + alertas)
    r.guardar("barrido_reporte.json", {
        "fecha": r.ahora().isoformat(), "por_marca": por_marca, "relojes": len(todos),
        "referencias_con_comparables": len(mercado), "por_referencia": stats,
        "triage": {"lotes": len(lotes), "hechos": hechos, "fallidos": fallidos, "marcados": marcados},
        "finalistas": len(finalistas), "revisados": len(evaluados), "motivos": motivos,
        "llamadas_ebay": eb.llamadas, "cuota_agotada": eb.agotado,
        "alertas": [{k: c.get(k) for k in ("marca", "titulo", "total", "ratio_justo", "url", "vendedor")} |
                    {"claude": c.get("claude")} for c in alertas],
        "evaluados": evaluados,
        "no_revisados": [{k: f.get(k) for k in ("marca", "titulo", "total", "score", "via", "url", "triage", "mercado")}
                         for f in finalistas[FINALISTAS_MAX:]],
    })
    log("Listo.")


if __name__ == "__main__":
    main()
