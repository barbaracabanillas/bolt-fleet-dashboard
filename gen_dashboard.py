"""
gen_dashboard.py  –  Supply Fleet Madrid Dashboard Builder
===========================================================
Runs nightly via GitHub Actions.
Reads cohort classifications from cohorts.csv (exported from Google Sheet
"ES MAD Fleets cohort") and car-level online-hours from Databricks, then
injects the result into dashboard_template.html → index.html (GitHub Pages).

CLASSIFICATION RULE (per car):
  - If the company is Fleet Agreement or Locked Supply  →  Google Sheet wins
  - Otherwise (No Agreement / Branding companies)       →  car decides:
        car has search_category_name = 'Branding'  →  Branding
        otherwise                                  →  No Agreement
"""

import os
import json
import csv
import io
import datetime
from pathlib import Path

import pandas as pd
from databricks import sql as databricks_sql

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

MADRID_CITY_ID       = 150
LOOKBACK_DAYS_WEEKLY = 365   # how many days back to fetch car-level weekly data
LOOKBACK_DAYS_M30    = 30    # for M30 section
COHORTS_CSV          = "cohorts.csv"         # path relative to repo root
FO_GROUPS_CSV        = "fo_groups.csv"       # company_id → FO group name

VALID_COHORTS   = ["Fleet Agreement", "Locked Supply", "Branding", "No Agreement"]
FIXED_COHORTS   = {"Fleet Agreement", "Locked Supply"}   # Google Sheet always wins


# ──────────────────────────────────────────────────────────────────────────────
# DATABRICKS CONNECTION
# ──────────────────────────────────────────────────────────────────────────────

