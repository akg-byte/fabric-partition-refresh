"""Before/after verification of partition refresh times."""

from __future__ import annotations

import pandas as pd

_KEYS = ["table_name", "partition_name"]


def build_verification_frame(before: pd.DataFrame, after: pd.DataFrame) -> pd.DataFrame:
    """Compare selected partitions before and after the refresh.

    Args:
        before: The selected-partition frame captured before refreshing.
        after: A fresh inventory read after refreshing.

    Returns:
        One row per selected partition with ``*_before`` / ``*_after`` columns and a
        boolean ``refreshed`` that is true when ``refreshed_time`` moved forward.
    """
    b = before[[*_KEYS, "refreshed_time", "modified_time", "state", "error_message"]].rename(
        columns={
            "refreshed_time": "refreshed_time_before",
            "modified_time": "modified_time_before",
            "state": "state_before",
            "error_message": "error_message_before",
        }
    )
    a = after[
        [
            *_KEYS,
            "refreshed_time",
            "modified_time",
            "range_start",
            "range_end",
            "state",
            "mode",
            "error_message",
        ]
    ].rename(
        columns={
            "refreshed_time": "refreshed_time_after",
            "modified_time": "modified_time_after",
            "state": "state_after",
            "error_message": "error_message_after",
        }
    )
    if "row_count" in before.columns and "row_count" in after.columns:
        b = b.merge(
            before[[*_KEYS, "row_count"]].rename(columns={"row_count": "row_count_before"}),
            on=_KEYS,
        )
        a = a.merge(
            after[[*_KEYS, "row_count"]].rename(columns={"row_count": "row_count_after"}),
            on=_KEYS,
        )
    merged = b.merge(a, on=_KEYS, how="left")
    if "row_count_before" in merged.columns:
        merged["row_count_change"] = merged["row_count_after"] - merged["row_count_before"]

    t_before = pd.to_datetime(merged["refreshed_time_before"], errors="coerce")
    t_after = pd.to_datetime(merged["refreshed_time_after"], errors="coerce")
    merged["refreshed"] = t_after.notna() & (t_before.isna() | (t_after > t_before))
    return merged.sort_values("partition_name").reset_index(drop=True)
