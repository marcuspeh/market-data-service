import pandas as pd
import requests
import io
import logging

logger = logging.getLogger(__name__)

SPY_URL = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"

def fetch_spy_constituents():
    logger.info(f"Fetching SPY constituents from {SPY_URL}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    response = requests.get(SPY_URL, headers=headers)
    response.raise_for_status()
    
    # Load the excel file
    # SSGA Excel files typically have metadata in the first few rows.
    # We need to find the actual table start.
    # Looking at standard SSGA holdings files, they often start the header around row 4 or 5.
    
    df = pd.read_excel(io.BytesIO(response.content), skiprows=4)
    
    # Clean up column names (strip whitespace)
    df.columns = [str(col).strip() for col in df.columns]
    
    # Expected columns: 'Ticker', 'Name', 'Weight' (or similar)
    # Based on SSGA SPY holdings format:
    # 'Ticker', 'Name', 'Weight' are usually there.
    
    required_cols = ['Ticker', 'Name', 'Weight']
    for col in required_cols:
        if col not in df.columns:
            # Fallback check for case-insensitive or slight variations
            matches = [c for c in df.columns if col.lower() in c.lower()]
            if matches:
                df.rename(columns={matches[0]: col}, inplace=True)
            else:
                raise ValueError(f"Required column '{col}' not found in Excel file. Found: {df.columns.tolist()}")

    # Filter out empty rows or footer rows
    df = df.dropna(subset=['Ticker', 'Weight'])
    
    # Convert to list of dicts
    constituents = []
    for _, row in df.iterrows():
        try:
            constituents.append({
                "ticker": str(row['Ticker']).strip(),
                "name": str(row['Name']).strip(),
                "weight": float(row['Weight'])
            })
        except (ValueError, TypeError):
            continue
            
    return constituents

if __name__ == "__main__":
    # Test script
    logging.basicConfig(level=logging.INFO)
    try:
        data = fetch_spy_constituents()
        print(f"Fetched {len(data)} constituents")
        print(data[:5])
    except Exception as e:
        print(f"Error: {e}")
