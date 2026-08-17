"""Tests for the constituents fetcher (registry + provider dispatch)."""

from __future__ import annotations

import pytest

from app.services.constituents_fetcher import (
    ETF_REGISTRY,
    PROVIDER_PARSERS,
    UnsupportedSymbolError,
    get_entry,
)


class TestRegistry:
    def test_supported_symbols_match_providers(self):
        """Every registry entry must point to a provider that has a parser."""
        for symbol, entry in ETF_REGISTRY.items():
            assert entry["provider"] in PROVIDER_PARSERS, (
                f"{symbol} references unknown provider {entry['provider']!r}"
            )
            assert entry["link"].startswith("http"), (
                f"{symbol} has a malformed link: {entry['link']!r}"
            )


class TestGetEntry:
    def test_known_symbol(self):
        entry = get_entry("SPY")
        assert entry["provider"] == "ssga"
        assert "spy" in entry["link"].lower()

    def test_uppercased_lookup(self):
        assert get_entry("spy") == get_entry("SPY")

    def test_unknown_symbol_raises(self):
        with pytest.raises(UnsupportedSymbolError, match="XLK"):
            get_entry("XLK")


class TestProviders:
    def test_both_expected_providers_registered(self):
        assert "ssga" in PROVIDER_PARSERS
        assert "ishares" in PROVIDER_PARSERS