"""
gen_dashboard.py  –  Supply Fleet Madrid Dashboard Builder
===========================================================
Runs nightly via GitHub Actions.
Reads cohort classifications from cohorts.csv (exported from Google Sheet
"ES MAD Fleets cohort") and car-level online-hours from Databricks, then
injects the result into dashboard_template.html → index.html (GitHub Pages).

CLASSIFICATION RULE (per company):
  The Google Sheet "Cohorts by Grouping" is the source of truth for grouping,
  fleet type and cohort.
  - Company IN the Sheet with Fleet Type == "Strategic"  →  STRATEGIC,
        cohort taken straight from the Sheet (Branding / Fleet Agreement /
        Locked Supply / No Agreement).
  - Any other company (different fleet type, or absent from the Sheet)  →
        NON-STRATEGIC, cohort "All Free floating (Branded TBD)".
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
LOOKBACK_DAYS_WEEKLY = 800   # ~all available history (Spain data starts 2024-05-31)
LOOKBACK_DAYS_M30    = 30    # for M30 section
COHORTS_CSV          = "fo_groups.csv"       # single source of truth (Company, Company ID, FO, Fleet Type, Cohort)
FO_GROUPS_CSV        = "fo_groups.csv"       # same file — Company ID → FO group name

# Optional data cut-off date (YYYY-MM-DD). When set, only data up to and
# including this date is fetched — useful to lock the dashboard to the last
# complete week. If empty, data goes up to today (original behaviour).
DATA_CUTOFF          = os.environ.get("DATA_CUTOFF", "").strip()
if not DATA_CUTOFF:
    # Default: cut off at the most recent COMPLETED week (last Sunday), so the
    # "last period" KPI never compares against a half-finished current week.
    _today  = datetime.datetime.utcnow().date()
    _offset = (_today.weekday() + 1) % 7 or 7   # days back to the previous Sunday
    DATA_CUTOFF = (_today - datetime.timedelta(days=_offset)).isoformat()
_CUTOFF_CLAUSE       = f"AND calendar_date_local <= DATE '{DATA_CUTOFF}'" if DATA_CUTOFF else ""

# The Google Sheet ("Cohorts by Grouping") is the SOURCE OF TRUTH for grouping,
# fleet type and cohort. A company is STRATEGIC only if it appears in the Sheet
# with Fleet Type == "Strategic"; every other company (incl. those absent from
# the Sheet) is NON-STRATEGIC and gets the catch-all cohort below.
STRATEGIC_LABEL     = "Strategic"
# Internal fleet-type value kept as "Free Floating" (the left chart's data key);
# it is DISPLAYED as "Non strategic" in the dashboard legend.
NONSTRATEGIC_LABEL  = "Free Floating"

# ── COHORTS (right-hand "Cohort" chart) ──────────────────────────────────────
# Strategic cohorts come from the Sheet's Cohort column (mapped to display names).
COHORT_STRATEGIC = {
    "Fleet Agreement": "Strategic - Fleet Agreement",
    "Branding":        "Strategic - Branded",
    "Locked Supply":   "Strategic - Locked",
    "No Agreement":    "Strategic - No agreement",
}
# Free-floating split comes from Databricks branding data (fact_car_branding_periods):
FF_BRANDED     = "Free floating - Branded"
FF_NOT_BRANDED = "Free floating - Not branded"

# Compact 1-char codes for the per-row cohort in the payload (keeps JSON small).
COHORT_CODE = {
    "Strategic - Fleet Agreement": "A",
    "Strategic - Branded":         "B",
    "Strategic - Locked":          "L",
    "Strategic - No agreement":    "N",
    FF_BRANDED:                    "F",
    FF_NOT_BRANDED:                "X",
}

VALID_COHORTS = list(COHORT_STRATEGIC.values()) + [FF_BRANDED, FF_NOT_BRANDED]

# Weekly online-hours-per-car targets ("double shift" scenarios) used to estimate
# a fleet's upside: for each car, headroom = max(0, target - car's weekly OH).
FLEET_TARGETS = [60, 70, 80, 90, 100]


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
# GOOGLE SHEET SYNC  (optional: refresh fo_groups.csv from a published CSV URL)
# ──────────────────────────────────────────────────────────────────────────────

def sync_grouping_from_sheet() -> None:
    """
    If SHEET_CSV_URL is set (a 'Publish to web' CSV export of the Sheet's
    "Cohorts by Grouping" tab), download it into fo_groups.csv so the nightly
    build always uses the latest grouping. If unset, the committed fo_groups.csv
    is used as-is (manual sync). Failures fall back to the committed CSV.
    """
    # Default to the published CSV of the Sheet's "Cohorts by Grouping" tab
    # (public "Publish to web" link — overridable via the SHEET_CSV_URL env var).
    _default = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vS4ktNlCZUwdqI05J_5c8jYk9j"
                "-tSu2uSGnsxE3Nxq8DY9gTpQoBqbNWA39IG2rjI2Bp5GMRX6j4zH2/pub"
                "?gid=490803376&single=true&output=csv")
    url = os.environ.get("SHEET_CSV_URL", _default).strip()
    if not url:
        print("[sheet] SHEET_CSV_URL empty — using committed fo_groups.csv")
        return
    try:
        # Read Company ID as text so it stays "59137" (not float "59137.0"),
        # otherwise it won't match the Databricks company_id.
        df = pd.read_csv(url, dtype={"Company ID": str})
        expected = {"Company", "Company ID", "FO", "Fleet Type", "Cohort"}
        if not expected.issubset(set(df.columns)):
            print(f"[sheet] ⚠️  URL columns {list(df.columns)} != expected — keeping committed CSV")
            return
        df.to_csv(FO_GROUPS_CSV, index=False)
        print(f"[sheet] Synced {len(df):,} rows from SHEET_CSV_URL → {FO_GROUPS_CSV}")
    except Exception as e:
        print(f"[sheet] ⚠️  Could not sync from SHEET_CSV_URL ({str(e)[:100]}) — keeping committed CSV")


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
    Read the Admin Madrid Google Sheet CSV and return a dict:
        { "company_id_str": {"name": ..., "fleet_type": ..., "cohort": ..., "grouping": ...} }

    Supports two column layouts (auto-detected):

    NEW format (fo_groups.csv):
        Company | Company ID | FO | Fleet Type | Cohort

    OLD format (legacy cohorts.csv):
        Company name | Company ID | Grouping | Strategic | No Agreement

    Multi-market tab (lowercase):
        company_id | company_name | grouping | fleet_type | cohort
    """
    cohort_map = {}

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # ── Detect which header format we have ──────────────────────────────
        if "Company ID" in headers and "FO" in headers and "Fleet Type" in headers and "Cohort" in headers:
            # NEW format: Company, Company ID, FO, Fleet Type, Cohort
            id_col       = "Company ID"
            name_col     = "Company"
            grouping_col = "FO"             # FO group stored here
            type_col     = "Fleet Type"
            cohort_col   = "Cohort"
        elif "Company ID" in headers:
            # OLD Madrid tab format: Company name, Company ID, Grouping, Strategic, No Agreement
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
                "Expected 'Company ID', 'FO', 'Fleet Type', 'Cohort' columns."
            )

        for row in reader:
            raw_id = row.get(id_col, "").strip()
            if not raw_id:
                continue
            cohort_raw = row.get(cohort_col, "").strip()
            # Normalise cohort label (handle minor typos / case differences)
            cohort = _normalise_cohort(cohort_raw)
            grouping = row.get(grouping_col, "").strip()
            # Reject suspicious grouping values (CSV-formatted multi-company blobs)
            # Allow single commas in company names like "Agencia Negociadora, S.L."
            if grouping and (grouping.count(",") >= 3 or len(grouping) > 100):
                print(f"[cohort_map] ⚠️  Skipping suspicious grouping for company {raw_id}: {grouping[:60]}...")
                grouping = ""
            cohort_map[raw_id] = {
                "name":       row.get(name_col, "").strip(),
                "grouping":   grouping,
                "fleet_type": row.get(type_col, "Free-Floating").strip(),
                "cohort":     cohort,
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

def is_strategic(info: dict) -> bool:
    """A company is strategic only if the Sheet lists it with Fleet Type 'Strategic'."""
    return bool(info) and str(info.get("fleet_type", "")).strip().lower() == "strategic"


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
        city_id,
        city_name,
        COUNT(DISTINCT date_hour_ts_local)                        AS online_hours,
        SUM(rides_driver_total_earnings_with_vat_eur_local)       AS earnings_eur,
        SUM(rides_gmv_before_discounts_billing_with_vat_eur_local) AS gmv_eur
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE country_id = 67   -- Spain (all cities)
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
      {_CUTOFF_CLAUSE}
    GROUP BY 1, 2, 3, 4, 5
    """
    df = run_query(sql)
    # Cars with no company (id=-1) get Free-Floating / No Agreement by default
    null_cars = len(df[df["company_id"] == -1])
    if null_cars > 0:
        print(f"[car_weekly] ⚠️  {null_cars:,} car-week rows had no company_id → assigned to Free-Floating")
    print(f"[car_weekly] Fetched {len(df):,} car-week rows total")
    return df


def fetch_m30_data() -> pd.DataFrame:
    """Daily company-level data for the last 30 days (M30 section + Daily granularity view)."""
    sql = f"""
    SELECT
        calendar_date_local                                                    AS date,
        COALESCE(company_id, -1)                                               AS company_id,
        COUNT(DISTINCT CONCAT(CAST(car_id AS STRING), '|',
              CAST(date_hour_ts_local AS STRING)))                             AS online_hours,
        SUM(rides_driver_total_earnings_with_vat_eur_local)                    AS earnings_eur,
        SUM(rides_gmv_before_discounts_billing_with_vat_eur_local)             AS gmv_eur,
        COUNT(DISTINCT car_id)                                                 AS active_cars,
        COUNT(DISTINCT driver_id)                                              AS active_drivers
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE country_id = 67   -- Spain (all cities)
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_M30} DAYS
      {_CUTOFF_CLAUSE}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[m30] Fetched {len(df):,} rows")
    return df


