"""Refresh specific partitions of a Microsoft Fabric / Power BI semantic model."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .audit import build_verification_frame
from .backend import FabricBackend, SempyBackend
from .exceptions import (
    ConfigurationError,
    FabricApiError,
    InventoryError,
    NoMatchingPartitionsError,
    PartitionRefreshError,
    RefreshFailedError,
    RefreshInProgressError,
    RefreshTimeoutError,
)
from .inventory import standardize_partition_inventory, summarize_inventory
from .models import (
    BatchResult,
    PartitionSelector,
    RefreshOptions,
    RefreshReport,
    Strategy,
)
from .refresh import (
    build_enhanced_payload,
    build_tmsl_script,
    discover_partitions,
    list_partitions,
    refresh_partitions,
    summarize_tables,
)
from .selection import select_partitions
from .ui import RefreshController, RefreshPanel, launch_refresh_ui

try:
    __version__ = version("fabric-partition-refresh")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

__all__ = [
    "BatchResult",
    "ConfigurationError",
    "FabricApiError",
    "FabricBackend",
    "InventoryError",
    "NoMatchingPartitionsError",
    "PartitionRefreshError",
    "PartitionSelector",
    "RefreshController",
    "RefreshFailedError",
    "RefreshInProgressError",
    "RefreshOptions",
    "RefreshPanel",
    "RefreshReport",
    "RefreshTimeoutError",
    "SempyBackend",
    "Strategy",
    "__version__",
    "build_enhanced_payload",
    "build_tmsl_script",
    "build_verification_frame",
    "discover_partitions",
    "launch_refresh_ui",
    "list_partitions",
    "refresh_partitions",
    "select_partitions",
    "standardize_partition_inventory",
    "summarize_inventory",
    "summarize_tables",
]
