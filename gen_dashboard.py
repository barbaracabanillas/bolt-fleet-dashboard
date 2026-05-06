"""
gen_dashboard.py
────────────────
Pulls data from Databricks SQL and generates docs/index.html.
Run nightly by GitHub Actions; output is published on GitHub Pages.

Tables used (all in main.mart_models):
  - mart_fleet_company_daily_history          → T1 fleet performance (weekly)
  - mart_driver_car_city_hour_earnings_and_fees_eur_local → heatmap + T2 cars

Env vars (stored as GitHub Secrets):
  DATABRICKS_HOST       e.g. adb-1234567890.12.azuredatabricks.net
  DATABRICKS_HTTP_PATH  e.g. /sql/1.0/warehouses/abc123
  DATABRICKS_TOKEN      personal access token or service-principal secret
"""

import os, json, datetime, pathlib
from databricks import sql
import pandas as pd

# ── Connection ────────────────────────────────────────────────────────────────
HOST      = os.environ["DATABRICKS_HOST"]
HTTP_PATH = os.environ["DATABRICKS_HTTP_PATH"]
TOKEN     = os.environ["DATABRICKS_TOKEN"]

MADRID_CITY_ID      = 150
LOOKBACK_DAYS_PERF  = 730   # 2 years — needed for Year-over-Year tab
LOOKBACK_DAYS_HOURLY = 90   # 90 days — hourly data is much denser

def run_query(query: str) -> pd.DataFrame:
    with sql.connect(
        server_hostname=HOST,
        http_path=HTTP_PATH,
        access_token=TOKEN,
        _socket_timeout=600,       # 10 min socket timeout
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


# ── Query 1 — Weekly fleet performance per company (T1) ──────────────────────
# Source: mart_fleet_company_daily_history (one row per company per day)
# Each day has individual metrics (fleet_online_hours = that day's hours),
# so we can SUM daily rows to get weekly totals.
def fetch_fleet_performance() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            DATE_TRUNC('week', calendar_date)                       AS week_date,
            company_id,
            company_city_id,
            is_actual_fleet_company,
            invoicing_strategy_corrected                             AS invoicing_strategy,
            SUM(fleet_online_hours)                                  AS online_hours,
            SUM(fleet_rides_earnings_before_discounts_eur)           AS earnings_eur,
            SUM(fleet_count_finished_rides)                          AS finished_rides,
            AVG(fleet_count_active_drivers)                          AS avg_active_drivers,
            AVG(fleet_count_active_vehicles)                         AS avg_active_vehicles,
            MAX(fleet_rank_91d_earnings_per_hour)                    AS eph_rank
        FROM main.mart_models.mart_fleet_company_daily_history
        WHERE company_city_id = {MADRID_CITY_ID}
          AND is_fleet_company = true
          AND calendar_date   >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_PERF} DAYS
        GROUP BY 1, 2, 3, 4, 5
        HAVING SUM(fleet_online_hours) > 0
        ORDER BY week_date DESC, earnings_eur DESC
    """)


# ── Query 2 — Hourly activity per car+company (heatmap + T2) ─────────────────
# Source: mart_driver_car_city_hour_earnings_and_fees_eur_local
# One row = one hour a car was active in a city.
# Grouping by car + calendar_date + hour gives:
#   online_hours  = COUNT(DISTINCT date_hour_ts_local) per car (1 hour per row)
#   finished_orders = SUM(driver_reportable_activities)
#   earnings        = SUM(driver_total_earnings_with_vat)
#   EPH             = earnings / online_hours
def fetch_hourly_car_data() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            calendar_date_local                                           AS date,
            CAST(DATE_FORMAT(date_hour_ts_local, 'HH') AS INT)            AS hour_of_day,
            DAYOFWEEK(date_hour_ts_local)                                 AS day_of_week_spark,
            company_id,
            car_id,
            search_category_id,
            search_category_name,
            COUNT(DISTINCT date_hour_ts_local)                            AS online_hours,
            SUM(driver_reportable_activities)                             AS finished_orders,
            SUM(driver_total_earnings_with_vat)                           AS earnings_eur,
            SUM(gmv_before_discounts_with_vat)                            AS gmv_eur
        FROM main.mart_models.mart_driver_car_city_hour_earnings_and_fees_eur_local
        WHERE city_name = 'Madrid'
          AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_HOURLY} DAYS
        GROUP BY 1, 2, 3, 4, 5, 6, 7
        ORDER BY date DESC, hour_of_day
    """)


# ── Query 3 — Company snapshot (for company metadata + names if available) ───
# TODO: company_name is not yet found in mart_models. When the source table
# is identified, add a JOIN here. For now we expose company_id + city filters.
def fetch_company_snapshot() -> pd.DataFrame:
    return run_query(f"""
        SELECT
            company_id,
            company_city_id,
            invoicing_strategy_corrected   AS invoicing_strategy,
            company_drivers_type_corrected AS drivers_type,
            is_actual_fleet_company,
            fleet_is_active,
            number_of_drivers,
            fleet_size,
            fleet_rank_91d_earnings_per_hour AS eph_rank_91d,
            fleet_online_hours_91d,
            fleet_rides_earnings_before_discounts_eur_91d AS earnings_91d,
            fleet_count_finished_rides_91d,
            fleet_count_active_vehicles_91d
        FROM main.mart_models.mart_fleet_company_latest_snapshot
        WHERE company_city_id = {MADRID_CITY_ID}
          AND is_fleet_company    = true
          AND fleet_is_active     = true
          AND is_actual_fleet_company = true
          AND is_latest_snapshot  = true
        ORDER BY eph_rank_91d ASC
    """)


# ── Helpers ───────────────────────────────────────────────────────────────────
def df_to_records(df: pd.DataFrame) -> list:
    """DataFrame → JSON-safe list of dicts (dates as ISO strings)."""
    return json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))


def generate_html(data: dict) -> str:
    template_path = pathlib.Path(__file__).parent / "dashboard_template.html"
    html = template_path.read_text(encoding="utf-8")
    data_json = json.dumps(data, ensure_ascii=False, default=str)
    return html.replace("/* __PRELOADED_DATA__ */", f"window.PRELOADED_DATA = {data_json};")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    generated_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{generated_at}] Fetching data from Databricks…")

    results = {}

    for name, fetch_fn in [
        ("fleet_performance", fetch_fleet_performance),
        ("hourly_car_data",   fetch_hourly_car_data),
        ("company_snapshot",  fetch_company_snapshot),
    ]:
        try:
            df = fetch_fn()
            results[name] = df_to_records(df)
            print(f"  ✓ {name}: {len(df):,} rows")
        except Exception as e:
            print(f"  ✗ {name} failed: {e}")
            results[name] = []

    data = {"generated_at": generated_at, **results}

    html = generate_html(data)

    out_dir = pathlib.Path("docs")
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"  ✓ Dashboard → {out_path}  ({len(html)//1024} KB)")
    print("Done.")


if __name__ == "__main__":
    main()
