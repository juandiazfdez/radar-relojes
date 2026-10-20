# Radar de relojes (eBay API)

Cada mañana a las 6:17 a. m. (Santo Domingo) GitHub corre `radar_ebay.py`: busca en eBay los modelos de `guia_precios.csv`, solo listados de precio fijo ubicados en EE. UU., y guarda en `data/latest.json` los que estén a 65% o menos de tu precio ("oportunidad") o entre 65% y 80% ("otras"). El reporte de las 6:54 lee ese archivo.

## Lo que tienes que hacer tú (una sola vez, ~20 min)

1. **Cuenta de desarrollador de eBay**: entra a developer.ebay.com, regístrate con tu cuenta de eBay y crea una app (Application Keys → *Production*).
   - eBay te pedirá un endpoint para "Marketplace Account Deletion". Como el radar no guarda datos de usuarios, elige la opción de **exención** (*I do not persist eBay data*) y continúa.
   - Copia el **App ID (Client ID)** y el **Cert ID (Client Secret)** de producción.
2. **Repositorio privado en GitHub**: crea uno llamado `radar-relojes` (privado) y sube estos archivos tal como están (incluida la carpeta `.github`).
3. **Guarda las llaves como secretos**: en el repo, Settings → Secrets and variables → Actions → *New repository secret*:
   - `EBAY_CLIENT_ID` = tu App ID
   - `EBAY_CLIENT_SECRET` = tu Cert ID
4. **Prueba**: pestaña Actions → "Radar diario eBay" → *Run workflow*. En un minuto debe aparecer `data/latest.json`.

## Ajustar la guía de precios

Edita `guia_precios.csv` (se abre en Excel):
- `busqueda_ebay`: lo que se escribe en eBay.
- `debe_contener`: palabras que tienen que estar en el título (separadas por `|`).
- `excluir`: si el título tiene alguna de estas palabras, se descarta (`ladies`, `medium`, otras referencias…).
- `tu_precio_usd`: tu precio de venta. Las filas marcadas "verificar" son precios que salieron del radar y conviene confirmar.

Chrono24 no tiene API pública, así que sigue cubierto por la búsqueda web del reporte diario.