def fetch_drivers_weekly() -> pd.DataFrame:
    """Distinct active drivers per company-week (for the 'Active drivers' metric)."""
    sql = f"""
    SELECT
        DATE_TRUNC('week', calendar_date_local)  AS week_start,
        COALESCE(company_id, -1)                 AS company_id,
        COUNT(DISTINCT driver_id)                AS active_drivers
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE country_id = 67   -- Spain (all cities)
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
      {_CUTOFF_CLAUSE}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[drivers_weekly] Fetched {len(df):,} company-week rows")
    return df


def fetch_finished_rides_weekly() -> pd.DataFrame:
    """
    Finished rides per (week, city) from the canonical orders table
    (order_state = 'finished'). company_id is NULL in this table, but
    order_city_id is fully populated and matches the hourly table's city_id, so
    we aggregate by city. In main() each city's finished rides are split across
    that city's cohort/FO rows in proportion to online hours — the per-city (and
    hence national) total stays exact. Matches the reference dashboard's
    "Finished Orders", which is also city-level.

    Note: this table uses `created_date_local` (not calendar_date_local), so it
    needs its own cutoff clause.
    """
    cutoff = f"AND created_date_local <= DATE '{DATA_CUTOFF}'" if DATA_CUTOFF else ""
    sql = f"""
    SELECT
        DATE_TRUNC('week', created_date_local)   AS week_start,
        order_city_id                            AS city_id,
        COUNT(*)                                 AS finished_rides
    FROM main.core_models.fact_rides_order
    WHERE order_country_id = 67   -- Spain (all cities)
      AND order_state = 'finished'
      AND order_city_id IS NOT NULL
      AND created_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
      {cutoff}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[finished_rides] Fetched {len(df):,} week-city rows "
          f"| Total finished = {df['finished_rides'].sum():,.0f}")
    return df


