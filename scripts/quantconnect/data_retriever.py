from QuantConnect import *
from QuantConnect.Research import QuantBook
from datetime import datetime
import pandas as pd
import base64
from IPython.display import HTML


# ============================================================
# Configuration
# ============================================================

ETF = "QQQ"

START_DATE = datetime(2010, 1, 1)
END_DATE   = datetime(2025, 12, 31)


# ============================================================
# Initialize QuantBook
# ============================================================

qb = QuantBook()

etf_symbol = qb.AddEquity(ETF).Symbol

universe = qb.AddUniverse(
    qb.Universe.ETF(etf_symbol)
)


# ============================================================
# Fetch ETF constituent history
# ============================================================

history = qb.UniverseHistory(
    universe,
    START_DATE,
    END_DATE
)


# ============================================================
# Convert to:
#
# date,ticker
#
# One row per constituent per observation date.
# ============================================================

records = []


for (universe_symbol, date), constituents in history.items():

    date = pd.Timestamp(date).strftime("%Y-%m-%d")

    for constituent in constituents:

        ticker = constituent.Symbol.Value

        records.append({
            "date": date,
            "ticker": ticker
        })


# ============================================================
# Create DataFrame
# ============================================================

df = pd.DataFrame(
    records,
    columns=["date", "ticker"]
)


# ============================================================
# Clean / normalize
# ============================================================

df["date"] = pd.to_datetime(df["date"])

df["ticker"] = (
    df["ticker"]
    .astype(str)
    .str.upper()
    .str.strip()
)


# Remove accidental duplicate observations
df = (
    df
    .drop_duplicates(["date", "ticker"])
    .sort_values(["date", "ticker"])
    .reset_index(drop=True)
)


# ============================================================
# Convert date back to YYYY-MM-DD for CSV
# ============================================================

df["date"] = df["date"].dt.strftime("%Y-%m-%d")


# ============================================================
# Export CSV
# ============================================================

csv_data = df.to_csv(index=False)

b64 = base64.b64encode(
    csv_data.encode()
).decode()

filename = f"{ETF}_constituents.csv"

href = (
    f'<a href="data:text/csv;base64,{b64}" '
    f'download="{filename}" '
    f'style="font-size:16px; font-weight:bold; color:#1E88E5;">'
    f'Click here to download {filename}'
    f'</a>'
)

display(HTML(href))


# ============================================================
# Preview
# ============================================================

display(df.head(50))

print(f"Rows: {len(df):,}")
print(f"Unique tickers: {df['ticker'].nunique():,}")
print(f"Date range: {df['date'].min()} -> {df['date'].max()}")