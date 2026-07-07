# bolt-fleet-dashboard

Dashboard de **Supply Fleet Madrid**: muestra horas online, earnings y GMV de las
compañías de flota de Madrid, clasificadas por cohorte (Fleet Agreement, Locked
Supply, Branding, No Agreement).

Web publicada: https://barbaracabanillas.github.io/bolt-fleet-dashboard/

## Cómo funciona

Es un dashboard estático que se genera automáticamente cada noche:

```
fo_groups.csv ──┐
Databricks   ───┼──►  gen_dashboard.py  ──►  docs/index.html  ──►  GitHub Pages
dashboard_template.html ─┘   (Python + pandas)     (web publicada)
```

- **`dashboard_template.html`** — la plantilla visual (gráficos, tablas, estilos).
  Tiene dos huecos que el script rellena con los datos:
  `/* __PRELOADED_DATA__ */` y `/* __EMBEDDED_AGREEMENTS__ */`.
- **`gen_dashboard.py`** — lee los CSV, consulta Databricks (ciudad Madrid,
  `city_id = 150`), clasifica cada compañía en su cohorte e inyecta los datos en
  la plantilla, generando `docs/index.html`.
- **`fo_groups.csv`** — clasificación de compañías (Company, Company ID, FO,
  Fleet Type, Cohort), exportada del Google Sheet "ES MAD Fleets cohort".
- **`.github/workflows/generate_dashboard.yml`** — ejecuta el script cada noche a
  las 5:00 UTC y lo publica en GitHub Pages. También se puede lanzar a mano.

## Ejecutarlo en local

Requisitos: Python 3.11 y acceso a Databricks.

```bash
pip install -r requirements.txt

# Credenciales de Databricks (no compartir ni subir al repo)
export DATABRICKS_HOST="..."
export DATABRICKS_HTTP_PATH="..."
export DATABRICKS_TOKEN="..."

python gen_dashboard.py   # genera docs/index.html
```

Después, abre `docs/index.html` en el navegador para ver el resultado.

## Fuentes de datos (Databricks)

- `main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local`
  — métricas horarias por coche (horas online, earnings, GMV).
- `main.mart_models.mart_fleet_company_daily_history` — snapshot de compañías.