def fetch_mart_weekly() -> pd.DataFrame:
    """Canonical SUPPLY metrics per (week, company) from the fleet mart:
    online hours, GMV (rides earnings before discounts) and active drivers.
    The mart is fresh (loaded same-day) and complete, unlike the rides-earnings
    table. In main() each company's totals are spread across its cohort/city/FO
    rows in proportion to its active hours, keeping the split while matching the
    canonical per-company (and national) totals."""
    cutoff = f"AND calendar_date <= DATE '{DATA_CUTOFF}'" if DATA_CUTOFF else ""
    sql = f"""
    SELECT
        DATE_TRUNC('week', calendar_date)               AS week_start,
        COALESCE(company_id, -1)                        AS company_id,
        SUM(fleet_online_hours)                         AS online_hours,
        SUM(fleet_rides_earnings_before_discounts_eur)  AS gmv_eur,
        SUM(fleet_count_active_drivers)                 AS active_drivers
    FROM main.mart_models.mart_fleet_company_daily_history
    WHERE LOWER(company_country_code) = 'es'
      AND calendar_date >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
      {cutoff}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[mart] Fetched {len(df):,} week-company rows | Canonical OH = {df['online_hours'].sum():,.0f} "
          f"| GMV = {df['gmv_eur'].sum():,.0f} | drivers = {df['active_drivers'].sum():,.0f}")
    return df


def fetch_mart_daily() -> pd.DataFrame:
    """Canonical supply metrics per (day, company) for the last M30 days (Day view)."""
    cutoff = f"AND calendar_date <= DATE '{DATA_CUTOFF}'" if DATA_CUTOFF else ""
    sql = f"""
    SELECT
        calendar_date                                   AS day_date,
        COALESCE(company_id, -1)                        AS company_id,
        SUM(fleet_online_hours)                         AS online_hours,
        SUM(fleet_rides_earnings_before_discounts_eur)  AS gmv_eur,
        SUM(fleet_count_active_drivers)                 AS active_drivers
    FROM main.mart_models.mart_fleet_company_daily_history
    WHERE LOWER(company_country_code) = 'es'
      AND calendar_date >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_M30} DAYS
      {cutoff}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[mart_daily] Fetched {len(df):,} day-company rows "
          f"| Canonical OH = {df['online_hours'].sum():,.0f}")
    return df


def _scale_metric_to_canonical(rows_df, date_col, canon_lookup, target_col, weight_col):
    """Set each row's `target_col` so every (date, company)'s total matches the
    canonical mart value in canon_lookup ({(date10, company_id): total}), split
    across the company's rows in proportion to `weight_col` (its active-activity
    share). Rows of companies missing from canon_lookup keep their existing
    target_col value (fallback). Operates on rows that still carry company_id."""
    if rows_df is None or rows_df.empty:
        return rows_df
    dk   = rows_df[date_col].astype(str).str[:10].tolist()
    comp = rows_df["company_id"].astype(str).tolist()
    w    = [float(x or 0) for x in rows_df[weight_col].tolist()]
    cur  = [float(x or 0) for x in rows_df[target_col].tolist()]
    keys = list(zip(dk, comp))
    wsum = {}
    for k, x in zip(keys, w):
        wsum[k] = wsum.get(k, 0.0) + x
    out = []
    for k, x, c in zip(keys, w, cur):
        canon = canon_lookup.get(k)
        s = wsum.get(k, 0.0)
        out.append(canon * (x / s) if (canon is not None and s > 0) else c)
    rows_df = rows_df.copy()
    rows_df[target_col] = out
    return rows_df


def fetch_finished_rides_daily() -> pd.DataFrame:
    """Finished rides per (day, city) for the last M30 days — for the Fleet
    Review 'Day' granularity. Same source/logic as the weekly version."""
    cutoff = f"AND created_date_local <= DATE '{DATA_CUTOFF}'" if DATA_CUTOFF else ""
    sql = f"""
    SELECT
        created_date_local   AS day_date,
        order_city_id        AS city_id,
        COUNT(*)             AS finished_rides
    FROM main.core_models.fact_rides_order
    WHERE order_country_id = 67
      AND order_state = 'finished'
      AND order_city_id IS NOT NULL
      AND created_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_M30} DAYS
      {cutoff}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[finished_daily] Fetched {len(df):,} day-city rows "
          f"| Total finished = {df['finished_rides'].sum():,.0f}")
    return df


def fetch_taxi_vtc_weekly() -> pd.DataFrame:
    """
    Weekly GMV split into Taxi vs VTC per city (for the Taxi vs VTC widget).
    A ride is 'taxi' when its search_category_name starts with 'Taxi'
    (e.g. 'Taxi Madrid M30'); everything else counts as VTC.
    """
    sql = f"""
    SELECT
        DATE_TRUNC('week', calendar_date_local)  AS week_start,
        city_name,
        SUM(CASE WHEN LOWER(search_category_name) LIKE 'taxi%'
                 THEN rides_gmv_before_discounts_billing_with_vat_eur_local ELSE 0 END) AS taxi_gmv,
        SUM(CASE WHEN LOWER(search_category_name) LIKE 'taxi%' THEN 0
                 ELSE rides_gmv_before_discounts_billing_with_vat_eur_local END)        AS vtc_gmv
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE country_id = 67   -- Spain (all cities)
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_WEEKLY} DAYS
      {_CUTOFF_CLAUSE}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[taxi_vtc] Fetched {len(df):,} week-city rows")
    return df


