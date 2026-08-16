import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta

DATA_DIR = Path("scripts/quantconnect")
CLEAN_DIR = DATA_DIR / "cleaned"
CLEAN_DIR.mkdir(exist_ok=True)

RAW_FILES = {
    "IWM": [
        DATA_DIR / "IWM_constituents_2010_2015.csv",
        DATA_DIR / "IWM_constituents_2016_2020.csv",
        DATA_DIR / "IWM_constituents_2021_2025.csv",
    ],
    "QQQ": [DATA_DIR / "QQQ_constituents.csv"],
    "SPY": [DATA_DIR / "SPY_constituents.csv"],
}

START_DATE = pd.Timestamp("2010-01-01")
END_DATE = pd.Timestamp("2026-08-16")

EXPECTED_COUNTS = {"SPY": 500, "QQQ": 100, "IWM": 2000}


def load_raw(etf, files):
    dfs = []
    for f in files:
        if f.exists():
            df = pd.read_csv(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df)
    df = pd.concat(dfs, ignore_index=True)
    df = df.dropna(subset=["date", "ticker"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df = df[df["ticker"] != ""]
    df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)
    return df


def remove_weekends(df):
    mask = df["date"].dt.dayofweek < 5
    removed = (~mask).sum()
    print(f"  Removing {removed} weekend rows...")
    return df[mask].reset_index(drop=True)


def business_dates_between(start, end):
    return pd.date_range(start=start, end=end, freq="B")


def forward_fill_missing_dates(df):
    all_dates = sorted(df["date"].unique())
    if len(all_dates) == 0:
        return df

    data_end = pd.Timestamp(all_dates[-1])
    all_bd = business_dates_between(START_DATE, data_end)
    existing_dates = set(pd.Timestamp(d) for d in all_dates)

    gaps = []
    last_valid = pd.Timestamp(all_dates[0])
    for d in all_bd:
        d_ts = pd.Timestamp(d)
        if d_ts in existing_dates:
            last_valid = d_ts
        else:
            gaps.append((d_ts, last_valid))

    if len(gaps) > 0:
        print(f"  Found {len(gaps)} missing business dates; forward-filling...")
        fill_dfs = []
        for gap_date, src_date in gaps:
            src_rows = df[df["date"] == src_date].copy()
            src_rows["date"] = gap_date
            fill_dfs.append(src_rows)
        fill_dfs.append(df)
        df = pd.concat(fill_dfs, ignore_index=True)
        df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
        df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    return df


def extend_to_2026(df):
    all_dates = sorted(df["date"].unique())
    if len(all_dates) == 0:
        return df
    last_date = pd.Timestamp(all_dates[-1])
    if last_date >= END_DATE:
        return df

    last_snapshot = df[df["date"] == last_date].copy()
    target_dates = business_dates_between(last_date + timedelta(days=1), END_DATE)

    if len(target_dates) == 0:
        print(f"  No dates to extend for 2026...")
        return df

    print(f"  Extending {len(target_dates)} business dates: {last_date.date()} -> {END_DATE.date()}...")
    extend_rows = []
    for d in target_dates:
        rows = last_snapshot.copy()
        rows["date"] = pd.Timestamp(d)
        extend_rows.append(rows)
    df = pd.concat([df] + extend_rows, ignore_index=True)
    df = df.drop_duplicates(subset=["date", "ticker"], keep="first")
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)
    return df


def filter_date_range(df):
    mask = (df["date"] >= START_DATE) & (df["date"] <= END_DATE)
    return df[mask].reset_index(drop=True)


def print_stats(df, etf):
    dates = sorted(df["date"].unique())
    counts = df.groupby("date").size()
    print(f"\n  Stats for {etf}:")
    print(f"    Rows: {len(df):,}")
    print(f"    Date range: {dates[0].date()} to {dates[-1].date()}")
    print(f"    Unique dates: {len(dates):,}")
    print(f"    Unique tickers: {df['ticker'].nunique():,}")
    print(f"    Constituents/day: min={counts.min()}, max={counts.max()}, mean={counts.mean():.0f}, median={counts.median():.0f}")
    expected = EXPECTED_COUNTS.get(etf)
    if expected:
        low = (counts < expected * 0.95).sum()
        if low > 0:
            print(f"    Dates with <95% of expected ({int(expected * 0.95)}): {low}")


def save_yearly_parquets(df, etf):
    etf_dir = CLEAN_DIR / etf
    etf_dir.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date.astype("date32[pyarrow]")
    for year, group in df.groupby(df["date"].dt.year):
        out_path = etf_dir / f"{year}.parquet"
        group.to_parquet(out_path, index=False, engine="pyarrow")
        print(f"    Wrote {out_path} ({len(group):,} rows)")


def save_merged_csv(df, etf):
    out_path = CLEAN_DIR / f"{etf}_constituents_cleaned.csv"
    df_copy = df.copy()
    df_copy["date"] = df_copy["date"].dt.strftime("%Y-%m-%d")
    df_copy.to_csv(out_path, index=False)
    print(f"    Wrote {out_path} ({len(df_copy):,} rows)")


print("=" * 70)
print("CLEANING ETF CONSTITUENTS DATA")
print("=" * 70)

all_cleaned = {}

for etf, files in RAW_FILES.items():
    print(f"\n--- Processing {etf} ---")
    df = load_raw(etf, files)
    print(f"  Loaded {len(df):,} raw rows")

    df = remove_weekends(df)
    df = forward_fill_missing_dates(df)
    df = extend_to_2026(df)
    df = filter_date_range(df)
    df = df.sort_values(["date", "ticker"]).reset_index(drop=True)

    print_stats(df, etf)

    save_yearly_parquets(df, etf)
    save_merged_csv(df, etf)

    all_cleaned[etf] = df

print(f"\n{'=' * 70}")
print("VALIDATION")
print("=" * 70)
print("\nVerifying cleaned data integrity:")
for etf, df in all_cleaned.items():
    print(f"\n  {etf}:")
    dates = sorted(df["date"].unique())
    weekend_count = sum(1 for d in dates if pd.Timestamp(d).dayofweek >= 5)
    print(f"    Weekend dates: {weekend_count}")
    print(f"    NaN date: {df['date'].isna().sum()}")
    print(f"    NaN ticker: {df['ticker'].isna().sum()}")
    print(f"    Duplicates (date,ticker): {df.duplicated(subset=['date', 'ticker']).sum()}")
    bdates = business_dates_between(START_DATE, END_DATE)
    print(f"    Business dates covered {START_DATE.date()}-{END_DATE.date()}: {len(bdates):,}")
    print(f"    Actual unique dates: {len(dates):,}")
    missing_count = len(set(bdates) - set(pd.Timestamp(d) for d in dates))
    print(f"    Missing business dates: {missing_count}")
    print(f"    First date: {dates[0].date()}, Last date: {dates[-1].date()}")

print(f"\nDone! Cleaned data saved to: {CLEAN_DIR.resolve()}")
