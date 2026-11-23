from __future__ import annotations

import pytest

from conftest import FakeBackend
from fabric_partition_refresh import ConfigurationError, RefreshController, RefreshFailedError
from fabric_partition_refresh.ui import build_panel


@pytest.fixture
def controller(backend: FakeBackend) -> RefreshController:
    c = RefreshController(backend)
    c.load()
    return c


# ---- controller (no ipywidgets needed) --------------------------------------
def test_table_options_have_placeholder_and_counts(controller):
    opts = controller.table_options()
    assert opts[0][1] is None
    assert ("FactSales  (4)", "FactSales") in opts
    assert ("DimStore  (1)", "DimStore") in opts


def test_partition_options_show_refresh_time(controller):
    label, value = controller.partition_options("FactSales")[0]
    assert value == "2025Q101"
    assert "1,000 rows" in label
    assert "refreshed 2026-01-01 00:00 UTC" in label


def test_selection_summary_totals_rows(controller):
    assert controller.selection_summary(None, []) == "No table selected"
    assert controller.selection_summary("FactSales", []) == "0 of 4 partition(s) selected"
    text = controller.selection_summary("FactSales", ["2025Q101", "2025Q102"])
    assert text == "2 of 4 partition(s) selected - 3,000 rows"


def test_names_matching_and_range(controller):
    assert controller.names_matching("FactSales", "Q1") == ["2025Q101", "2025Q102"]
    assert controller.names_matching("FactSales", "  ") == []
    # range is order-insensitive
    assert controller.names_in_range("FactSales", "2025Q204", "2025Q102") == [
        "2025Q102",
        "2025Q204",
    ]


def test_controller_run_validates_input(controller):
    opts = controller.make_options(
        dry_run=True, strategy="auto", refresh_type="full", batch_size=0, retry_count=2
    )
    with pytest.raises(ConfigurationError):
        controller.run(None, ["x"], opts)
    with pytest.raises(ConfigurationError):
        controller.run("FactSales", [], opts)


def test_controller_run_executes(controller, backend):
    opts = controller.make_options(
        dry_run=False, strategy="tmsl", refresh_type="full", batch_size=2, retry_count=0
    )
    report = controller.run("FactSales", ["2025Q101", "2025Q102", "2025Q204"], opts)
    assert report.status == "Completed"
    assert [len(s["refresh"]["objects"]) for s in backend.tmsl_scripts] == [2, 1]


# ---- widget panel -----------------------------------------------------------
ipywidgets = pytest.importorskip("ipywidgets")


@pytest.fixture
def panel(backend):
    return build_panel(backend)


def test_panel_starts_disabled_until_partitions_chosen(panel):
    assert panel.run_btn.disabled is True
    assert panel.dry_cb.value is True


def test_choose_table_lists_partitions(panel):
    panel.table_dd.value = "FactSales"
    assert [v for _, v in panel.part_sel.options] == [
        "2025Q101",
        "2025Q102",
        "2025Q204",
        "2025Q307",
    ]
    assert panel.from_dd.value == "2025Q101" and panel.to_dd.value == "2025Q307"
    assert panel.run_btn.disabled is True


def test_quick_select_buttons(panel):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    assert len(panel.part_sel.value) == 4
    panel.btn_none.click()
    assert panel.part_sel.value == ()
    panel.filter_txt.value = "q1"
    panel.btn_match.click()
    assert panel.part_sel.value == ("2025Q101", "2025Q102")
    panel.from_dd.value, panel.to_dd.value = "2025Q102", "2025Q204"
    panel.btn_range.click()
    assert panel.part_sel.value == ("2025Q102", "2025Q204")
    assert panel.run_btn.disabled is False


def _text(out) -> str:
    return "".join(o.get("text", "") for o in out.outputs)


def _has_frame(out) -> bool:
    return any(o.get("output_type") == "display_data" for o in out.outputs)


def test_selection_shows_rows_in_summary(panel):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    assert "10,000 rows" in panel.count_lbl.value


