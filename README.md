# Radar de relojes (eBay) – v3, por marca

Corre en GitHub Actions en un bucle que repite la búsqueda **cada 15 minutos**
(un trabajo de ~6 h que se relanza solo; el cron horario es vigilante) y mira
las publicaciones **nuevas** en EE. UU. de las tres marcas de `marcas.csv`:
**Rolex, Cartier y Tudor**.
No hay lista de modelos ni filtros de género, tamaño o año, y **no hay mínimo de
precio** (`piso_usd` = 0; solo se aplica un filtro de precio si se pone un piso > 0):
cualquier modelo de esas marcas, también piezas de US$600–1,000, entra si el precio
es de oportunidad para su referencia, año y estado.

## Cómo decide

1. **Título**: descarta solo piezas, correas, accesorios, réplicas y lotes.
2. **Triage barato** (para no gastar cuota ni dinero en todo):
   - Si el título trae la **referencia exacta** (16233, W2SA0046, 79230B…),
     busca los listados activos de esa misma referencia y condición y compara
     con su cuartil bajo (los precios pedidos de eBay están inflados). Pasa si
     está ≤ 85 % del cuartil bajo. Si está < 35 % se marca sospechoso.
   - Si no hay referencia (o hay < 5 comparables), **Claude rápido** mira título,
     precio, condición y miniatura, identifica el modelo/generación y estima el
     precio justo. Pasa si está ≤ 85 % de ese precio justo.
3. **Finalistas**: abre la publicación (descripción, especificaciones, 4 fotos,
   vendedor). Descarta "as is", "not working", "needs service" y réplicas.
   **No hay filtro de vendedor**: sus valoraciones y % positivo solo se muestran
   en la alerta como información.
4. **Revisión completa con Claude** (fotos + texto): veredicto, estado, riesgo de
   autenticidad, **precio justo** del reloj concreto y precio de reventa en RD.
   Alerta solo si el precio ≤ 85 % del precio justo y no fue descartado.
5. **Telegram** con foto, precio, precio justo, comparables, vendedor y enlace.
   Nunca avisa dos veces del mismo artículo.

## Archivos en `data/`

| archivo | qué es |
|---|---|
| `latest.json` | última corrida: nuevos vistos, descartes por motivo, alertas de hoy |
| `alertas.json` | alertas de los últimos 90 días |
| `revisados_ultimos.json` | últimos 600 artículos evaluados con su motivo (para afinar) |
| `mercado.json` | comparables por referencia (caché diario) |
| `vistos.json` | ids ya vistos (30 días) |
| `ultimo_log.txt` | salida de la última corrida |

## Secretos

`EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Flujos manuales

- **Radar manual**: `prueba_aviso` (reenvía la última alerta) o `barrido` (revisa todo lo activo).
- **Consulta puntual eBay**: lista todo lo activo de una búsqueda (p. ej. "bvlgari serpenti").
- **Enviar mensaje Telegram**: manda un archivo de `mensajes/` al chat.

## Cuota de eBay

5 000 llamadas/día. Cada pasada usa ~10–20 búsquedas + 1 consulta por referencia
nueva del día + 1 apertura por finalista. Si se agota, el script se detiene limpio
(`cuota_agotada` en `latest.json`) y sigue en la siguiente pasada.
