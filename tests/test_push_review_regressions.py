"""Cross-component regressions found while reviewing origin/main..HEAD."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from control_plane.core.evaluation_contracts import ContractError
from control_plane.core.workspace_artifacts import resolve_workspace_artifact
from control_plane.evaluation.preparation_phase import PreparationPhase
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.web.package_landing import PackageLandingService
from tests import test_termination_consumption as termination_tests
from tests import test_adapter_batch_queue_example as batch_tests


class PushReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def service(self):
        service = PackageLandingService(self.root)
        self.addCleanup(service.close)
        return service

    def land(self, service, **overrides):
        body = {"package_name": "review-package", "deck_text": "set x = 1\n", **overrides}
        submitted = service.submit_package(body)
        job = service.wait_job(submitted["job_id"])
        self.assertEqual(job["status"], "registered", job)
        return body, job

    def test_uploaded_and_listed_revisions_resolve_to_registered_manifest(self):
        body, job = self.land(self.service())
        package = job["package"]
        resolved = resolve_workspace_artifact(self.root, package["artifact_id"],
            revision=package["revision"], expected_kind="input-package")
        self.assertEqual(resolved.path, self.root / package["path"])
        middleware = EvaluationMiddleware(mock.Mock(), project_root=self.root)
        listed = middleware.list_packages()[0]
        self.assertEqual(listed["revision"], package["revision"])
        self.assertEqual(listed["deck_file_content"], body["deck_text"])

    def test_changed_auxiliary_file_is_not_deduplicated(self):
        service = self.service()
        _, first = self.land(service, files={"material.txt": "old"})
        _, second = self.land(service, files={"material.txt": "new"})
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertNotEqual(first["package"]["revision"], second["package"]["revision"])
        self.assertEqual((self.root / second["package"]["path"] / "material.txt").read_text(), "new")

    def test_restart_reuses_noninitial_revision(self):
        service = self.service()
        self.land(service)
        body, second = self.land(service, deck_text="set x = 2\n")
        service.close()
        restarted = self.service()
        submitted = restarted.submit_package(body)
        repeated = restarted.wait_job(submitted["job_id"])
        self.assertEqual(repeated["status"], "registered", repeated)
        self.assertEqual(repeated["package"], second["package"])

    def test_upload_cannot_overwrite_deck_or_manifest(self):
        service = self.service()
        for fields in ({"files": {"deck.in": "different deck"}},
                       {"files": {"manifest.json": "{}"}},
                       {"deck_file": "manifest.json"}):
            with self.subTest(fields=fields), self.assertRaises(ContractError):
                service.submit_package({"package_name": "review-invalid", "deck_text": "set x = 1", **fields})

    def test_preparation_failure_releases_claim_and_continues(self):
        repository = mock.Mock()
        repository.preparation_window_occupancy.return_value = 0
        repository.list_queued_evaluations.return_value = []
        repository.claim_preparation_slots.return_value = [
            {"claim_id": "bad-claim", "evaluation_id": "bad"},
            {"claim_id": "good-claim", "evaluation_id": "good"},
        ]
        phase = PreparationPhase(repository, self.root, window_limit=2, lookahead=0)
        with mock.patch.object(phase, "_make", side_effect=[ValueError("bad input"), {}]):
            self.assertEqual(phase.prepare_once(), 1)
        repository.release_preparation_claim.assert_called_once()
        repository.mark_unresolved.assert_called_once()
        repository.commit_preparation_claim.assert_called_once_with("good-claim", "runtime", {})

    def test_restart_termination_restores_batch_binding_before_confirmation(self):
        fixture = termination_tests.TerminationConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        attempt = fixture._requested()
        attempt["allocation"] = {**attempt["allocation"], "remote_workspace_root": str(self.root)}
        queue = batch_tests.FakeBatchQueue()
        first_worker = batch_tests.BatchQueueWorker(queue)
        first_worker.start_session(attempt["execution_plan"], attempt["allocation"], attempt["session_ref"])
        job_id = first_worker.job_id_for(attempt["session_ref"])
        dispatcher = fixture._dispatcher(batch_tests.BatchQueueWorker(queue))
        with mock.patch.object(dispatcher.middleware, "get_next_pending_termination", return_value=attempt):
            dispatcher.recover_once(now=termination_tests.BASE_TIME)
        self.assertIsNone(queue.query_active(job_id))
        self.assertEqual(fixture.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "confirmed")

    def test_failed_binding_restore_must_not_confirm_absent(self):
        fixture = termination_tests.TerminationConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        attempt = fixture._requested()
        worker = mock.Mock()
        worker.resume_session.side_effect = RuntimeError("binding store unavailable")
        worker.terminate_session.return_value = "absent"
        fixture._dispatcher(worker).recover_once(now=termination_tests.BASE_TIME)
        worker.terminate_session.assert_not_called()
        self.assertEqual(fixture.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "requested")

    def test_close_drains_already_accepted_jobs(self):
        entered = threading.Event()
        release = threading.Event()
        service = self.service()
        original = service._process_job
        def blocked(job_id):
            entered.set()
            release.wait(5)
            original(job_id)
        with mock.patch.object(service, "_process_job", side_effect=blocked):
            first = service.submit_package({"package_name": "first", "deck_text": "set x = 1"})
            self.assertTrue(entered.wait(2))
            second = service.submit_package({"package_name": "second", "deck_text": "set x = 2"})
            service.close(wait=False)
            release.set()
            service._worker_thread.join(5)
        self.assertEqual(service.get_job(first["job_id"])["status"], "registered")
        self.assertEqual(service.get_job(second["job_id"])["status"], "registered")

    def test_closed_service_rejects_new_jobs(self):
        service = self.service()
        service.close()
        with self.assertRaises(ContractError):
            service.submit_package({"package_name": "too-late", "deck_text": "set x = 1"})

    def test_list_packages_preserves_latest_revision_and_its_deck(self):
        service = self.service()
        self.land(service)
        body, latest = self.land(service, deck_text="set x = 2\n")
        packages = EvaluationMiddleware(mock.Mock(), project_root=self.root).list_packages()
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["revision"], latest["package"]["revision"])
        self.assertEqual(packages[0]["deck_file_content"], body["deck_text"])
