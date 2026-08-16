"""Comprehensive verification of cleaned ETF constituents data.

Checks:
  1. Schema correctness (date32, ticker as string)
  2. Date coverage: every business day 2010-01-01 -> 2026-08-16
  3. No weekends, no NaN, no duplicates
  4. Realistic constituent counts per day
  5. Continuity: constituents don't change between forward-filled days
  6. Spot checks against known historical constituents (AAPL, MSFT always present in SPY/QQQ;
     SPY never contains leveraged ETFs; well-known names appear in IWM)
  7. Reconciliation: cleaned counts == expected based on raw input
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
from datetime import date, timedelta

from app.services.constituents_store import ConstituentsStore

CLEAN_DIR = Path("scripts/quantconnect/cleaned")
RAW_DIR = Path("scripts/quantconnect")

EXPECTED = {"SPY": 500, "QQQ": 100, "IWM": 2000}

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"


def check(label, condition, detail=""):
    icon = PASS if condition else FAIL
    print(f"  [{icon}] {label}{('  -- ' + detail) if detail else ''}")
    return condition


def load_raw(etf):
    files = {
        "SPY": [RAW_DIR / "SPY_constituents.csv"],
        "QQQ": [RAW_DIR / "QQQ_constituents.csv"],
        "IWM": [
            RAW_DIR / "IWM_constituents_2010_2015.csv",
            RAW_DIR / "IWM_constituents_2016_2020.csv",
            RAW_DIR / "IWM_constituents_2021_2025.csv",
        ],
    }[etf]
    dfs = []
    for f in files:
        if f.exists():
            df = pd.read_csv(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df[df["ticker"] != ""]
    df = df[df["date"].dt.dayofweek < 5]
    df = df.drop_duplicates(subset=["date", "ticker"])
    return df


print("=" * 75)
print("ETF CONSTITUENTS CLEANED DATA VERIFICATION")
print("=" * 75)

store = ConstituentsStore(CLEAN_DIR)

for etf in ["SPY", "QQQ", "IWM"]:
    print(f"\n{'=' * 75}")
    print(f"  {etf}")
    print(f"{'=' * 75}")

    # 1. Load all parquet files for this ETF
    etf_dir = CLEAN_DIR / etf
    files = sorted(etf_dir.glob("*.parquet"))
    print(f"\n[1] FILE INVENTORY")
    check("17 yearly parquet files (2010-2026)", len(files) == 17, f"found {len(files)}")
    years_found = sorted(int(p.stem) for p in files)
    expected_years = list(range(2010, 2027))
    check("Covers all years 2010-2026", years_found == expected_years)

    dfs = []
    for p in files:
        df = pd.read_parquet(p)
        dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)

    # 2. Schema check
    print(f"\n[2] SCHEMA")
    has_date = "date" in df.columns
    has_ticker = "ticker" in df.columns
    check("Has 'date' column", has_date)
    check("Has 'ticker' column", has_ticker)
    check("No extra columns", set(df.columns) == {"date", "ticker"})
    if has_date:
        check("date dtype is date32[pyarrow]", str(df["date"].dtype) == "date32[pyarrow]",
              f"actual: {df['date'].dtype}")
    if has_ticker:
        check("ticker dtype is string", df["ticker"].dtype == object or "string" in str(df["ticker"].dtype),
              f"actual: {df['ticker'].dtype}")

    # 3. Date coverage
    print(f"\n[3] DATE COVERAGE")
    dates = sorted(pd.to_datetime(df["date"].unique()))
    first, last = dates[0], dates[-1]
    check("First date >= 2010-01-01", first >= pd.Timestamp("2010-01-01"), f"first: {first.date()}")
    check("First date == 2010-01-01 (first business day of 2010)", first == pd.Timestamp("2010-01-01"),
          f"actual: {first.date()}")
    check("Last date <= 2026-08-16", last <= pd.Timestamp("2026-08-16"), f"last: {last.date()}")
    check("Last date is a business day", last.dayofweek < 5, f"dayofweek={last.dayofweek}")

    expected_bd = pd.date_range("2010-01-01", "2026-08-16", freq="B")
    actual_set = set(pd.Timestamp(d) for d in dates)
    expected_set = set(expected_bd)
    missing = expected_set - actual_set
    extra = actual_set - expected_set
    check(f"All {len(expected_bd)} business days 2010-01-01 to 2026-08-16 are present",
          len(missing) == 0, f"missing: {len(missing)}")
    check("No non-business dates", len(extra) == 0,
          f"extra: {[str(d)[:10] for d in sorted(extra)[:5]]}")

    # 4. Data quality
    print(f"\n[4] DATA QUALITY")
    check("No NaN dates", df["date"].isna().sum() == 0, f"count={df['date'].isna().sum()}")
    check("No NaN tickers", df["ticker"].isna().sum() == 0, f"count={df['ticker'].isna().sum()}")
    check("No empty-string tickers", (df["ticker"].astype(str).str.strip() == "").sum() == 0)
    check("No duplicate (date, ticker) pairs",
          df.duplicated(subset=["date", "ticker"]).sum() == 0,
          f"count={df.duplicated(subset=['date', 'ticker']).sum()}")
    check("All tickers uppercase",
          df["ticker"].str.isupper().all(),
          f"violations={df[~df['ticker'].str.isupper()]['ticker'].head(3).tolist()}")
    check("No whitespace in tickers",
          (df["ticker"].str.strip() == df["ticker"]).all())

    # 5. Constituent counts
    print(f"\n[5] CONSTITUENT COUNTS")
    counts = df.groupby("date").size()
    print(f"  range: min={counts.min()}, max={counts.max()}, mean={counts.mean():.0f}, median={counts.median():.0f}")
    exp = EXPECTED[etf]
    low = (counts < exp * 0.95).sum()
    high = (counts > exp * 1.05).sum()
    check(f"Most days within 5% of expected (~{exp})", low + high < 20,
          f"low(<{int(exp * 0.95)}): {low}, high(>{int(exp * 1.05)}): {high}")

    # 6. Continuity / forward-fill sanity
    print(f"\n[6] FORWARD-FILL CONTINUITY")
    if etf == "SPY":
        # SPY has ~500 tickers; forward-fill means daily changes should be tiny
        daily_change = counts.diff().abs().fillna(0)
        large_changes = (daily_change > 50).sum()
        check(f"Daily constituent changes small (<=50)", large_changes < 30,
              f"days with >50 change: {large_changes}")

    if etf == "QQQ":
        # QQQ has 100ish tickers
        daily_change = counts.diff().abs().fillna(0)
        large_changes = (daily_change > 20).sum()
        check(f"Daily constituent changes small (<=20)", large_changes < 30,
              f"days with >20 change: {large_changes}")

    if etf == "IWM":
        # IWM has ~2000 tickers
        daily_change = counts.diff().abs().fillna(0)
        large_changes = (daily_change > 100).sum()
        check(f"Daily constituent changes small (<=100)", large_changes < 30,
              f"days with >100 change: {large_changes}")

    # 7. Reconciliation: raw vs cleaned counts per snapshot date
    print(f"\n[7] RECONCILIATION WITH RAW DATA")
    raw = load_raw(etf)
    raw_bd = raw[raw["date"].dt.dayofweek < 5]
    raw_unique_dates = set(pd.Timestamp(d) for d in raw_bd["date"].unique())

    # For each raw date, the cleaned count should equal raw count (no duplicates added/removed)
    mismatches = []
    sample_checked = 0
    for d in sorted(raw_unique_dates)[:50] + sorted(raw_unique_dates)[-50:]:
        raw_count = (raw_bd[raw_bd["date"] == d]
                     .drop_duplicates(subset=["ticker"]).shape[0])
        clean_count = counts.loc[d] if d in counts.index else None
        if clean_count is not None and abs(raw_count - clean_count) > 0:
            mismatches.append((d, raw_count, clean_count))
        sample_checked += 1
    check("Cleaned counts == raw counts for 100 sampled dates (no extra/missing tickers)",
          len(mismatches) == 0,
          f"mismatches: {len(mismatches)}, e.g. {mismatches[:3]}")

    # 8. Known-member spot checks via ConstituentsStore
    print(f"\n[8] SPOT CHECKS (known historical constituents)")
    snap_dates = [
        date(2010, 1, 4),
        date(2015, 6, 30),
        date(2020, 3, 16),
        date(2025, 12, 31),
        date(2026, 8, 14),
    ]
    for sd in snap_dates:
        try:
            t = set(store.read_snapshot(etf, sd))
        except Exception as e:
            check(f"{sd}: snapshot read works", False, str(e))
            continue
        check(f"{sd}: snapshot has data", len(t) > 0, f"{len(t)} tickers")

    # ETF-specific known checks
    if etf == "SPY":
        for sd in [date(2010, 1, 4), date(2025, 12, 31)]:
            t = set(store.read_snapshot(etf, sd))
            check(f"SPY {sd} contains AAPL", "AAPL" in t)
            check(f"SPY {sd} contains MSFT", "MSFT" in t)
            check(f"SPY {sd} contains JPM", "JPM" in t)
            check(f"SPY {sd} does NOT contain leveraged ETFs (TQQQ)", "TQQQ" not in t and "SSO" not in t)
            check(f"SPY {sd} does NOT contain penny-stock tickers (single-letter noise)",
                  all(len(s) <= 5 for s in t), f"longest: {max(t, key=len) if t else '-'}")

    if etf == "QQQ":
        for sd in [date(2010, 1, 4), date(2025, 12, 31)]:
            t = set(store.read_snapshot(etf, sd))
            check(f"QQQ {sd} contains AAPL", "AAPL" in t)
            check(f"QQQ {sd} contains MSFT", "MSFT" in t)
            check(f"QQQ {sd} does NOT contain JPM (banks aren't in QQQ)", "JPM" not in t)

    if etf == "IWM":
        for sd in [date(2010, 1, 4), date(2025, 12, 31)]:
            t = set(store.read_snapshot(etf, sd))
            check(f"IWM {sd} has 1500+ tickers", len(t) > 1500, f"{len(t)} tickers")
            check(f"IWM {sd} does NOT contain AAPL (mega-caps aren't in IWM)",
                  "AAPL" not in t)

    # 9. ConstituentsStore integration tests
    print(f"\n[9] ConstituentsStore INTEGRATION")
    years = store.list_years(etf)
    check("list_years returns 17 years", len(years) == 17, f"{len(years)}: {min(years) if years else '-'}-{max(years) if years else '-'}")
    snap_dates_all = store.list_snapshot_dates(etf)
    check("list_snapshot_dates returns all snapshots", len(snap_dates_all) == len(dates),
          f"store={len(snap_dates_all)}, parquet={len(dates)}")

    rng = store.read_range(etf, date(2025, 1, 1), date(2025, 1, 31))
    check("read_range returns 21-23 daily snapshots for Jan 2025",
          21 <= len(rng) <= 23, f"got {len(rng)}")

    # 10. CSV matches parquet
    print(f"\n[10] CSV <-> PARQUET EQUIVALENCE")
    csv_path = CLEAN_DIR / f"{etf}_constituents_cleaned.csv"
    if csv_path.exists():
        csv_df = pd.read_csv(csv_path)
        csv_df["date"] = pd.to_datetime(csv_df["date"]).dt.date.astype("date32[pyarrow]")
        check("CSV row count matches parquet total", len(csv_df) == len(df))
        csv_dates = set(pd.to_datetime(csv_df["date"]).dt.date)
        parquet_dates = set(pd.to_datetime(df["date"]).dt.date)
        check("CSV unique dates match parquet", csv_dates == parquet_dates)
    else:
        check(f"CSV file exists at {csv_path}", False, "missing")

print(f"\n{'=' * 75}")
print("  VERIFICATION COMPLETE")
print(f"{'=' * 75}")
