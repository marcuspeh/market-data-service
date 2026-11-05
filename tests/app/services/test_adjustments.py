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

    def test_missing_vwap_is_left_alone(self):
        """Longbridge daily bars carry no VWAP; they must not gain one."""
        rows = [_row(D1, 200.0)]
        rows[0]["vwap"] = None

        out = apply_adjustments(rows, [_split(D2, 1.0, 2.0)])

        assert out[0]["vwap"] is None
        assert out[0]["close"] == pytest.approx(100.0)

    def test_absent_vwap_key_is_tolerated(self):
        rows = [_row(D1, 200.0)]
        del rows[0]["vwap"]

        out = apply_adjustments(rows, [_split(D2, 1.0, 2.0)])

        assert "vwap" not in out[0]
        assert out[0]["close"] == pytest.approx(100.0)

    def test_two_splits_compound(self):
        out = apply_adjustments(
            _bars(400.0, 200.0, 100.0, 100.0),
            [_split(D2, 1.0, 2.0), _split(D3, 1.0, 2.0)],
        )

        # 4:1 cumulative across the two splits.
        assert [r["close"] for r in out] == pytest.approx(
            [100.0, 100.0, 100.0, 100.0]
        )
        assert [r["volume"] for r in out] == pytest.approx(
            [4000.0, 2000.0, 1000.0, 1000.0]
        )

    def test_split_after_every_bar_back_adjusts_all_of_them(self):
        """A split later than the whole cached range still rebases it."""
        out = apply_adjustments(_bars(100.0, 100.0), [_split(D4, 1.0, 2.0)])

        assert [r["close"] for r in out] == pytest.approx([50.0, 50.0])
        assert [r["volume"] for r in out] == pytest.approx([2000.0, 2000.0])

    def test_action_on_a_non_trading_day_still_applies(self):
        """A split dated on a Saturday adjusts every earlier bar."""
        saturday = date(2026, 1, 10)
        out = apply_adjustments(
            _bars(200.0, 200.0, 200.0, 200.0), [_split(saturday, 1.0, 2.0)]
        )

        assert [r["close"] for r in out] == pytest.approx(
            [100.0, 100.0, 100.0, 100.0]
        )

    def test_unsorted_actions_are_ordered_before_use(self):
        out = apply_adjustments(
            _bars(400.0, 200.0, 100.0, 100.0),
            [_split(D3, 1.0, 2.0), _split(D2, 1.0, 2.0)],
        )

        assert [r["close"] for r in out] == pytest.approx(
            [100.0, 100.0, 100.0, 100.0]
        )


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

        # Each dividend is sized off its own prior close independently:
        # D3 → 1/100, D4 → 1/100, so the two ex-dates compound to 0.99².
        assert [r["close"] for r in out] == pytest.approx(
            [98.01, 98.01, 99.0, 100.0]
        )

    def test_dividends_are_total_return_consistent(self):
        """A price that falls exactly by the dividend each ex-date is flat.

        This is the invariant that breaks if a dividend is sized against a
        close already scaled by later dividends.
        """
        out = apply_adjustments(
            [_row(D1, 100.0), _row(D2, 99.0), _row(D3, 98.0), _row(D4, 97.0)],
            [_dividend(D2, 1.0), _dividend(D3, 1.0), _dividend(D4, 1.0)],
        )

        assert [r["close"] for r in out] == pytest.approx([97.0, 97.0, 97.0, 97.0])

    def test_dividend_without_prior_bar_is_ignored(self):
        out = apply_adjustments(_bars(100.0, 100.0), [_dividend(D1, 2.0)])

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0])

    def test_dividend_larger_than_prior_close_is_ignored(self):
        out = apply_adjustments(_bars(100.0, 100.0), [_dividend(D2, 150.0)])

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0])

    def test_dividend_equal_to_prior_close_is_ignored(self):
        """A multiplier of zero would wipe the series; skip it instead."""
        out = apply_adjustments(_bars(100.0, 100.0), [_dividend(D2, 100.0)])

        assert [r["close"] for r in out] == pytest.approx([100.0, 100.0])

    def test_dividend_with_zero_prior_close_is_ignored(self):
        out = apply_adjustments(
            [_row(D1, 0.0), _row(D2, 100.0)], [_dividend(D2, 1.0)]
        )

        assert [r["close"] for r in out] == pytest.approx([0.0, 100.0])

    def test_dividend_is_sized_off_the_split_adjusted_close(self):
        """Cash and price must be expressed in the same share basis.

        The prior close is 200 before the 2-for-1 split, i.e. 100 in
        today's basis, so a 2.00 dividend is 2% of it — not 1%.
        """
        out = apply_adjustments(
            _bars(200.0, 200.0, 200.0, 200.0),
            [_split(D3, 1.0, 2.0), _dividend(D3, 2.0)],
        )

        # Split halves to 100; the dividend then scales by 1 - 2/100.
        assert out[0]["close"] == pytest.approx(98.0)
        assert out[1]["close"] == pytest.approx(98.0)

    def test_dividend_before_a_later_split_uses_the_split_adjusted_close(self):
        out = apply_adjustments(
            [_row(D1, 200.0), _row(D2, 198.0), _row(D3, 200.0), _row(D4, 200.0)],
            [_dividend(D2, 2.0), _split(D3, 1.0, 2.0)],
        )

        # D1's prior close is 200, split-adjusted to 100 → 1 - 2/100.
        assert out[0]["close"] == pytest.approx(98.0)
        # D2 is on the ex-date: split-adjusted by the later split, and the
        # dividend only touches bars strictly before its own date.
        assert out[1]["close"] == pytest.approx(99.0)

    def test_dividend_on_a_non_trading_day_uses_the_prior_trading_day(self):
        saturday = date(2026, 1, 10)
        out = apply_adjustments(
            _bars(100.0, 100.0, 100.0, 100.0), [_dividend(saturday, 2.0)]
        )

        # Every bar precedes the ex-date, so all are scaled.
        assert [r["close"] for r in out] == pytest.approx([98.0] * 4)

    def test_dividend_exactly_on_the_ex_date_does_not_scale_that_bar(self):
        out = apply_adjustments(
            _bars(100.0, 100.0, 100.0, 100.0), [_dividend(D3, 2.0)]
        )

        assert [r["close"] for r in out] == pytest.approx(
            [98.0, 98.0, 100.0, 100.0]
        )


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
        "distribution_type",
        ["recurring", "special", "supplemental", "irregular", "unknown"],
    )
    def test_accepts_v1_distribution_types(self, distribution_type: str):
        """Polygon v1 classifies *recurrence*, not payout kind — all are cash."""
        payload = {
            "ex_dividend_date": D1.isoformat(),
            "cash_amount": 0.27,
            "distribution_type": distribution_type,
            "split_adjusted_cash_amount": 0.27,
            "historical_adjustment_factor": 0.999138,
        }
        action = dividend_action(payload)
        assert action is not None
        assert action["cash_amount"] == pytest.approx(0.27)

    def test_accepts_real_v1_response_shape(self):
        """The exact payload Polygon returns from /stocks/v1/dividends."""
        payload = {
            "id": "E1e778b36bcedb4d1589a970be52c16294ad4bc89c8627d56a3b970f6cf2a6e10",
            "ticker": "AAPL",
            "record_date": "2026-08-10",
            "pay_date": "2026-08-13",
            "declaration_date": "2026-07-30",
            "ex_dividend_date": "2026-08-10",
            "frequency": 4,
            "cash_amount": 0.27,
            "currency": "USD",
            "distribution_type": "recurring",
            "historical_adjustment_factor": 0.999138,
            "split_adjusted_cash_amount": 0.27,
        }
        action = dividend_action(payload)
        assert action is not None
        assert action["date"] == date(2026, 8, 10)
        assert action["cash_amount"] == pytest.approx(0.27)

    @pytest.mark.parametrize("dividend_type", ["CD", "SC"])
    def test_accepts_legacy_cash_dividend_types(self, dividend_type: str):
        """The deprecated /v3/reference/dividends payload shape."""
        payload = {
            "ex_dividend_date": D1.isoformat(),
            "cash_amount": 0.22,
            "dividend_type": dividend_type,
        }
        assert dividend_action(payload) is not None

    @pytest.mark.parametrize("dividend_type", ["LT", "ST"])
    def test_ignores_legacy_capital_gain_distributions(self, dividend_type: str):
        payload = {
            "ex_dividend_date": D1.isoformat(),
            "cash_amount": 1.0,
            "dividend_type": dividend_type,
        }
        assert dividend_action(payload) is None

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
