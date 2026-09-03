#!/usr/bin/env python3
"""Contract drift prevention tests for core storage and execution ports."""

from __future__ import annotations

import inspect
import unittest
from typing import Any

from control_plane.core.ports import (
    ArtifactStore,
    ControlStore,
    ResourceMonitor,
    TargetCatalog,
)
from control_plane.evaluation.execution_topology import ProjectFileTargetCatalog
from control_plane.evaluation.project_ports import ProjectFileControlStore


class PortsContractDriftTests(unittest.TestCase):
    """Ensure built-in implementations never drift from declared public Protocols."""

    def _assert_protocol_methods_implemented(
        self,
        protocol: type[Any],
        implementation: type[Any],
        *,
        required_methods: set[str],
    ) -> None:
        """Verify implementation implements all required protocol methods with matching signatures."""
        for method_name in required_methods:
            self.assertTrue(
                hasattr(protocol, method_name),
                f"Protocol {protocol.__name__} is missing declared method {method_name}",
            )
            self.assertTrue(
                hasattr(implementation, method_name),
                f"Implementation {implementation.__name__} is missing required method {method_name} from {protocol.__name__}",
            )

            proto_method = getattr(protocol, method_name)
            impl_method = getattr(implementation, method_name)

            self.assertTrue(
                callable(proto_method),
                f"{protocol.__name__}.{method_name} must be callable",
            )
            self.assertTrue(
                callable(impl_method),
                f"{implementation.__name__}.{method_name} must be callable",
            )

            proto_sig = inspect.signature(proto_method)
            impl_sig = inspect.signature(impl_method)

            # Compare positional/required parameter names (excluding 'self')
            proto_params = [
                p for name, p in proto_sig.parameters.items()
                if name != "self" and p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            impl_params = [
                p for name, p in impl_sig.parameters.items()
                if name != "self" and p.default is inspect.Parameter.empty
                and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]

            proto_param_names = [p.name for p in proto_params]
            impl_param_names = [p.name for p in impl_params]

            self.assertEqual(
                proto_param_names,
                impl_param_names,
                f"Positional parameters for {method_name} mismatch between {protocol.__name__} and {implementation.__name__}",
            )

    def test_control_store_contract_alignment(self) -> None:
        required = {"read_project_state", "read_project_state_with_revision"}
        self._assert_protocol_methods_implemented(
            ControlStore, ProjectFileControlStore, required_methods=required
        )

    def test_target_catalog_contract_alignment(self) -> None:
        required = {"read_targets", "read_targets_with_revision"}
        self._assert_protocol_methods_implemented(
            TargetCatalog, ProjectFileTargetCatalog, required_methods=required
        )

    def test_resource_monitor_protocol_declarations(self) -> None:
        # ResourceMonitor declares locked_snapshot, record_decision (required) and locked_dispatch (optional)
        for method_name in ("locked_snapshot", "record_decision", "locked_dispatch"):
            self.assertTrue(
                hasattr(ResourceMonitor, method_name),
                f"ResourceMonitor missing declaration for {method_name}",
            )
            self.assertTrue(
                callable(getattr(ResourceMonitor, method_name)),
                f"ResourceMonitor.{method_name} must be callable",
            )

        sig = inspect.signature(ResourceMonitor.record_decision)
        # Ensure standard positional args exist
        expected_pos = ["decision", "candidates", "active_allocations", "resource_snapshot"]
        actual_pos = [
            name for name, p in sig.parameters.items()
            if name != "self" and p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual(actual_pos, expected_pos)

        # Ensure optional kwargs have defaults
        kw_params = {
            name: p for name, p in sig.parameters.items()
            if p.kind == inspect.Parameter.KEYWORD_ONLY
        }
        self.assertIn("scheduling_policy", kw_params)
        self.assertIn("decision_time", kw_params)
        self.assertIn("capacity_envelope", kw_params)
        self.assertIn("capacity_profile_snapshot", kw_params)
        self.assertIn("capacity_scope", kw_params)
        self.assertIn("task_classes", kw_params)
        self.assertIn("overrides", kw_params)
        self.assertIn("scheduling_policy_provenance", kw_params)

    def test_ports_module_exports(self) -> None:
        import control_plane.core.ports as ports

        expected_exports = {
            "ControlStore",
            "ArtifactStore",
            "TargetCatalog",
            "ResourceMonitor",
        }
        self.assertEqual(set(ports.__all__), expected_exports)


if __name__ == "__main__":
    unittest.main()
