#!/usr/bin/env python3
"""Checks for the read-only web status server (Phase W1)."""

from __future__ import annotations

import contextlib
import io

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


class MainArgumentTests(unittest.TestCase):
    def test_remote_writes_are_rejected_before_server_start(self) -> None:
        stderr = io.StringIO()
        with patch.object(status_server, "StatusServer") as server:
            with contextlib.redirect_stderr(stderr):
                result = status_server.main([
                    "--host", "0.0.0.0", "--allow-writes",
                ])
        self.assertEqual(result, 2)
        server.assert_not_called()
        self.assertEqual(
            stderr.getvalue(),
            "写接口无认证,请绑定回环并经反向代理暴露;确需直接暴露加 --allow-remote-writes\n",
        )

    def test_allow_remote_writes_flag_permits_start_without_listening(self) -> None:
        fake_server = Mock()
        fake_server.server_address = ("0.0.0.0", 8321)
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        stderr = io.StringIO()
        with patch.object(status_server, "StatusServer", return_value=fake_server), \
                patch.object(status_server.EvaluationMiddleware, "for_project",
                             return_value=Mock()):
            with contextlib.redirect_stderr(stderr):
                result = status_server.main([
                    "--host", "0.0.0.0", "--allow-writes",
                    "--allow-remote-writes",
                ])
        self.assertEqual(result, 0)
        fake_server.serve_forever.assert_called_once_with()
        fake_server.server_close.assert_called_once_with()




class StatusServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = Mock()
        self.middleware.get_study_status.return_value = {
            "study": {"study_id": "study:abc"},
            "evaluations": [],
        }
        self.middleware.list_studies.return_value = [{"study_id": "study:abc"}]
        self.middleware.list_problems.return_value = [{"problem_id": "problem:p1"}]
        self.middleware.list_evaluations.return_value = [{"evaluation_id": "evaluation:1"}]
        self.middleware.list_schemas.return_value = [{"revision": "sha256:abc", "extract_names": []}]
        self.middleware.list_packages.return_value = [{"package_name": "pkg-a", "artifact_id": "pkg:pkg-a"}]
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
                "/api/packages",
                "/api/schemas",
                "/api/problems",
                "/api/studies",
                "/api/evaluations",
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

    def test_read_only_post_exemptions_and_write_gated_rejections(self) -> None:
        # POST to /api/packages/parse succeeds with 200 on read-only server
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/packages/parse",
            data=json.dumps({"deck_text": "set a = 1.0\n"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(len(data["parameters"]), 1)

        # POST to write-gated endpoints like /api/schemas returns 405
        for path in ("/api/schemas", "/api/packages", "/api/problems", "/api/studies"):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{path}",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 405, path)
    def test_unknown_path_returns_404(self) -> None:
        status, _, payload = self._get("/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_study_path_segment_is_unquoted_once(self) -> None:
        status, _, _ = self._get("/api/studies/study%3Aabc")
        self.assertEqual(status, 200)
        self.middleware.get_study_status.assert_called_once_with("study:abc")

    def test_template_options_do_not_load_solver_code(self) -> None:
        entries = [
            {"adapter_id": "alpha", "status": "active", "capabilities": ["solver-a"], "module": "private.module"},
            {"adapter_id": "beta", "status": "experimental", "capabilities": ["solver-b"]},
            {"adapter_id": "off", "status": "disabled", "capabilities": ["solver-a"]},
        ]
        with patch.object(status_server, "load_catalog", return_value=entries), \
                patch("control_plane.simulation.adapter_catalog.resolve_adapter") as resolve:
            status, _, payload = self._get("/api/template-options")
        self.assertEqual(status, 200)
        self.assertEqual([row["adapter_id"] for row in payload["adapters"]], ["alpha", "beta"])
        self.assertTrue(all(row["selectable"] for row in payload["adapters"]))
        self.assertNotIn("module", str(payload))
        resolve.assert_not_called()

    def test_template_options_flag_ambiguous_capabilities(self) -> None:
        entries = [
            {"adapter_id": "alpha", "status": "active", "capabilities": ["shared"]},
            {"adapter_id": "beta", "status": "active", "capabilities": ["shared", "extra"]},
        ]
        with patch.object(status_server, "load_catalog", return_value=entries):
            status, _, payload = self._get("/api/template-options")
        self.assertEqual(status, 200)
        self.assertEqual([row["selectable"] for row in payload["adapters"]], [False, True])

    def test_template_detail_exists_before_first_study(self) -> None:
        self.middleware.list_studies.return_value = []
        self.middleware.list_problem_evaluations.return_value = []
        status, _, payload = self._get("/api/problems/problem%3Ap1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["problem"], {"problem_id": "problem:p1"})
        self.assertEqual(payload["studies"], [])

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

    def test_list_endpoints_return_items_wrapper(self) -> None:
        for path, expected_key, expected_item in (
            ("/api/packages", "package_name", "pkg-a"),
            ("/api/schemas", "revision", "sha256:abc"),
            ("/api/problems", "problem_id", "problem:p1"),
            ("/api/studies", "study_id", "study:abc"),
            ("/api/evaluations", "evaluation_id", "evaluation:1"),
        ):
            status, headers, payload = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8", path)
            self.assertEqual(headers["Cache-Control"], "no-store", path)
            self.assertIn("items", payload, path)
            self.assertEqual(len(payload["items"]), 1, path)
            self.assertEqual(payload["items"][0][expected_key], expected_item, path)

    def test_list_endpoints_return_empty_list_when_none(self) -> None:
        self.middleware.list_packages.return_value = []
        self.middleware.list_schemas.return_value = []
        self.middleware.list_problems.return_value = []
        self.middleware.list_studies.return_value = []
        self.middleware.list_evaluations.return_value = []
        for path in ("/api/packages", "/api/schemas", "/api/problems", "/api/studies", "/api/evaluations"):
            status, _, payload = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(payload, {"items": []}, path)
    def test_static_route_serving_valid_file(self) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/static/index.html")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get("Content-Type"), "text/html; charset=utf-8")
            self.assertEqual(resp.headers.get("Cache-Control"), "no-store")
            body = resp.read()
            self.assertTrue(len(body) > 0)

    def test_static_route_serving_custom_whitelisted_files(self) -> None:
        # Create temp files in static dir with whitelist extensions and verify MIME types
        static_dir = status_server.STATIC_DIR
        test_files = [
            ("test_style.css", b"body { margin: 0; }", "text/css; charset=utf-8"),
            ("test_script.js", b"console.log('test');", "application/javascript; charset=utf-8"),
            ("test_data.json", b'{"key": "value"}', "application/json; charset=utf-8"),
            ("test_icon.svg", b"<svg></svg>", "image/svg+xml"),
        ]
        created = []
        try:
            for fname, content, expected_mime in test_files:
                fpath = static_dir / fname
                fpath.write_bytes(content)
                created.append(fpath)
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/static/{fname}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200, fname)
                    self.assertEqual(resp.headers.get("Content-Type"), expected_mime, fname)
                    self.assertEqual(resp.read(), content, fname)
        finally:
            for fpath in created:
                try:
                    fpath.unlink(missing_ok=True)
                except OSError:
                    pass

    def test_static_route_rejects_traversal_attempts(self) -> None:
        traversal_paths = [
            "/static/../status_server.py",
            "/static/..%2fstatus_server.py",
            "/static/%2e%2e/status_server.py",
            "/static/%2e%2e%2fstatus_server.py",
            "/static/sub/../../status_server.py",
            "/static/sub%2f..%2f..%2fstatus_server.py",
        ]
        for path in traversal_paths:
            status, _, payload = self._get(path)
            self.assertEqual(status, 403, f"Expected 403 for {path}")
            self.assertIn("error", payload, path)
            self.assertIn("traversal", payload["error"].lower(), path)

    def test_static_route_rejects_disallowed_extensions(self) -> None:
        disallowed = [
            "/static/status_server.py",
            "/static/test.db",
            "/static/test.exe",
            "/static/test.sh",
            "/static/test.env",
        ]
        for path in disallowed:
            status, _, payload = self._get(path)
            self.assertEqual(status, 403, f"Expected 403 for {path}")
            self.assertIn("error", payload, path)
            self.assertIn("disallowed", payload["error"].lower(), path)

    def test_static_route_returns_404_for_missing_file(self) -> None:
        status, _, payload = self._get("/static/nonexistent_file_xyz_98765.html")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)
        self.assertIn("not found", payload["error"].lower())
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

    def test_transfer_encoding_rejected_and_handler_timeout(self) -> None:
        self.assertEqual(StatusRequestHandler.timeout, 10.0)
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/health",
            headers={"Transfer-Encoding": "chunked"},
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

    def _get(self, path: str) -> tuple[int, dict, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}{path}"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return (
                    response.status,
                    dict(response.headers),
                    json.loads(response.read().decode("utf-8")),
                )
        except urllib.error.HTTPError as error:
            with error:
                body = error.read().decode("utf-8")
            return error.code, dict(error.headers), json.loads(body)

    def test_demo_get_endpoints_use_fixture_without_project_files(self) -> None:
        with patch.object(status_server, "parse_execution_topology",
                          side_effect=AssertionError("demo read project topology")), \
             patch.object(status_server, "resolve_governed_scheduling_policy",
                          side_effect=AssertionError("demo read project policy")):
            for path in ("/api/health", "/api/capacity", "/api/shapes",
                         "/api/overview", "/api/studies/demo-study-a",
                         "/api/problems/demo-problem",
                         "/api/packages", "/api/schemas", "/api/problems", "/api/studies", "/api/evaluations"):
                status, headers, payload = self._get(path)
                self.assertEqual(status, 200, path)
                self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8", path)
                self.assertIsInstance(payload, dict, path)

    def test_demo_five_list_fixtures_parity_and_extract_names(self) -> None:
        # 0. /api/packages
        status, _, payload = self._get("/api/packages")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        self.assertEqual(len(payload["items"]), 1)
        pkg_item = payload["items"][0]
        self.assertEqual(pkg_item["package_name"], "demo-package")
        self.assertEqual(pkg_item["artifact_id"], "pkg:demo-package")
        self.assertEqual(pkg_item["status"], "registered")
        self.assertIn("revision", pkg_item)
        self.assertEqual(pkg_item["path"], "data/inputs/packages/demo-package")
        self.assertEqual(pkg_item["deck_file"], "deck.in")

        # 1. /api/schemas
        status, _, payload = self._get("/api/schemas")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        self.assertEqual(len(payload["items"]), 1)
        schema_item = payload["items"][0]
        self.assertEqual(
            set(schema_item.keys()),
            {"revision", "kind", "registered_at", "extract_names", "parameter_count", "problem_hint", "source_package"},
        )
        self.assertNotIn("canonical_json", schema_item)
        self.assertNotIn("schema", schema_item)
        self.assertEqual(schema_item["extract_names"], ["1Eff", "1Jsc"])
        self.assertEqual(schema_item["parameter_count"], 2)

        # 3. /api/studies
        status, _, payload = self._get("/api/studies")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        self.assertEqual(len(payload["items"]), 1)
        study_item = payload["items"][0]
        self.assertEqual(study_item["study_id"], "demo-study-a")
        self.assertEqual(study_item["problem_id"], "demo-problem")

        # 4. /api/evaluations
        status, _, payload = self._get("/api/evaluations")
        self.assertEqual(status, 200)
        self.assertIn("items", payload)
        self.assertEqual(len(payload["items"]), 1)
        eval_item = payload["items"][0]
        self.assertIn("evaluation_id", eval_item)
        self.assertEqual(eval_item["problem_id"], "demo-problem")
        self.assertEqual(eval_item["status"], "queued")

    def test_demo_health_redacts_root_and_identifies_mode(self) -> None:
        status, _, payload = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["demo"])
        self.assertEqual(payload["project_root"], "demo")
        self.assertNotIn(str(Path("__missing_demo_project__").resolve()), json.dumps(payload))
class RealRepositoryListEndpointsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test-real-status-")
        self.project_root = Path(self.temp_dir.name).resolve()
        (self.project_root / "data" / "control").mkdir(parents=True, exist_ok=True)
        (self.project_root / "project").mkdir(parents=True, exist_ok=True)
        (self.project_root / "project" / "PROJECT_STATE.json").write_text("{}", encoding="utf-8")
        self.middleware = status_server.EvaluationMiddleware.for_project(self.project_root)
        self._quiet_logs = patch.object(
            StatusRequestHandler, "log_message", lambda *args, **kwargs: None
        )
        self._quiet_logs.start()
        self.server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=self.project_root,
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._quiet_logs.stop()
        self.temp_dir.cleanup()

    def _get(self, path: str) -> tuple[int, dict, dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return (
                    resp.status,
                    dict(resp.headers),
                    json.loads(resp.read().decode("utf-8")),
                )
        except urllib.error.HTTPError as error:
            with error:
                body = error.read().decode("utf-8")
            return error.code, dict(error.headers), json.loads(body)

    def test_real_registries_empty_and_populated_lifecycle(self) -> None:
        # 1. Initial state: all 5 lists return {"items": []}
        for path in ("/api/packages", "/api/schemas", "/api/problems", "/api/studies", "/api/evaluations"):
            status, headers, payload = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8", path)
            self.assertEqual(headers["Cache-Control"], "no-store", path)
            self.assertEqual(payload, {"items": []}, path)

        # 2. Register Schema with extracts
        schema_dict = {
            "kind": "parameter-schema",
            "problem_hint": "ten-junction-thickness",
            "source_package": {
                "artifact_id": "package.thickness-vector.v1",
                "revision": "sha256:" + "a" * 64,
            },
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 1.0},
                    "default": 0.551,
                }
            ],
            "extracts": [
                {"name": "1Jsc", "expression": "$Jsc", "line": 10},
            ],
        }
        schema_rec = self.middleware.register_schema(schema_dict)
        schema_rev = schema_rec["revision"]

        # GET /api/schemas returns 1 item with extract_names
        status, _, payload = self._get("/api/schemas")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        schema_item = payload["items"][0]
        self.assertEqual(
            set(schema_item.keys()),
            {"revision", "kind", "registered_at", "extract_names", "parameter_count", "problem_hint", "source_package"},
        )
        self.assertEqual(schema_item["revision"], schema_rev)
        self.assertEqual(schema_item["kind"], "parameter-schema")
        self.assertEqual(schema_item["extract_names"], ["1Jsc"])
        self.assertEqual(schema_item["parameter_count"], 1)
        self.assertIn("registered_at", schema_item)
        self.assertNotIn("canonical_json", schema_item)
        self.assertNotIn("schema", schema_item)

        # Full document is available on /api/schemas/<revision>
        status, _, full_doc = self._get(f"/api/schemas/{schema_rev}")
        self.assertEqual(status, 200)
        self.assertIn("extracts", full_doc)

        # Register a second schema without extracts -> extract_names should be []
        schema_no_ext = {
            "kind": "parameter-schema",
            "problem_hint": "bare-schema",
            "parameters": [
                {
                    "name": "param_a",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.0, "max": 1.0},
                    "default": 0.5,
                }
            ],
        }
        rec_no_ext = self.middleware.register_schema(schema_no_ext)
        status, _, payload = self._get("/api/schemas")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 2)
        item_no_ext = next(it for it in payload["items"] if it["revision"] == rec_no_ext["revision"])
        self.assertEqual(item_no_ext["extract_names"], [])
        self.assertEqual(item_no_ext["parameter_count"], 1)
        self.assertNotIn("canonical_json", item_no_ext)
        self.assertNotIn("schema", item_no_ext)
        # 3. Register Problem
        problem_spec = make_problem_definition(
            problem_id="problem:ten-junction",
            parameter_schema_revision=schema_rev,
            constraint_revision="sha256:" + "0" * 64,
            simulation_capabilities=["cpu"],
            metric_schema_revision="sha256:" + "0" * 64,
        )
        problem_rec = self.middleware.register_problem(problem_spec)
        problem_rev = problem_rec["revision"]

        # GET /api/problems returns 1 item
        status, _, payload = self._get("/api/problems")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        prob_item = payload["items"][0]
        self.assertEqual(prob_item["problem_id"], "problem:ten-junction")
        self.assertEqual(prob_item["revision"], problem_rev)
        self.assertEqual(prob_item["parameter_schema_revision"], schema_rev)

        # 4. Create Study
        study_rec = self.middleware.create_study(
            study_id="study:real-1",
            problem_id="problem:ten-junction",
            problem_revision=problem_rev,
            metadata={"objective": "maximize efficiency"},
        )

        # GET /api/studies returns 1 item
        status, _, payload = self._get("/api/studies")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        study_item = payload["items"][0]
        self.assertEqual(study_item["study_id"], "study:real-1")
        self.assertEqual(study_item["problem_id"], "problem:ten-junction")
        self.assertEqual(study_item["problem_revision"], problem_rev)
        self.assertEqual(study_item["metadata"], {"objective": "maximize efficiency"})

        # 5. Submit Evaluation
        candidate = make_candidate(
            problem_id="problem:ten-junction",
            problem_revision=problem_rev,
            parameters={"t_total1": 0.551},
        )
        request = make_evaluation_request(
            candidate_id=candidate["candidate_id"],
            fidelity="high",
            requested_outputs=["score"],
            evidence_profile="default",
        )
        eval_rec = self.middleware.submit(candidate, request, study_id="study:real-1")

        # GET /api/evaluations returns 1 item
        status, _, payload = self._get("/api/evaluations")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        eval_item = payload["items"][0]
        self.assertEqual(eval_item["evaluation_id"], eval_rec["evaluation_id"])
        self.assertEqual(eval_item["candidate_id"], candidate["candidate_id"])
        self.assertEqual(eval_item["status"], "queued")
        self.assertIn("created_at", eval_item)
        self.assertIn("updated_at", eval_item)

        # 6. Real Package materialization discovery
        pkg_dir = self.project_root / "data" / "inputs" / "packages" / "pkg-real-test"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        deck_path = pkg_dir / "deck.in"
        deck_path.write_bytes(b"go atlas\nset x = 1\nend\n")
        manifest_data = {
            "schema_version": 2,
            "artifact_id": "pkg:pkg-real-test",
            "package_name": "pkg-real-test",
            "package_kind": "input-package",
            "created_at": "2026-08-28T12:00:00+00:00",
            "deck_file": "deck.in",
            "dependencies": [],
            "files": [{"name": "deck.in", "bytes": 24, "sha256": "4b5d..."}],
        }
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")

        status, _, payload = self._get("/api/packages")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        pkg_item = payload["items"][0]
        self.assertEqual(pkg_item["package_name"], "pkg-real-test")
        self.assertEqual(pkg_item["artifact_id"], "pkg:pkg-real-test")
        self.assertEqual(pkg_item["path"], "data/inputs/packages/pkg-real-test")
        self.assertEqual(pkg_item["deck_file"], "deck.in")
        self.assertEqual(pkg_item["status"], "registered")
        self.assertEqual(pkg_item["created_at"], "2026-08-28T12:00:00+00:00")
        self.assertIn("revision", pkg_item)

    def test_evaluations_origin_query_filter_and_lineage(self) -> None:
        schema_dict = {
            "kind": "parameter-schema",
            "problem_hint": "origin-filter",
            "source_package": {
                "artifact_id": "package.origin.v1",
                "revision": "sha256:" + "a" * 64,
            },
            "parameters": [
                {
                    "name": "t_total1",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 1.0},
                    "default": 0.551,
                }
            ],
            "extracts": [
                {"name": "1Jsc", "expression": "$Jsc", "line": 10},
            ],
        }
        schema_rec = self.middleware.register_schema(schema_dict)
        schema_rev = schema_rec["revision"]
        problem = make_problem_definition(
            problem_id="problem:origin-filter",
            parameter_schema_revision=schema_rev,
            constraint_revision="sha256:" + "0" * 64,
            simulation_capabilities=["cpu"],
            metric_schema_revision="sha256:" + "0" * 64,
        )
        problem_rec = self.middleware.register_problem(problem)
        problem_rev = problem_rec["revision"]
        self.middleware.create_study(
            study_id="study:origin-filter-a",
            problem_id="problem:origin-filter",
            problem_revision=problem_rev,
        )

        cand_a = make_candidate(
            problem_id="problem:origin-filter",
            problem_revision=problem_rev,
            parameters={"t_total1": 0.551},
        )
        req_a = make_evaluation_request(
            candidate_id=cand_a["candidate_id"],
            fidelity="high",
            requested_outputs=["score"],
            evidence_profile="default",
            origin="designer:smoke",
        )
        eval_a = self.middleware.submit(cand_a, req_a, study_id="study:origin-filter-a")

        cand_b = make_candidate(
            problem_id="problem:origin-filter",
            problem_revision=problem_rev,
            parameters={"t_total1": 0.652},
        )
        req_b = make_evaluation_request(
            candidate_id=cand_b["candidate_id"],
            fidelity="high",
            requested_outputs=["score"],
            evidence_profile="default",
            origin="cli:batch",
        )
        eval_b = self.middleware.submit(cand_b, req_b)

        status, _, payload = self._get("/api/evaluations")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 2)

        status, _, payload = self._get("/api/evaluations?origin=designer%3Asmoke")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        row = payload["items"][0]
        self.assertEqual(row["evaluation_id"], eval_a["evaluation_id"])
        self.assertEqual(row["origin"], "designer:smoke")
        self.assertEqual(row["problem_id"], "problem:origin-filter")
        self.assertEqual(row["problem_revision"], problem_rev)
        self.assertEqual(row["study_ids"], ["study:origin-filter-a"])

        status, _, payload = self._get("/api/evaluations?origin=cli%3Abatch")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        row = payload["items"][0]
        self.assertEqual(row["evaluation_id"], eval_b["evaluation_id"])
        self.assertEqual(row["origin"], "cli:batch")
        self.assertEqual(row["study_ids"], [])

        status, _, payload = self._get("/api/evaluations?origin=no-such-origin")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])

        status, _, _ = self._get("/api/evaluations?origin=not%20valid")
        self.assertEqual(status, 400)