def aggregate_daily_by_cohort(m30_df: pd.DataFrame, agreements: dict,
                              canon_oh=None, canon_gmv=None, canon_drv=None) -> pd.DataFrame:
    """
    Convert daily company-level M30 data into the same shape as fleet_performance
    (cohort-tagged), but keyed by day_date instead of week_date.
    Used by the 'Day' granularity button in the dashboard.

    canon_oh / canon_gmv / canon_drv ({(day10, company_id): value}) rescale the
    respective columns to the canonical mart totals per (day, company) before
    collapsing cohorts (same treatment as the weekly path).
    """
    if m30_df.empty:
        return pd.DataFrame()

    rows = []
    for _, row in m30_df.iterrows():
        cid = str(row["company_id"])
        ag  = agreements.get(cid, {"c": FF_NOT_BRANDED, "f": NONSTRATEGIC_LABEL})
        rows.append({
            "day_date":           str(row["date"]),
            "company_id":         cid,
            "cohort":             ag["c"],
            "invoicing_strategy": ag["f"],
            "city":               ag.get("city"),
            "fo":                 ag.get("g") or "",
            "online_hours":       row["online_hours"],
            "earnings_eur":       row["earnings_eur"],
            "gmv_eur":            row["gmv_eur"],
            "active_drivers":     row.get("active_drivers", 0),
            "active_cars":        row.get("active_cars", 0),
        })

    result = pd.DataFrame(rows)
    # Rescale OH / GMV / drivers to canonical mart totals per (day, company),
    # spread by active-OH share, before collapsing.
    if canon_oh or canon_gmv or canon_drv:
        result["_w"] = result["online_hours"]
        if canon_oh:  result = _scale_metric_to_canonical(result, "day_date", canon_oh,  "online_hours",   "_w")
        if canon_gmv: result = _scale_metric_to_canonical(result, "day_date", canon_gmv, "gmv_eur",        "_w")
        if canon_drv: result = _scale_metric_to_canonical(result, "day_date", canon_drv, "active_drivers", "_w")
    # Aggregate to per (day, city, FO, cohort, fleetType) — drop company_id but
    # keep a distinct-company count (n) for the table's "Companies" column.
    result = (
        result
        .groupby(["day_date", "city", "fo", "cohort", "invoicing_strategy"],
                 as_index=False, dropna=False)
        .agg(online_hours=("online_hours", "sum"),
             earnings_eur=("earnings_eur", "sum"),
             gmv_eur=("gmv_eur", "sum"),
             active_drivers=("active_drivers", "sum"),
             active_cars=("active_cars", "sum"),
             n=("company_id", "nunique"))
    )
    print(f"[daily] {len(result):,} day-city-FO-cohort rows for 'Day' granularity view")
    return result


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


def fetch_branded_cars() -> set:
    """
    Distinct car_ids that are currently branded, matching the Looker "Branded Tag"
    definition: a branding tag in state 'approved' whose period is active right now
    (start_date <= today <= end_date).
    Source: main.stg_models.stg_car_branding_periods_car_branding_periods.
    Used to split the non-strategic fleet into Branded vs Not-branded PER CAR.
    """
    sql = """
    SELECT DISTINCT car_id
    FROM main.stg_models.stg_car_branding_periods_car_branding_periods
    WHERE car_branding_state = 'approved'
      AND car_branding_start_date <= CURRENT_DATE
      AND car_branding_end_date   >= CURRENT_DATE
    """
    df = run_query(sql)
    cars = (set(pd.to_numeric(df["car_id"], errors="coerce").dropna().astype("int64").tolist())
            if not df.empty else set())
    print(f"[branded] {len(cars):,} cars with an active approved branding tag")
    return cars


# ──────────────────────────────────────────────────────────────────────────────
# BUILD EMBEDDED_AGREEMENTS  (injected into the dashboard template)
# ──────────────────────────────────────────────────────────────────────────────

def build_embedded_agreements(car_df: pd.DataFrame, cohort_map: dict, fo_map: dict = None,
                              branded_companies: set = None) -> dict:
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
    n_strategic = 0
    branded_companies = branded_companies or set()

    # Each company operates in a single city — attach it so the dashboard can
    # filter by city. Take the first city seen for the company in the car data.
    city_by_company = {}
    if "city_name" in car_df.columns:
        city_by_company = {
            str(k): v for k, v in
            car_df.drop_duplicates("company_id").set_index("company_id")["city_name"].to_dict().items()
        }

    for company_id in car_df["company_id"].unique():
        cid_str = str(company_id)
        info    = cohort_map.get(cid_str)   # None if the company is not in the Sheet
        city    = city_by_company.get(cid_str)

        # FO group: fo_map is the primary source; cohort_map.grouping is fallback
        fo_group = (fo_map or {}).get(cid_str) or (info or {}).get("grouping") or None
        # Sanity check: reject any value that looks like raw CSV content
        if fo_group and (fo_group.count(",") >= 3 or len(fo_group) > 100):
            print(f"[agreements] ⚠️  Rejected suspicious fo_group for company {cid_str}: {str(fo_group)[:60]}...")
            fo_group = None

        if is_strategic(info):
            # Strategic → cohort from the Sheet, mapped to its display name
            # (Strategic - Fleet Agreement / Branded / Locked / No agreement).
            agreements[cid_str] = {
                "n": info.get("name", ""),
                "f": STRATEGIC_LABEL,
                "c": COHORT_STRATEGIC.get(info.get("cohort"), "Strategic - No agreement"),
                "g": fo_group,
                "city": city,
            }
            n_strategic += 1
        else:
            # Non-strategic (not in the Sheet as Strategic) → Free floating.
            # Branded vs not-branded comes from Databricks branding data.
            agreements[cid_str] = {
                "n": (info or {}).get("name", ""),
                "f": NONSTRATEGIC_LABEL,
                "c": FF_BRANDED if cid_str in branded_companies else FF_NOT_BRANDED,
                "g": fo_group,
                "city": city,
            }

    print(f"[agreements] Built {len(agreements)} company entries "
          f"({n_strategic} strategic, {len(agreements) - n_strategic} non-strategic)")
    return agreements


# ──────────────────────────────────────────────────────────────────────────────
# AGGREGATE WEEKLY OH PER COMPANY+COHORT  (for the charts)
# ──────────────────────────────────────────────────────────────────────────────

