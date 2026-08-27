#!/usr/bin/env python3
"""Web status server for the evaluation control plane (Phase W1).

Serves one static page plus JSON status and, when explicitly enabled,
mutation endpoints backed by the shared EvaluationMiddleware.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
MAX_CONCURRENT_REQUESTS = 8
MAX_BODY_BYTES = 2 * 1024 * 1024
STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"

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
    ) -> None:
        super().__init__(server_address, StatusRequestHandler)
        self.middleware = middleware
        self.project_root = Path(project_root)
        self.allow_writes = allow_writes
        self.demo = demo
        self.topology = topology
        self.policy = policy
        self.started_at = time.monotonic()
        self.request_semaphore = threading.BoundedSemaphore(max_concurrent_requests)
        self.mutation_lock = threading.Lock()


class DemoPolicy:
    """Small policy object used by the self-contained demo server."""

    def as_mapping(self) -> dict[str, Any]:
        return {"capacity_envelope": {"license_sessions": 4, "license_reserve": 1}}


class DemoMiddleware:
    """In-memory fixture implementing the read views used by the status server."""

    def __init__(self) -> None:
        self._studies = [{"study_id": "demo-study-a", "problem_id": "demo-problem"}]
        self._evaluations: list[dict[str, Any]] = []

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
        return {"study": study, "evaluations": self._evaluations}

    def list_studies(self, problem_id: str) -> list[dict[str, Any]]:
        return [s for s in self._studies if s["problem_id"] == problem_id]

    def list_problem_evaluations(self, problem_id: str, *_args: Any) -> list[dict[str, Any]]:
        return [e for e in self._evaluations if e.get("problem_id") == problem_id]

    def register_problem(self, definition: dict[str, Any]) -> dict[str, Any]:
        return dict(definition)

    def create_study(self, **kwargs: Any) -> dict[str, Any]:
        study = {k: v for k, v in kwargs.items() if k in {"study_id", "problem_id"}}
        self._studies.append(study)
        return study

    def submit(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "accepted", "demo": True}

class StatusRequestHandler(BaseHTTPRequestHandler):
    """Route GET requests and optionally gated mutation POST requests."""
    server_version = "StatusServer/0.1"
    server: StatusServer

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
        if not self.server.allow_writes:
            self._method_not_allowed()
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/api/contracts/build", "/api/problems", "/api/studies", "/api/evaluations"}:
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
                elif path == "/api/studies":
                    payload = mutation_views.create_study(self.server.middleware, body)
                else:
                    payload = mutation_views.submit_evaluation(self.server.middleware, body)
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
        try:
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
        if path.startswith("/api/studies/"):
            study_id = urllib.parse.unquote(path[len("/api/studies/"):])
            return self.server.middleware.get_study_status(study_id)
        if path.startswith("/api/problems/"):
            problem_id = urllib.parse.unquote(path[len("/api/problems/"):])
            studies = self.server.middleware.list_studies(problem_id)
            evaluations = self.server.middleware.list_problem_evaluations(problem_id)
            if not studies and not evaluations:
                raise _HttpError(404, f"unknown Problem: {problem_id}")
            return {"problem_id": problem_id, "studies": studies, "evaluations": evaluations}
        raise _HttpError(404, f"unknown path: {path}")

    def _send_static(self) -> None:
        try:
            body = STATIC_INDEX.read_bytes()
        except OSError:
            self._send_json(404, {"error": "status page is not installed"})
            return
        self._send_bytes(200, body, "text/html; charset=utf-8")

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

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="run with an in-memory fixture (no project files)")
    args = parser.parse_args(argv)
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
