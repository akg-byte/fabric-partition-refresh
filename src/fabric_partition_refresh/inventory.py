"""Read and normalise partition metadata (``INFO.PARTITIONS`` / ``INFO.TABLES``)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pandas as pd

from .exceptions import InventoryError

PARTITIONS_DAX = "EVALUATE INFO.PARTITIONS()"
TABLES_DAX = "EVALUATE INFO.TABLES()"

INVENTORY_COLUMNS = [
    "table_name",
    "partition_name",
    "row_count",
    "refreshed_time",
    "modified_time",
    "range_start",
    "range_end",
    "state",
    "type",
    "mode",
    "error_message",
    "table_id",
    "partition_id",
]


# Only codes that are certain are translated; anything else is passed through unchanged.
_STATE_NAMES = {1: "Ready", 3: "NoData", 4: "CalculationNeeded"}
_MODE_NAMES = {0: "Import", 1: "DirectQuery"}


def _decode(series: Any, names: dict[int, str]) -> Any:
    """Translate numeric codes to names; leave strings and unknown codes as they are."""

    def one(value: Any) -> Any:
        try:
            return names.get(int(value), value)
        except (TypeError, ValueError):
            return value

    return [one(v) for v in series]


def normalize_key(value: Any) -> str:
    """Normalise a column label so DAX-style names compare equal.

    ``INFO.PARTITIONS[TableID]``, ``[TableID]`` and ``Table ID`` all become ``tableid``.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if "[" in text and "]" in text:
        text = text.split("[")[-1].split("]")[0]
    return re.sub(r"[^A-Za-z0-9]", "", text).lower()


def find_column(
    df: pd.DataFrame, candidates: Sequence[str], *, required: bool = True
) -> str | None:
    """Return the real column of ``df`` matching one of ``candidates`` (normalised)."""
    lookup = {normalize_key(c): c for c in df.columns}
    for name in candidates:
        key = normalize_key(name)
        if key in lookup:
            return str(lookup[key])
    if required:
        raise InventoryError(
            f"Could not find required column. Tried: {list(candidates)}. "
            f"Available columns: {list(df.columns)}"
        )
    return None


def _optional(df: pd.DataFrame, candidates: Sequence[str]) -> Any:
    col = find_column(df, candidates, required=False)
    return df[col] if col is not None else [None] * len(df)


def standardize_partition_inventory(
    raw_partitions: pd.DataFrame, raw_tables: pd.DataFrame
) -> pd.DataFrame:
    """Join ``INFO.PARTITIONS()`` to ``INFO.TABLES()`` into a clean inventory.

    Returns a DataFrame with the columns in :data:`INVENTORY_COLUMNS`, sorted by
    table then partition name.
    """
    p_table_id = find_column(raw_partitions, ["TableID", "Table ID"])
    p_name = find_column(raw_partitions, ["Name"])
    p_id = find_column(raw_partitions, ["ID"], required=False)

    parts = pd.DataFrame(
        {
            "table_id": raw_partitions[p_table_id].astype(str),
            "partition_name": raw_partitions[p_name].astype(str),
            "partition_id": raw_partitions[p_id].astype(str) if p_id else None,
            "row_count": float("nan"),
            "refreshed_time": _optional(raw_partitions, ["RefreshedTime", "Refreshed Time"]),
            "modified_time": _optional(raw_partitions, ["ModifiedTime", "Modified Time"]),
            "range_start": _optional(raw_partitions, ["RangeStart", "Range Start"]),
            "range_end": _optional(raw_partitions, ["RangeEnd", "Range End"]),
            "state": _decode(_optional(raw_partitions, ["State"]), _STATE_NAMES),
            "type": _optional(raw_partitions, ["Type"]),
            "mode": _decode(_optional(raw_partitions, ["Mode"]), _MODE_NAMES),
            "error_message": _optional(raw_partitions, ["ErrorMessage", "Error Message"]),
        }
    )

    t_id = find_column(raw_tables, ["ID"])
    t_name = find_column(raw_tables, ["Name"])
    tables = pd.DataFrame(
        {
            "table_id": raw_tables[t_id].astype(str),
            "table_name": raw_tables[t_name].astype(str),
        }
    )

    inventory = parts.merge(tables, on="table_id", how="left")[INVENTORY_COLUMNS]
    return inventory.sort_values(["table_name", "partition_name"]).reset_index(drop=True)


def summarize_inventory(inventory: pd.DataFrame) -> pd.DataFrame:
    """Per-table partition count and most recent refresh time."""
    return (
        inventory.groupby("table_name", dropna=False)
        .agg(
            partition_count=("partition_name", "count"),
            latest_refreshed_time=("refreshed_time", "max"),
        )
        .reset_index()
        .sort_values(["partition_count", "table_name"], ascending=[False, True])
        .reset_index(drop=True)
    )


_ROW_COUNT_CANDIDATES = ["Record Count", "RecordCount", "Row Count", "Rows", "Records"]
_STATS_TABLE_CANDIDATES = ["Table Name", "Table", "TableName"]
_STATS_PARTITION_CANDIDATES = ["Partition Name", "Partition", "PartitionName", "Name"]


def standardize_partition_stats(raw: pd.DataFrame) -> pd.DataFrame | None:
    """Reduce a partition-statistics frame to ``table_name, partition_name, row_count``.

    Returns ``None`` when the row-count column cannot be identified.
    """
    t = find_column(raw, _STATS_TABLE_CANDIDATES, required=False)
    p = find_column(raw, _STATS_PARTITION_CANDIDATES, required=False)
    r = find_column(raw, _ROW_COUNT_CANDIDATES, required=False)
    if t is None or p is None or r is None:
        return None
    return pd.DataFrame(
        {
            "table_name": raw[t].astype(str),
            "partition_name": raw[p].astype(str),
            "row_count": pd.to_numeric(raw[r], errors="coerce"),
        }
    )


def attach_row_counts(inventory: pd.DataFrame, stats: pd.DataFrame | None) -> pd.DataFrame:
    """Fill ``inventory['row_count']`` from ``stats`` (left join; missing = NaN)."""
    out = inventory.drop(columns=["row_count"], errors="ignore")
    if stats is None or stats.empty:
        out.insert(2, "row_count", float("nan"))
        return out
    stats = stats.drop_duplicates(["table_name", "partition_name"])
    merged = out.merge(stats, on=["table_name", "partition_name"], how="left")
    cols = [c for c in INVENTORY_COLUMNS if c in merged.columns]
    return merged[cols]
