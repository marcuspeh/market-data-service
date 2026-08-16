# QuantConnect ETF Constituents Scripts

Tools for fetching, cleaning, and verifying the QuantConnect ETF constituents
dataset (SPY, QQQ, IWM) used as the seed data for `ConstituentsStore`.

## Layout

```
.
├── data_retriever.py          # QuantConnect notebook script (fetch raw CSVs)
├── clean_constituents.py      # Local script: clean & forward-fill the raw CSVs
├── verify_constituents.py     # Local script: verify cleaned data integrity
├── *_constituents.csv         # Raw downloads from QuantConnect
└── cleaned/                   # Output: parquet-per-year + merged CSV
    ├── SPY/<year>.parquet
    ├── QQQ/<year>.parquet
    ├── IWM/<year>.parquet
    └── *_constituents_cleaned.csv
```

## 1. Fetch raw data (`data_retriever.py`)

Runs inside a [QuantConnect Research](https://www.quantconnect.com/docs/research/overview)
notebook. Pulls daily ETF constituent history via `QuantBook.UniverseHistory`
and exports a `date,ticker` CSV download link.

Edit the `ETF` and `START_DATE` / `END_DATE` constants, then run all cells.
The notebook ends with a clickable link to download `QQQ_constituents.csv`
(or whichever symbol you set).

The three raw files used by this project:

| ETF | Source file | Notes |
|-----|-------------|-------|
| SPY | `SPY_constituents.csv` | single file from one notebook run |
| QQQ | `QQQ_constituents.csv` | single file from one notebook run |
| IWM | `IWM_constituents_2010_2015.csv`<br>`IWM_constituents_2016_2020.csv`<br>`IWM_constituents_2021_2025.csv` | split across 3 runs because IWM has ~2000 constituents per day and a single notebook export was too large |

Drop the downloaded CSV(s) into this directory before running the cleaning step.

## 2. Clean & produce parquet store (`clean_constituents.py`)

Local Python script. Reads the raw CSVs, cleans them, and writes:

- `cleaned/<ETF>/<year>.parquet` — yearly files matching the layout expected
  by `app/services/constituents_store.py` (`ConstituentsStore`).
- `cleaned/<ETF>_constituents_cleaned.csv` — one merged CSV per ETF.

```bash
uv run python scripts/quantconnect/clean_constituents.py
```

What it does:

1. Concatenates the raw files per ETF.
2. Drops rows with NaN dates/tickers, strips whitespace, uppercases, removes
   empty strings.
3. Drops duplicate `(date, ticker)` pairs.
4. Removes weekend dates (QuantConnect occasionally returns Saturdays/Sundays).
5. Forward-fills any missing business day by carrying forward the previous
   trading day's constituents (QuantConnect has gaps of 1-2 business days).
6. Extends the dataset to today by forward-filling the latest snapshot onto
   every business day from the last available date through today.
7. Filters to `[2010-01-01, today]` and sorts.
8. Writes yearly parquets (date typed as `date32[pyarrow]`) and a merged CSV.

The resulting `cleaned/` directory is drop-in compatible with `ConstituentsStore`:

```python
from app.services.constituents_store import ConstituentsStore
store = ConstituentsStore("scripts/quantconnect/cleaned")
snap = store.read_snapshot("SPY", date(2025, 12, 31))  # -> list[str]
```

## 3. Verify (`verify_constituents.py`)

Local Python script. Loads the cleaned parquets and runs ~40 integrity
checks across all three ETFs.

```bash
uv run python scripts/quantconnect/verify_constituents.py
```

Checks include:

- File inventory: 17 yearly parquets per ETF covering 2010-2026.
- Schema: `date` as `date32[pyarrow]`, `ticker` as string, no extra columns.
- Date coverage: every business day 2010-01-01 → today is present, no
  weekends, no missing dates.
- Data quality: no NaN, no empty tickers, no duplicate `(date, ticker)`
  pairs, all tickers uppercase, no whitespace.
- Counts: SPY ≈ 500, QQQ ≈ 100, IWM ≈ 2000 constituents per day (allowing
  ±5%). Reconciles exactly with raw counts on sampled dates.
- Continuity: daily constituent-count change is small (no large forward-fill
  artifacts).
- Spot checks via `ConstituentsStore`: AAPL/MSFT/JPM in SPY, AAPL/MSFT in
  QQQ, no AAPL in IWM, no leveraged ETFs (TQQQ) in SPY.
- `ConstituentsStore` API: `list_years`, `list_snapshot_dates`,
  `read_snapshot`, `read_range`.
- CSV ↔ parquet equivalence.

Any check that fails prints `[FAIL]` and a brief detail; passing checks
print `[PASS]`.

## Regenerating the cleaned data

If you re-run `data_retriever.py` and get newer raw CSVs (e.g. extending
beyond 2025), drop them into this directory and re-run `clean_constituents.py`.
The script picks up whatever raw files exist for each ETF based on its
filename prefix.
