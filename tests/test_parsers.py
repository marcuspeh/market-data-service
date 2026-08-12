"""Tests for the per-provider holdings parsers."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from app.services.parsers.ishares import parse as ishares_parse
from app.services.parsers.ssga import parse as ssga_parse


def _xlsx_bytes(df: pd.DataFrame, skiprows: int = 0) -> bytes:
    """Build a real .xlsx that mimics the SSGA layout: `skiprows` blank
    metadata rows, then a header row, then data. The SSGA parser calls
    ``pd.read_excel(skiprows=skiprows)`` which will then read the row
    immediately after the skiprows as the column header."""
    buf = io.BytesIO()
    # Write the DataFrame with its real headers and data, then prepend
    # skiprows blank rows so the resulting layout is:
    #   blank rows  (rows 0..skiprows-1)
    #   header row  (row skiprows)
    #   data rows   (row skiprows+1..)
    rows: list[list[str]] = (
        [[""] * len(df.columns) for _ in range(skiprows)]
        + [list(df.columns)]
        + df.astype(str).values.tolist()
    )
    sheet = pd.DataFrame(rows)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        sheet.to_excel(writer, index=False, header=False)
    buf.seek(0)
    return buf.read()


class TestSsgaParser:
    def test_happy_path(self):
        df = pd.DataFrame(
            {
                "Ticker": ["NVDA", "AAPL", "MSFT"],
                "Name": ["NVIDIA CORP", "APPLE INC", "MICROSOFT"],
                "Weight": [8.13, 6.88, 5.56],
            }
        )
        result = ssga_parse(_xlsx_bytes(df, skiprows=4))
        assert len(result) == 3
        assert result[0] == {"ticker": "NVDA", "name": "NVIDIA CORP", "weight": 8.13}

    def test_skips_blank_and_footer_rows(self):
        df = pd.DataFrame(
            {
                "Ticker": ["NVDA", None, "AAPL"],
                "Name": ["NVIDIA CORP", "FOOTER NOISE", "APPLE INC"],
                "Weight": [8.13, None, 6.88],
            }
        )
        result = ssga_parse(_xlsx_bytes(df, skiprows=4))
        assert len(result) == 2
        assert [r["ticker"] for r in result] == ["NVDA", "AAPL"]

    def test_column_name_fallback_case_insensitive(self):
        """If SSGA renames 'Weight' to 'Portfolio Weight', the parser
        should still find it."""
        df = pd.DataFrame(
            {
                "Ticker": ["NVDA"],
                "Name": ["NVIDIA CORP"],
                "Portfolio Weight": [8.13],
            }
        )
        result = ssga_parse(_xlsx_bytes(df, skiprows=4))
        assert result[0]["weight"] == 8.13

    def test_missing_required_column_raises(self):
        df = pd.DataFrame({"Ticker": ["NVDA"], "Name": ["NVIDIA CORP"]})
        with pytest.raises(ValueError, match="Required column 'Weight'"):
            ssga_parse(_xlsx_bytes(df, skiprows=4))

    def test_strips_whitespace_around_ticker_and_name(self):
        df = pd.DataFrame(
            {
                "Ticker": ["  NVDA  ", "AAPL\t"],
                "Name": [" NVIDIA CORP ", " APPLE INC "],
                "Weight": [8.13, 6.88],
            }
        )
        result = ssga_parse(_xlsx_bytes(df, skiprows=4))
        assert result[0]["ticker"] == "NVDA"
        assert result[0]["name"] == "NVIDIA CORP"
        assert result[1]["ticker"] == "AAPL"


class TestIsharesParser:
    def test_happy_path(self):
        csv = (
            "iShares Russell 2000 ETF\n"
            'Fund Holdings as of,"Aug 07, 2026"\n'
            'Inception Date,"May 22, 2000"\n'
            'Shares Outstanding,"270,650,000.00"\n'
            'Stock,"-"\n'
            'Bond,"-"\n'
            'Cash,"-"\n'
            'Other,"-"\n'
            "\n"
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n"
            '"NVDA","NVIDIA CORP","Information Technology","Equity","1","8.56"\n'
            '"AAPL","APPLE INC","Information Technology","Equity","2","7.27"\n'
        )
        result = ishares_parse(csv.encode("utf-8"))
        assert len(result) == 2
        assert result[0] == {"ticker": "NVDA", "name": "NVIDIA CORP", "weight": 8.56}
        assert result[1]["weight"] == 7.27

    def test_strips_dashes_and_blanks(self):
        csv = (
            "header\n"
            'meta1,"v"\n'
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n"
            '"NVDA","NVIDIA CORP","Tech","Equity","1","8.56"\n'
            '"-","CASH LINE","Cash","Cash","2","0.5"\n'
            '"","BLANK","x","y","3","0.1"\n'
        )
        result = ishares_parse(csv.encode("utf-8"))
        # Only the real ticker survives; "-" and "" are dropped.
        assert len(result) == 1
        assert result[0]["ticker"] == "NVDA"

    def test_handles_utf8_bom(self):
        csv = (
            "\ufeffiShares ETF\n"
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n"
            '"NVDA","NVIDIA CORP","Tech","Equity","1","8.56"\n'
        )
        result = ishares_parse(csv.encode("utf-8-sig"))
        assert result[0]["ticker"] == "NVDA"

    def test_handles_percent_suffix_in_weight(self):
        csv = (
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n"
            '"NVDA","NVIDIA CORP","Tech","Equity","1","8.56%"\n'
        )
        result = ishares_parse(csv.encode("utf-8"))
        assert result[0]["weight"] == 8.56

    def test_skips_malformed_weight(self):
        csv = (
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n"
            '"NVDA","NVIDIA CORP","Tech","Equity","1","8.56"\n'
            '"BAD","BAD CORP","Tech","Equity","2","not-a-number"\n'
            '"OK","OK CORP","Tech","Equity","3","1.0"\n'
        )
        result = ishares_parse(csv.encode("utf-8"))
        assert [r["ticker"] for r in result] == ["NVDA", "OK"]

    def test_no_header_raises(self):
        csv = "garbage\nmore garbage\n"
        with pytest.raises(ValueError, match="Could not locate holdings header"):
            ishares_parse(csv.encode("utf-8"))