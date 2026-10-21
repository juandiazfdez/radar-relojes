# Radar de relojes (eBay) – v2

Corre en GitHub Actions en un bucle que repite la búsqueda **cada 15 minutos** (un trabajo de ~6 h que se relanza solo; el cron horario es vigilante) y solo mira publicaciones nuevas (ubicadas en EE. UU.) de las
últimas 3 horas para los modelos de `guia_precios.csv`.

## Cómo decide si algo es oportunidad

1. **Título limpio**: descarta piezas, accesorios, réplicas, relojes de mujer.
2. **Abre la publicación** (`getItem`) y lee descripción, condición y
   especificaciones (departamento, referencia, tamaño de caja, año, caja/papeles).
   Descarta si dice "as is", "not working", "needs service", "for parts",
   "replica", "untested", "no returns", etc.; si la caja es menor que
   `caja_min_mm` o el año menor que `ano_min`; o si el vendedor tiene < 20
   valoraciones o < 97 % positivo.
3. **Compara con el mercado, no con tu precio**: busca los listados activos en
   EE. UU. de la **misma referencia** (o del mismo modelo si no hay referencia),
   misma condición (usado/nuevo). Como los precios pedidos en eBay están
   inflados, la referencia es el **cuartil bajo (p25)** de esos precios, no la
   mediana. Hace falta un mínimo de 5 comparables.
   - Pasa a la siguiente etapa si el precio total (con envío) es **≤ 80 % del p25**.
   - Si es **< 40 %** se marca como sospechoso (estafa o pieza) y no se avisa.
4. **Revisión con Claude** (secreto `ANTHROPIC_API_KEY`): manda título,
   especificaciones, descripción y hasta 4 fotos; Claude devuelve
   `alerta` / `dudoso` / `descartar`, razones, riesgo de autenticidad y un
   **precio justo** (lo que ese reloj se vende realmente). Es el filtro final:
   solo se avisa si el precio está **≤ 85 % del precio justo** y no fue descartado.
5. **Avisa** (si hay credenciales) por Telegram y/o correo, con foto y enlace.
   Nunca avisa dos veces del mismo artículo.

`tu_precio_usd` ya no decide nada: solo sirve como piso (25 %) para no abrir
piezas y para calcular el margen en el reporte.

## Archivos en `data/`

| archivo | qué es |
|---|---|
| `latest.json` | resumen de la última corrida: nuevos vistos, descartes por motivo, alertas de hoy, mercado |
| `alertas.json` | todas las alertas de los últimos 90 días |
| `revisados_ultimos.json` | últimos 400 artículos abiertos con el motivo de descarte (para afinar filtros) |
| `mercado.json` | mediana/p25/p75 por referencia, se renueva a diario |
| `vistos.json` | ids ya vistos (30 días) para no repetir |
| `ultimo_log.txt` | salida de la última corrida |

## Secretos (Settings → Secrets and variables → Actions)

| secreto | obligatorio | para qué |
|---|---|---|
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | sí | eBay Browse API |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no | aviso instantáneo al celular |
| `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `ALERTA_EMAIL` | no | aviso por correo (contraseña de aplicación de Google) |
| `ANTHROPIC_API_KEY` (y opcional `CLAUDE_MODEL`) | no | revisión con fotos |

## Cuota de eBay

El keyset nuevo tiene 5 000 llamadas/día. Cada corrida usa ~24 búsquedas +
1 llamada por artículo nuevo que pase el título + 1 búsqueda por referencia
nueva al día. Si se agota, el script se detiene limpio y lo anota en
`latest.json` (`cuota_agotada`).
