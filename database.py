import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "cache.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS etf_constituents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                etf_symbol TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                weight REAL NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_etf_symbol ON etf_constituents(etf_symbol)")
        conn.commit()

def save_constituents(etf_symbol: str, constituents: List[Dict]):
    with get_db_connection() as conn:
        # Clear old cache for this symbol
        conn.execute("DELETE FROM etf_constituents WHERE etf_symbol = ?", (etf_symbol,))
        
        # Insert new data
        now = datetime.now().isoformat()
        for c in constituents:
            conn.execute("""
                INSERT INTO etf_constituents (etf_symbol, ticker, name, weight, fetched_at)
                VALUES (?, ?, ?, ?, ?)
            """, (etf_symbol, c['ticker'], c['name'], c['weight'], now))
        conn.commit()

def get_cached_constituents(etf_symbol: str, max_age_days: int = 7) -> Optional[List[Dict]]:
    with get_db_connection() as conn:
        # Check if we have any data and how old it is
        cursor = conn.execute("""
            SELECT fetched_at FROM etf_constituents 
            WHERE etf_symbol = ? 
            LIMIT 1
        """, (etf_symbol,))
        row = cursor.fetchone()
        
        if not row:
            return None
            
        fetched_at = datetime.fromisoformat(row['fetched_at'])
        if datetime.now() - fetched_at > timedelta(days=max_age_days):
            return None
            
        # If valid, return all constituents
        cursor = conn.execute("""
            SELECT ticker, name, weight FROM etf_constituents 
            WHERE etf_symbol = ?
        """, (etf_symbol,))
        return [dict(row) for row in cursor.fetchall()]
