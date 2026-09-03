#!/usr/bin/env python3
"""Focused unit tests for GET /api/algorithms and GET /api/algorithms/<id>."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from control_plane.core.evaluation_contracts import (
    ContractError,
    make_algorithm_event,
    make_algorithm_result,
    make_algorithm_run,
    validate_algorithm_event,
    validate_algorithm_result,
    validate_algorithm_run,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError
from control_plane.evaluation import status_views
from control_plane.web.status_server import StatusRequestHandler, StatusServer

REV_A = "sha256:" + "a" * 64
REV_B = "sha256:" + "b" * 64
REV_C = "sha256:" + "c" * 64

CORE_ALGORITHM_RUN_FIELDS = {
    "contract_version",
    "algorithm_run_id",
    "algorithm_id",
    "algorithm_revision",
    "problem_id",
    "problem_revision",
    "configuration_revision",
    "configuration",
    "input_artifact_refs",
    "retention_class",
}


def _fixture_run(run_id: str = "run:algo-1") -> dict:
    base = make_algorithm_run(
        algorithm_run_id=run_id,
        algorithm_id="code.algorithm.demo-v1",
        algorithm_revision=REV_A,
        problem_id="problem:p1",
        problem_revision=REV_B,
        configuration={"iterations": 10},
        input_artifact_refs=[],
        retention_class="project-lifetime",
    )
    return {
        **base,
        "status": "completed",
        "terminal_status": "completed",
        "archive": None,
        "created_at": "2026-08-27T00:00:00.000000Z",
        "updated_at": "2026-08-27T00:05:00.000000Z",
        "completed_at": "2026-08-27T00:05:00.000000Z",
    }


def _fixture_event(run_id: str = "run:algo-1", seq: int = 1) -> dict:
    base = make_algorithm_event(
        algorithm_run_id=run_id,
        event_key=f"event-{seq:04d}",
        event_type="trial-assessed",
        run_status="completed",
        payload_schema_revision=REV_C,
        input_observation_ids=[],
        artifact_ids=[],
        payload={"score": 0.95},
    )
    return {
        **base,
        "sequence": seq,
        "created_at": "2026-08-27T00:01:00.000000Z",
    }


def _fixture_result(run_id: str = "run:algo-1", seq: int = 1) -> dict:
    base = make_algorithm_result(
        algorithm_run_id=run_id,
        algorithm_id="code.algorithm.demo-v1",
        algorithm_revision=REV_A,
        problem_id="problem:p1",
        problem_revision=REV_B,
        result_type="optimization-summary",
        input_observation_ids=[],
        payload={"best_loss": 0.05},
    )
    return {
        **base,
        "sequence": seq,
        "created_at": "2026-08-27T00:05:00.000000Z",
    }


class StatusServerAlgorithmsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = Mock()
        self.run_1 = _fixture_run("run:algo-1")
        self.run_2 = _fixture_run("run:algo-2")
        self.event_1 = _fixture_event("run:algo-1", 1)
        self.result_1 = _fixture_result("run:algo-1", 1)

        self.middleware.list_algorithm_runs.return_value = [self.run_1, self.run_2]

        def _get_run(run_id: str) -> dict:
            if run_id == "run:algo-1":
                return self.run_1
            if run_id == "run:algo-2":
                return self.run_2
            raise RepositoryError(f"unknown AlgorithmRun: {run_id}")

        self.middleware.get_algorithm_run.side_effect = _get_run
        self.middleware.list_algorithm_events.return_value = [self.event_1]
        self.middleware.list_algorithm_results.return_value = [self.result_1]
        self.middleware.study_overviews.return_value = {
            "study_count": 2,
            "studies": [
                {
                    "study_id": "study:s1",
                    "algorithm_run_id": "run:algo-1",
                    "evaluation_count": 5,
                    "active_count": 2,
                },
                {
                    "study_id": "study:s2",
                    "algorithm_run_id": "run:algo-1",
                    "evaluation_count": 3,
                    "active_count": 0,
                },
                {
                    "study_id": "study:s3",
                    "algorithm_run_id": "run:other",
                    "evaluation_count": 10,
                    "active_count": 1,
                },
            ],
        }

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
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._quiet_logs.stop()

    def _get(self, path: str) -> tuple[int, dict, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
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

    def test_list_algorithms_200_and_contract_fields(self) -> None:
        status, headers, payload = self._get("/api/algorithms")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")

        self.assertEqual(payload["algorithm_count"], 2)
        self.assertEqual(len(payload["algorithms"]), 2)

        # Validate run 1 with correlated studies
        algo_1 = payload["algorithms"][0]
        validate_algorithm_run({k: algo_1[k] for k in CORE_ALGORITHM_RUN_FIELDS})
        self.assertEqual(algo_1["algorithm_run_id"], "run:algo-1")
        self.assertEqual(algo_1["status"], "completed")
        self.assertEqual(algo_1["terminal_status"], "completed")
        self.assertEqual(algo_1["study_ids"], ["study:s1", "study:s2"])
        self.assertEqual(algo_1["study_count"], 2)
        self.assertEqual(algo_1["evaluation_count"], 8)
        self.assertEqual(algo_1["active_count"], 2)

        # Validate run 2 without matching studies
        algo_2 = payload["algorithms"][1]
        validate_algorithm_run({k: algo_2[k] for k in CORE_ALGORITHM_RUN_FIELDS})
        self.assertEqual(algo_2["algorithm_run_id"], "run:algo-2")
        self.assertEqual(algo_2["status"], "completed")
        self.assertEqual(algo_2["study_ids"], [])
        self.assertEqual(algo_2["study_count"], 0)
        self.assertEqual(algo_2["evaluation_count"], 0)
        self.assertEqual(algo_2["active_count"], 0)

    def test_get_algorithm_by_id_200_and_contract_fields(self) -> None:
        status, headers, payload = self._get("/api/algorithms/run%3Aalgo-1")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")

        self.assertIn("algorithm", payload)
        self.assertIn("events", payload)
        self.assertIn("results", payload)

        algo = payload["algorithm"]
        validate_algorithm_run({k: algo[k] for k in CORE_ALGORITHM_RUN_FIELDS})
        self.assertEqual(algo["algorithm_run_id"], "run:algo-1")
        self.assertEqual(algo["status"], "completed")
        self.assertEqual(algo["terminal_status"], "completed")
        self.assertEqual(algo["study_ids"], ["study:s1", "study:s2"])
        self.assertEqual(algo["study_count"], 2)
        self.assertEqual(algo["evaluation_count"], 8)
        self.assertEqual(algo["active_count"], 2)

        self.assertEqual(len(payload["events"]), 1)
        event_dict = payload["events"][0]
        self.assertEqual(event_dict["sequence"], 1)
        self.assertIn("created_at", event_dict)
        validate_algorithm_event({
            k: event_dict[k]
            for k in (
                "contract_version",
                "algorithm_run_id",
                "algorithm_event_id",
                "event_key",
                "event_type",
                "run_status",
                "payload_schema_revision",
                "input_observation_ids",
                "artifact_ids",
                "payload",
            )
        })
        self.assertEqual(event_dict["event_key"], "event-0001")

        self.assertEqual(len(payload["results"]), 1)
        result_dict = payload["results"][0]
        self.assertEqual(result_dict["sequence"], 1)
        self.assertIn("created_at", result_dict)
        validate_algorithm_result({
            k: result_dict[k]
            for k in (
                "contract_version",
                "algorithm_result_id",
                "algorithm_run_id",
                "algorithm_id",
                "algorithm_revision",
                "problem_id",
                "problem_revision",
                "result_type",
                "input_observation_ids",
                "payload",
            )
        })
        self.assertEqual(result_dict["result_type"], "optimization-summary")

    def test_unknown_algorithm_run_returns_404_without_traceback(self) -> None:
        status, _, payload = self._get("/api/algorithms/run%3Aunknown")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "unknown AlgorithmRun: run:unknown"})
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_invalid_algorithm_run_id_returns_400_without_traceback(self) -> None:
        status, _, payload = self._get("/api/algorithms/invalid%20id%20with%20spaces")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", json.dumps(payload))

    def test_empty_id_returns_400(self) -> None:
        status, _, payload = self._get("/api/algorithms/")
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_non_get_method_returns_405(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/algorithms", data=b"{}", method="POST"
        )
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(context.exception.code, 405)
        self.assertEqual(context.exception.headers["Allow"], "GET")
        with context.exception as error:
            payload = json.loads(error.read().decode("utf-8"))
        self.assertIn("error", payload)


class StatusViewsAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = Mock()
        self.run = _fixture_run("run:algo-test")
        self.middleware.list_algorithm_runs.return_value = [self.run]
        self.middleware.get_algorithm_run.return_value = self.run
        self.middleware.list_algorithm_events.return_value = [_fixture_event("run:algo-test", 1)]
        self.middleware.list_algorithm_results.return_value = [_fixture_result("run:algo-test", 1)]
        self.middleware.study_overviews.return_value = {
            "study_count": 1,
            "studies": [
                {
                    "study_id": "study:test-1",
                    "algorithm_run_id": "run:algo-test",
                    "evaluation_count": 12,
                    "active_count": 4,
                }
            ],
        }

    def test_algorithms_overview_assembly(self) -> None:
        result = status_views.algorithms_overview(self.middleware)
        self.assertEqual(result["algorithm_count"], 1)
        self.assertEqual(len(result["algorithms"]), 1)
        item = result["algorithms"][0]
        self.assertEqual(item["algorithm_run_id"], "run:algo-test")
        self.assertEqual(item["study_ids"], ["study:test-1"])
        self.assertEqual(item["study_count"], 1)
        self.assertEqual(item["evaluation_count"], 12)
        self.assertEqual(item["active_count"], 4)

    def test_algorithms_overview_tolerates_missing_study_overviews(self) -> None:
        self.middleware.study_overviews.side_effect = RuntimeError("study_overviews error")
        result = status_views.algorithms_overview(self.middleware)
        self.assertEqual(result["algorithm_count"], 1)
        item = result["algorithms"][0]
        self.assertEqual(item["study_ids"], [])
        self.assertEqual(item["study_count"], 0)
        self.assertEqual(item["evaluation_count"], 0)
        self.assertEqual(item["active_count"], 0)

    def test_algorithm_detail_assembly(self) -> None:
        result = status_views.algorithm_detail(self.middleware, "run:algo-test")
        self.assertIn("algorithm", result)
        self.assertEqual(result["algorithm"]["algorithm_run_id"], "run:algo-test")
        self.assertEqual(result["algorithm"]["study_ids"], ["study:test-1"])
        self.assertEqual(result["algorithm"]["study_count"], 1)
        self.assertEqual(result["algorithm"]["evaluation_count"], 12)
        self.assertEqual(result["algorithm"]["active_count"], 4)
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(result["results"]), 1)

    def test_algorithm_detail_invalid_id_raises_contract_error(self) -> None:
        with self.assertRaises(ContractError):
            status_views.algorithm_detail(self.middleware, "illegal id with spaces")


if __name__ == "__main__":
    unittest.main()
