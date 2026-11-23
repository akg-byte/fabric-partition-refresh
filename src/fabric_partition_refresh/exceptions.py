"""Exception hierarchy for fabric-partition-refresh."""

from __future__ import annotations


class PartitionRefreshError(Exception):
    """Base class for all errors raised by this package."""


class ConfigurationError(PartitionRefreshError, ValueError):
    """Invalid user-supplied configuration (selector or options)."""


class InventoryError(PartitionRefreshError):
    """The partition metadata returned by the model could not be interpreted."""


class NoMatchingPartitionsError(PartitionRefreshError, ValueError):
    """The selector matched no partitions.

    Attributes:
        available_tables: Table names present in the model.
        available_partitions: Partition names of the target table (if it exists).
    """

    def __init__(
        self,
        message: str,
        available_tables: list[str] | None = None,
        available_partitions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.available_tables = available_tables or []
        self.available_partitions = available_partitions or []


class RefreshInProgressError(PartitionRefreshError):
    """Another refresh is already running against the semantic model."""


class FabricApiError(PartitionRefreshError):
    """A Power BI / Fabric REST call or TMSL command failed."""

    def __init__(self, action: str, status_code: int | None, body: str = "") -> None:
        self.action = action
        self.status_code = status_code
        self.body = body
        super().__init__(f"{action} failed. HTTP status: {status_code}. Response: {body}")


class RefreshFailedError(PartitionRefreshError):
    """A refresh finished in a failed state.

    When raised by :func:`fabric_partition_refresh.refresh_partitions`, ``report``
    holds the partial :class:`~fabric_partition_refresh.RefreshReport` so you can
    see which batches completed.
    """

    def __init__(self, message: str, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class RefreshTimeoutError(PartitionRefreshError, TimeoutError):
    """A refresh did not reach a terminal state within the polling window."""