def get_connection():
    """Return a Databricks SQL connection using GitHub Secrets (env vars)."""
    return databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def run_query(sql: str) -> pd.DataFrame:
    """Execute a SQL query and return results as a DataFrame."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            result = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
    return pd.DataFrame(result, columns=columns)


# ──────────────────────────────────────────────────────────────────────────────
# FO GROUP MAP  (from fo_groups.csv = Admin Madrid sheet)
# ──────────────────────────────────────────────────────────────────────────────

def load_fo_group_map(csv_path: str = FO_GROUPS_CSV) -> dict:
    """
    Read fo_groups.csv and return a dict:
        { "company_id_str": "FO group name" }

    Expected columns: Company, Company ID, FO, Fleet Type, Cohort
    """
    fo_map = {}
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cid = str(row.get("Company ID", "")).strip()
                fo  = str(row.get("FO", "")).strip()
                if cid and fo:
                    fo_map[cid] = fo
        print(f"[fo_groups] Loaded {len(fo_map)} company→FO mappings from {csv_path}")
    except FileNotFoundError:
        print(f"[fo_groups] ⚠️  {csv_path} not found — FO groups will be empty")
    return fo_map


# ──────────────────────────────────────────────────────────────────────────────
# COHORT MAP  (from cohorts.csv = Google Sheet export)
# ──────────────────────────────────────────────────────────────────────────────

def load_cohort_map(csv_path: str = COHORTS_CSV) -> dict:
    """
    Read the exported Google Sheet CSV and return a dict:
        { "company_id_str": {"name": ..., "fleet_type": ..., "cohort": ..., "grouping": ...} }

    The CSV must have these columns (exact names):
        Company ID | Company name | Grouping | Strategic | No Agreement

    'Strategic' stores fleet type  → e.g. "Free-Floating" / "Strategic"
    'No Agreement' column stores the cohort label  → "Fleet Agreement" /
        "Locked Supply" / "Branding" / "No Agreement"

    If the file has a different structure (multi-market tab), the function
    falls back to looking for columns: company_id, fleet_type, cohort.
    """
    cohort_map = {}

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # ── Detect which header format we have ──────────────────────────────
        if "Company ID" in headers:
            # Madrid tab format
            id_col       = "Company ID"
            name_col     = "Company name"
            grouping_col = "Grouping"
            type_col     = "Strategic"      # fleet type stored here
            cohort_col   = "No Agreement"   # cohort label stored here
        elif "company_id" in headers:
            # Multi-market tab format
            id_col       = "company_id"
            name_col     = "company_name"
            grouping_col = "grouping"
            type_col     = "fleet_type"
            cohort_col   = "cohort"
        else:
            raise ValueError(
                f"Unrecognised CSV columns: {headers}\n"
                "Expected 'Company ID' or 'company_id' column."
            )

        for row in reader:
            raw_id = row.get(id_col, "").strip()
            if not raw_id:
                continue
            cohort_raw = row.get(cohort_col, "").strip()
            # Normalise cohort label (handle minor typos / case differences)
            cohort = _normalise_cohort(cohort_raw)
            cohort_map[raw_id] = {
                "name":      row.get(name_col, "").strip(),
                "grouping":  row.get(grouping_col, "").strip(),
                "fleet_type": row.get(type_col, "Free-Floating").strip(),
                "cohort":    cohort,
            }

    print(f"[cohort_map] Loaded {len(cohort_map)} companies from {csv_path}")
    return cohort_map


def _normalise_cohort(raw: str) -> str:
    """Map common variants to our four canonical cohort names."""
    mapping = {
        "fleet agreement": "Fleet Agreement",
        "locked supply":   "Locked Supply",
        "locked":          "Locked Supply",
        "branding":        "Branding",
        "no agreement":    "No Agreement",
        "":                "No Agreement",
    }
    return mapping.get(raw.lower(), raw)  # fall back to raw if unknown


# ──────────────────────────────────────────────────────────────────────────────
# CLASSIFICATION RULE
# ──────────────────────────────────────────────────────────────────────────────

def classify_car(company_id: str, is_branding_car: bool, cohort_map: dict) -> tuple:
    """
    Return (cohort, fleet_type) for a given car.

    Rules:
      1. If company is Fleet Agreement or Locked Supply  →  always keep that cohort
      2. Otherwise (Branding / No Agreement company, or unknown company):
           - car has branding vinyl (is_branding_car=True)  →  "Branding"
           - car has no vinyl                               →  "No Agreement"
    """
    info = cohort_map.get(str(company_id), {
        "fleet_type": "Free-Floating",
        "cohort":     "No Agreement",
        "name":       "",
        "grouping":   "",
    })
    cohort     = info["cohort"]
    fleet_type = info["fleet_type"]

    if cohort in FIXED_COHORTS:
        # Google Sheet wins – fleet agreement and locked supply are never overridden
        return cohort, fleet_type

    # For Branding / No Agreement companies: let the car decide
    if is_branding_car:
        return "Branding", fleet_type
    else:
        return "No Agreement", fleet_type


# ──────────────────────────────────────────────────────────────────────────────
# DATABRICKS QUERIES
# ──────────────────────────────────────────────────────────────────────────────

def fetch_car_weekly_data() -> pd.DataFrame:
    """
    Fetch weekly online-hours per car from Databricks (car-level).
    Also flags each car as branding if search_category_name = 'Branding'
    for any hour in that week.

    Cars without company_id (null) are assigned company_id = -1 so their
    hours are not lost — they will be classified as Free-Floating / No Agreement.

    Returns columns:
        week_start, company_id, car_id, is_branding_car,
        online_hours, earnings_eur, gmv_eur
    """
    sql = f"""
    SELECT
        DATE_TRUNC('week', calendar_date_local)      AS week_start,
        COALESCE(company_id, -1)                      AS company_id,
        car_id,
        MAX(CASE
            WHEN LOWER(search_category_name) = 'branding' THEN 1
            ELSE 0
        END)                                          AS is_branding_car,
        COUNT(DISTINCT date_hour_ts_local)                        AS online_hours,
        SUM(rides_driver_total_earnings_with_vat_eur_local)       AS earnings_eur,
        SUM(rides_gmv_before_discounts_billing_with_vat_eur_local) AS gmv_eur
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE city_id = 150
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
    GROUP BY 1, 2, 3
    """
    df = run_query(sql)
    # Cars with no company (id=-1) get Free-Floating / No Agreement by default
    null_cars = len(df[df["company_id"] == -1])
    if null_cars > 0:
        print(f"[car_weekly] ⚠️  {null_cars:,} car-week rows had no company_id → assigned to Free-Floating")
    print(f"[car_weekly] Fetched {len(df):,} car-week rows total")
    return df


def fetch_m30_data() -> pd.DataFrame:
    """M30 performance data – unchanged from original."""
    sql = f"""
    SELECT
        calendar_date_local                  AS date,
        COALESCE(company_id, -1)              AS company_id,
        SUM(rides_driver_total_earnings_with_vat_eur_local)        AS earnings_eur,
        SUM(rides_gmv_before_discounts_billing_with_vat_eur_local) AS gmv_eur,
        COUNT(DISTINCT car_id)                                     AS active_cars
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE city_id = 150
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_M30} DAYS
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[m30] Fetched {len(df):,} rows")
    return df


