# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org).

## [Unreleased]

### Fixed
- `auto` strategy now also falls back to TMSL when the service accepts the Enhanced Refresh request and then fails it asynchronously with the refresh-policy error.

### Changed
- UI: real refreshes now start from a notebook cell, `panel.run()`, so Fabric shows the cell as running for the whole refresh, with live log and a heartbeat every minute. The panel button is for dry runs (Plan) only. The confirmation text is shown in full.
- `state` and `mode` codes shown as names (Ready, Import ...) where certain; refresh times in the UI are labelled UTC.
- UI: refresh runs in a background thread; results (status, before/after, audit) stay on screen and are stored in `panel.last_report`; dry run now lists the partitions it would refresh.
- Row counts shown per partition while selecting and in the before/after table (when the model exposes them).

### Added
- Point-and-click `launch_refresh_ui()` panel (`ipywidgets`) with dry-run default and explicit confirmation before a real refresh; `notebooks/refresh_ui.ipynb`.
- `list_partitions()` to browse one table's partitions; notebook now follows connect → discover → browse → select.

## [0.1.0]

### Added
- Initial release: partition discovery, selection (names, substring, regex, range), refresh via Enhanced Refresh API or TMSL with automatic fallback, batching, retries, running-refresh guard, before/after verification and audit frame.
