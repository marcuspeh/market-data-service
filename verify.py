import requests
import sqlite3
import os
import time
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8001"
DB_PATH = "cache.db"

def test_health():
    print("Testing /health...")
    resp = requests.get(f"{BASE_URL}/health")
    assert resp.status_code == 200
    print("✓ Health OK")

def test_spy_constituents():
    print("Testing /constituents?symbol=SPY...")
    # First call (likely external)
    start_time = time.time()
    resp = requests.get(f"{BASE_URL}/constituents?symbol=SPY")
    duration = time.time() - start_time
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "SPY"
    assert len(data["constituents"]) > 0
    print(f"✓ Received {len(data['constituents'])} constituents in {duration:.2f}s (Source: {data.get('source')})")
    
    # Second call (should be cache)
    start_time = time.time()
    resp = requests.get(f"{BASE_URL}/constituents?symbol=SPY")
    duration = time.time() - start_time
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "cache"
    print(f"✓ Received cached data in {duration:.4f}s")

def test_unsupported_symbol():
    print("Testing unsupported symbol...")
    resp = requests.get(f"{BASE_URL}/constituents?symbol=VOO")
    assert resp.status_code == 400
    print("✓ Correctly rejected VOO")

def verify_db_population():
    print("Verifying SQLite population...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT count(*) FROM etf_constituents WHERE etf_symbol = 'SPY'")
    count = cursor.fetchone()[0]
    assert count > 0
    print(f"✓ Found {count} rows in database")
    conn.close()

def verify_cache_invalidation():
    print("Verifying cache invalidation logic...")
    conn = sqlite3.connect(DB_PATH)
    # Manually set the timestamp to 8 days ago
    old_date = (datetime.now() - timedelta(days=8)).isoformat()
    conn.execute("UPDATE etf_constituents SET fetched_at = ? WHERE etf_symbol = 'SPY'", (old_date,))
    conn.commit()
    conn.close()
    
    print("Requesting after manual expiration...")
    resp = requests.get(f"{BASE_URL}/constituents?symbol=SPY")
    assert resp.status_code == 200
    assert resp.json()["source"] == "external"
    print("✓ Cache correctly invalidated and refreshed")

if __name__ == "__main__":
    # Note: This script assumes the server is running on localhost:8001
    try:
        test_health()
        test_spy_constituents()
        test_unsupported_symbol()
        verify_db_population()
        verify_cache_invalidation()
        print("\nAll tests passed!")
    except Exception as e:
        print(f"\nTest failed: {e}")
