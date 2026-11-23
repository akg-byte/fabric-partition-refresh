"""Choose which partitions to refresh."""

from __future__ import annotations

import re

import pandas as pd

from .exceptions import NoMatchingPartitionsError
from .models import PartitionSelector


def select_partitions(inventory: pd.DataFrame, selector: PartitionSelector) -> pd.DataFrame:
    """Apply ``selector`` to ``inventory``.

    Raises:
        NoMatchingPartitionsError: the table does not exist, or no partition
            survives the filters. The exception lists what *is* available.
    """
    table_key = selector.table.strip().lower()
    in_table = inventory[inventory["table_name"].astype(str).str.lower() == table_key]

    if in_table.empty:
        tables = sorted(inventory["table_name"].dropna().astype(str).unique())
        raise NoMatchingPartitionsError(
            f"Table not found: {selector.table!r}. Available tables: {tables}",
            available_tables=tables,
        )

    names = in_table["partition_name"].astype(str)
    mask = pd.Series(True, index=in_table.index)

    if selector.names is not None:
        wanted = {n.strip().lower() for n in selector.names}
        mask &= names.str.lower().isin(wanted)
    if selector.contains:
        mask &= names.str.contains(selector.contains, case=False, na=False, regex=False)
    if selector.pattern:
        mask &= names.str.contains(selector.pattern, flags=re.IGNORECASE, na=False, regex=True)
    if selector.min_partition:
        mask &= names >= selector.min_partition
    if selector.max_partition:
        mask &= names <= selector.max_partition

    selected = in_table[mask].sort_values("partition_name").reset_index(drop=True)

    if selected.empty:
        available = sorted(names.unique())
        raise NoMatchingPartitionsError(
            f"No partitions of table {selector.table!r} match the selector {selector}. "
            f"Available partitions: {available}",
            available_partitions=available,
        )
    return selected
