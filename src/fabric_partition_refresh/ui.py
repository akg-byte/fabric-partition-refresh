"""Point-and-click refresh panel for Fabric notebooks.

Designed for support teams: pick a table, tick partitions (with row counts and
last refresh time shown), plan (dry run) or refresh. No code editing required.

The module is split in two so the logic is testable without a notebook:

* :class:`RefreshController` - pure Python state and actions.
* :class:`RefreshPanel` - the ``ipywidgets`` view on top of the controller.

A dry run ("Plan") is started with the panel button. A **real refresh is started by
running a notebook cell, ``panel.run()``**, so Fabric's normal "running" indicator is
visible for as long as the refresh takes. The outcome is kept in
``panel.last_report`` / ``panel.last_error`` and shown in a results area.
"""

from __future__ import annotations

import html
import logging
import sys
import threading
import time
from datetime import datetime
from typing import Any

import pandas as pd

from .backend import FabricBackend, SempyBackend
from .exceptions import ConfigurationError, PartitionRefreshError
from .inventory import summarize_inventory
from .models import PartitionSelector, RefreshOptions, RefreshReport
from .refresh import discover_partitions, refresh_partitions

_PACKAGE_LOGGER = "fabric_partition_refresh"
_PLACEHOLDER = "- select a table -"
_HEARTBEAT_SECONDS = 60.0  # "still running" line interval for panel.run()

_SELECTED_COLS = ["table_name", "partition_name", "row_count", "refreshed_time", "state"]
_VERIFY_COLS = [
    "partition_name",
    "row_count_before",
    "row_count_after",
    "row_count_change",
    "refreshed_time_before",
    "refreshed_time_after",
    "refreshed",
    "state_after",
    "error_message_after",
]
_AUDIT_COLS = [
    "batch",
    "partition",
    "strategy",
    "status",
    "started_utc",
    "finished_utc",
    "attempts",
    "error",
]


