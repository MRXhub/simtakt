#!/usr/bin/env python3
"""Web status server for the evaluation control plane (Phase W1).

Serves one static page plus JSON status and, when explicitly enabled,
mutation endpoints backed by the shared EvaluationMiddleware.
"""

from __future__ import annotations

import argparse
import ipaddress

import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_plane.core.evaluation_contracts import ContractError
from control_plane.data.sqlite_evaluation_repository import RepositoryError
from control_plane.evaluation import status_views
from control_plane.evaluation.execution_topology import parse_execution_topology
from control_plane.evaluation.scheduling_policy import resolve_governed_scheduling_policy
from control_plane.evaluation.service import EvaluationMiddleware

from control_plane.evaluation import mutation_views
from control_plane.web.package_landing import PackageLandingService

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
MAX_CONCURRENT_REQUESTS = 8
MAX_BODY_BYTES = 2 * 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_INDEX = STATIC_DIR / "index.html"
STATIC_EXTENSION_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
}
def parse_overview_limit(query: str) -> int | None:
    """Parse the optional overview limit without accepting ambiguous values."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    values = params.get("limit", [])
    if len(values) > 1 or (
        values
        and (
            not values[0]
            or not all("0" <= char <= "9" for char in values[0])
        )
    ):
        raise _HttpError(400, "limit must be a positive integer")
    limit = None if not values else int(values[0])
    if limit is not None and limit <= 0:
        raise _HttpError(400, "limit must be a positive integer")
    return limit

def parse_origin_query(query: str) -> str | None:
    """Parse the optional /api/evaluations origin filter, validating the token."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    values = params.get("origin", [])
    if len(values) > 1 or (values and not values[0]):
        raise _HttpError(400, "origin must be a single stable token")
    if not values:
        return None
    from control_plane.core.evaluation_contracts import normalize_token
    try:
        return normalize_token(values[0], "origin")
    except ContractError:
        raise _HttpError(400, "origin must be a valid stable token")



