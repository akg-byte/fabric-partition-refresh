# Architecture

```
                      refresh_partitions()            <- public entry point (refresh.py)
                             |
   +-------------------------+--------------------------+
   |                         |                          |
 inventory.py            selection.py              models.py
 INFO.* -> DataFrame     filter by selector        PartitionSelector, RefreshOptions,
                                                   RefreshReport (dataclasses, validated)
   |
 backend.py  <-- FabricBackend (Protocol)
   |            SempyBackend: the only module that imports sempy
 audit.py       before/after verification
```

## Principles

- **All Fabric access is behind `FabricBackend`.** Everything else is pure Python and tested with `tests/conftest.py::FakeBackend`. CI never needs a Fabric capacity.
- **Safe by default.** `dry_run=True`; empty partition lists are refused at the builder level.
- **Fail loudly, keep the evidence.** Failures raise, and the exception carries the partial report.
- **Validated configuration.** Frozen dataclasses validate in `__post_init__`; bad values fail before any service call.

## Why TMSL fallback exists

The Enhanced Refresh REST API rejects requests that name partitions of a table with an incremental-refresh policy ("Cannot specify partition for table having refresh policy defined"). A TMSL `refresh` command sent via XMLA (`fabric.execute_tmsl`) has no such restriction. `Strategy.AUTO` tries the API first and falls back when it sees that message.

TMSL is synchronous (the call blocks until the refresh ends), so there is no polling for that path.

## Adding a backend

Implement the five methods of `FabricBackend` (`query_dax`, `execute_tmsl`, `start_enhanced_refresh`, `get_refresh`, `recent_refreshes`) plus `workspace` and `semantic_model` attributes, and pass it as `backend=`. This is how you could run outside Fabric (e.g. with `azure-identity` and raw XMLA/REST).