def _format_time(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "never" if pd.isna(ts) else f"{ts:%Y-%m-%d %H:%M} UTC"


def _format_rows(value: Any) -> str:
    return "rows n/a" if pd.isna(value) else f"{int(value):,} rows"


def _duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _pick(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return df[[c for c in cols if c in df.columns]]


class RefreshController:
    """UI-independent state and actions."""

    def __init__(self, backend: FabricBackend) -> None:
        self.backend = backend
        self.inventory: pd.DataFrame = pd.DataFrame()

    # -- loading ---------------------------------------------------------------
    def load(self) -> None:
        """(Re)read partition metadata (and row counts, when available)."""
        self.inventory = discover_partitions(self.backend)

    # -- browsing --------------------------------------------------------------
    def table_options(self) -> list[tuple[str, str | None]]:
        """Dropdown options ``(label, table_name)``: 'Table (N partitions)'."""
        summary = summarize_inventory(self.inventory).sort_values("table_name")
        options: list[tuple[str, str | None]] = [(_PLACEHOLDER, None)]
        for _, row in summary.iterrows():
            n = int(row["partition_count"])
            options.append((f"{row['table_name']}  ({n})", str(row["table_name"])))
        return options

    def partitions(self, table: str) -> pd.DataFrame:
        inv = self.inventory
        return inv[inv["table_name"] == table].sort_values("partition_name").reset_index(drop=True)

    def partition_options(self, table: str) -> list[tuple[str, str]]:
        """List options ``(label, partition_name)`` with row count and last refresh."""
        return [
            (
                f"{r.partition_name}   |   {_format_rows(r.row_count)}   |   "
                f"refreshed {_format_time(r.refreshed_time)}",
                str(r.partition_name),
            )
            for r in self.partitions(table).itertuples()
        ]

    def selection_summary(self, table: str | None, names: list[str]) -> str:
        """e.g. '3 of 26 partition(s) selected - 1,234,567 rows'."""
        if not table:
            return "No table selected"
        parts = self.partitions(table)
        chosen = parts[parts["partition_name"].isin(names)]
        text = f"{len(chosen)} of {len(parts)} partition(s) selected"
        if len(chosen) and chosen["row_count"].notna().any():
            text += f" - {int(chosen['row_count'].sum()):,} rows"
        return text

    # -- quick selections ------------------------------------------------------
    def names_matching(self, table: str, text: str) -> list[str]:
        needle = text.strip().lower()
        names = self.partitions(table)["partition_name"].tolist()
        return [n for n in names if needle in n.lower()] if needle else []

    def names_in_range(self, table: str, first: str, last: str) -> list[str]:
        lo, hi = sorted((first, last))
        names = self.partitions(table)["partition_name"].tolist()
        return [n for n in names if lo <= n <= hi]

    # -- running ---------------------------------------------------------------
    @staticmethod
    def make_options(
        *,
        dry_run: bool,
        strategy: str,
        refresh_type: str,
        batch_size: int,
        retry_count: int,
    ) -> RefreshOptions:
        return RefreshOptions(
            dry_run=dry_run,
            strategy=strategy,  # type: ignore[arg-type]
            refresh_type=refresh_type,
            batch_size=batch_size or None,
            retry_count=retry_count,
        )

    def run(self, table: str | None, names: list[str], options: RefreshOptions) -> RefreshReport:
        if not table:
            raise ConfigurationError("Select a table first.")
        if not names:
            raise ConfigurationError("Select at least one partition.")
        selector = PartitionSelector(table=table, names=tuple(names))
        return refresh_partitions(
            self.backend.workspace,
            self.backend.semantic_model,
            selector,
            options,
            backend=self.backend,
        )


class _OutputLogHandler(logging.Handler):
    """Mirror log records into an ``ipywidgets.Output`` widget."""

    def __init__(self, out: Any) -> None:
        super().__init__(level=logging.INFO)
        self._out = out

    def emit(self, record: logging.LogRecord) -> None:
        self._out.append_stdout(self.format(record) + "\n")


class RefreshPanel:
    """The ``ipywidgets`` view. ``panel.widget`` is the root widget to display.

    After a run, ``panel.last_report`` (or ``panel.last_error``) holds the outcome,
    even if the on-screen output was lost. Use ``panel.last_report.verification``.
    """

    def __init__(self, controller: RefreshController) -> None:
        import ipywidgets as w

        self.controller = controller
        self.last_report: RefreshReport | None = None
        self.last_error: Exception | None = None
        self._worker: threading.Thread | None = None
        full = w.Layout(width="auto")

        self.title = w.HTML(
            f"<h3>Refresh partitions</h3>"
            f"<b>Workspace:</b> {html.escape(controller.backend.workspace)} &nbsp; "
            f"<b>Model:</b> {html.escape(controller.backend.semantic_model)}"
        )
        self.reload_btn = w.Button(description="Reload metadata", icon="refresh")
        self.table_dd = w.Dropdown(
            options=controller.table_options(), description="Table", layout=full
        )
        self.part_sel = w.SelectMultiple(
            options=[],
            rows=12,
            description="Partitions",
            layout=w.Layout(width="auto"),
        )
        self.btn_all = w.Button(description="Select all")
        self.btn_none = w.Button(description="Clear")
        self.filter_txt = w.Text(
            placeholder="text in name, e.g. 2025", layout=w.Layout(width="200px")
        )
        self.btn_match = w.Button(description="Select matching")
        self.from_dd = w.Dropdown(options=[], description="From", layout=w.Layout(width="200px"))
        self.to_dd = w.Dropdown(options=[], description="To", layout=w.Layout(width="200px"))
        self.btn_range = w.Button(description="Select range")
        self.count_lbl = w.HTML("")

        self.strategy_dd = w.Dropdown(
            options=["auto", "tmsl", "enhanced"], value="auto", description="Strategy"
        )
        self.type_dd = w.Dropdown(
            options=["full", "automatic", "dataOnly", "calculate", "clearValues"],
            value="full",
            description="Type",
        )
        self.batch_int = w.BoundedIntText(
            value=0, min=0, max=1000, description="Batch size", tooltip="0 = one request"
        )
        self.retry_int = w.BoundedIntText(value=2, min=0, max=10, description="Retries")
        self.dry_cb = w.Checkbox(
            value=True, description="Dry run (plan only - nothing is refreshed)", indent=False
        )
        self.confirm_lbl = w.HTML("")
        self.confirm_cb = w.Checkbox(value=False, description="I confirm", indent=False)
        self.run_btn = w.Button(description="Plan (dry run)", button_style="info", icon="search")
        self.show_btn = w.Button(description="Show last result", icon="list", disabled=True)
        self.status_lbl = w.HTML("")
        self.log_out = w.Output(layout=w.Layout(border="1px solid #ddd", padding="6px"))
        self.results_out = w.Output(layout=w.Layout(border="1px solid #ddd", padding="6px"))

        self._controls = [
            self.reload_btn,
            self.table_dd,
            self.part_sel,
            self.btn_all,
            self.btn_none,
            self.filter_txt,
            self.btn_match,
            self.from_dd,
            self.to_dd,
            self.btn_range,
            self.strategy_dd,
            self.type_dd,
            self.batch_int,
            self.retry_int,
            self.dry_cb,
            self.confirm_cb,
        ]

        self.widget = w.VBox(
            [
                self.title,
                w.HBox([self.table_dd, self.reload_btn]),
                self.part_sel,
                w.HBox([self.btn_all, self.btn_none, self.filter_txt, self.btn_match]),
                w.HBox([self.from_dd, self.to_dd, self.btn_range]),
                self.count_lbl,
                w.HBox([self.strategy_dd, self.type_dd]),
                w.HBox([self.batch_int, self.retry_int]),
                self.dry_cb,
                self.confirm_lbl,
                self.confirm_cb,
                w.HBox([self.run_btn, self.show_btn]),
                self.status_lbl,
                w.HTML("<b>Log</b>"),
                self.log_out,
                w.HTML("<b>Result</b>"),
                self.results_out,
            ]
        )

        self.table_dd.observe(self._on_table, names="value")
        self.part_sel.observe(self._sync_state, names="value")
        self.dry_cb.observe(self._sync_state, names="value")
        self.confirm_cb.observe(self._sync_state, names="value")
        self.btn_all.on_click(lambda _b: self._select(list(self._all_names())))
        self.btn_none.on_click(lambda _b: self._select([]))
        self.btn_match.on_click(self._on_match)
        self.btn_range.on_click(self._on_range)
        self.reload_btn.on_click(self._on_reload)
        self.run_btn.on_click(self._on_run)
        self.show_btn.on_click(lambda _b: self._render_last())
        self._sync_state()

    # -- helpers ---------------------------------------------------------------
    def _all_names(self) -> list[str]:
        t = self.table_dd.value
        return self.controller.partitions(t)["partition_name"].tolist() if t else []

    def _select(self, names: list[str]) -> None:
        self.part_sel.value = tuple(n for n in self._all_names() if n in set(names))

    def _sync_state(self, _change: Any = None) -> None:
        n = len(self.part_sel.value)
        summary = self.controller.selection_summary(self.table_dd.value, list(self.part_sel.value))
        self.count_lbl.value = f"<b>{html.escape(summary)}</b>"
        dry = self.dry_cb.value
        self.confirm_cb.layout.display = "none" if dry else "flex"
        self.confirm_lbl.layout.display = "none" if dry else "flex"
        self.confirm_lbl.value = (
            f"<b style='color:#b00'>This will refresh</b> {html.escape(summary)}<br>"
            f"Table: <b>{html.escape(str(self.table_dd.value))}</b> &nbsp; "
            f"Strategy: <b>{self.strategy_dd.value}</b> &nbsp; Type: <b>{self.type_dd.value}</b>"
            "<br>Tick <b>I confirm</b>, then <b>run the next cell</b> (<code>panel.run()</code>) "
            "to start. That cell shows as running until the refresh finishes."
        )
        if dry:
            self.confirm_cb.value = False
            self.run_btn.description = "Plan (dry run)"
            self.run_btn.button_style = "info"
            self.run_btn.icon = "search"
            self.run_btn.disabled = n == 0
        else:
            self.run_btn.description = "Refresh: run the next cell"
            self.run_btn.button_style = "warning"
            self.run_btn.icon = "arrow-down"
            self.run_btn.disabled = True  # real refreshes start from the notebook cell

    def _set_busy(self, busy: bool) -> None:
        for c in self._controls:
            c.disabled = busy
        if busy:
            self.run_btn.disabled = True

    # -- events ----------------------------------------------------------------
    def _on_table(self, change: Any) -> None:
        table = change["new"]
        opts = self.controller.partition_options(table) if table else []
        self.part_sel.options = opts
        names = [v for _, v in opts]
        self.from_dd.options = names
        self.to_dd.options = names
        if names:
            self.from_dd.value, self.to_dd.value = names[0], names[-1]
        self.part_sel.value = ()
        self.confirm_cb.value = False
        self._sync_state()

    def _on_match(self, _b: Any) -> None:
        t = self.table_dd.value
        if t:
            self._select(self.controller.names_matching(t, self.filter_txt.value))

    def _on_range(self, _b: Any) -> None:
        t = self.table_dd.value
        if t and self.from_dd.value and self.to_dd.value:
            self._select(self.controller.names_in_range(t, self.from_dd.value, self.to_dd.value))

    def _on_reload(self, _b: Any) -> None:
        current = self.table_dd.value
        self.controller.load()
        self.table_dd.options = self.controller.table_options()
        known = {v for _, v in self.table_dd.options}
        self.table_dd.value = current if current in known else None
        if self.table_dd.value:  # refresh partition list / row counts / times
            self._on_table({"new": self.table_dd.value})

    def _on_run(self, _b: Any) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        table = self.table_dd.value
        names = list(self.part_sel.value)
        try:
            options = self.controller.make_options(
                dry_run=self.dry_cb.value,
                strategy=self.strategy_dd.value,
                refresh_type=self.type_dd.value,
                batch_size=self.batch_int.value,
                retry_count=self.retry_int.value,
            )
        except PartitionRefreshError as exc:
            self.status_lbl.value = f"<b style='color:#b00'>Error:</b> {html.escape(str(exc))}"
            return

        self.log_out.clear_output()
        self.results_out.clear_output()
        self._set_busy(True)
        started = time.monotonic()
        self.status_lbl.value = (
            f"<b>{'Planning' if options.dry_run else 'Refreshing'}...</b> started "
            f"{datetime.now():%H:%M:%S}. TMSL refreshes show no progress until they finish; "
            "this panel updates when done."
        )
        self._worker = threading.Thread(
            target=self._run_worker, args=(table, names, options, started), daemon=True
        )
        self._worker.start()

    def _run_worker(
        self, table: str | None, names: list[str], options: RefreshOptions, started: float
    ) -> None:
        handler = _OutputLogHandler(self.log_out)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger(_PACKAGE_LOGGER)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.last_report, self.last_error = None, None
        try:
            self.last_report = self.controller.run(table, names, options)
        except PartitionRefreshError as exc:
            self.last_error = exc
            self.last_report = getattr(exc, "report", None)
        except Exception as exc:
            self.last_error = exc
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            elapsed = _duration(time.monotonic() - started)
            self._render_last()
            self.status_lbl.value = self._final_status(elapsed)
            self.show_btn.disabled = False
            self.confirm_cb.value = False  # force a fresh confirmation for the next run
            self._set_busy(False)
            self._sync_state()

    def run(self) -> None:
        """Run the refresh configured in the panel, **in this notebook cell**.

        Use after ticking **I confirm** in the panel. The cell shows as running until
        the refresh finishes, prints the log live and a heartbeat every minute, then
        shows the results. Raises if the refresh fails, so the cell turns red.
        """
        from IPython.display import display as ipy_display

        show: Any = ipy_display
        if self._worker is not None and self._worker.is_alive():
            raise ConfigurationError("A plan is still running in the panel. Wait for it to end.")
        table = self.table_dd.value
        names = list(self.part_sel.value)
        if not table or not names:
            raise ConfigurationError(
                "Select a table and at least one partition in the panel above first."
            )
        options = self.controller.make_options(
            dry_run=self.dry_cb.value,
            strategy=self.strategy_dd.value,
            refresh_type=self.type_dd.value,
            batch_size=self.batch_int.value,
            retry_count=self.retry_int.value,
        )
        if not options.dry_run and not self.confirm_cb.value:
            raise ConfigurationError(
                "Tick 'I confirm' in the panel above, then run this cell again."
            )

        self.log_out.clear_output()
        self.results_out.clear_output()
        self._set_busy(True)
        started = time.monotonic()
        verb = "Planning" if options.dry_run else "Refreshing"
        self.status_lbl.value = (
            f"<b>{verb}...</b> started {datetime.now():%H:%M:%S} - see the running cell below."
        )

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logger = logging.getLogger(_PACKAGE_LOGGER)
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(_HEARTBEAT_SECONDS):
                elapsed = _duration(time.monotonic() - started)
                print(
                    f"{datetime.now():%H:%M:%S}  ... still running, {elapsed} elapsed", flush=True
                )

        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        error: Exception | None = None
        self.last_report, self.last_error = None, None
        try:
            print(
                f"{verb} {len(names)} partition(s) of '{table}'. Started {datetime.now():%H:%M:%S}."
            )
            self.last_report = self.controller.run(table, names, options)
        except PartitionRefreshError as exc:
            error = exc
            self.last_error = exc
            self.last_report = getattr(exc, "report", None)
        finally:
            stop.set()
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            elapsed = _duration(time.monotonic() - started)
            self._render_last()
            self.status_lbl.value = self._final_status(elapsed)
            self.show_btn.disabled = False
            self.confirm_cb.value = False  # a new real run needs a fresh confirmation
            self._set_busy(False)
            self._sync_state()

        print(f"\nFinished in {elapsed}.")
        self._print_report(show)
        if error is not None:
            raise error

    def _print_report(self, show: Any) -> None:
        report = self.last_report
        if report is None:
            return
        print(f"Status: {report.status}")
        if report.dry_run:
            print("Dry run: nothing was refreshed. These partitions WOULD be refreshed:")
            show(_pick(report.selected, _SELECTED_COLS))
        if report.verification is not None:
            print("\nBefore / after (refreshed = refresh time moved forward):")
            show(_pick(report.verification, _VERIFY_COLS))
        if report.batches:
            print("\nAudit:")
            show(_pick(report.to_audit_frame(), _AUDIT_COLS))

    def wait(self, timeout: float | None = None) -> None:
        """Block until the current run (if any) finishes. Handy in scripts and tests."""
        if self._worker is not None:
            self._worker.join(timeout)

    # -- rendering -------------------------------------------------------------
    def _final_status(self, elapsed: str) -> str:
        if self.last_error is not None:
            return (
                f"<b style='color:#b00'>Failed</b> after {elapsed}: "
                f"{html.escape(str(self.last_error))}"
            )
        report = self.last_report
        if report is None:
            return f"Finished in {elapsed}."
        if report.dry_run:
            return (
                f"<b style='color:#06c'>Dry run complete</b> in {elapsed}. Nothing was refreshed."
            )
        return f"<b style='color:#080'>{html.escape(report.status)}</b> in {elapsed}."

    def _render_last(self) -> None:
        """(Re)draw the stored outcome into the results area."""
        out = self.results_out
        out.clear_output()
        if self.last_error is not None:
            out.append_stdout(f"ERROR: {self.last_error}\n")
        report = self.last_report
        if report is None:
            if self.last_error is None:
                out.append_stdout("No result yet.\n")
            return
        out.append_stdout(f"Status: {report.status}\n")
        if report.dry_run:
            out.append_stdout(
                "Dry run: nothing was refreshed. These partitions WOULD be refreshed "
                "(untick 'Dry run' to refresh):\n"
            )
            out.append_display_data(_pick(report.selected, _SELECTED_COLS))
        if report.verification is not None:
            out.append_stdout("\nBefore / after (refreshed = refresh time moved forward):\n")
            out.append_display_data(_pick(report.verification, _VERIFY_COLS))
        if report.batches:
            out.append_stdout("\nAudit:\n")
            out.append_display_data(_pick(report.to_audit_frame(), _AUDIT_COLS))


def build_panel(backend: FabricBackend) -> RefreshPanel:
    """Load metadata and build the panel for an existing backend (testable)."""
    controller = RefreshController(backend)
    controller.load()
    return RefreshPanel(controller)


def launch_refresh_ui(
    workspace: str, semantic_model: str, *, backend: FabricBackend | None = None
) -> RefreshPanel:
    """Display the refresh panel in a Fabric notebook.

    Assign the result (``panel = launch_refresh_ui(...)``) so that
    ``panel.last_report`` stays available after a run.

    Args:
        workspace: Workspace name.
        semantic_model: Semantic model name.
        backend: Optional custom backend (defaults to :class:`SempyBackend`).
    """
    from IPython.display import display

    show: Any = display
    panel = build_panel(backend or SempyBackend(workspace, semantic_model))
    show(panel.widget)
    return panel
