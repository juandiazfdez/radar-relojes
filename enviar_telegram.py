"""Envia por Telegram el contenido de un archivo HTML (mensajes separados por una linea '---').
Si un mensaje empieza con una linea FOTO:<url>, se manda como foto con el resto de texto como pie."""
import os, sys, urllib.parse, urllib.request
tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
partes = open(sys.argv[1], encoding="utf-8").read().split("\n---\n")
for p in partes:
    p = p.strip()
    if p.startswith("FOTO:"):
        foto, texto = p.split("\n", 1)
        body = urllib.parse.urlencode({"chat_id": chat, "photo": foto[5:].strip(), "caption": texto.strip(),
                                       "parse_mode": "HTML"}).encode()
        metodo = "sendPhoto"
    else:
        body = urllib.parse.urlencode({"chat_id": chat, "text": p, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        metodo = "sendMessage"
    r = urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/{metodo}", data=body, timeout=30).read()
    print(r[:80])
