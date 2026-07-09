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
LOOKBACK_DAYS_WEEKLY = 365   # how many days back to fetch car-level weekly data
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
# NOTE: kept as "Free Floating" (not "Non-strategic") so the existing dashboard
# charts keep working without JS changes. The classification logic is the new one
# (strategic only if the Sheet says so); only the DISPLAY term is deferred to a
# later "title-level" rename to Non-strategic.
NONSTRATEGIC_LABEL  = "Free Floating"
NONSTRATEGIC_COHORT = "All Free floating (Branded TBD)"

# Cohorts that can come from the Sheet (for strategic companies), plus the
# catch-all used for every non-strategic company.
VALID_COHORTS   = ["Fleet Agreement", "Locked Supply", "Branding", "No Agreement", NONSTRATEGIC_COHORT]


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
        COUNT(DISTINCT car_id)                                                 AS active_cars
    FROM main.int_models.int_driver_car_city_hour_earnings_and_fees_metrics_eur_local
    WHERE country_id = 67   -- Spain (all cities)
      AND calendar_date_local >= CURRENT_DATE - INTERVAL {LOOKBACK_DAYS_M30} DAYS
      {_CUTOFF_CLAUSE}
    GROUP BY 1, 2
    """
    df = run_query(sql)
    print(f"[m30] Fetched {len(df):,} rows")
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
        ag  = agreements.get(cid, {"c": NONSTRATEGIC_COHORT, "f": NONSTRATEGIC_LABEL})
        rows.append({
            "day_date":           str(row["date"]),
            "company_id":         cid,
            "cohort":             ag["c"],
            "invoicing_strategy": ag["f"],
            "online_hours":       row["online_hours"],
            "earnings_eur":       row["earnings_eur"],
            "gmv_eur":            row["gmv_eur"],
        })

    result = pd.DataFrame(rows)
    result = (
        result
        .groupby(["day_date", "company_id", "cohort", "invoicing_strategy"], as_index=False)
        .agg({"online_hours": "sum", "earnings_eur": "sum", "gmv_eur": "sum"})
    )
    print(f"[daily] {len(result):,} company-day rows for 'Day' granularity view")
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
    n_strategic = 0

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
            # Strategic → trust the Sheet's cohort (this is where Branding,
            # Fleet Agreement, Locked Supply and No Agreement come from)
            agreements[cid_str] = {
                "n": info.get("name", ""),
                "f": STRATEGIC_LABEL,
                "c": info.get("cohort") or "No Agreement",
                "g": fo_group,
                "city": city,
            }
            n_strategic += 1
        else:
            # Everything else (incl. companies absent from the Sheet) → Non-strategic.
            # We can't yet tell branded from non-branded here, so use the catch-all.
            agreements[cid_str] = {
                "n": (info or {}).get("name", ""),
                "f": NONSTRATEGIC_LABEL,
                "c": NONSTRATEGIC_COHORT,
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
            ag = {"c": NONSTRATEGIC_COHORT, "f": NONSTRATEGIC_LABEL}
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
        f"const EMBEDDED_AGREEMENTS = {agreements_json};",
    )

    return html


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def _compact_perf(df: pd.DataFrame, date_col: str) -> list:
    """
    Compact a performance DataFrame into the minimal rows the dashboard needs:
        {"w": date, "ci": company_id, "oh": online_hours, "e": earnings_eur}
    Cohort / fleet type / FO / city are derived client-side from EMBEDDED_AGREEMENTS,
    so they are NOT repeated per row. Short keys + rounded numbers keep the embedded
    JSON small (this is what got the page from ~55 MB down to a few MB).
    """
    if df is None or df.empty:
        return []
    out = []
    for row in df.itertuples(index=False):
        d = row._asdict()
        out.append({
            "w":  str(d[date_col])[:10],
            "ci": str(d["company_id"]),
            "oh": round(float(d.get("online_hours") or 0)),
            "e":  round(float(d.get("earnings_eur") or 0)),
        })
    return out


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

    # 3. Build EMBEDDED_AGREEMENTS (one entry per company)
    agreements = build_embedded_agreements(car_df, cohort_map, fo_map)

    # 4. Aggregate weekly OH by company+cohort (for dashboard charts)
    weekly_df = aggregate_weekly_by_cohort(car_df, agreements)

    # 4b. Aggregate daily OH by company+cohort (for 'Day' granularity button)
    daily_df = aggregate_daily_by_cohort(m30_df, agreements)

    # 5. Convert dataframes to JSON-serialisable dicts
    data = {
        "generated_at":      datetime.datetime.utcnow().isoformat() + "Z",
        "agreements":        agreements,
        "fleet_performance": _compact_perf(weekly_df, "week_date"),
        "daily_performance": _compact_perf(daily_df,  "day_date"),
        "hourly_car_data":   [],   # car-level hourly data not yet wired up
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
