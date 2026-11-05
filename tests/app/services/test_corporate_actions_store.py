"""Tests for the per-ticker CorporateActionsStore."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.services.corporate_actions_store import CorporateActionsStore

D1 = date(2024, 5, 10)
D2 = date(2024, 8, 12)
D3 = date(2025, 2, 7)


def _split(d: date, split_from: float, split_to: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": "split",
        "split_from": split_from,
        "split_to": split_to,
        "cash_amount": None,
    }


def _dividend(d: date, cash: float) -> dict[str, Any]:
    return {
        "date": d,
        "kind": "dividend",
        "split_from": None,
        "split_to": None,
        "cash_amount": cash,
    }


@pytest.fixture
def store(tmp_path: Path) -> CorporateActionsStore:
    return CorporateActionsStore(tmp_path / "corporate_actions")


class TestLayout:
    def test_path_for(self, store: CorporateActionsStore):
        path = store.path_for("aapl")
        assert path.name == "AAPL.parquet"
        assert path.parent.name == "corporate_actions"


class TestUpsert:
    def test_round_trips_records(self, store: CorporateActionsStore):
        assert store.upsert("AAPL", [_split(D1, 1.0, 4.0)]) == 1

        actions = store.read("AAPL", D1, D1)
        assert len(actions) == 1
        assert actions[0]["kind"] == "split"
        assert actions[0]["split_from"] == pytest.approx(1.0)
        assert actions[0]["split_to"] == pytest.approx(4.0)
        assert actions[0]["cash_amount"] is None

    def test_empty_actions_are_a_noop(self, store: CorporateActionsStore):
        assert store.upsert("AAPL", []) == 0
        assert not store.path_for("AAPL").exists()

    def test_merges_with_existing_records(self, store: CorporateActionsStore):
        store.upsert("AAPL", [_split(D1, 1.0, 2.0)])
        assert store.upsert("AAPL", [_dividend(D2, 0.5)]) == 2

        actions = store.read("AAPL", D1, D2)
        assert [a["kind"] for a in actions] == ["split", "dividend"]

    def test_same_date_and_kind_replaces_the_record(
        self, store: CorporateActionsStore
    ):
        store.upsert("AAPL", [_dividend(D1, 0.5)])
        assert store.upsert("AAPL", [_dividend(D1, 0.75)]) == 1

        actions = store.read("AAPL", D1, D1)
        assert len(actions) == 1
        assert actions[0]["cash_amount"] == pytest.approx(0.75)

    def test_never_drops_history_missing_from_a_refresh(
        self, store: CorporateActionsStore
    ):
        """A 2-year free-tier refresh must not erase older records."""
        store.upsert("AAPL", [_dividend(D1, 0.5), _split(D2, 1.0, 2.0)])
        assert store.upsert("AAPL", [_dividend(D3, 0.25)]) == 3

        actions = store.read("AAPL", D1, D3)
        assert [a["date"] for a in actions] == [D1, D2, D3]

    def test_records_are_sorted_by_date_then_kind(
        self, store: CorporateActionsStore
    ):
        store.upsert(
            "AAPL",
            [_dividend(D2, 0.5), _split(D1, 1.0, 2.0), _dividend(D1, 0.1)],
        )

        actions = store.read("AAPL", D1, D2)
        assert [(a["date"], a["kind"]) for a in actions] == [
            (D1, "dividend"),
            (D1, "split"),
            (D2, "dividend"),
        ]

    def test_idempotent_upsert(self, store: CorporateActionsStore):
        actions = [_split(D1, 1.0, 2.0), _dividend(D2, 0.5)]
        assert store.upsert("AAPL", actions) == 2
        assert store.upsert("AAPL", actions) == 2


class TestRead:
    def test_missing_file_returns_empty(self, store: CorporateActionsStore):
        assert store.read("AAPL", D1, D3) == []

    def test_filters_by_window(self, store: CorporateActionsStore):
        store.upsert("AAPL", [_split(D1, 1.0, 2.0), _dividend(D2, 0.5)])

        assert store.read("AAPL", D2, D3) == [
            {
                "date": D2,
                "kind": "dividend",
                "split_from": None,
                "split_to": None,
                "cash_amount": 0.5,
            }
        ]
        assert store.read("AAPL", D3, D3) == []
