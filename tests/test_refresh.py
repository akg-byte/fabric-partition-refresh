from __future__ import annotations

import pytest

from conftest import POLICY_ERROR
from fabric_partition_refresh import (
    ConfigurationError,
    FabricApiError,
    NoMatchingPartitionsError,
    PartitionSelector,
    RefreshFailedError,
    RefreshInProgressError,
    RefreshOptions,
    RefreshTimeoutError,
    Strategy,
    build_enhanced_payload,
    build_tmsl_script,
    refresh_partitions,
)
from fabric_partition_refresh.refresh import chunk

SEL = PartitionSelector("FactSales")
RUN = {"dry_run": False}


def run(backend, selector=SEL, **opts):
    return refresh_partitions("WS", "Model", selector, RefreshOptions(**opts), backend=backend)


# ---- builders ---------------------------------------------------------------
def test_build_tmsl_script_shape():
    script = build_tmsl_script("M", "T", ["p1", "p2"], "full")
    assert script == {
        "refresh": {
            "type": "full",
            "objects": [
                {"database": "M", "table": "T", "partition": "p1"},
                {"database": "M", "table": "T", "partition": "p2"},
            ],
        }
    }


def test_build_enhanced_payload_shape():
    payload = build_enhanced_payload("T", ["p1"], RefreshOptions(max_parallelism=2))
    assert payload["objects"] == [{"table": "T", "partition": "p1"}]
    assert payload["maxParallelism"] == 2
    assert payload["commitMode"] == "transactional"


@pytest.mark.parametrize(
    "builder",
    [
        lambda: build_enhanced_payload("T", [], RefreshOptions()),
        lambda: build_tmsl_script("M", "T", [], "full"),
    ],
)
def test_empty_partition_list_is_refused(builder):
    with pytest.raises(ValueError, match="whole model"):
        builder()


def test_chunk():
    assert chunk(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert chunk(list("abc"), None) == [["a", "b", "c"]]
    assert chunk(list("abc"), 10) == [["a", "b", "c"]]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"refresh_type": "bogus"},
        {"commit_mode": "bogus"},
        {"max_parallelism": 0},
        {"timeout": "4h"},
        {"batch_size": 0},
        {"strategy": "nope"},
    ],
)
def test_options_validation(kwargs):
    with pytest.raises((ConfigurationError, ValueError)):
        RefreshOptions(**kwargs)


# ---- dry run ----------------------------------------------------------------
def test_default_is_dry_run_and_refreshes_nothing(backend):
    report = refresh_partitions("WS", "Model", SEL, backend=backend)
    assert report.status == "DryRun"
    assert len(report.selected) == 4
    assert backend.tmsl_scripts == [] and backend.enhanced_payloads == []


def test_no_match_raises_before_any_refresh(backend):
    with pytest.raises(NoMatchingPartitionsError):
        run(backend, PartitionSelector("FactSales", contains="1999"))
    assert backend.tmsl_scripts == []


# ---- TMSL -------------------------------------------------------------------
def test_tmsl_strategy(backend):
    report = run(backend, strategy="tmsl", **RUN)
    assert report.status == "Completed"
    assert len(backend.tmsl_scripts) == 1
    objs = backend.tmsl_scripts[0]["refresh"]["objects"]
    assert [o["partition"] for o in objs] == ["2025Q101", "2025Q102", "2025Q204", "2025Q307"]
    assert objs[0]["database"] == "Test Model"
    assert report.verification is not None and report.verification["refreshed"].all()
    assert set(report.to_audit_frame()["status"]) == {"Completed"}


def test_tmsl_batches(backend):
    report = run(backend, strategy="tmsl", batch_size=3, **RUN)
    assert [len(s["refresh"]["objects"]) for s in backend.tmsl_scripts] == [3, 1]
    assert [b.index for b in report.batches] == [1, 2]


def test_tmsl_retries_transient_failure(backend):
    backend.tmsl_failures = 2
    report = run(backend, strategy="tmsl", retry_count=2, **RUN)
    assert report.batches[0].attempts == 3
    assert report.status == "Completed"


def test_tmsl_failure_raises_with_partial_report_and_skips_rest(backend):
    backend.tmsl_failures = 99
    with pytest.raises(RefreshFailedError) as ei:
        run(backend, strategy="tmsl", batch_size=2, retry_count=0, **RUN)
    report = ei.value.report
    assert [b.status for b in report.batches] == ["Failed", "Skipped"]
    assert report.status == "Failed"
    assert "transient" in report.batches[0].error