class StatusServer(ThreadingHTTPServer):
    """Threading HTTP server carrying middleware and write-mode state."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        middleware: Any,
        project_root: Path | str = ".",
        max_concurrent_requests: int = MAX_CONCURRENT_REQUESTS,
        allow_writes: bool = False,
        demo: bool = False,
        topology: Any | None = None,
        policy: Any | None = None,
        package_landing: PackageLandingService | None = None,
    ) -> None:
        super().__init__(server_address, StatusRequestHandler)
        self.middleware = middleware
        self.project_root = Path(project_root)
        self.allow_writes = allow_writes
        self.demo = demo
        self.topology = topology
        self.policy = policy
        if package_landing is not None:
            self.package_landing = package_landing
        elif self.allow_writes:
            self.package_landing = PackageLandingService(self.project_root, autostart=True)
        else:
            self.package_landing = None
        self.started_at = time.monotonic()
        self.request_semaphore = threading.BoundedSemaphore(max_concurrent_requests)
        self.mutation_lock = threading.Lock()

    def server_close(self) -> None:
        if getattr(self, "package_landing", None) is not None:
            self.package_landing.close()
        super().server_close()


class DemoPolicy:
    """Small policy object used by the self-contained demo server."""

    def as_mapping(self) -> dict[str, Any]:
        return {"capacity_envelope": {"license_sessions": 4, "license_reserve": 1}}


class DemoMiddleware:
    """In-memory fixture implementing the read views used by the status server."""

    def __init__(self) -> None:
        self._packages: list[dict[str, Any]] = []
        self._schemas: dict[str, dict[str, Any]] = {}
        self._problems: list[dict[str, Any]] = []
        self._studies: list[dict[str, Any]] = []
        self._evaluations: list[dict[str, Any]] = []
        self._study_evaluations: dict[str, list[str]] = {}
        self._init_fixtures()

    def _init_fixtures(self) -> None:
        from control_plane.evaluation.parameter_schema import (
            compute_schema_revision,
            validate_parameter_schema,
        )
        demo_package = {
            "package_name": "demo-package",
            "artifact_id": "pkg:demo-package",
            "revision": "sha256:" + "5" * 64,
            "path": "data/inputs/packages/demo-package",
            "deck_file": "deck.in",
            "status": "registered",
            "created_at": "2026-08-28T00:00:00+00:00",
            "dependencies": [],
            "files": [
                {
                    "name": "deck.in",
                    "bytes": 100,
                    "sha256": "5" * 64,
                }
            ],
        }
        self._packages.append(demo_package)

        sample_schema = {
            "kind": "parameter-schema",
            "problem_hint": "demo-problem",
            "source_package": {
                "artifact_id": "pkg:demo-package",
                "revision": "sha256:" + "5" * 64,
            },
            "parameters": [
                {
                    "name": "thickness",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 0.1, "max": 10.0},
                    "default": 1.0,
                },
                {
                    "name": "doping",
                    "type": "float",
                    "role": "variable",
                    "bounds": {"min": 1e14, "max": 1e18},
                    "default": 1e16,
                },
            ],
            "extracts": [
                {"name": "1Jsc", "expression": "$Jsc", "line": 10},
                {"name": "1Eff", "expression": "$Eff", "line": 12},
            ],
        }
        canonical_schema = validate_parameter_schema(sample_schema)
        schema_rev = compute_schema_revision(canonical_schema)
        extracts = canonical_schema.get("extracts", [])
        extract_names = [e["name"] for e in extracts if isinstance(e, dict) and "name" in e]
        self._schemas[schema_rev] = {
            "revision": schema_rev,
            "kind": canonical_schema.get("kind", "parameter-schema"),
            "canonical_json": json.dumps(canonical_schema),
            "registered_at": "2026-08-28T00:00:00+00:00",
            "schema": canonical_schema,
            "extract_names": extract_names,
        }
        demo_problem = {
            "contract_version": 1,
            "problem_id": "demo-problem",
            "parameter_schema_revision": schema_rev,
            "constraint_revision": "sha256:" + "0" * 64,
            "simulation_capabilities": ["cpu"],
            "metric_schema_revision": "sha256:" + "1" * 64,
            "revision": "sha256:" + "2" * 64,
        }
        demo_problem["status"] = "active"
        self._problems.append(demo_problem)

        demo_study = {
            "study_id": "demo-study-a",
            "problem_id": "demo-problem",
            "problem_revision": demo_problem["revision"],
            "created_at": "2026-08-28T00:00:00+00:00",
            "metadata": {"description": "Demo Study A"},
            "algorithm_run_id": "demo-run-a",
            "artifact_refs": [],
            "automation_profile": "assisted",
        }
        self._studies.append(demo_study)
        self._study_evaluations = {"demo-study-a": ["evaluation:00000000-0000-0000-0000-000000000001"]}

        demo_eval = {
            "contract_version": 1,
            "evaluation_id": "evaluation:00000000-0000-0000-0000-000000000001",
            "candidate_id": "candidate:sha256:" + "3" * 64,
            "problem_id": "demo-problem",
            "problem_revision": demo_problem["revision"],
            "fidelity": "high",
            "requested_outputs": ["score"],
            "evidence_profile": "default",
            "independence_requirement": "normal",
            "priority": "normal",
            "origin": "designer:smoke",
            "idempotency_key": "sha256:" + "4" * 64,
            "status": "queued",
            "observation_id": None,
            "created_at": "2026-08-28T00:00:00+00:00",
            "updated_at": "2026-08-28T00:00:00+00:00",
        }
        self._evaluations.append(demo_eval)

    def active_allocations(self) -> list[dict[str, Any]]:
        return [{"target_id": "demo-target-a"}]

    def capacity_counts(self) -> dict[str, int]:
        return {"queued": 1, "recovering": 0, "reconciling": 0}

    def stale_reconciling_attempts(self, _stale_seconds: int = 3600) -> list[Any]:
        return []

    def task_shape_statistics(self) -> list[dict[str, Any]]:
        return [{"task_shape": "demo-task", "count": 1}]

    def study_overviews(self, limit: int | None = None) -> dict[str, Any]:
        studies = self._studies[:limit] if limit is not None else self._studies
        return {"study_count": len(studies), "studies": studies}

    def get_study_status(self, study_id: str) -> dict[str, Any]:
        study = next((s for s in self._studies if s["study_id"] == study_id), None)
        if study is None:
            raise RepositoryError(f"unknown Study: {study_id}")
        eval_ids = set(self._study_evaluations.get(study_id, []))
        study_evals = [e for e in self._evaluations if e.get("evaluation_id") in eval_ids]
        return {"study": study, "evaluations": study_evals}

    def list_studies(self, problem_id: str | None = None) -> list[dict[str, Any]]:
        if problem_id is None:
            return list(self._studies)
        return [s for s in self._studies if s.get("problem_id") == problem_id]

    def list_problem_evaluations(self, problem_id: str, *_args: Any) -> list[dict[str, Any]]:
        return [e for e in self._evaluations if e.get("problem_id") == problem_id]

    def list_evaluations(
        self,
        problem_id: str | None = None,
        *,
        origin: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            e for e in self._evaluations
            if (problem_id is None or e.get("problem_id") == problem_id)
            and (origin is None or e.get("origin") == origin)
        ]
        study_of: dict[str, list[str]] = {}
        for study_id, eval_ids in self._study_evaluations.items():
            for eval_id in eval_ids:
                study_of.setdefault(eval_id, []).append(study_id)
        enriched = []
        for item in rows:
            row = dict(item)
            row["study_ids"] = sorted(study_of.get(row.get("evaluation_id"), []))
            enriched.append(row)
        return enriched

    def list_problems(self) -> list[dict[str, Any]]:
        return list(self._problems)

    def register_problem(self, definition: dict[str, Any]) -> dict[str, Any]:
        prob = dict(definition)
        self._problems.append(prob)
        return prob
    def set_problem_status(self, problem_id: str, revision: str, status: str) -> dict[str, Any]:
        for problem in self._problems:
            if problem.get("problem_id") == problem_id and problem.get("revision") == revision:
                problem["status"] = status
                return dict(problem)
        raise RepositoryError(f"unknown ProblemDefinition: {problem_id}")

    def create_study(self, **kwargs: Any) -> dict[str, Any]:
        study = dict(kwargs)
        if "created_at" not in study:
            study["created_at"] = datetime.now(timezone.utc).isoformat()
        self._studies.append(study)
        return study

    def submit(self, *args: Any, study_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if study_id:
            candidate = args[0] if len(args) > 0 else kwargs.get("candidate")
            request = args[1] if len(args) > 1 else kwargs.get("request")
            if isinstance(request, dict) and "evaluation_id" in request:
                self._study_evaluations.setdefault(study_id, []).append(request["evaluation_id"])
        return {"status": "accepted", "demo": True}

    def list_algorithm_runs(self) -> list[dict[str, Any]]:
        return []

    def get_algorithm_run(self, algorithm_run_id: str) -> dict[str, Any]:
        raise RepositoryError(f"unknown AlgorithmRun: {algorithm_run_id}")

    def list_algorithm_events(self, algorithm_run_id: str) -> list[dict[str, Any]]:
        return []

    def list_algorithm_results(self, algorithm_run_id: str) -> list[dict[str, Any]]:
        return []

    def register_schema(self, document: Mapping[str, Any]) -> dict[str, Any]:
        from control_plane.evaluation.parameter_schema import (
            compute_schema_revision,
            validate_parameter_schema,
        )
        canonical = validate_parameter_schema(document)
        rev = compute_schema_revision(canonical)
        if rev not in self._schemas:
            extracts = canonical.get("extracts", [])
            extract_names = [e["name"] for e in extracts if isinstance(e, dict) and "name" in e]
            self._schemas[rev] = {
                "revision": rev,
                "kind": canonical.get("kind", "parameter-schema"),
                "canonical_json": json.dumps(canonical),
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "schema": canonical,
                "extract_names": extract_names,
            }
        return self._schemas[rev]

    def get_schema(self, revision: str) -> dict[str, Any]:
        rev = str(revision).strip().lower()
        if rev not in self._schemas:
            raise RepositoryError(f"unknown Schema: {revision}")
        return self._schemas[rev]

    def list_schemas(self) -> list[dict[str, Any]]:
        results = []
        for s in self._schemas.values():
            schema_obj = s.get("schema", {})
            params = schema_obj.get("parameters") if isinstance(schema_obj, dict) else None
            param_count = len(params) if isinstance(params, list) else 0
            results.append({
                "revision": s["revision"],
                "kind": s["kind"],
                "registered_at": s["registered_at"],
                "extract_names": s.get("extract_names", []),
                "parameter_count": param_count,
            })
        return results

    def list_packages(self) -> list[dict[str, Any]]:
        return list(self._packages)

class StatusRequestHandler(BaseHTTPRequestHandler):
    """Route GET requests and optionally gated mutation POST requests."""
    server_version = "StatusServer/0.1"
    server: StatusServer
    timeout = 10.0

    def do_GET(self) -> None:  # noqa: N802
        with self.server.request_semaphore:
            try:
                self._dispatch_get()
            except (ConnectionError, TimeoutError):
                pass

    def do_POST(self) -> None:  # noqa: N802
        with self.server.request_semaphore:
            try:
                self._dispatch_post()
            except _HttpError as exc:
                self._send_json(exc.status, {"error": exc.message})
            except (ConnectionError, TimeoutError):
                pass

    def _method_not_allowed(self) -> None:
        self._send_json(405, {"error": "method not allowed; this status server is read-only"})

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def _dispatch_post(self) -> None:
        if "Transfer-Encoding" in self.headers:
            raise _HttpError(400, "Transfer-Encoding is not supported")
        path = urllib.parse.urlsplit(self.path).path
        if not self.server.allow_writes and path not in {
            "/api/packages/parse",
            "/api/candidates/validate",
        }:
            self._method_not_allowed()
            return
        if path not in {
            "/api/contracts/build",
            "/api/problems",
            "/api/problems/status",
            "/api/studies",
            "/api/evaluations",
            "/api/packages/parse",
            "/api/packages",
            "/api/schemas",
            "/api/candidates/validate",
        }:
            self._send_json(404, {"error": f"unknown path: {path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _HttpError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, "request body too large")
        if length < 0:
            raise _HttpError(400, "invalid Content-Length")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid JSON"})
            return
        try:
            with self.server.mutation_lock:
                if path == "/api/contracts/build":
                    payload = mutation_views.build_contract(body)
                elif path == "/api/problems":
                    payload = mutation_views.register_problem(self.server.middleware, body)
                elif path == "/api/problems/status":
                    payload = mutation_views.set_problem_status(self.server.middleware, body)
                elif path == "/api/studies":
                    payload = mutation_views.create_study(self.server.middleware, body)
                elif path == "/api/evaluations":
                    payload = mutation_views.submit_evaluation(self.server.middleware, body)
                elif path == "/api/packages/parse":
                    payload = mutation_views.parse_deck(body)
                elif path == "/api/packages":
                    payload = self.server.package_landing.submit_package(body)
                    self._send_json(202, payload)
                    return
                elif path == "/api/schemas":
                    payload = mutation_views.register_schema(self.server.middleware, body)
                elif path == "/api/candidates/validate":
                    payload = mutation_views.validate_candidate_parameters(self.server.middleware, body)
        except _HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        except ContractError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except RepositoryError as exc:
            message = str(exc)
            self._send_json(404 if message.startswith("unknown") else 500, {"error": message})
            return
        except TypeError:
            self._send_json(400, {"error": "invalid request fields"})
            return
        except Exception:
            self._send_json(500, {"error": "internal server error"})
            return
        self._send_json(200, payload)

    def _dispatch_get(self) -> None:
        split_path = urllib.parse.urlsplit(self.path)
        path = split_path.path
        if path == "/":
            self._send_static()
            return
        if path.startswith("/static/"):
            try:
                self._send_static_file(path)
            except _HttpError as exc:
                self._send_json(exc.status, {"error": exc.message})
            return
        try:
            if "Transfer-Encoding" in self.headers:
                raise _HttpError(400, "Transfer-Encoding is not supported")
            payload = self._api_payload(path, split_path.query)
        except _HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
            return
        except ContractError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except RepositoryError as exc:
            message = str(exc)
            self._send_json(404 if message.startswith("unknown") else 500, {"error": message})
            return
        except Exception:
            self._send_json(500, {"error": "internal server error"})
            return
        self._send_json(200, payload)

    def _api_payload(self, path: str, query: str = "") -> dict[str, Any]:
        if path == "/api/health":
            return {
                "status": "ok",
                "time": datetime.now(timezone.utc).isoformat(),
                "project_root": "demo" if self.server.demo else "configured",
                "uptime_seconds": time.monotonic() - self.server.started_at,
                "writes_enabled": self.server.allow_writes,
                "demo": self.server.demo,
            }
        if path == "/api/capacity":
            topology = self.server.topology or parse_execution_topology(self.server.project_root)
            policy = self.server.policy or resolve_governed_scheduling_policy(self.server.project_root)
            return status_views.capacity_status(self.server.middleware, topology, policy)
        if path == "/api/shapes":
            return status_views.shape_stats(self.server.middleware)
        if path == "/api/overview":
            limit = parse_overview_limit(query)
            overview = self.server.middleware.study_overviews(limit)
            return {"generated_at": datetime.now(timezone.utc).isoformat(),
                    "study_count": overview["study_count"],
                    "global": self.server.middleware.capacity_counts(),
                    "studies": overview["studies"]}
        if path == "/api/algorithms":
            return status_views.algorithms_overview(self.server.middleware)
        if path.startswith("/api/algorithms/"):
            algorithm_run_id = urllib.parse.unquote(path[len("/api/algorithms/"):])
            return status_views.algorithm_detail(self.server.middleware, algorithm_run_id)
        if path.startswith("/api/packages/jobs/"):
            if self.server.package_landing is None:
                raise _HttpError(404, "package landing service is disabled on read-only server")
            job_id = urllib.parse.unquote(path[len("/api/packages/jobs/"):])
            if not job_id:
                raise _HttpError(404, "job_id is required")
            job = self.server.package_landing.get_job(job_id)
            if job is None:
                raise _HttpError(404, f"Package job not found: {job_id}")
            return job
        if path == "/api/packages":
            return {"items": self.server.middleware.list_packages()}
        if path == "/api/schemas":
            return {"items": self.server.middleware.list_schemas()}
        if path.startswith("/api/schemas/"):
            revision = urllib.parse.unquote(path[len("/api/schemas/"):])
            return status_views.schema_detail(self.server.middleware, revision)
        if path == "/api/problems":
            return {"items": self.server.middleware.list_problems()}
        if path.startswith("/api/problems/"):
            problem_id = urllib.parse.unquote(path[len("/api/problems/"):])
            studies = self.server.middleware.list_studies(problem_id)
            evaluations = self.server.middleware.list_problem_evaluations(problem_id)
            if not studies and not evaluations:
                raise _HttpError(404, f"unknown Problem: {problem_id}")
            return {"problem_id": problem_id, "studies": studies, "evaluations": evaluations}
        if path == "/api/studies":
            return {"items": self.server.middleware.list_studies()}
        if path.startswith("/api/studies/"):
            study_id = urllib.parse.unquote(path[len("/api/studies/"):])
            return self.server.middleware.get_study_status(study_id)
        if path == "/api/evaluations":
            origin = parse_origin_query(query)
            return {"items": self.server.middleware.list_evaluations(origin=origin)}
        raise _HttpError(404, f"unknown path: {path}")

    def _send_static(self) -> None:
        try:
            body = STATIC_INDEX.read_bytes()
        except OSError:
            self._send_json(404, {"error": "status page is not installed"})
            return
        self._send_bytes(200, body, "text/html; charset=utf-8")

    def _send_static_file(self, raw_path: str) -> None:
        rel_path = raw_path[len("/static/"):]
        unquoted = urllib.parse.unquote(rel_path)
        if not unquoted or "\0" in unquoted:
            raise _HttpError(404, "static file not found")
        if ".." in unquoted or "\\" in unquoted:
            raise _HttpError(403, "path traversal detected")
        norm_parts = Path(unquoted).parts
        if any(part in {"..", "/", "\\"} or not part for part in norm_parts):
            raise _HttpError(403, "path traversal detected")

        target = (STATIC_DIR / unquoted).resolve()
        static_dir_resolved = STATIC_DIR.resolve()
        try:
            target.relative_to(static_dir_resolved)
        except ValueError:
            raise _HttpError(403, "path traversal detected")

        ext = target.suffix.lower()
        if not ext or ext not in STATIC_EXTENSION_MIME:
            raise _HttpError(403, f"disallowed file extension: {ext}")

        if not target.is_file():
            raise _HttpError(404, f"static file not found: {unquoted}")

        try:
            body = target.read_bytes()
        except OSError:
            raise _HttpError(500, "failed to read static file")

        content_type = STATIC_EXTENSION_MIME[ext]
        self._send_bytes(200, body, content_type)
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if status == 405:
            self.send_header("Allow", "GET")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _HttpError(Exception):
    """Internal routing error carrying one HTTP status and client message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

