"""Platform contract for registered simulation adapters."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any, Protocol
from control_plane.simulation.gateway import SimulationGateway

PLATFORM_ADAPTER_INTERFACE_VERSIONS = {1}

class SimulationAdapter(Protocol):
    """Adapter boundary.

    ``qualify`` receives a read-only context Mapping with at least
    ``evaluation_id`` (str), ``candidate_id`` (str), ``attempt_ids``
    (tuple[str, ...]), and ``artifact_ids`` (tuple[str, ...]) collected for
    this evaluation.  Adapters should use these identities to locate evidence
    through the middleware rather than reverse-engineering repository state.
    """
    adapter_id: str
    def build_gateway(self, context: Mapping[str, Any]) -> SimulationGateway: ...
    def materialize_package(self, evaluation_input: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, str]: ...
    def validate_package(self, context: Mapping[str, Any], task: Mapping[str, Any], preparation: Mapping[str, Any], package: Mapping[str, str]) -> None: ...
    def qualify(self, middleware: Any, attempt_id: str, context: Mapping[str, Any]) -> Mapping[str, Any]: ...