def fetch_company_snapshot() -> pd.DataFrame:
    """Company snapshot – unchanged from original."""
    sql = """
    SELECT
        company_id,
        company_billing_type AS invoicing_strategy
    FROM main.mart_models.mart_fleet_company_daily_history
    WHERE company_city_id = 150
      AND calendar_date = (
          SELECT MAX(calendar_date)
          FROM main.mart_models.mart_fleet_company_daily_history
          WHERE company_city_id = 150
      )
    """
    df = run_query(sql)
    print(f"[snapshot] Fetched {len(df):,} company rows")
    return df


# ──────────────────────────────────────────────────────────────────────────────
# BUILD EMBEDDED_AGREEMENTS  (injected into the dashboard template)
# ──────────────────────────────────────────────────────────────────────────────

def build_embedded_agreements(car_df: pd.DataFrame, cohort_map: dict, fo_map: dict = None) -> dict:
    """
    For every company that appears in the car-level data, determine its cohort
    by applying classify_car() across all its cars, then return the
    EMBEDDED_AGREEMENTS dict used by the dashboard template:

        {
          "company_id_str": {
              "n":  company_name,
              "f":  fleet_type,          # e.g. "Free-Floating" / "Strategic"
              "c":  cohort,              # e.g. "Fleet Agreement"
              "g":  grouping_or_null
          },
          ...
        }

    A company's cohort is determined by majority-vote across its cars
    (or fixed if the company is Fleet Agreement / Locked Supply).
    """
    agreements = {}

    for company_id, group in car_df.groupby("company_id"):
        cid_str = str(company_id)
        info    = cohort_map.get(cid_str, {
            "name":      "",
            "grouping":  None,
            "fleet_type": "Free-Floating",
            "cohort":    "No Agreement",
        })

        # FO group: use fo_map (Admin Madrid sheet) as source of truth
        fo_group = (fo_map or {}).get(cid_str) or info.get("grouping") or None

        # If fixed cohort (Fleet Agreement / Locked Supply) → done immediately
        if info["cohort"] in FIXED_COHORTS:
            agreements[cid_str] = {
                "n": info["name"],
                "f": info["fleet_type"],
                "c": info["cohort"],
                "g": fo_group,
            }
            continue

        # Otherwise count branding cars vs non-branding cars
        branding_cars = int(group["is_branding_car"].sum())
        total_cars    = len(group["car_id"].unique())

        # Classify the company by majority of its cars
        # (even one branding car classifies as Branding if company is not fixed)
        is_branding_company = branding_cars > 0

        cohort, fleet_type = classify_car(cid_str, is_branding_company, cohort_map)

        agreements[cid_str] = {
            "n": info["name"],
            "f": fleet_type,
            "c": cohort,
            "g": fo_group,
        }

    print(f"[agreements] Built {len(agreements)} company entries")
    return agreements


# ──────────────────────────────────────────────────────────────────────────────
# AGGREGATE WEEKLY OH PER COMPANY+COHORT  (for the charts)
# ──────────────────────────────────────────────────────────────────────────────