def test_dry_run_plans_only_and_shows_partitions(panel, backend):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    panel.run_btn.click()
    panel.wait(10)
    assert backend.tmsl_scripts == [] and backend.enhanced_payloads == []
    assert "DryRun" in _text(panel.results_out)
    assert "WOULD be refreshed" in _text(panel.results_out)
    assert _has_frame(panel.results_out)  # the selected-partitions table
    assert "Dry run complete" in panel.status_lbl.value
    assert panel.last_report is not None and panel.last_report.status == "DryRun"
    assert panel.show_btn.disabled is False


def test_real_refresh_is_started_from_the_cell_not_the_button(panel, backend, capsys):
    panel.table_dd.value = "FactSales"
    panel.strategy_dd.value = "tmsl"
    panel.btn_all.click()
    panel.dry_cb.value = False
    assert panel.run_btn.disabled is True  # no button path for real refreshes
    assert "panel.run()" in panel.confirm_lbl.value
    with pytest.raises(ConfigurationError, match="I confirm"):
        panel.run()
    assert backend.tmsl_scripts == []

    panel.confirm_cb.value = True
    panel.run()
    out = capsys.readouterr().out
    assert len(backend.tmsl_scripts) == 1
    assert "Finished in" in out and "Status: Completed" in out and "Before / after" in out
    assert panel.confirm_cb.value is False  # must re-confirm for the next run
    assert "Completed" in panel.status_lbl.value
    verification = panel.last_report.verification
    assert verification["refreshed"].all()
    assert (verification["row_count_change"] == 5).all()
    assert panel.table_dd.disabled is False  # controls re-enabled after the run


def test_run_requires_a_selection(panel):
    with pytest.raises(ConfigurationError, match="Select a table"):
        panel.run()


def test_run_dry_prints_plan_and_changes_nothing(panel, backend, capsys):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    panel.run()
    out = capsys.readouterr().out
    assert "WOULD be refreshed" in out and "2025Q307" in out
    assert backend.tmsl_scripts == []


def test_run_prints_live_log_and_heartbeat(panel, backend, capsys, monkeypatch):
    import threading

    monkeypatch.setattr("fabric_partition_refresh.ui._HEARTBEAT_SECONDS", 0.02)
    original = backend.execute_tmsl

    def slow(script):
        threading.Event().wait(0.2)  # time.sleep is stubbed out by conftest
        original(script)

    backend.execute_tmsl = slow
    panel.table_dd.value = "FactSales"
    panel.strategy_dd.value = "tmsl"
    panel.btn_all.click()
    panel.dry_cb.value = False
    panel.confirm_cb.value = True
    panel.run()
    out = capsys.readouterr().out
    assert "still running" in out
    assert "Selected 4 partition(s)" in out  # live log line


def test_show_last_result_redraws(panel):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    panel.run_btn.click()
    panel.wait(10)
    panel.results_out.clear_output()
    panel.show_btn.click()
    assert "DryRun" in _text(panel.results_out)


def test_failed_run_raises_so_the_cell_turns_red(panel, backend, capsys):
    backend.tmsl_failures = 99
    panel.table_dd.value = "FactSales"
    panel.strategy_dd.value = "tmsl"
    panel.retry_int.value = 0
    panel.btn_all.click()
    panel.dry_cb.value = False
    panel.confirm_cb.value = True
    with pytest.raises(RefreshFailedError):
        panel.run()
    assert "Failed" in panel.status_lbl.value
    assert panel.last_error is not None and panel.last_report is not None
    assert "Failed" in _text(panel.results_out)
    assert panel.table_dd.disabled is False


def test_confirmation_text_is_fully_visible(panel):
    panel.table_dd.value = "FactSales"
    panel.btn_all.click()
    panel.dry_cb.value = False
    assert "FactSales" in panel.confirm_lbl.value
    assert "4 of 4 partition(s) selected" in panel.confirm_lbl.value
    assert panel.confirm_lbl.layout.display == "flex"
    panel.dry_cb.value = True
    assert panel.confirm_lbl.layout.display == "none"
