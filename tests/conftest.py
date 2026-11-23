"""Shared fixtures: an in-memory fake of the Fabric backend."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from fabric_partition_refresh.exceptions import FabricApiError

POLICY_ERROR = "Cannot specify partition for table having refresh policy defined."


def raw_partitions() -> pd.DataFrame:
    """Shaped like INFO.PARTITIONS() output from sempy (bracketed column names)."""
    rows = []
    for table_id, names in {
        "10": ["2025Q101", "2025Q102", "2025Q204", "2025Q307"],
        "20": ["Partition1"],
    }.items():
        for i, n in enumerate(names):
            rows.append(
                {
                    "[ID]": f"{table_id}{i}",
                    "[TableID]": table_id,
                    "[Name]": n,
                    "[RefreshedTime]": pd.Timestamp("2026-01-01"),
                    "[ModifiedTime]": pd.Timestamp("2026-01-01"),
                    "[RangeStart]": None,
                    "[RangeEnd]": None,
                    "[State]": "Ready",
                    "[Type]": "M",
                    "[Mode]": "Import",
                    "[ErrorMessage]": None,
                }
            )
    return pd.DataFrame(rows)


def raw_tables() -> pd.DataFrame:
    return pd.DataFrame({"[ID]": [10, 20], "[Name]": ["FactSales", "DimStore"]})


class FakeBackend:
    """Records calls; behaviour is scripted per test."""

    workspace = "Test Workspace"
    semantic_model = "Test Model"

    def __init__(self) -> None:
        self.tmsl_scripts: list[dict[str, Any]] = []
        self.enhanced_payloads: list[dict[str, Any]] = []
        self.enhanced_error: Exception | None = None
        self.tmsl_failures: int = 0  # number of initial TMSL calls that fail
        self.refresh_records: list[dict[str, Any]] = [
            {"status": "Completed", "endTime": "2026-01-01T00:00:00Z"}
        ]
        self.history: list[dict[str, Any]] = []
        self.refreshed = False

    def query_dax(self, dax: str) -> pd.DataFrame:
        parts = raw_partitions()
        if self.refreshed:  # simulate refreshed_time moving forward after a refresh
            parts["[RefreshedTime]"] = pd.Timestamp("2026-06-01")
        return parts if "PARTITIONS" in dax else raw_tables()

    def partition_stats(self) -> pd.DataFrame:
        bump = 5 if self.refreshed else 0
        rows = [
            ("FactSales", "2025Q101", 1000 + bump),
            ("FactSales", "2025Q102", 2000 + bump),
            ("FactSales", "2025Q204", 3000 + bump),
            ("FactSales", "2025Q307", 4000 + bump),
            ("DimStore", "Partition1", 50),
        ]
        return pd.DataFrame(rows, columns=["table_name", "partition_name", "row_count"])

    def execute_tmsl(self, script: dict[str, Any]) -> None:
        if self.tmsl_failures > 0:
            self.tmsl_failures -= 1
            raise FabricApiError("TMSL refresh", None, "transient")
        self.tmsl_scripts.append(script)
        self.refreshed = True

    def start_enhanced_refresh(self, payload: dict[str, Any]) -> str | None:
        if self.enhanced_error:
            raise self.enhanced_error
        self.enhanced_payloads.append(payload)
        self.refreshed = True
        return "req-1"

    def get_refresh(self, request_id: str) -> dict[str, Any]:
        if len(self.refresh_records) > 1:
            return self.refresh_records.pop(0)
        return self.refresh_records[0]

    def recent_refreshes(self, top: int = 5) -> list[dict[str, Any]]:
        return self.history


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fabric_partition_refresh.refresh.time.sleep", lambda _s: None)
