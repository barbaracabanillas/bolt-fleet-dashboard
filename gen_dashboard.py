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


def aggregate_daily_by_cohort(m30_df: pd.DataFrame, agreements: dict) -> pd.DataFrame:
    """
    Convert daily company-level M30 data into the same shape as fleet_performance
    (cohort-tagged), but keyed by day_date instead of week_date.
    Used by the 'Day' granularity button in the dashboard.
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
        })

    result = pd.DataFrame(rows)
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
        out.append({
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
        })
    return out


def main():
    print("=" * 60)
    print(f"Dashboard build started at {datetime.datetime.utcnow().isoformat()}Z")
    print("=" * 60)

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

    # 4a. Merge distinct active-drivers per company-week (for the metric selector)
    drivers_weekly = fetch_drivers_weekly()
    dw_lookup = {
        (str(r.week_start)[:10], str(r.company_id)): int(r.active_drivers)
        for r in drivers_weekly.itertuples(index=False)
    }
    # 4a-bis. Finished rides per (week, city) from the canonical orders table.
    #         order_city_id → city_name via the hourly table's own mapping.
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

    # Exact distinct-company count per (week, city, FO, strategy), computed while
    # company_id is still present (before cohorts are collapsed) so a free-floating
    # company with both branded and non-branded cars is counted ONCE. Feeds the
    # "Active companies" KPI. Empty frame keeps the emit below simple.
    company_weekly = pd.DataFrame(columns=["week_date", "city", "fo", "invoicing_strategy", "n"])

    if not weekly_df.empty:
        wk = weekly_df["week_date"].astype(str).str[:10]
        comp = weekly_df["company_id"].astype(str)
        # Attach the company's active-driver count to EACH of its cohort rows
        # (same value per company — matches the previous per-company behaviour).
        weekly_df["active_drivers"] = [dw_lookup.get(k, 0) for k in zip(wk, comp)]

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

    # 4b. Aggregate daily OH by company+cohort (for 'Day' granularity button)
    daily_df = aggregate_daily_by_cohort(m30_df, agreements)

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
