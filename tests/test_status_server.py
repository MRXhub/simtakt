#!/usr/bin/env python3
"""Checks for the read-only web status server (Phase W1)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control_plane.core.evaluation_contracts import (
    ContractError,
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError
from control_plane.web import status_server
from control_plane.web.status_server import StatusRequestHandler, StatusServer


class StatusServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = Mock()
        self.middleware.get_study_status.return_value = {
            "study": {"study_id": "study:abc"},
            "evaluations": [],
        }
        self.middleware.list_studies.return_value = [{"study_id": "study:abc"}]
        self.middleware.list_problem_evaluations.return_value = []
        self.middleware.task_shape_statistics.return_value = []
        self.middleware.active_allocations.return_value = []
        self.middleware.capacity_counts.return_value = {
            "queued": 0, "recovering": 0, "reconciling": 0
        }
        self.middleware.study_overviews.return_value = {
            "study_count": 1,
            "studies": [{"study_id": "study:abc"}],
        }
        self.middleware.stale_reconciling_attempts.return_value = []
        self.topology = {
            "targets": [{
                "target_id": "target:a",
                "host_id": "host:a",
                "formal_execution": True,
            }],
            "license_pool_groups": {"pool:a": ["target:a"]},
        }
        self.policy = SimpleNamespace(as_mapping=lambda: {
            "capacity_envelope": {"license_sessions": 3}
        })
        self._quiet_logs = patch.object(
            StatusRequestHandler, "log_message", lambda *args, **kwargs: None
        )
        self._quiet_logs.start()
        self.server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=Path(tempfile.mkdtemp()),
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._quiet_logs.stop()

    def _get(self, path: str) -> tuple[int, dict, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}"
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return (
                    response.status,
                    dict(response.headers),
                    json.loads(response.read().decode("utf-8")),
                )
        except urllib.error.HTTPError as error:
            with error:
                body = error.read().decode("utf-8")
            return error.code, dict(error.headers), json.loads(body)

    def test_all_api_endpoints_return_json_with_no_store(self) -> None:
        with patch.object(
            status_server, "parse_execution_topology",
            return_value=self.topology,
        ), patch.object(
            status_server, "resolve_governed_scheduling_policy",
            return_value=self.policy,
        ):
            for path in (
                "/api/health",
                "/api/capacity",
                "/api/shapes",
                "/api/overview",
                "/api/studies/study%3Aabc",
                "/api/problems/problem%3Ap1",
            ):
                status, headers, payload = self._get(path)
                self.assertEqual(status, 200, path)
                self.assertEqual(
                    headers["Content-Type"],
                    "application/json; charset=utf-8",
                    path,
                )
                self.assertEqual(headers["Cache-Control"], "no-store", path)
                self.assertIsInstance(payload, dict, path)

    def test_overview_payload_and_limit_passthrough(self) -> None:
        status, _, payload = self._get("/api/overview?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"generated_at", "study_count", "global", "studies"}
        )
        self.assertEqual(payload["study_count"], 1)
        self.middleware.study_overviews.assert_called_once_with(1)

        status, _, payload = self._get("/api/overview?limit=0")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, _, payload = self._get("/api/overview?limit=bogus")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_health_payload_shape(self) -> None:
        status, _, payload = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIsInstance(payload["time"], str)
        self.assertIsInstance(payload["project_root"], str)
        self.assertIsInstance(payload["uptime_seconds"], float)

    def test_unknown_repository_error_maps_to_404_without_traceback(
        self,
    ) -> None:
        self.middleware.get_study_status.side_effect = RepositoryError(
            "unknown Study: x"
        )
        status, _, payload = self._get("/api/studies/x")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "unknown Study: x"})
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_other_repository_error_maps_to_500(self) -> None:
        self.middleware.get_study_status.side_effect = RepositoryError(
            "database is locked"
        )
        status, _, payload = self._get("/api/studies/x")
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "database is locked"})

    def test_contract_error_maps_to_400(self) -> None:
        self.middleware.get_study_status.side_effect = ContractError(
            "study_id must be a non-empty string"
        )
        status, _, payload = self._get("/api/studies/x")
        self.assertEqual(status, 400)
        self.assertEqual(
            payload, {"error": "study_id must be a non-empty string"}
        )

    def test_unexpected_error_maps_to_generic_500(self) -> None:
        self.middleware.get_study_status.side_effect = ValueError("secret")
        status, _, payload = self._get("/api/studies/x")
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": "internal server error"})
        self.assertNotIn("secret", json.dumps(payload))

    def test_non_get_method_returns_405(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health", data=b"{}"
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 405)
        self.assertEqual(context.exception.headers["Allow"], "GET")
        with context.exception as error:
            payload = json.loads(error.read().decode("utf-8"))
        self.assertIn("error", payload)

    def test_unknown_path_returns_404(self) -> None:
        status, _, payload = self._get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_study_path_segment_is_unquoted_once(self) -> None:
        status, _, _ = self._get("/api/studies/study%3Aabc")
        self.assertEqual(status, 200)
        self.middleware.get_study_status.assert_called_once_with("study:abc")

    def test_unknown_problem_returns_404(self) -> None:
        self.middleware.list_studies.return_value = []
        self.middleware.list_problem_evaluations.return_value = []
        status, _, payload = self._get("/api/problems/problem%3Anone")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "unknown Problem: problem:none"})

    def test_problem_payload_shape(self) -> None:
        status, _, payload = self._get("/api/problems/problem%3Ap1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["problem_id"], "problem:p1")
        self.assertEqual(payload["studies"], [{"study_id": "study:abc"}])
        self.assertEqual(payload["evaluations"], [])

class WriteEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = Mock()
        self.server = StatusServer(
            ("127.0.0.1", 0), middleware=self.middleware,
            project_root=Path(tempfile.mkdtemp()), allow_writes=True,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(self, path: str, payload: object) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_health_advertises_write_mode(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/health", timeout=5
        ) as response:
            self.assertTrue(json.loads(response.read())["writes_enabled"])

    def test_build_contract_three_kinds(self) -> None:
        revision = "sha256:" + "a" * 64
        cases = [
            ("problem", {"problem_id": "problem:p", "parameter_schema_revision": revision,
             "constraint_revision": revision, "simulation_capabilities": ["cpu"],
             "metric_schema_revision": revision}),
            ("candidate", {"problem_id": "problem:p", "problem_revision": revision,
             "parameters": {"x": 1}}),
            ("evaluation_request", {"candidate_id": "candidate:sha256:" + "b" * 64,
             "fidelity": "high", "requested_outputs": ["score"],
             "evidence_profile": "default"}),
        ]
        for kind, spec in cases:
            status, payload = self._post("/api/contracts/build", {"kind": kind, "spec": spec})
            self.assertEqual(status, 200, kind)
            if kind == "evaluation_request":
                expected = make_evaluation_request(
                    **spec, evaluation_id=payload["contract"]["evaluation_id"]
                )
            else:
                expected = {
                    "problem": make_problem_definition,
                    "candidate": make_candidate,
                }[kind](**spec)
            self.assertEqual(payload["contract"], expected)

    def test_studies_unknown_key_and_invalid_json(self) -> None:
        status, payload = self._post("/api/studies", {"study_id": "study:x", "extra": 1})
        self.assertEqual(status, 400)
        self.assertNotIn("Traceback", json.dumps(payload))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/contracts/build",
            data=b"{bad", headers={"Content-Length": "4"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 400)


class DemoModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = StatusServer(
            ("127.0.0.1", 0),
            middleware=status_server.DemoMiddleware(),
            project_root=Path("__missing_demo_project__"),
            demo=True,
            topology={
                "targets": [{"target_id": "demo-target-a", "host_id": "demo-host-a",
                             "formal_execution": True}],
                "license_pool_groups": {"demo-pool": ["demo-target-a"]},
            },
            policy=status_server.DemoPolicy(),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}", timeout=5
        ) as response:
            return json.loads(response.read())

    def test_demo_get_endpoints_use_fixture_without_project_files(self) -> None:
        with patch.object(status_server, "parse_execution_topology",
                          side_effect=AssertionError("demo read project topology")), \
             patch.object(status_server, "resolve_governed_scheduling_policy",
                          side_effect=AssertionError("demo read project policy")):
            for path in ("/api/health", "/api/capacity", "/api/shapes",
                         "/api/overview", "/api/studies/demo-study-a",
                         "/api/problems/demo-problem"):
                self.assertIsInstance(self._get(path), dict)

    def test_demo_health_redacts_root_and_identifies_mode(self) -> None:
        payload = self._get("/api/health")
        self.assertTrue(payload["demo"])
        self.assertEqual(payload["project_root"], "demo")
        self.assertNotIn(str(Path("__missing_demo_project__").resolve()), json.dumps(payload))

if __name__ == "__main__":
    unittest.main()
