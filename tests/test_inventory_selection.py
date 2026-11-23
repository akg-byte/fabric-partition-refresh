from __future__ import annotations

import pandas as pd
import pytest

from conftest import raw_partitions, raw_tables
from fabric_partition_refresh import (
    ConfigurationError,
    InventoryError,
    NoMatchingPartitionsError,
    PartitionSelector,
    select_partitions,
    standardize_partition_inventory,
    summarize_inventory,
)
from fabric_partition_refresh.inventory import find_column, normalize_key


@pytest.fixture
def inventory() -> pd.DataFrame:
    return standardize_partition_inventory(raw_partitions(), raw_tables())


def test_normalize_key_variants():
    assert normalize_key("INFO.PARTITIONS[TableID]") == "tableid"
    assert normalize_key("[TableID]") == "tableid"
    assert normalize_key("Table ID") == "tableid"
    assert normalize_key(None) == ""


def test_find_column_required_raises():
    with pytest.raises(InventoryError):
        find_column(pd.DataFrame({"a": [1]}), ["Nope"])


def test_inventory_joins_table_names(inventory):
    assert len(inventory) == 5
    assert set(inventory["table_name"]) == {"FactSales", "DimStore"}
    assert list(inventory.columns[:2]) == ["table_name", "partition_name"]


def test_summary(inventory):
    s = summarize_inventory(inventory)
    assert s.iloc[0]["table_name"] == "FactSales"
    assert s.iloc[0]["partition_count"] == 4


def test_select_all_partitions_of_table_case_insensitive(inventory):
    sel = select_partitions(inventory, PartitionSelector(table="factsales"))
    assert sel["partition_name"].tolist() == ["2025Q101", "2025Q102", "2025Q204", "2025Q307"]


def test_select_contains(inventory):
    sel = select_partitions(inventory, PartitionSelector("FactSales", contains="q1"))
    assert sel["partition_name"].tolist() == ["2025Q101", "2025Q102"]


def test_select_contains_is_literal_not_regex(inventory):
    with pytest.raises(NoMatchingPartitionsError):
        select_partitions(inventory, PartitionSelector("FactSales", contains="2025Q1."))


def test_select_pattern(inventory):
    sel = select_partitions(inventory, PartitionSelector("FactSales", pattern=r"Q[12]0[24]$"))
    assert sel["partition_name"].tolist() == ["2025Q102", "2025Q204"]


def test_select_range(inventory):
    sel = select_partitions(
        inventory,
        PartitionSelector("FactSales", min_partition="2025Q102", max_partition="2025Q204"),
    )
    assert sel["partition_name"].tolist() == ["2025Q102", "2025Q204"]


def test_select_names(inventory):
    sel = select_partitions(inventory, PartitionSelector("FactSales", names=("2025q307",)))
    assert sel["partition_name"].tolist() == ["2025Q307"]


def test_unknown_table_lists_available(inventory):
    with pytest.raises(NoMatchingPartitionsError) as ei:
        select_partitions(inventory, PartitionSelector("Nope"))
    assert ei.value.available_tables == ["DimStore", "FactSales"]


def test_no_partition_match_lists_available(inventory):
    with pytest.raises(NoMatchingPartitionsError) as ei:
        select_partitions(inventory, PartitionSelector("FactSales", contains="1999"))
    assert "2025Q101" in ei.value.available_partitions


@pytest.mark.parametrize(
    "kwargs",
    [
        {"table": " "},
        {"table": "T", "pattern": "("},
        {"table": "T", "min_partition": "b", "max_partition": "a"},
    ],
)
def test_selector_validation(kwargs):
    with pytest.raises(ConfigurationError):
        PartitionSelector(**kwargs)


def test_standardize_partition_stats_maps_columns_and_rejects_unknown():
    from fabric_partition_refresh.inventory import standardize_partition_stats

    raw = pd.DataFrame(
        {"Table Name": ["T"], "Partition Name": ["P"], "Record Count": ["1234"], "Mode": ["Import"]}
    )
    out = standardize_partition_stats(raw)
    assert out.to_dict("records") == [{"table_name": "T", "partition_name": "P", "row_count": 1234}]
    assert standardize_partition_stats(pd.DataFrame({"Table Name": ["T"]})) is None


def test_state_and_mode_codes_are_translated():
    parts = raw_partitions()
    parts["[State]"] = [1, 1, 4, 99, 3]
    parts["[Mode]"] = [0, 0, 1, 7, 0]
    inv = standardize_partition_inventory(parts, raw_tables())
    fact = inv[inv["table_name"] == "FactSales"]
    assert fact["state"].tolist() == ["Ready", "Ready", "CalculationNeeded", 99]
    assert fact["mode"].tolist() == ["Import", "Import", "DirectQuery", 7]