def aggregate_weekly_by_cohort(car_df: pd.DataFrame,
                                agreements: dict) -> pd.DataFrame:
    """
    Roll up car-level weekly data to company+cohort level so the dashboard
    charts work the same way they did before (just now cohort-aware at car level).

    Returns columns: week_start, company_id, cohort, fleet_type,
                     online_hours, earnings_eur, gmv_eur
    """
    total_oh_input = car_df["online_hours"].sum()
    print(f"[aggregate] Total OH in car_df before aggregation: {total_oh_input:,.0f}")

    rows = []
    dropped_oh = 0
    for _, row in car_df.iterrows():
        cid = str(row["company_id"])
        ag  = agreements.get(cid)
        if ag is None:
            # Should not happen — assign default instead of dropping
            dropped_oh += row["online_hours"]
            ag = {"c": "No Agreement", "f": "Free-Floating"}
        rows.append({
            "week_date":          row["week_start"],
            "company_id":         cid,
            "cohort":             ag["c"],
            "invoicing_strategy": ag["f"],
            "online_hours":       row["online_hours"],
            "earnings_eur":       row["earnings_eur"],
            "gmv_eur":            row["gmv_eur"],
        })

    if dropped_oh > 0:
        print(f"[aggregate] ⚠️  {dropped_oh:,.0f} OH had no agreement entry → assigned No Agreement")

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    result = (
        result
        .groupby(["week_date", "company_id", "cohort", "invoicing_strategy"], as_index=False)
        .agg({"online_hours": "sum", "earnings_eur": "sum", "gmv_eur": "sum"})
    )
    total_oh_output = result["online_hours"].sum()
    print(f"[aggregate] {len(result):,} company-week-cohort rows | Total OH after aggregation: {total_oh_output:,.0f}")
    return result


# ──────────────────────────────────────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def generate_html(data: dict) -> str:
    """Load dashboard_template.html and inject PRELOADED_DATA + EMBEDDED_AGREEMENTS."""
    template_path = Path("dashboard_template.html")
    html = template_path.read_text(encoding="utf-8")

    # 1. Inject PRELOADED_DATA (performance timeseries for charts)
    data_json = json.dumps(data, default=str, ensure_ascii=False)
    html = html.replace(
        "/* __PRELOADED_DATA__ */",
        f"window.PRELOADED_DATA = {data_json};",
    )

    # 2. Inject EMBEDDED_AGREEMENTS (company→cohort mapping)
    agreements_json = json.dumps(data["agreements"], ensure_ascii=False)
    html = html.replace(
        "/* __EMBEDDED_AGREEMENTS__ */",
        f"window.EMBEDDED_AGREEMENTS = {agreements_json};",
    )

    return html


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"Dashboard build started at {datetime.datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    # 1. Load cohort classification and FO group mapping
    cohort_map = load_cohort_map(COHORTS_CSV)
    fo_map     = load_fo_group_map(FO_GROUPS_CSV)

    # 2. Fetch data from Databricks
    car_df      = fetch_car_weekly_data()
    m30_df      = fetch_m30_data()
    snapshot_df = fetch_company_snapshot()

    # 3. Build EMBEDDED_AGREEMENTS (one entry per company)
    agreements = build_embedded_agreements(car_df, cohort_map, fo_map)

    # 4. Aggregate weekly OH by company+cohort (for dashboard charts)
    weekly_df = aggregate_weekly_by_cohort(car_df, agreements)

    # 5. Convert dataframes to JSON-serialisable dicts
    data = {
        "generated_at":      datetime.datetime.utcnow().isoformat() + "Z",
        "agreements":        agreements,
        "fleet_performance": weekly_df.to_dict(orient="records"),
        "company_snapshot":  snapshot_df.to_dict(orient="records"),
        "hourly_car_data":   [],   # car-level hourly data not yet wired up
        "m30":               m30_df.to_dict(orient="records"),
    }

    # 6. Generate HTML
    html = generate_html(data)

    # 7. Write output
    output_path = Path("docs/index.html")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"\n✅  Dashboard written to {output_path} ({len(html):,} bytes)")
    print("=" * 60)


if __name__ == "__main__":
    main()
