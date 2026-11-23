"""Refresh orchestration: plan, submit, poll, verify."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .audit import build_verification_frame
from .backend import FabricBackend, SempyBackend
from .exceptions import (
    FabricApiError,
    PartitionRefreshError,
    RefreshFailedError,
    RefreshInProgressError,
    RefreshTimeoutError,
)
from .inventory import (
    PARTITIONS_DAX,
    TABLES_DAX,
    attach_row_counts,
    standardize_partition_inventory,
    summarize_inventory,
)
from .models import (
    BatchResult,
    PartitionSelector,
    RefreshOptions,
    RefreshReport,
    Strategy,
)
from .selection import select_partitions

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"Completed", "Failed", "Cancelled", "Disabled"})
#: Message returned when the Enhanced Refresh API is asked to target partitions of
#: a table that has an incremental-refresh policy. TMSL does not have this limit.
_REFRESH_POLICY_MARKERS = ("refresh policy", "cannot specify partition")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Pure builders (no I/O) - easy to unit test
# --------------------------------------------------------------------------- #
def build_enhanced_payload(
    table: str, partitions: Sequence[str], options: RefreshOptions
) -> dict[str, Any]:
    """Body for the Enhanced Refresh REST API."""
    if not partitions:
        raise ValueError(
            "No partitions given. Refusing to build a payload that could refresh the whole model."
        )
    return {
        "type": options.refresh_type,
        "commitMode": options.commit_mode,
        "maxParallelism": options.max_parallelism,
        "retryCount": options.retry_count,
        "timeout": options.timeout,
        "objects": [{"table": table, "partition": p} for p in partitions],
    }


def build_tmsl_script(
    database: str, table: str, partitions: Sequence[str], refresh_type: str
) -> dict[str, Any]:
    """TMSL ``refresh`` command for specific partitions (the SSMS 'script' equivalent)."""
    if not partitions:
        raise ValueError(
            "No partitions given. Refusing to build a script that could refresh the whole model."
        )
    return {
        "refresh": {
            "type": refresh_type,
            "objects": [{"database": database, "table": table, "partition": p} for p in partitions],
        }
    }


def chunk(items: Sequence[str], size: int | None) -> list[list[str]]:
    """Split ``items`` into lists of at most ``size`` (``None`` = one list)."""
    items = list(items)
    if not size or size >= len(items):
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def discover_partitions(backend: FabricBackend) -> pd.DataFrame:
    """Return the clean partition inventory of the model behind ``backend``."""
    raw_partitions = backend.query_dax(PARTITIONS_DAX)
    raw_tables = backend.query_dax(TABLES_DAX)
    inventory = standardize_partition_inventory(raw_partitions, raw_tables)
    stats_fn = getattr(backend, "partition_stats", None)  # optional backend capability
    if stats_fn is not None:
        try:
            inventory = attach_row_counts(inventory, stats_fn())
        except Exception as exc:
            logger.warning("Row counts unavailable: %s", exc)
    return inventory


def list_partitions(backend: FabricBackend, table: str) -> pd.DataFrame:
    """Return the partitions of one table (case-insensitive name), for browsing."""
    inventory = discover_partitions(backend)
    key = table.strip().lower()
    return inventory[inventory["table_name"].astype(str).str.lower() == key].reset_index(drop=True)


def summarize_tables(backend: FabricBackend) -> pd.DataFrame:
    """Convenience: per-table partition counts, to find a ``table`` name."""
    return summarize_inventory(discover_partitions(backend))


# --------------------------------------------------------------------------- #
# Execution helpers
# --------------------------------------------------------------------------- #
def assert_no_running_refresh(backend: FabricBackend) -> None:
    """Raise :class:`RefreshInProgressError` if a refresh is already running."""
    for record in backend.recent_refreshes(top=5):
        if record.get("status") in {"InProgress", "Unknown"} and not record.get("endTime"):
            raise RefreshInProgressError(
                "A refresh appears to already be running "
                f"(status={record.get('status')}, requestId={record.get('requestId')}, "
                f"start={record.get('startTime')}). Wait for it to finish or set "
                "check_running_refresh=False."
            )
    logger.info("No active refresh found.")


def poll_until_done(
    backend: FabricBackend, request_id: str, options: RefreshOptions
) -> dict[str, Any]:
    """Poll an Enhanced Refresh request until it reaches a terminal state."""
    max_attempts = max(1, int(options.max_poll_minutes * 60 / options.poll_interval_seconds))
    for attempt in range(1, max_attempts + 1):
        record = backend.get_refresh(request_id)
        status = record.get("status")
        logger.info(
            "[%d/%d] status=%s start=%s end=%s",
            attempt,
            max_attempts,
            status,
            record.get("startTime"),
            record.get("endTime"),
        )
        if status in TERMINAL_STATUSES or (status == "Unknown" and record.get("endTime")):
            return record
        time.sleep(options.poll_interval_seconds)
    raise RefreshTimeoutError(
        f"Refresh request {request_id} did not finish within {options.max_poll_minutes} minutes."
    )


def _run_enhanced(
    backend: FabricBackend, table: str, partitions: Sequence[str], options: RefreshOptions
) -> str:
    payload = build_enhanced_payload(table, partitions, options)
    request_id = backend.start_enhanced_refresh(payload)
    if not request_id:
        raise FabricApiError(
            "Trigger enhanced refresh", None, "No request id returned by the service."
        )
    record = poll_until_done(backend, request_id, options)
    if record.get("status") != "Completed":
        raise RefreshFailedError(
            f"Refresh {request_id} ended with status {record.get('status')}: {json.dumps(record)}"
        )
    return request_id


def _run_tmsl(
    backend: FabricBackend,
    table: str,
    partitions: Sequence[str],
    options: RefreshOptions,
    result: BatchResult,
) -> None:
    script = build_tmsl_script(backend.semantic_model, table, partitions, options.refresh_type)
    total_attempts = options.retry_count + 1
    for attempt in range(1, total_attempts + 1):
        result.attempts = attempt
        try:
            backend.execute_tmsl(script)
            return
        except FabricApiError as exc:
            if attempt == total_attempts:
                raise
            logger.warning(
                "TMSL batch %d failed (attempt %d/%d): %s. Retrying in %ds.",
                result.index,
                attempt,
                total_attempts,
                exc,
                options.retry_delay_seconds,
            )
            time.sleep(options.retry_delay_seconds)


def _is_refresh_policy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _REFRESH_POLICY_MARKERS)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def refresh_partitions(
    workspace: str,
    semantic_model: str,
    selector: PartitionSelector,
    options: RefreshOptions | None = None,
    *,
    backend: FabricBackend | None = None,
) -> RefreshReport:
    """Refresh selected partitions of one table in a semantic model.

    By default this is a **dry run**: partitions are discovered and selected, and
    the plan is returned without refreshing anything. Pass
    ``RefreshOptions(dry_run=False)`` to execute.

    Args:
        workspace: Workspace name.
        semantic_model: Semantic model name.
        selector: Which partitions to refresh.
        options: Execution options.
        backend: Service access. Defaults to :class:`SempyBackend` (Fabric notebook).

    Returns:
        A :class:`RefreshReport`.

    Raises:
        NoMatchingPartitionsError: nothing matched ``selector``.
        RefreshInProgressError: the model is already refreshing.
        RefreshFailedError: a batch failed; ``.report`` has the partial results.
    """
    options = options or RefreshOptions()
    backend = backend or SempyBackend(workspace, semantic_model)

    logger.info("Reading partition metadata from '%s' / '%s'...", workspace, semantic_model)
    inventory = discover_partitions(backend)
    selected = select_partitions(inventory, selector)
    table = str(selected["table_name"].iloc[0])  # canonical casing from the model
    partitions = selected["partition_name"].astype(str).tolist()
    batches = chunk(partitions, options.batch_size)

    report = RefreshReport(
        workspace=workspace,
        semantic_model=semantic_model,
        table=table,
        refresh_type=options.refresh_type,
        dry_run=options.dry_run,
        selected=selected,
    )
    logger.info(
        "Selected %d partition(s) of '%s' in %d batch(es): %s",
        len(partitions),
        table,
        len(batches),
        partitions,
    )

    if options.dry_run:
        logger.info(
            "dry_run=True: nothing was refreshed. Pass RefreshOptions(dry_run=False) to run."
        )
        return report

    if options.check_running_refresh:
        assert_no_running_refresh(backend)

    use_enhanced = options.strategy in {Strategy.AUTO, Strategy.ENHANCED}
    failure: Exception | None = None

    for index, batch in enumerate(batches, start=1):
        result = BatchResult(
            index=index,
            partitions=batch,
            strategy_used="enhanced" if use_enhanced else "tmsl",
            status="Skipped" if failure else "Failed",
        )
        report.batches.append(result)
        if failure:
            continue

        result.started_utc = _utc_now()
        logger.info(
            "Batch %d/%d (%s): %d partition(s)",
            index,
            len(batches),
            result.strategy_used,
            len(batch),
        )
        try:
            if use_enhanced:
                try:
                    result.request_id = _run_enhanced(backend, table, batch, options)
                except PartitionRefreshError as exc:
                    if options.strategy is Strategy.AUTO and _is_refresh_policy_error(exc):
                        logger.info(
                            "Table has a refresh policy; Enhanced Refresh cannot target its "
                            "partitions. Falling back to TMSL."
                        )
                        use_enhanced = False
                        result.strategy_used = "tmsl"
                        _run_tmsl(backend, table, batch, options, result)
                    else:
                        raise
            else:
                _run_tmsl(backend, table, batch, options, result)
            result.status = "Completed"
        except PartitionRefreshError as exc:
            result.error = str(exc)
            failure = exc
            logger.error("Batch %d failed: %s", index, exc)
        finally:
            result.finished_utc = _utc_now()

    _verify(backend, report)

    if failure:
        done = len(report.refreshed_partitions)
        raise RefreshFailedError(
            f"Refresh failed after {done}/{len(partitions)} partition(s): {failure}",
            report=report,
        ) from failure
    return report


def _verify(backend: FabricBackend, report: RefreshReport) -> None:
    """Re-read metadata and attach a before/after comparison (best effort)."""
    try:
        after = discover_partitions(backend)
        report.verification = build_verification_frame(report.selected, after)
        not_moved = report.verification.loc[~report.verification["refreshed"], "partition_name"]
        if len(not_moved):
            logger.warning(
                "%d partition(s) show no newer refreshed_time: %s",
                len(not_moved),
                not_moved.tolist(),
            )
    except Exception as exc:
        logger.warning("Post-refresh verification skipped: %s", exc)