def aggregate_weekly_by_cohort(car_df: pd.DataFrame,
                                agreements: dict,
                                branded_cars: set = None) -> pd.DataFrame:
    """
    Roll up car-level weekly data to company+cohort level. Cohort is assigned
    PER CAR: strategic companies use the Sheet cohort; non-strategic (free
    floating) cars are split into Branded vs Not-branded by the admin
    is_car_branded tag (branded_cars). So a free-floating company can appear in
    both 'Free floating - Branded' and 'Free floating - Not branded'.

    This is STAGE 1 only: it returns per-company rows (still keyed by
    company_id) carrying city + FO, so active_drivers can be merged per
    (week, company) in main() BEFORE collapsing to the final
    (week, city, FO, cohort, fleetType) shape. Attaching drivers at the car
    level would multiply them across a company's many car rows.

    Returns columns: week_date, company_id, cohort, invoicing_strategy,
                     city, fo, online_hours, earnings_eur, gmv_eur, active_cars
    """
    branded_cars = branded_cars or set()
    total_oh_input = car_df["online_hours"].sum()
    print(f"[aggregate] Total OH in car_df before aggregation: {total_oh_input:,.0f}")

    has_city_col = "city_name" in car_df.columns
    rows = []
    for _, row in car_df.iterrows():
        cid = str(row["company_id"])
        ag  = agreements.get(cid) or {"c": FF_NOT_BRANDED, "f": NONSTRATEGIC_LABEL}
        if ag["f"] == STRATEGIC_LABEL:
            cohort = ag["c"]
        else:
            # Free floating → split by the car's admin branded tag
            try:
                car_id = int(row["car_id"])
            except (ValueError, TypeError):
                car_id = None
            cohort = FF_BRANDED if car_id in branded_cars else FF_NOT_BRANDED
        city = ag.get("city") or (row["city_name"] if has_city_col else None)
        fo   = ag.get("g") or ""
        rows.append({
            "week_date":          row["week_start"],
            "company_id":         cid,
            "car_id":             row["car_id"],
            "cohort":             cohort,
            "invoicing_strategy": ag["f"],
            "city":               city,
            "fo":                 fo,
            "online_hours":       row["online_hours"],
            "earnings_eur":       row["earnings_eur"],
            "gmv_eur":            row["gmv_eur"],
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    # Stage 1: collapse cars → one row per (week, company, cohort, ...).
    # city/fo are constant per company so they add no new groups.
    # active_cars = distinct car_id in the group (cars belong to one company, so
    # summing this across companies in stage 2 stays a correct distinct count).
    result = (
        result
        .groupby(["week_date", "company_id", "cohort", "invoicing_strategy", "city", "fo"],
                 as_index=False, dropna=False)
        .agg(online_hours=("online_hours", "sum"),
             earnings_eur=("earnings_eur", "sum"),
             gmv_eur=("gmv_eur", "sum"),
             active_cars=("car_id", "nunique"))
    )
    total_oh_output = result["online_hours"].sum()
    print(f"[aggregate] {len(result):,} company-week-cohort rows (stage 1) | Total OH after aggregation: {total_oh_output:,.0f}")
    return result


def compute_car_headroom(car_df: pd.DataFrame, agreements: dict, branded_cars: set,
                         moh_lookup: dict) -> pd.DataFrame:
    """Per-car OH upside per (week, city, FO, cohort, strategy). For each car we
    take its weekly online hours scaled to the canonical mart level (same factor
    used for the fleet OH), then for each target T sum max(0, T - car_online_h).
    Returns one row per group with columns hr60, hr70, … (the extra OH a fleet
    could do if each of its cars reached that weekly target). moh_lookup:
    {(week10, company_id): canonical company OH}."""
    branded_cars = branded_cars or set()
    if car_df.empty:
        return pd.DataFrame()

    # Pass 1 — accumulate ACTIVE OH per DISTINCT car within its group (a car is
    # fragmented across several earnings rows / company_ids; those must be merged
    # so a busy car is not double-counted as several idle "cars"). Also collect,
    # per group, the total active OH and the set of companies (for the canonical
    # scaling factor).
    car_active  = {}   # (group_key, car_id) -> active OH
    grp_active  = {}   # group_key -> active OH (Σ over its cars)
    grp_comps   = {}   # group_key -> set(company_id)
    comp_active = {}   # (week, company) -> active OH (canonical fallback)
    for row in car_df.itertuples(index=False):
        w = str(row.week_start)[:10]
        c = str(row.company_id)
        o = float(row.online_hours or 0)
        try:
            carid = int(row.car_id)
        except (ValueError, TypeError):
            carid = None
        ag = agreements.get(c) or {"c": FF_NOT_BRANDED, "f": NONSTRATEGIC_LABEL}
        cohort = ag["c"] if ag["f"] == STRATEGIC_LABEL else \
                 (FF_BRANDED if carid in branded_cars else FF_NOT_BRANDED)
        ccity = getattr(row, "city_name", None)
        city  = ag.get("city") or ccity
        gk = (w, "" if city is None or (isinstance(city, float) and pd.isna(city)) else str(city),
              "" if ag.get("g") is None else str(ag.get("g") or ""), cohort, ag["f"])
        car_active[(gk, carid)] = car_active.get((gk, carid), 0.0) + o
        grp_active[gk] = grp_active.get(gk, 0.0) + o
        grp_comps.setdefault(gk, set()).add(c)
        comp_active[(w, c)] = comp_active.get((w, c), 0.0) + o

    # Group canonical OH = Σ company mart OH (fallback to that company's active).
    grp_canon = {}
    for gk, comps in grp_comps.items():
        w = gk[0]
        grp_canon[gk] = sum(moh_lookup.get((w, c), comp_active.get((w, c), 0.0)) for c in comps)

    # Pass 2 — per distinct car, scale its active OH to the canonical level and
    # sum the headroom max(0, target - car online hours) into its group.
    groups = {}
    for (gk, carid), oa in car_active.items():
        ga = grp_active.get(gk, 0.0)
        online = oa * (grp_canon[gk] / ga) if ga > 0 else oa
        arr = groups.get(gk)
        if arr is None:
            arr = [0.0] * len(FLEET_TARGETS)
            groups[gk] = arr
        for i, T in enumerate(FLEET_TARGETS):
            if online < T:
                arr[i] += (T - online)

    recs = []
    for (w, city, fo, cohort, strat), arr in groups.items():
        rec = {"week_date": w, "city": city, "fo": fo, "cohort": cohort, "invoicing_strategy": strat}
        for i, T in enumerate(FLEET_TARGETS):
            rec[f"hr{T}"] = arr[i]
        recs.append(rec)
    print(f"[headroom] {len(recs):,} groups | total upside @80: "
          f"{sum(r['hr80'] for r in recs):,.0f} OH")
    return pd.DataFrame(recs)


# ──────────────────────────────────────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────────────────────────────────────

def generate_html(data: dict) -> str:
    """Load dashboard_template.html and inject PRELOADED_DATA.

    All dimensions (city / FO / cohort / fleet type) now travel on each
    aggregated performance row, so the per-company EMBEDDED_AGREEMENTS lookup
    is no longer emitted."""
    template_path = Path("dashboard_template.html")
    html = template_path.read_text(encoding="utf-8")

    # Inject PRELOADED_DATA (performance timeseries for charts)
    data_json = json.dumps(data, default=str, ensure_ascii=False)
    html = html.replace(
        "/* __PRELOADED_DATA__ */",
        f"window.PRELOADED_DATA = {data_json};",
    )

    return html


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def _distribute_finished(df: pd.DataFrame, date_col: str, fr_lookup: dict) -> pd.DataFrame:
    """Add a 'rides' column to an aggregated df (needs date_col, city,
    online_hours) by splitting each (date, city)'s finished rides across its
    rows in proportion to online hours; a 2nd pass spreads each date's
    unassigned remainder nationally by OH so the per-date total stays exact.
    fr_lookup: {(date10, city_name): finished_rides}."""
    if df is None or df.empty:
        if df is not None:
            df["rides"] = []
        return df
    dk    = df[date_col].astype(str).str[:10].tolist()
    oh    = [float(x or 0) for x in df["online_hours"].tolist()]
    ckey  = list(zip(dk, df["city"].tolist()))
    oh_sum, oh_by_date, assigned_by_date, fetched_by_date = {}, {}, {}, {}
    for k, o in zip(ckey, oh):
        oh_sum[k] = oh_sum.get(k, 0.0) + o
        oh_by_date[k[0]] = oh_by_date.get(k[0], 0.0) + o
    for (d, _c), v in fr_lookup.items():
        fetched_by_date[d] = fetched_by_date.get(d, 0.0) + v
    finished = []
    for k, o in zip(ckey, oh):
        tot, denom = fr_lookup.get(k, 0.0), oh_sum.get(k, 0.0)
        v = tot * (o / denom) if (tot > 0 and denom > 0) else 0.0
        finished.append(v)
        assigned_by_date[k[0]] = assigned_by_date.get(k[0], 0.0) + v
    for i, (k, o) in enumerate(zip(ckey, oh)):
        d = k[0]
        res, denom = fetched_by_date.get(d, 0.0) - assigned_by_date.get(d, 0.0), oh_by_date.get(d, 0.0)
        if res > 0 and denom > 0:
            finished[i] += res * (o / denom)
    df = df.copy()
    df["rides"] = finished
    return df


def _finished_lookup(finished_df, date_attr, cityname_by_id):
    """Build {(date10, city_name): finished} from a (date, city_id, finished) frame."""
    out = {}
    for r in finished_df.itertuples(index=False):
        try:
            cname = cityname_by_id.get(int(r.city_id))
        except (ValueError, TypeError):
            cname = None
        if cname is not None:
            out[(str(getattr(r, date_attr))[:10], cname)] = float(r.finished_rides)
    return out


def _compact_perf(df: pd.DataFrame, date_col: str) -> list:
    """
    Compact an aggregated performance DataFrame into the minimal rows the
    dashboard needs — one per (period, city, FO, cohort, fleetType):
        {"w": date, "f": fleetTypeCode, "co": cohortCode, "city": city,
         "fo": foGroup, "oh": online_hours, "e": earnings_eur, "g": gmv,
         "d": active_drivers, "n": distinct_companies}
    fleetTypeCode is 'S' (Strategic) or 'F' (Free Floating); all dimensions are
    carried on the row so the front-end no longer needs a per-company lookup.
    Short keys + rounded numbers keep the embedded JSON small.
    """
    if df is None or df.empty:
        return []
    out = []
    for row in df.itertuples(index=False):
        d = row._asdict()
        city = d.get("city")
        fo   = d.get("fo")
        rec = {
            "w":    str(d[date_col])[:10],
            "f":    "S" if d.get("invoicing_strategy") == STRATEGIC_LABEL else "F",
            "co":   COHORT_CODE.get(d.get("cohort"), "X"),
            "city": "" if pd.isna(city) else str(city),
            "fo":   "" if pd.isna(fo) else str(fo),
            "oh":   round(float(d.get("online_hours") or 0)),
            "e":    round(float(d.get("earnings_eur") or 0)),
            "g":    round(float(d.get("gmv_eur") or 0)),
            "d":    int(d.get("active_drivers") or 0),
            "c":    int(d.get("active_cars") or 0),
            "r":    round(float(d.get("rides") or 0)),
            "n":    int(d.get("n") or 0),
        }
        # Per-car OH upside at each FLEET_TARGETS level (weekly rows only).
        if ("hr%d" % FLEET_TARGETS[0]) in d:
            rec["hr"] = [round(float(d.get("hr%d" % T) or 0)) for T in FLEET_TARGETS]
        out.append(rec)
    return out


def refine_cutoff_to_complete_week():
    """Move DATA_CUTOFF back to the last Sunday whose full Mon–Sun week is
    present in the fleet MART (the fresh, headline-metric source). The mart is
    loaded same-day, so on Monday the previous week is already complete — this
    keeps the dashboard showing the latest COMPLETE week without waiting for the
    laggy rides-earnings table. Skipped when DATA_CUTOFF is set explicitly."""
    global DATA_CUTOFF, _CUTOFF_CLAUSE
    if os.environ.get("DATA_CUTOFF", "").strip():
        return
    df = run_query("""
        SELECT calendar_date AS d, SUM(fleet_online_hours) AS oh
        FROM main.mart_models.mart_fleet_company_daily_history
        WHERE LOWER(company_country_code) = 'es'
          AND calendar_date >= CURRENT_DATE - INTERVAL 45 DAYS
        GROUP BY 1
    """)
    complete = {str(r.d)[:10] for r in df.itertuples(index=False) if float(r.oh or 0) > 0}
    if not complete:
        print("[cutoff] No completeness data; keeping default cutoff.")
        return
    d = datetime.date.fromisoformat(max(complete))
    for _ in range(35):
        if d.weekday() == 6:  # Sunday = end of a Mon–Sun week
            week = [(d - datetime.timedelta(days=k)).isoformat() for k in range(7)]
            if all(w in complete for w in week):
                if d.isoformat() != DATA_CUTOFF:
                    print(f"[cutoff] Refined DATA_CUTOFF {DATA_CUTOFF} → {d.isoformat()} "
                          f"(last complete week in the fleet mart)")
                DATA_CUTOFF = d.isoformat()
                _CUTOFF_CLAUSE = f"AND calendar_date_local <= DATE '{DATA_CUTOFF}'"
                return
        d -= datetime.timedelta(days=1)
    print("[cutoff] No fully-loaded week found in window; keeping default cutoff.")


def main():
    print("=" * 60)
    print(f"Dashboard build started at {datetime.datetime.utcnow().isoformat()}Z")
    print("=" * 60)

    # 0. Pin the cutoff to the last fully-loaded week (unless DATA_CUTOFF is set).
    refine_cutoff_to_complete_week()
    print(f"[cutoff] Using DATA_CUTOFF = {DATA_CUTOFF}")

    # 1. Load cohort classification and FO group mapping
    #    (optionally refreshed from the Google Sheet first, if SHEET_CSV_URL is set)
    sync_grouping_from_sheet()
    cohort_map = load_cohort_map(COHORTS_CSV)
    fo_map     = load_fo_group_map(FO_GROUPS_CSV)

    # 2. Fetch data from Databricks
    car_df       = fetch_car_weekly_data()
    m30_df       = fetch_m30_data()
    branded_cars = fetch_branded_cars()

    # 3. Build EMBEDDED_AGREEMENTS (one entry per company). A non-strategic company
    #    counts as 'Free floating - Branded' if any of its cars was branded.
    _car_ids = pd.to_numeric(car_df["car_id"], errors="coerce")
    branded_companies = set(
        car_df.loc[_car_ids.isin(branded_cars), "company_id"].astype(str)
    ) if branded_cars else set()
    print(f"[branded] {len(branded_companies):,} companies with >=1 branded car")
    agreements = build_embedded_agreements(car_df, cohort_map, fo_map, branded_companies)

    # 4. Aggregate weekly OH by company+cohort (per-car cohort split for free floating)
    weekly_df = aggregate_weekly_by_cohort(car_df, agreements, branded_cars)

    # 4·MART. Replace the earnings-based proxies with the canonical SUPPLY metrics
    #   (fleet mart): online hours, GMV and active drivers. Each company's mart
    #   total is spread across its cohort/city/FO rows in proportion to its active
    #   online hours, so the split is kept while per-company totals match the mart.
    mart_weekly = fetch_mart_weekly()
    moh = {(str(r.week_start)[:10], str(r.company_id)): float(r.online_hours) for r in mart_weekly.itertuples(index=False)}
    mgm = {(str(r.week_start)[:10], str(r.company_id)): float(r.gmv_eur)      for r in mart_weekly.itertuples(index=False)}
    # Active drivers: DISTINCT drivers per (week, company) from the earnings table
    # — the mart only has DAILY counts (summing 7 days would ~7x inflate a weekly
    # distinct count), so weekly drivers stay on the correct distinct measure.
    dw_lookup = {
        (str(r.week_start)[:10], str(r.company_id)): int(r.active_drivers)
        for r in fetch_drivers_weekly().itertuples(index=False)
    }
    # Finished rides per (week, city) from the canonical orders table (fresh + exact).
    finished_weekly = fetch_finished_rides_weekly()
    cityname_by_id = {}
    for cid, cname in zip(car_df["city_id"], car_df["city_name"]):
        try:
            cityname_by_id[int(cid)] = cname
        except (ValueError, TypeError):
            pass
    fr_lookup = {}
    for r in finished_weekly.itertuples(index=False):
        try:
            cname = cityname_by_id.get(int(r.city_id))
        except (ValueError, TypeError):
            cname = None
        if cname is not None:
            fr_lookup[(str(r.week_start)[:10], cname)] = float(r.finished_rides)

    company_weekly = pd.DataFrame(columns=["week_date", "city", "fo", "invoicing_strategy", "n"])

    if not weekly_df.empty:
        wk = weekly_df["week_date"].astype(str).str[:10]
        comp = weekly_df["company_id"].astype(str)
        # Weight for spreading company totals across its rows = active online hours.
        weekly_df["_w"] = weekly_df["online_hours"]
        # OH & GMV ← canonical mart totals (spread by active-OH share).
        weekly_df = _scale_metric_to_canonical(weekly_df, "week_date", moh, "online_hours", "_w")
        weekly_df = _scale_metric_to_canonical(weekly_df, "week_date", mgm, "gmv_eur",      "_w")
        # Drivers ← distinct weekly count, also spread by active-OH share (so a
        # company's cohort rows sum to its distinct weekly drivers, no double count).
        weekly_df["active_drivers"] = 0.0
        weekly_df = _scale_metric_to_canonical(weekly_df, "week_date", dw_lookup, "active_drivers", "_w")
        print(f"[mart] Weekly after rescale — OH: {weekly_df['online_hours'].sum():,.0f} "
              f"| GMV: {weekly_df['gmv_eur'].sum():,.0f} | drivers: {weekly_df['active_drivers'].sum():,.0f}")

        # Split each city's finished rides across that city's cohort/FO rows in
        # proportion to online hours (pass 1). Some (week, city) have finished
        # orders but no OH rows here — mostly strategic fleets whose Sheet city
        # differs from their operating city — so their finished would be lost.
        # Pass 2 spreads each week's UN-assigned remainder across that week's rows
        # nationally (by OH), so the national weekly total stays EXACT.
        ckey = list(zip(wk, weekly_df["city"]))
        oh_sum = {}
        for k, oh in zip(ckey, weekly_df["online_hours"]):
            oh_sum[k] = oh_sum.get(k, 0.0) + float(oh or 0)
        oh_by_week, fetched_by_week = {}, {}
        for k, oh in zip(ckey, weekly_df["online_hours"]):
            oh_by_week[k[0]] = oh_by_week.get(k[0], 0.0) + float(oh or 0)
        for (w, _cn), val in fr_lookup.items():
            fetched_by_week[w] = fetched_by_week.get(w, 0.0) + val

        finished_col, assigned_by_week = [], {}
        for k, oh in zip(ckey, weekly_df["online_hours"]):
            total_fin = fr_lookup.get(k, 0.0)
            denom = oh_sum.get(k, 0.0)
            v = total_fin * (float(oh or 0) / denom) if (total_fin > 0 and denom > 0) else 0.0
            finished_col.append(v)
            assigned_by_week[k[0]] = assigned_by_week.get(k[0], 0.0) + v
        # Pass 2: national remainder per week → by OH.
        for i, (k, oh) in enumerate(zip(ckey, weekly_df["online_hours"])):
            w = k[0]
            res = fetched_by_week.get(w, 0.0) - assigned_by_week.get(w, 0.0)
            denom = oh_by_week.get(w, 0.0)
            if res > 0 and denom > 0:
                finished_col[i] += res * (float(oh or 0) / denom)
        weekly_df["finished_rides"] = finished_col

        # Exact company count (see note above) — before collapsing cohorts.
        company_weekly = (
            weekly_df
            .groupby(["week_date", "city", "fo", "invoicing_strategy"],
                     as_index=False, dropna=False)
            .agg(n=("company_id", "nunique"))
        )

        # …THEN collapse to the final (week, city, FO, cohort, fleetType) shape,
        # summing metrics + drivers and counting distinct companies as `n`.
        weekly_df = (
            weekly_df
            .groupby(["week_date", "city", "fo", "cohort", "invoicing_strategy"],
                     as_index=False, dropna=False)
            .agg(online_hours=("online_hours", "sum"),
                 earnings_eur=("earnings_eur", "sum"),
                 gmv_eur=("gmv_eur", "sum"),
                 rides=("finished_rides", "sum"),
                 active_cars=("active_cars", "sum"),
                 active_drivers=("active_drivers", "sum"),
                 n=("company_id", "nunique"))
        )
        print(f"[aggregate] {len(weekly_df):,} week-city-FO-cohort rows (stage 2) "
              f"| Total OH: {weekly_df['online_hours'].sum():,.0f} "
              f"| Total finished rides: {weekly_df['rides'].sum():,.0f}")

        # Per-car OH upside (headroom to each weekly target) merged onto the rows.
        hr_df = compute_car_headroom(car_df, agreements, branded_cars, moh)
        hr_lookup = {}
        for r in hr_df.itertuples(index=False):
            hr_lookup[(str(r.week_date)[:10], r.city or "", r.fo or "", r.cohort, r.invoicing_strategy)] = \
                [getattr(r, f"hr{T}") for T in FLEET_TARGETS]
        wk2 = weekly_df["week_date"].astype(str).str[:10].tolist()
        cols = {T: [] for T in FLEET_TARGETS}
        for w, city, fo, cohort, strat in zip(
                wk2, weekly_df["city"], weekly_df["fo"], weekly_df["cohort"], weekly_df["invoicing_strategy"]):
            key = (w, "" if pd.isna(city) else str(city), "" if pd.isna(fo) else str(fo), cohort, strat)
            arr = hr_lookup.get(key, [0.0] * len(FLEET_TARGETS))
            for i, T in enumerate(FLEET_TARGETS):
                cols[T].append(arr[i])
        for T in FLEET_TARGETS:
            weekly_df[f"hr{T}"] = cols[T]

    # 4b. Aggregate daily by company+cohort (for 'Day' granularity button),
    #     rescaled to canonical mart OH/GMV/drivers, then distribute daily finished.
    mart_daily = fetch_mart_daily()
    d_oh = {(str(r.day_date)[:10], str(r.company_id)): float(r.online_hours)   for r in mart_daily.itertuples(index=False)}
    d_gm = {(str(r.day_date)[:10], str(r.company_id)): float(r.gmv_eur)        for r in mart_daily.itertuples(index=False)}
    d_dr = {(str(r.day_date)[:10], str(r.company_id)): float(r.active_drivers) for r in mart_daily.itertuples(index=False)}
    daily_df = aggregate_daily_by_cohort(m30_df, agreements, d_oh, d_gm, d_dr)
    finished_daily = fetch_finished_rides_daily()
    frd_lookup = _finished_lookup(finished_daily, "day_date", cityname_by_id)
    daily_df = _distribute_finished(daily_df, "day_date", frd_lookup)
    if not daily_df.empty:
        print(f"[daily] Total finished rides distributed: {daily_df['rides'].sum():,.0f}")

    # 4c. Taxi vs VTC GMV per week+city (for the Taxi vs VTC widget)
    taxi_df = fetch_taxi_vtc_weekly()

    # 5. Convert dataframes to JSON-serialisable dicts
    data = {
        "generated_at":      datetime.datetime.utcnow().isoformat() + "Z",
        "fleet_performance": _compact_perf(weekly_df, "week_date"),
        "daily_performance": _compact_perf(daily_df,  "day_date"),
        "taxi_vtc": [
            {"w": str(r.week_start)[:10], "city": r.city_name,
             "t": round(float(r.taxi_gmv or 0)), "v": round(float(r.vtc_gmv or 0))}
            for r in taxi_df.itertuples(index=False)
        ],
        # Exact distinct-company count per (week, city, FO, strategy) for the
        # "Active companies" KPI (no cross-cohort double count).
        "company_weekly": [
            {"w": str(r.week_date)[:10],
             "city": "" if pd.isna(r.city) else str(r.city),
             "fo": "" if pd.isna(r.fo) else str(r.fo),
             "f": "S" if r.invoicing_strategy == STRATEGIC_LABEL else "F",
             "n": int(r.n)}
            for r in company_weekly.itertuples(index=False)
        ],
        "fleet_targets":     FLEET_TARGETS,   # weekly OH-per-car targets for the upside tool
        "hourly_car_data":   [],   # car-level hourly data not yet wired up
    }

    # 5a. Sanity check — total OH across the emitted (aggregated) rows should
    #     match the pre-aggregation car-level total.
    total_oh_emitted = sum(r["oh"] for r in data["fleet_performance"])
    print(f"[sanity] fleet_performance: {len(data['fleet_performance']):,} rows | "
          f"Total OH = {total_oh_emitted:,.0f}")

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
