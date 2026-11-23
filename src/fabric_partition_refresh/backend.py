"""Service access layer.

Everything that touches Microsoft Fabric lives behind the small
:class:`FabricBackend` protocol. The rest of the package is pure Python and is
unit-tested against an in-memory fake, so no Fabric capacity is needed for CI.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from .exceptions import FabricApiError, PartitionRefreshError
from .inventory import standardize_partition_stats

logger = logging.getLogger(__name__)


@runtime_checkable
class FabricBackend(Protocol):
    """Operations the refresher needs from Fabric / Power BI."""

    workspace: str
    semantic_model: str

    def query_dax(self, dax: str) -> pd.DataFrame:
        """Run a DAX query against the semantic model."""

    def execute_tmsl(self, script: dict[str, Any]) -> None:
        """Run a TMSL command (synchronous). Raise :class:`FabricApiError` on failure."""

    def start_enhanced_refresh(self, payload: dict[str, Any]) -> str | None:
        """POST an Enhanced Refresh request; return the request id."""

    def get_refresh(self, request_id: str) -> dict[str, Any]:
        """Return the refresh record for ``request_id``."""

    def recent_refreshes(self, top: int = 5) -> list[dict[str, Any]]:
        """Return the most recent refresh history records."""


class SempyBackend:
    """:class:`FabricBackend` implemented with semantic-link (``sempy``).

    Works inside a Microsoft Fabric notebook, where ``sempy`` is preinstalled and
    authenticates as the notebook user.
    """

    def __init__(self, workspace: str, semantic_model: str) -> None:
        try:
            import sempy.fabric as fabric
        except ImportError as exc:  # pragma: no cover - only outside Fabric
            raise PartitionRefreshError(
                "The 'sempy' package (semantic-link) is required. Run this inside a "
                "Microsoft Fabric notebook, or `pip install fabric-partition-refresh[fabric]`."
            ) from exc

        self._fabric = fabric
        self.workspace = workspace
        self.semantic_model = semantic_model
        self.workspace_id: str = str(fabric.resolve_workspace_id(workspace))
        self.semantic_model_id: str = str(
            fabric.resolve_dataset_id(semantic_model, workspace=self.workspace_id)
        )
        self._client = fabric.FabricRestClient()

    # -- helpers ---------------------------------------------------------------
    @property
    def _refreshes_path(self) -> str:
        return f"/v1.0/myorg/groups/{self.workspace_id}/datasets/{self.semantic_model_id}/refreshes"

    @staticmethod
    def _check(response: Any, action: str) -> None:
        if not 200 <= response.status_code < 300:
            try:
                body = response.text
            except Exception:
                body = ""
            raise FabricApiError(action, response.status_code, body)

    # -- FabricBackend ---------------------------------------------------------
    def query_dax(self, dax: str) -> pd.DataFrame:
        df: pd.DataFrame = self._fabric.evaluate_dax(
            dataset=self.semantic_model_id, dax_string=dax, workspace=self.workspace_id
        )
        return df

    def partition_stats(self) -> pd.DataFrame:
        """Row counts per partition (``table_name, partition_name, row_count``).

        Returns an empty frame if the service does not expose them.
        """
        raw = self._fabric.list_partitions(
            dataset=self.semantic_model_id, extended=True, workspace=self.workspace_id
        )
        stats = standardize_partition_stats(raw)
        if stats is None:
            logger.warning(
                "Row counts unavailable: no record-count column found. Columns returned: %s",
                list(raw.columns),
            )
            return pd.DataFrame(columns=["table_name", "partition_name", "row_count"])
        return stats

    def execute_tmsl(self, script: dict[str, Any]) -> None:
        try:
            self._fabric.execute_tmsl(script=script, workspace=self.workspace_id)
        except Exception as exc:
            raise FabricApiError("TMSL refresh", None, str(exc)) from exc

    def start_enhanced_refresh(self, payload: dict[str, Any]) -> str | None:
        response = self._client.post(self._refreshes_path, json=payload)
        self._check(response, "Trigger enhanced refresh")
        location = response.headers.get("Location") or response.headers.get("location")
        return location.rstrip("/").split("/")[-1] if location else None

    def get_refresh(self, request_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self._refreshes_path}/{request_id}")
        self._check(response, f"Get refresh request {request_id}")
        return dict(response.json())

    def recent_refreshes(self, top: int = 5) -> list[dict[str, Any]]:
        response = self._client.get(f"{self._refreshes_path}?$top={top}")
        self._check(response, "Get refresh history")
        return list(response.json().get("value", []))