def test_second_batch_failure_is_partial(backend):
    original = backend.execute_tmsl
    calls = {"n": 0}

    def flaky(script):
        calls["n"] += 1
        if calls["n"] == 2:
            raise FabricApiError("TMSL refresh", None, "boom")
        original(script)

    backend.execute_tmsl = flaky
    with pytest.raises(RefreshFailedError) as ei:
        run(backend, strategy="tmsl", batch_size=2, retry_count=0, **RUN)
    assert ei.value.report.status == "PartiallyCompleted"
    assert ei.value.report.refreshed_partitions == ["2025Q101", "2025Q102"]


# ---- Enhanced ---------------------------------------------------------------
def test_enhanced_strategy_polls_until_completed(backend):
    backend.refresh_records = [
        {"status": "InProgress"},
        {"status": "InProgress"},
        {"status": "Completed", "endTime": "x"},
    ]
    report = run(backend, strategy="enhanced", **RUN)
    assert report.batches[0].request_id == "req-1"
    assert report.batches[0].strategy_used == "enhanced"
    assert backend.enhanced_payloads[0]["objects"][0] == {
        "table": "FactSales",
        "partition": "2025Q101",
    }


def test_enhanced_failed_record_raises(backend):
    backend.refresh_records = [{"status": "Failed", "endTime": "x"}]
    with pytest.raises(RefreshFailedError):
        run(backend, strategy="enhanced", **RUN)


def test_enhanced_poll_timeout(backend):
    backend.refresh_records = [{"status": "InProgress"}]
    with pytest.raises(RefreshFailedError) as ei:
        run(backend, strategy="enhanced", max_poll_minutes=1, poll_interval_seconds=30, **RUN)
    assert isinstance(ei.value.__cause__, RefreshTimeoutError)


# ---- AUTO -------------------------------------------------------------------
def test_auto_falls_back_to_tmsl_on_refresh_policy_error(backend):
    backend.enhanced_error = FabricApiError("Trigger enhanced refresh", 400, POLICY_ERROR)
    report = run(backend, strategy=Strategy.AUTO, batch_size=2, **RUN)
    assert [b.strategy_used for b in report.batches] == ["tmsl", "tmsl"]
    assert len(backend.tmsl_scripts) == 2
    assert report.status == "Completed"


def test_auto_does_not_swallow_other_errors(backend):
    backend.enhanced_error = FabricApiError("Trigger enhanced refresh", 403, "Forbidden")
    with pytest.raises(RefreshFailedError):
        run(backend, strategy="auto", **RUN)
    assert backend.tmsl_scripts == []


def test_auto_uses_enhanced_when_it_works(backend):
    report = run(backend, strategy="auto", **RUN)
    assert report.batches[0].strategy_used == "enhanced"
    assert backend.tmsl_scripts == []


# ---- guard ------------------------------------------------------------------
def test_refuses_when_refresh_already_running(backend):
    backend.history = [{"status": "Unknown", "requestId": "abc", "startTime": "t"}]
    with pytest.raises(RefreshInProgressError):
        run(backend, strategy="tmsl", **RUN)
    assert backend.tmsl_scripts == []


def test_guard_can_be_disabled(backend):
    backend.history = [{"status": "Unknown", "requestId": "abc"}]
    report = run(backend, strategy="tmsl", check_running_refresh=False, **RUN)
    assert report.status == "Completed"


def test_list_partitions(backend):
    from fabric_partition_refresh import list_partitions

    df = list_partitions(backend, "factsales")
    assert df["partition_name"].tolist() == ["2025Q101", "2025Q102", "2025Q204", "2025Q307"]
    assert list_partitions(backend, "Nope").empty


def test_auto_falls_back_when_service_accepts_then_fails_async(backend):
    """Real service behaviour: POST is accepted, the refresh record then fails."""
    backend.refresh_records = [
        {
            "status": "Failed",
            "endTime": "x",
            "messages": [{"type": "Error", "message": POLICY_ERROR}],
        }
    ]
    report = run(backend, strategy="auto", **RUN)
    assert report.batches[0].strategy_used == "tmsl"
    assert report.batches[0].request_id is None
    assert len(backend.tmsl_scripts) == 1
    assert report.status == "Completed"


def test_inventory_includes_row_counts(backend):
    from fabric_partition_refresh import discover_partitions

    inv = discover_partitions(backend)
    fact = inv[inv["table_name"] == "FactSales"].set_index("partition_name")["row_count"]
    assert fact["2025Q101"] == 1000


def test_inventory_without_stats_has_nan_row_counts(backend):
    from fabric_partition_refresh import discover_partitions

    backend.partition_stats = lambda: (_ for _ in ()).throw(RuntimeError("no stats"))
    inv = discover_partitions(backend)
    assert inv["row_count"].isna().all()


def test_verification_reports_row_count_change(backend):
    report = run(backend, strategy="tmsl", **RUN)
    v = report.verification
    assert list(v["row_count_before"]) == [1000, 2000, 3000, 4000]
    assert (v["row_count_change"] == 5).all()
