"""Cross-component regressions found while reviewing origin/main..HEAD."""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from control_plane.core.evaluation_contracts import ContractError
from control_plane.core.workspace_artifacts import resolve_workspace_artifact
from control_plane.evaluation.preparation_phase import PreparationPhase
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.web.package_landing import PackageClaimConflict, PackageLandingService
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

    def legacy_package(self, service, **overrides):
        """A landed directory whose shard is gone, as left by pre-shard builds."""
        body, job = self.land(service, **overrides)
        shard = self.root / "records" / "artifacts" / f"{job['package']['artifact_id']}.json"
        shard.unlink()
        return body, job

    def test_unreferenced_directory_is_listed_as_unclaimed(self):
        service = self.service()
        _, job = self.legacy_package(service)
        listed = EvaluationMiddleware(mock.Mock(), project_root=self.root).list_packages()
        self.assertEqual([(p["status"], p["directory"]) for p in listed], [("unclaimed", "review-package")])
        self.assertEqual(listed[0]["path"], job["package"]["path"])

    def test_claim_registers_directory_and_restores_idempotency(self):
        service = self.service()
        body, job = self.legacy_package(service)
        claimed = service.claim_package("review-package")
        self.assertTrue(claimed["claimed"])
        self.assertEqual(claimed["revision"], job["package"]["revision"])
        self.assertEqual(claimed["path"], job["package"]["path"])
        listed = EvaluationMiddleware(mock.Mock(), project_root=self.root).list_packages()
        self.assertEqual([p["status"] for p in listed], ["active"])
        repeated = service.wait_job(service.submit_package(body)["job_id"])
        self.assertEqual(repeated["package"]["path"], job["package"]["path"])
        self.assertEqual(sorted(p.name for p in service.packages_dir.iterdir()), ["review-package"])

    def test_claim_is_idempotent(self):
        service = self.service()
        self.legacy_package(service)
        self.assertTrue(service.claim_package("review-package")["claimed"])
        self.assertFalse(service.claim_package("review-package")["claimed"])

    def test_claim_keeps_existing_latest_revision(self):
        service = self.service()
        _, legacy = self.legacy_package(service)
        _, latest = self.land(service, deck_text="set x = 2\n")
        claimed = service.claim_package("review-package")
        self.assertTrue(claimed["claimed"])
        shard = json.loads((self.root / "records/artifacts/pkg.review-package.json").read_text(encoding="utf-8"))
        self.assertEqual(shard["artifact"]["latest_revision"], latest["package"]["revision"])
        self.assertIn(legacy["package"]["revision"], [r["revision"] for r in shard["artifact"]["revisions"]])

    def test_claiming_a_copy_of_a_registered_revision_conflicts(self):
        service = self.service()
        _, job = self.land(service)
        source = self.root / job["package"]["path"]
        shutil.copytree(source, source.with_name("review-package__copy"))
        with self.assertRaises(PackageClaimConflict):
            service.claim_package("review-package__copy")

    def test_claim_rejects_unsafe_or_invalid_directories(self):
        service = self.service()
        _, job = self.legacy_package(service)
        for name in ("../review-package", ".staging_x", "Review-Package", "", None, "missing-package"):
            with self.subTest(name=name), self.assertRaises(ContractError):
                service.claim_package(name)
        (self.root / job["package"]["path"] / "deck.in").write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "failed validation"):
            service.claim_package("review-package")
        other = service.packages_dir / "other-package"
        shutil.copytree(self.root / job["package"]["path"], other)
        (other / "deck.in").write_text("set x = 1\n", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ContractError, "does not match"):
            service.claim_package("other-package")

    def test_claim_rejects_manifest_with_other_artifact_identity(self):
        service = self.service()
        _, job = self.legacy_package(service)
        manifest_path = self.root / job["package"]["path"] / "manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact_id in ("pkg:review-package", None):
            manifest = dict(original)
            if artifact_id is None:
                manifest.pop("artifact_id")
            else:
                manifest["artifact_id"] = artifact_id
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(artifact_id=artifact_id), self.assertRaisesRegex(ContractError, "artifact_id"):
                service.claim_package("review-package")
        self.assertFalse((self.root / "records/artifacts/pkg.review-package.json").exists())

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

    def _unbound_termination_fixture(self):
        fixture = termination_tests.TerminationConsumptionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    @staticmethod
    def _drop_allocation(fixture, attempt_id):
        with fixture.fx.repository._transaction() as connection:
            connection.execute("UPDATE attempts SET allocation_json = NULL WHERE attempt_id = ?", (attempt_id,))

    def test_unbound_termination_uses_session_ref_directly(self):
        fixture = self._unbound_termination_fixture()
        attempt = fixture._requested()
        self._drop_allocation(fixture, attempt["attempt_id"])
        worker = mock.Mock()
        worker.terminate_session.return_value = "terminated"
        result = fixture._dispatcher(worker).recover_once(now=termination_tests.BASE_TIME)
        worker.resume_session.assert_not_called()
        worker.terminate_session.assert_called_once_with(attempt["session_ref"])
        self.assertEqual(result["termination"], "confirmed")
        self.assertTrue(result["unbound"])
        self.assertEqual(fixture.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "confirmed")

    def test_unbound_termination_never_trusts_absent(self):
        fixture = self._unbound_termination_fixture()
        attempt = fixture._requested()
        self._drop_allocation(fixture, attempt["attempt_id"])
        worker = mock.Mock()
        worker.terminate_session.return_value = "absent"
        fixture._dispatcher(worker).recover_once(now=termination_tests.BASE_TIME)
        self.assertEqual(fixture.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "unavailable")

    def test_failed_unbound_termination_no_longer_blocks_the_queue(self):
        fixture = self._unbound_termination_fixture()
        unbound = fixture._requested()
        self._drop_allocation(fixture, unbound["attempt_id"])
        bound = fixture._requested()
        worker = mock.Mock()
        worker.terminate_session.side_effect = lambda ref: (
            (_ for _ in ()).throw(RuntimeError("unknown ref")) if ref == unbound["session_ref"] else "terminated"
        )
        dispatcher = fixture._dispatcher(worker)
        dispatcher.recover_once(now=termination_tests.BASE_TIME)
        dispatcher.recover_once(now=termination_tests.BASE_TIME + termination_tests.timedelta(seconds=1))
        repository = fixture.fx.repository
        self.assertEqual(repository.get_attempt(unbound["attempt_id"])["termination_state"], "unavailable")
        self.assertEqual(repository.get_attempt(bound["attempt_id"])["termination_state"], "confirmed")

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
