"""Tests for the split/dividend back-adjustment math."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.services.adjustments import (
    DIVIDEND,
    SPLIT,
    apply_adjustments,
    dividend_action,
    split_action,
)

D1 = date(2025, 3, 3)
D2 = date(2025, 3, 4)
D3 = date(2025, 3, 5)
D4 = date(2025, 3, 6)


def _row(d: date, close: float = 100.0, volume: float = 1000.0) -> dict[str, Any]:
    return {
        "date": d,
        "timestamp": 0,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "vwap": close,
        "trade_count": 10,
    }


def _bars(*closes: float) -> list[dict[str, Any]]:
    return [_row(d, close) for d, close in zip([D1, D2, D3, D4], closes)]


def _split(d: date, split_from: float, split_to: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": SPLIT,
        "split_from": split_from,
        "split_to": split_to,
        "cash_amount": None,
    }


def _dividend(d: date, cash: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": DIVIDEND,
        "split_from": None,
        "split_to": None,
        "cash_amount": cash,
    }


class TestSplits:
    def test_two_for_one_halves_earlier_prices_and_doubles_volume(self):
        out = apply_adjustments(
            _bars(200.0, 200.0, 200.0, 200.0), [_split(D3, 1.0, 2.0)]
        )

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0, 200.0, 200.0])
        assert [r["volume"] for r in out] == pytest.approx(
            [2000.0, 2000.0, 1000.0, 1000.0]
        )

    def test_reverse_split_scales_prices_up(self):
        out = apply_adjustments(
            _bars(50.0, 50.0, 50.0, 50.0), [_split(D3, 2.0, 1.0)]
        )

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0, 50.0, 50.0])
        assert [r["volume"] for r in out] == pytest.approx(
            [500.0, 500.0, 1000.0, 1000.0]
        )

    def test_execution_date_bar_is_not_scaled(self):
        out = apply_adjustments(_bars(200.0), [_split(D1, 1.0, 2.0)])

        assert out[0]["close"] == pytest.approx(200.0)

    def test_vwap_is_scaled_with_prices(self):
        out = apply_adjustments(_bars(200.0, 200.0), [_split(D2, 1.0, 2.0)])

        assert out[0]["vwap"] == pytest.approx(100.0)


class TestDividends:
    def test_scales_prices_before_ex_date_only(self):
        out = apply_adjustments(
            _bars(100.0, 100.0, 100.0, 100.0), [_dividend(D3, 2.0)]
        )

        assert [r["close"] for r in out] == pytest.approx([98.0, 98.0, 100.0, 100.0])
        # Cash dividends never adjust volume.
        assert [r["volume"] for r in out] == pytest.approx([1000.0] * 4)

    def test_multiple_dividends_compound(self):
        out = apply_adjustments(
            _bars(100.0, 100.0, 100.0, 100.0),
            [_dividend(D3, 1.0), _dividend(D4, 1.0)],
        )

        # D3 sized off 100, D4 off 99 → 98 / 99 / 100.
        assert [r["close"] for r in out] == pytest.approx([98.0, 98.0, 99.0, 100.0])

    def test_dividend_without_prior_bar_is_ignored(self):
        out = apply_adjustments(_bars(100.0, 100.0), [_dividend(D1, 2.0)])

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0])

    def test_dividend_larger_than_prior_close_is_ignored(self):
        out = apply_adjustments(_bars(100.0, 100.0), [_dividend(D2, 150.0)])

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0])


class TestCombined:
    def test_split_and_dividend_compose(self):
        out = apply_adjustments(
            _bars(200.0, 200.0, 200.0, 200.0),
            [_split(D3, 1.0, 2.0), _dividend(D4, 2.0)],
        )

        assert [r["close"] for r in out] == pytest.approx([99.0, 99.0, 198.0, 200.0])
        assert [r["volume"] for r in out] == pytest.approx(
            [2000.0, 2000.0, 1000.0, 1000.0]
        )


class TestPassThrough:
    def test_no_actions_returns_rows_unchanged(self):
        rows = _bars(100.0, 101.0)
        assert apply_adjustments(rows, []) == rows

    def test_no_rows_returns_empty(self):
        assert apply_adjustments([], [_split(D3, 1.0, 2.0)]) == []

    def test_input_rows_are_not_mutated(self):
        rows = _bars(200.0, 200.0)
        apply_adjustments(rows, [_split(D2, 1.0, 2.0)])

        assert [r["close"] for r in rows] == [200.0, 200.0]
        assert [r["volume"] for r in rows] == [1000.0, 1000.0]

    def test_output_is_sorted_by_date(self):
        rows = [_row(D3, 100.0), _row(D1, 100.0), _row(D2, 100.0)]
        out = apply_adjustments(rows, [_split(D4, 1.0, 2.0)])

        assert [r["date"] for r in out] == [D1, D2, D3]


class TestSplitAction:
    def test_normalises_polygon_payload(self):
        action = split_action(
            {"execution_date": D1.isoformat(), "split_from": 1, "split_to": 4}
        )

        assert action == {
            "date": D1,
            "kind": SPLIT,
            "split_from": 1.0,
            "split_to": 4.0,
            "cash_amount": None,
        }

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"execution_date": "not-a-date", "split_from": 1, "split_to": 2},
            {"execution_date": D1.isoformat(), "split_from": 0, "split_to": 2},
            {"execution_date": D1.isoformat(), "split_from": 1, "split_to": None},
        ],
    )
    def test_rejects_malformed_payloads(self, payload: dict[str, Any]):
        assert split_action(payload) is None


class TestDividendAction:
    def test_uses_split_adjusted_cash_amount(self):
        action = dividend_action(
            {
                "ex_dividend_date": D1.isoformat(),
                "cash_amount": 0.25,
                "split_adjusted_cash_amount": 0.5,
            }
        )

        assert action is not None
        assert action["cash_amount"] == pytest.approx(0.5)

    def test_falls_back_to_cash_amount(self):
        action = dividend_action(
            {"ex_dividend_date": D1.isoformat(), "cash_amount": 0.25}
        )

        assert action is not None
        assert action["cash_amount"] == pytest.approx(0.25)

    @pytest.mark.parametrize("distribution", ["SC", "CG", "ST"])
    def test_ignores_non_cash_distributions(self, distribution: str):
        payload = {
            "ex_dividend_date": D1.isoformat(),
            "cash_amount": 1.0,
            "distribution_type": distribution,
        }
        assert dividend_action(payload) is None

    def test_accepts_cash_distribution_type(self):
        payload = {
            "ex_dividend_date": D1.isoformat(),
            "cash_amount": 1.0,
            "distribution_type": "CD",
        }
        assert dividend_action(payload) is not None

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"ex_dividend_date": "not-a-date", "cash_amount": 1.0},
            {"ex_dividend_date": D1.isoformat(), "cash_amount": 0},
            {"ex_dividend_date": D1.isoformat()},
        ],
    )
    def test_rejects_unusable_payloads(self, payload: dict[str, Any]):
        assert dividend_action(payload) is None
