"""Recalibra las alertas del ultimo barrido contra el mercado real de su referencia.

Para cada alerta de data/barrido_alertas.json con referencia: busca en eBay EE. UU. los
listados activos de esa referencia y condicion y calcula el cuartil bajo. Clasifica:
  CONFIRMADA   total <= UMBRAL_ANCLA * p25 (el mercado lo respalda)
  NORMAL       total >  UMBRAL_ANCLA * p25 (precio normal para esa referencia)
  SIN_DATOS    sin referencia o < 5 comparables (solo hay estimacion de Claude)
Manda por Telegram la lista depurada y guarda data/barrido_calibrado.json.
"""
import json
import os
import urllib.parse
import urllib.request
import time

import radar_ebay as r


def telegram(texto):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print(texto)
        return
    for i in range(0, len(texto), 3800):
        body = urllib.parse.urlencode({"chat_id": chat, "text": texto[i:i + 3800], "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, timeout=30).read()
        time.sleep(1.2)


def main():
    eb = r.Ebay()
    alertas = r.cargar("barrido_alertas.json", [])
    cache = {}
    salida = {"CONFIRMADA": [], "NORMAL": [], "SIN_DATOS": []}
    for c in alertas:
        ref = (c["info"].get("referencia") or "").upper().replace(" ", "")
        fila = {k: c.get(k) for k in ("marca", "titulo", "total", "url", "vendedor", "ratio_justo")}
        fila["claude"] = {k: (c.get("claude") or {}).get(k) for k in ("modelo", "veredicto", "riesgo_autenticidad", "precio_justo_usd", "estado")}
        fila["referencia"] = ref or None
        m = None
        if ref and len(ref) >= 4:
            m = r.comparables(eb, c["marca"], ref, c["info"].get("condicion_id"), c["item_id"], cache)
        if m and m.get("n", 0) >= r.MIN_COMPARABLES:
            fila["mercado"] = {k: m.get(k) for k in ("n", "p25", "mediana")}
            fila["ratio_p25"] = round(c["total"] / m["p25"], 3)
            salida["CONFIRMADA" if fila["ratio_p25"] <= r.UMBRAL_ANCLA else "NORMAL"].append(fila)
        else:
            if m:
                fila["mercado"] = {k: m.get(k) for k in ("n", "p25", "mediana")}
            salida["SIN_DATOS"].append(fila)
    for k in salida:
        salida[k].sort(key=lambda f: f.get("ratio_p25") or f.get("ratio_justo") or 9)
    r.guardar("barrido_calibrado.json", salida)
    print({k: len(v) for k, v in salida.items()}, "llamadas", eb.llamadas)

    def linea(f):
        cl = f["claude"]
        base = f"<b>US${f['total']:,.0f}</b> · {f['marca']} {cl.get('modelo') or ''}"[:140]
        extra = (f"\n   {round(f['ratio_p25'] * 100)} % del cuartil bajo de {f['referencia']} "
                 f"(n={f['mercado']['n']}, p25 US${f['mercado']['p25']:,}, mediana US${f['mercado']['mediana']:,})"
                 if f.get("ratio_p25") else
                 f"\n   estimado por Claude: justo US${float(cl.get('precio_justo_usd') or 0):,.0f} ({round((f.get('ratio_justo') or 0) * 100)} %) · sin comparables")
        return f"{base}{extra} · riesgo {cl.get('riesgo_autenticidad')}\n   {f['url']}"

    conf, normal, sin = salida["CONFIRMADA"], salida["NORMAL"], salida["SIN_DATOS"]
    telegram(
        "<b>Corrección del barrido</b>\n"
        f"Revisé las {len(alertas)} alertas contra los listados reales de su misma referencia. "
        f"Muchas eran Breitling/IWC/Panerai nuevos a precio normal de mercado gris (Claude comparaba contra precio de tienda).\n\n"
        f"✅ Confirmadas por mercado: <b>{len(conf)}</b>\n"
        f"❌ Precio normal para su referencia (ignóralas): {len(normal)}\n"
        f"⚠️ Sin comparables suficientes, solo estimación: {len(sin)}")
    if conf:
        telegram("<b>✅ CONFIRMADAS por el mercado de su referencia</b>\n\n" + "\n\n".join(linea(f) for f in conf))
    prometedoras = [f for f in sin if (f.get("ratio_justo") or 1) <= 0.75 and f["claude"].get("riesgo_autenticidad") == "bajo"]
    if prometedoras:
        telegram("<b>⚠️ Sin comparables, pero Claude las ve ≤75 % y riesgo bajo</b> (verificar a mano)\n\n"
                 + "\n\n".join(linea(f) for f in prometedoras))


if __name__ == "__main__":
    main()
