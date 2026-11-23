# Contributing

Thanks for helping improve this project.

## Setup

```bash
pip install -e ".[dev]"
```

## Before opening a pull request

```bash
ruff check . && ruff format --check .
mypy
pytest
```

CI runs the same checks on Python 3.10-3.12.

## Guidelines

- Keep Fabric-specific calls inside `backend.py`. New logic should be testable with `FakeBackend`.
- Add or update tests for every behaviour change.
- Update `CHANGELOG.md` under **Unreleased**.
- Keep public API changes backward compatible, or call them out clearly (we follow [SemVer](https://semver.org)).
- Never commit real workspace names, IDs, tenant details or customer data, including in tests and examples.

## Reporting bugs

Open an issue with the library version, the strategy used, the (anonymised) error message, and the relevant log output (`logging.basicConfig(level=logging.INFO)`).

## Releasing (maintainers)

1. Move **Unreleased** notes in `CHANGELOG.md` under a new version heading.
2. Bump `version` in `pyproject.toml`.
3. Tag `vX.Y.Z` and create a GitHub release. The `release` workflow publishes to PyPI.