class AuditFindingsTests(unittest.TestCase):
    def test_read_only_start_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test-readonly-root-") as tmpdir:
            project_root = Path(tmpdir).resolve()
            packages_dir = project_root / "data" / "inputs" / "packages"
            packages_dir.mkdir(parents=True, exist_ok=True)
            staging_dir = packages_dir / ".staging_dummy"
            staging_dir.mkdir(parents=True, exist_ok=True)
            (staging_dir / "leftover.txt").write_text("content", encoding="utf-8")

            # Starting in read-only mode (allow_writes=False)
            middleware = Mock()
            server = StatusServer(
                ("127.0.0.1", 0),
                middleware=middleware,
                project_root=project_root,
                allow_writes=False,
            )
            try:
                self.assertIsNone(server.package_landing)
                # Staging dir must NOT be cleaned up by read-only server
                self.assertTrue(staging_dir.exists())
                self.assertTrue((staging_dir / "leftover.txt").exists())
            finally:
                server.server_close()

        # In a completely fresh dir without data/inputs/packages
        with tempfile.TemporaryDirectory(prefix="test-readonly-fresh-") as fresh_tmp:
            fresh_root = Path(fresh_tmp).resolve()
            fresh_server = StatusServer(
                ("127.0.0.1", 0),
                middleware=Mock(),
                project_root=fresh_root,
                allow_writes=False,
            )
            try:
                self.assertIsNone(fresh_server.package_landing)
                # data/inputs/packages should NOT be created
                self.assertFalse((fresh_root / "data" / "inputs" / "packages").exists())
            finally:
                fresh_server.server_close()

    def test_package_jobs_disabled_on_read_only_server(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test-readonly-jobs-") as tmpdir:
            project_root = Path(tmpdir).resolve()
            middleware = Mock()
            server = StatusServer(
                ("127.0.0.1", 0),
                middleware=middleware,
                project_root=project_root,
                allow_writes=False,
            )
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/packages/jobs/job-123")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 404)
                body = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertIn("disabled", body.get("error", "").lower())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_packages_path_containment_and_symlink_skipping(self) -> None:
        with tempfile.TemporaryDirectory(prefix="test-pkg-proj-") as proj_tmp, \
             tempfile.TemporaryDirectory(prefix="test-pkg-outside-") as out_tmp:
            project_root = Path(proj_tmp).resolve()
            outside_root = Path(out_tmp).resolve()

            # Create data/inputs/packages in project
            pkgs_dir = project_root / "data" / "inputs" / "packages"
            pkgs_dir.mkdir(parents=True, exist_ok=True)

            # 1. Valid package inside project
            valid_pkg = pkgs_dir / "valid-pkg"
            valid_pkg.mkdir(parents=True, exist_ok=True)
            (valid_pkg / "deck.in").write_text("set x = 1\n", encoding="utf-8")
            (valid_pkg / "manifest.json").write_text(json.dumps({
                "package_name": "valid-pkg",
                "artifact_id": "pkg:valid-pkg",
                "deck_file": "deck.in",
            }), encoding="utf-8")

            # 2. Outside package
            outside_pkg = outside_root / "outside-pkg"
            outside_pkg.mkdir(parents=True, exist_ok=True)
            (outside_pkg / "deck.in").write_text("set x = 2\n", encoding="utf-8")
            (outside_pkg / "manifest.json").write_text(json.dumps({
                "package_name": "outside-pkg",
                "artifact_id": "pkg:outside-pkg",
                "deck_file": "deck.in",
            }), encoding="utf-8")

            # Symlink from project pkgs_dir pointing outside (if OS allows symlinks)
            symlink_created = False
            try:
                symlink_pkg = pkgs_dir / "symlink-pkg"
                symlink_pkg.symlink_to(outside_pkg, target_is_directory=True)
                symlink_created = True
            except OSError:
                pass

            # 3. Artifact catalog shard with escaping path
            shards_dir = project_root / "records" / "artifacts"
            shards_dir.mkdir(parents=True, exist_ok=True)
            escaping_shard = {
                "schema_version": 1,
                "record_kind": "artifact-catalog-shard",
                "artifact": {
                    "kind": "input-package",
                    "status": "active",
                    "artifact_id": "pkg:escaping-shard",
                    "latest_revision": "rev1",
                    "revisions": [{
                        "revision": "rev1",
                        "locations": [{"role": "primary", "path": "../../outside-escape"}],
                    }],
                },
            }
            (shards_dir / "escaping.json").write_text(json.dumps(escaping_shard), encoding="utf-8")

            middleware = status_server.EvaluationMiddleware.for_project(project_root)
            packages = middleware.list_packages()

            # Escaping shard and symlink must not be included
            pkg_names = [p["package_name"] for p in packages]
            self.assertIn("valid-pkg", pkg_names)
            self.assertNotIn("escaping-shard", pkg_names)
            if symlink_created:
                self.assertNotIn("outside-pkg", pkg_names)

            for p in packages:
                self.assertFalse(Path(p["path"]).is_absolute())
                self.assertFalse(p["path"].startswith(".."))

    def test_demo_middleware_study_evaluations_filtering(self) -> None:
        middleware = status_server.DemoMiddleware()
        # Default demo study has 1 associated evaluation
        status_a = middleware.get_study_status("demo-study-a")
        self.assertEqual(len(status_a["evaluations"]), 1)
        self.assertEqual(status_a["evaluations"][0]["evaluation_id"], "evaluation:00000000-0000-0000-0000-000000000001")

        # Create a new study without evaluations
        middleware.create_study(
            study_id="demo-study-empty",
            problem_id="demo-problem",
            problem_revision="sha256:" + "2" * 64,
        )
        status_empty = middleware.get_study_status("demo-study-empty")
        self.assertEqual(status_empty["study"]["study_id"], "demo-study-empty")
        self.assertEqual(status_empty["evaluations"], [])
