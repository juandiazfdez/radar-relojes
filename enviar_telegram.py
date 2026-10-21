"""Envia por Telegram el contenido de un archivo HTML (uno o varios mensajes separados por una linea '---')."""
import os, sys, urllib.parse, urllib.request
tok, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
partes = open(sys.argv[1], encoding="utf-8").read().split("\n---\n")
for p in partes:
    body = urllib.parse.urlencode({"chat_id": chat, "text": p.strip(), "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    r = urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, timeout=30).read()
    print(r[:80])
