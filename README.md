# bolt-fleet-dashboard

Dashboard de **Supply Fleet · España** (Rides). Muestra online hours, drivers
activos, hours/driver, EPH y GMV de las flotas, con vistas Strategic vs Non
strategic y por Cohort, además de un widget Taxi vs VTC.

Web publicada: https://barbaracabanillas.github.io/bolt-fleet-dashboard/

## Cómo funciona

Generador estático que se ejecuta cada noche (GitHub Actions):

```
Google Sheet (grouping) ─┐
Databricks (métricas)   ─┼──►  gen_dashboard.py  ──►  docs/index.html  ──►  GitHub Pages
dashboard_template.html ─┘        (Python + pandas)      (datos inyectados)
```

- **`dashboard_template.html`** — plantilla visual (gráficos Chart.js, tablas, estilos).
- **`gen_dashboard.py`** — consulta Databricks (toda España, `country_id = 67`),
  clasifica compañías y cohorts, y genera `docs/index.html`.
- **`fo_groups.csv`** — export de la pestaña "Cohorts by Grouping" del Sheet
  `Admin_Madrid` (Company, Company ID, FO, Fleet Type, Cohort). Fuente de verdad
  del grouping / fleet type / cohort de las compañías **strategic**.
- **`.github/workflows/generate_dashboard.yml`** — build nocturno (5:00 UTC) + Pages.

## Cohorts (columna derecha "Cohort")

- **Strategic - Fleet Agreement / Branded / Locked / No agreement** — vienen del Sheet
  (Fleet Type = Strategic + su Cohort).
- **Free floating - Branded / Not branded** — compañías que NO están en el Sheet como
  strategic; "Branded" si tienen algún coche vinilado según Databricks
  (`main.core_models.fact_car_branding_periods`), si no "Not branded".

## Fuentes de datos (Databricks)

- `main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local` —
  online hours, earnings, GMV, drivers activos, categoría (taxi/VTC) por coche-hora.
- `main.core_models.fact_car_branding_periods` — periodos de vinilado por coche.

## Ejecutarlo en local

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt

export DATABRICKS_HOST="..."          # credenciales (no subir al repo)
export DATABRICKS_HTTP_PATH="..."
export DATABRICKS_TOKEN="..."

./.venv/bin/python gen_dashboard.py    # genera docs/index.html
```

Luego sírvelo: `python -m http.server 4173 --directory docs` y abre http://localhost:4173

### Variables opcionales

- **`DATA_CUTOFF`** (`YYYY-MM-DD`) — corta los datos en esa fecha. Si no se define,
  usa por defecto la **última semana completa** (domingo anterior), para no mostrar
  una semana a medias.
- **`SHEET_CSV_URL`** — si se define (una URL "Publicar en la web" CSV de la pestaña
  "Cohorts by Grouping"), el build refresca `fo_groups.csv` desde ahí automáticamente.
  Si no, usa el `fo_groups.csv` del repo (sync manual).

## Secretos (GitHub Actions)

`DATABRICKS_HOST`, `DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN` como GitHub Secrets.
(Opcional: `SHEET_CSV_URL` para el sync automático del grouping.)

### Workspace de Databricks (importante si el build falla con "Invalid access token" o "Invalid access to Org")

Estos 3 secrets deben apuntar a un SQL Warehouse de tu **workspace normal de Databricks**
(el "common workspace" al que tienes acceso por defecto) — **no** al workspace interno
`data` (id `1552831219997577`), que es exclusivo del equipo de Data Platform y al que no
deberíamos tener acceso (nos lo confirmaron en agosto 2026 tras un incidente: el pipeline
llevaba tiempo corriendo con acceso a ese workspace "por error").

Si el `DATABRICKS_TOKEN` caduca o el build falla con un error de acceso:
1. En tu workspace normal → **SQL Warehouses** → el warehouse que uses → pestaña
   **Connection details** → copia **Server hostname** y **HTTP path** →
   actualiza `DATABRICKS_HOST` y `DATABRICKS_HTTP_PATH` si han cambiado.
2. Genera un token nuevo desde ESE MISMO workspace (Settings → Developer → Access
   tokens) → actualiza `DATABRICKS_TOKEN`.
3. Relanza el workflow manualmente (Actions → 🔄 Generar Dashboard → Run workflow)
   para confirmar que funciona antes de esperar al cron.

Si tras esto el error cambia a uno de permisos/tabla no encontrada, pide a Data
Platform `SELECT` sobre estas 6 tablas (las únicas que toca `gen_dashboard.py`):

```
main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
main.core_models.dim_car
main.mart_models.mart_city_hour_local_rides
main.mart_models.mart_driver_city_hour_supply_spend_local
main.mart_models.mart_fleet_company_daily_history
main.stg_models.stg_car_branding_periods_car_branding_periods
```