def _is_loopback_bind_host(host: str) -> bool:
    """Return whether *host* is one of the explicitly safe loopback forms."""
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_loopback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--allow-remote-writes", action="store_true",
                        help="allow unauthenticated writes on a non-loopback bind")
    parser.add_argument("--demo", action="store_true",
                        help="run with an in-memory fixture (no project files)")
    args = parser.parse_args(argv)
    if (args.allow_writes and not args.allow_remote_writes
            and not _is_loopback_bind_host(args.host)):
        print("写接口无认证,请绑定回环并经反向代理暴露;确需直接暴露加 --allow-remote-writes",
              file=sys.stderr)
        return 2
    demo = args.demo or os.environ.get("STATUS_SERVER_DEMO", "").lower() in {
        "1", "true", "yes", "on"
    }
    if demo:
        project_root = Path(".")
        middleware = DemoMiddleware()
        topology = {
            "targets": [{"target_id": "demo-target-a", "host_id": "demo-host-a",
                         "formal_execution": True}],
            "license_pool_groups": {"demo-pool": ["demo-target-a"]},
        }
        policy = DemoPolicy()
    else:
        project_root = Path(args.project_root).resolve()
        middleware = EvaluationMiddleware.for_project(project_root)
        topology = policy = None
    server = StatusServer((args.host, args.port), middleware=middleware,
                          project_root=project_root, allow_writes=args.allow_writes,
                          demo=demo, topology=topology, policy=policy)
    host, port = server.server_address[0], server.server_address[1]
    print(f"status server listening on http://{host}:{port}/"
          + (" (demo mode)" if demo else ""), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
