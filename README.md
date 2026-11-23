# fabric-partition-refresh

Refresh **specific partitions** of a Microsoft Fabric / Power BI semantic model from a Fabric notebook, safely and repeatably.

Typical use: reloading historic partitions of an incremental-refresh table (for example after a source-data correction) without refreshing the whole model.

## Features

- **Dry run by default**: discovers and shows the selected partitions; refreshes nothing until you opt in.
- **Flexible selection** of one table's partitions: all, by name, substring, regex, or a name range (`min_partition` / `max_partition`).
- **Works with incremental-refresh tables.** The Enhanced Refresh API refuses to target partitions of a table with a refresh policy. With `strategy="auto"` the library tries the API and falls back to a TMSL `refresh` command automatically.
- **Batching**: refresh N partitions at a time so a failure doesn't throw away hours of work.
- **Guards**: refuses to start if the model is already refreshing; refuses to build a request with an empty partition list (which could refresh the whole model).
- **Retries, polling and timeouts** for long-running refreshes.
- **Verification**: re-reads partition metadata afterwards and gives you a before/after table showing which `refreshed_time` values moved.
- **Audit frame**: one row per partition with batch, status, timing and error.
- **Point-and-click panel** for support teams (no code editing).
- Business logic is plain Python, unit-tested without Fabric.

## Quick start

In a Fabric notebook:

```python
%pip install fabric-partition-refresh
```

```python
import logging
from fabric_partition_refresh import (
    PartitionSelector,
    RefreshOptions,
    SempyBackend,
    summarize_tables,
    list_partitions,
    refresh_partitions,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")

# 1. Connect (only workspace + model are needed up front)
backend = SempyBackend("My Workspace", "My Semantic Model")

# 2. Discover tables, then browse one table's partitions
display(summarize_tables(backend))
display(list_partitions(backend, "FactSales"))

# 3. Now choose what to refresh and do a dry run (default): refreshes nothing
selector = PartitionSelector(table="FactSales", min_partition="2025Q101", max_partition="2025Q412")
report = refresh_partitions("My Workspace", "My Semantic Model", selector, backend=backend)
display(report.selected)

# 4. Run for real
report = refresh_partitions(
    "My Workspace",
    "My Semantic Model",
    selector,
    RefreshOptions(dry_run=False, batch_size=4),
    backend=backend,
)
display(report.verification)  # before/after refreshed_time
display(report.to_audit_frame())  # one row per partition
```

A ready-made notebook is in [`notebooks/`](notebooks/refresh_partitions.ipynb).

## Point-and-click UI (for support teams)

No code editing needed. In a Fabric notebook:

```python
from fabric_partition_refresh import launch_refresh_ui

panel = launch_refresh_ui("My Workspace", "My Semantic Model")
```

Pick a table from the dropdown, select partitions (tick them, **Select all**, **Select matching** text, or a **From/To** range), then click **Plan** for a dry run. Each partition shows its **row count** and last refresh time in UTC (row counts need the semantic model to expose them; otherwise "rows n/a" is shown).

To refresh for real, untick **Dry run**, tick **I confirm**, then **run the next notebook cell**:

```python
panel.run()
```

That cell shows as *running* for as long as the refresh takes (a refresh can take many minutes), prints the log live and a "still running" line every minute, then shows the before/after table (refresh times and row-count change) and the audit table. If the refresh fails, the cell fails. The outcome is also stored in `panel.last_report` (for example `panel.last_report.verification`). See [`notebooks/refresh_ui.ipynb`](notebooks/refresh_ui.ipynb).

## Selecting partitions

```python
PartitionSelector("FactSales")  # every partition
PartitionSelector("FactSales", contains="2025")  # name contains text
PartitionSelector("FactSales", pattern=r"^2025Q[12]")  # regex
PartitionSelector("FactSales", names=("2025Q101", "2025Q102"))  # explicit list
PartitionSelector(
    "FactSales", min_partition="2024Q410", max_partition="2025Q412"
)  # inclusive range
```

Criteria combine with AND. Range comparison is by string, which is correct for the chronologically sortable names an incremental-refresh policy generates (`2025Q101`, `202511`, `20250115`).

## Options

| Option | Default | Meaning |
|---|---|---|
| `dry_run` | `True` | Plan only. Set `False` to refresh. |
| `strategy` | `auto` | `auto`, `enhanced` (REST API), or `tmsl` (XMLA). |
| `refresh_type` | `full` | `full`, `automatic`, `dataOnly`, `calculate`, `clearValues`, `defragment`. |
| `commit_mode` | `transactional` | Enhanced API only. |
| `max_parallelism` | `1` | Enhanced API only. |
| `retry_count` | `2` | Enhanced: service-side. TMSL: client-side retries per batch. |
| `timeout` | `04:00:00` | Enhanced API only. |
| `batch_size` | `None` | Partitions per request. `None` = one request. |
| `poll_interval_seconds` / `max_poll_minutes` | `30` / `180` | Enhanced API polling. |
| `check_running_refresh` | `True` | Refuse to start if a refresh is in progress. |

## Requirements and permissions

The person running the notebook (for example, a support engineer) needs:

| Need | Why |
|---|---|
| A Microsoft Fabric notebook (`sempy` / semantic-link is preinstalled) | Runs the code and authenticates as the signed-in user |
| **Write permission on the semantic model** (workspace Contributor, Member or Admin, or explicit Write permission on the model) | Required to refresh partitions |
| **XMLA endpoint set to read-write** on the capacity (Admin portal > Capacity settings > Power BI workloads) | Required for the TMSL path, which is what incremental-refresh tables use |
| The tenant setting **Allow XMLA endpoints and Analyze in Excel with on-premises datasets** enabled for the user | Lets the user reach the XMLA endpoint |

If the XMLA endpoint is read-only or blocked, TMSL refreshes fail. Ask your Fabric admin to enable read-write.

## Good to know

- **Incremental-refresh tables:** the Enhanced Refresh API cannot target their partitions, so `strategy="auto"` tries it, fails, and falls back to TMSL. Each such run leaves one **Failed** entry in the model's refresh history. If you have failure alerts, use `strategy="tmsl"` for these tables to skip the failed first attempt.
- **TMSL runs block the notebook** until the refresh finishes, and XMLA refreshes only appear in the refresh history once they complete.
- **Concurrent-refresh guard:** `check_running_refresh` reads the refresh history, so it detects scheduled and Enhanced API refreshes, but **not** another XMLA/TMSL refresh that is still in progress. Do not start a second run while one is still going.

## Failure behaviour

If a batch fails, remaining batches are skipped and `RefreshFailedError` is raised. Its `.report` attribute holds the partial `RefreshReport`, so you can see exactly which partitions completed and re-run only the rest.

## Development

```bash
git clone https://github.com/akg-byte/fabric-partition-refresh
cd fabric-partition-refresh
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/architecture.md](docs/architecture.md).

## License

MIT. See [LICENSE](LICENSE).
