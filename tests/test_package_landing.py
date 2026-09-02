"""Focused unit tests for package landing and async materialization worker."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from control_plane.core.workspace_artifacts import resolve_workspace_artifact
from control_plane.web.package_landing import (
    PackageLandingError,
    PackageLandingService,
    default_powershell_validator,
    is_safe_leaf_name,
    is_safe_package_name,
    relativize_text,
    truncate_diagnostic,
    validate_package_staging_dir,
)
from control_plane.web.status_server import DemoMiddleware, DemoPolicy, StatusServer


class PackageLandingDirectUnitTests(unittest.TestCase):
    """Direct unit tests for PackageLandingService lifecycle, staging, and validation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test-landing-")
        self.project_root = Path(self.temp_dir.name).resolve()
        self.packages_dir = self.project_root / "data" / "inputs" / "packages"
        self.packages_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_shard_preserves_multiple_revisions(self) -> None:
        records = self.project_root / "records" / "artifacts"
        records.mkdir(parents=True, exist_ok=True)
        service = PackageLandingService(
            self.project_root, packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            for deck in ("go atlas\nset v=1\nend\n", "go atlas\nset v=2\nend\n"):
                submitted = service.submit_package({
                    "package_name": "pkg-revisions", "deck_text": deck,
                })
                self.assertEqual(service.wait_job(submitted["job_id"])["status"], "registered")
            shard = json.loads((records / "pkg.pkg-revisions.json").read_text())
            revisions = shard["artifact"]["revisions"]
            self.assertEqual(len(revisions), 2)
            for entry in revisions:
                path = self.project_root / entry["locations"][0]["path"]
                actual = "sha256:" + hashlib.sha256((path / "manifest.json").read_bytes()).hexdigest()
                resolved = resolve_workspace_artifact(
                    self.project_root, "pkg.pkg-revisions",
                    revision=entry["revision"], expected_kind="input-package",
                )
                self.assertEqual(resolved.revision, entry["revision"])
        finally:
            service.close()

    def test_startup_scans_unreferenced_destination_without_mutation(self) -> None:
        dest = self.packages_dir / "pkg-orphan"
        dest.mkdir()
        sentinel = dest / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertLogs("control_plane.web.package_landing", level="WARNING") as logs:
            service = PackageLandingService(
                self.project_root, packages_dir=self.packages_dir,
                validator=lambda p: (True, None), autostart=False,
            )
        try:
            self.assertTrue(sentinel.is_file())
            self.assertTrue(any("not referenced by any shard" in line for line in logs.output))
        finally:
            service.close()

    def test_package_landing_lifecycle_success(self) -> None:
        """Job transitions queued -> staging -> verifying -> registering -> registered."""
        deck_content = "go atlas\nset thick = 1.25\nmesh\nend\n"
        content_hash = "sha256:" + hashlib.sha256(deck_content.encode("utf-8")).hexdigest().lower()

        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            res = service.submit_package({
                "package_name": "pkg-test-success",
                "deck_text": deck_content,
            })
            self.assertEqual(res["status"], "queued")
            self.assertEqual(res["content_hash"], content_hash)
            self.assertTrue(res["job_id"].startswith("job-pkg-"))

            job = service.wait_job(res["job_id"], timeout=5.0)
            self.assertEqual(job["status"], "registered")
            self.assertIsNone(job["error"])
            self.assertIsNotNone(job["package"])
            self.assertEqual(job["package"]["artifact_id"], "pkg:pkg-test-success")
            self.assertEqual(job["package"]["revision"], content_hash)

            # Check log tail step-by-step
            logs = " ".join(job["log_tail"])
            self.assertIn("[queued]", logs)
            self.assertIn("[staging]", logs)
            self.assertIn("[verifying]", logs)
            self.assertIn("[registering]", logs)
            self.assertIn("[registered]", logs)

            # Check manifest and files on disk
            final_pkg_dir = self.packages_dir / "pkg-test-success"
            self.assertTrue(final_pkg_dir.is_dir())
            manifest_file = final_pkg_dir / "manifest.json"
            self.assertTrue(manifest_file.is_file())

            manifest = json.loads(manifest_file.read_bytes().decode("utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["artifact_id"], "pkg:pkg-test-success")
            self.assertEqual(manifest["package_name"], "pkg-test-success")
            self.assertEqual(manifest["package_kind"], "input-package")
            self.assertEqual(manifest["deck_file"], "deck.in")
            self.assertEqual(len(manifest["files"]), 1)

            file_entry = manifest["files"][0]
            self.assertEqual(file_entry["name"], "deck.in")
            self.assertEqual(file_entry["bytes"], len(deck_content.encode("utf-8")))
            self.assertEqual(
                file_entry["sha256"],
                hashlib.sha256(deck_content.encode("utf-8")).hexdigest().lower(),
            )
        finally:
            service.close()

    def test_package_landing_lifecycle_failure_and_atomic_cleanup(self) -> None:
        """Validation failure updates status to failed and leaves no staging artifacts."""
        deck_content = "invalid deck without syntax\n"
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (False, "Syntax check rejected deck file"),
        )
        try:
            res = service.submit_package({
                "package_name": "pkg-test-fail",
                "deck_text": deck_content,
            })
            job = service.wait_job(res["job_id"], timeout=5.0)
            self.assertEqual(job["status"], "failed")
            self.assertIn("Syntax check rejected", str(job["error"]))
            self.assertIsNone(job["package"])

            logs = " ".join(job["log_tail"])
            self.assertIn("[failed]", logs)
            self.assertIn("atomically cleaned up", logs)

            # Target directory must NOT exist
            final_pkg_dir = self.packages_dir / "pkg-test-fail"
            self.assertFalse(final_pkg_dir.exists())

            # No lingering staging directory
            staging_dirs = [
                d.name for d in self.packages_dir.iterdir() if d.name.startswith(".staging")
            ]
            self.assertEqual(staging_dirs, [])
        finally:
            service.close()

    def test_dest_protection_directory_contents_intact_and_job_failed(self) -> None:
        """Contract 1: If target dest directory already exists, protect its contents and fail the job."""
        target_name = "pkg-existing-target"
        dest_dir = self.packages_dir / target_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        sentinel_file = dest_dir / "preserve_me.txt"
        sentinel_data = b"PROTECTED ORIGINAL CONTENT - DO NOT OVERWRITE"
        sentinel_file.write_bytes(sentinel_data)

        deck_content = "go atlas\nset attempt_overwrite = 1\nend\n"
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            res = service.submit_package({
                "package_name": target_name,
                "deck_text": deck_content,
            })
            job = service.wait_job(res["job_id"], timeout=5.0)
            self.assertEqual(job["status"], "failed")
            self.assertIn("destination directory already exists", str(job["error"]))

            # Assert existing dest content is 100% intact
            self.assertTrue(dest_dir.is_dir())
            self.assertTrue(sentinel_file.is_file())
            self.assertEqual(sentinel_file.read_bytes(), sentinel_data)

            # Assert staging directory was cleaned up
            staging_dirs = [
                d.name for d in self.packages_dir.iterdir() if d.name.startswith(".staging")
            ]
            self.assertEqual(staging_dirs, [])
        finally:
            service.close()

    def test_idempotency_key_includes_package_name(self) -> None:
        """Contract 2: Idempotency key includes package_name; same deck with different names are independent jobs."""
        deck_content = "go atlas\nset shared = 100\nend\n"
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            # First submit pkg-alpha
            job_a_submit = service.submit_package({
                "package_name": "pkg-alpha",
                "deck_text": deck_content,
            })
            # Submitting exact same package_name and deck returns identical job
            job_a_dup = service.submit_package({
                "package_name": "pkg-alpha",
                "deck_text": deck_content,
            })
            self.assertEqual(job_a_submit["job_id"], job_a_dup["job_id"])

            # Submit pkg-beta with identical deck -> MUST be a separate independent job
            job_b_submit = service.submit_package({
                "package_name": "pkg-beta",
                "deck_text": deck_content,
            })
            self.assertNotEqual(job_a_submit["job_id"], job_b_submit["job_id"])

            service.wait_job(job_a_submit["job_id"], timeout=5.0)
            service.wait_job(job_b_submit["job_id"], timeout=5.0)

            # Both directories must exist independently
            dir_a = self.packages_dir / "pkg-alpha"
            dir_b = self.packages_dir / "pkg-beta"
            self.assertTrue(dir_a.is_dir())
            self.assertTrue(dir_b.is_dir())
            self.assertTrue((dir_a / "manifest.json").is_file())
            self.assertTrue((dir_b / "manifest.json").is_file())
        finally:
            service.close()

    def test_resubmit_allowed_after_job_failed(self) -> None:
        """Contract 3: After a job fails, the client can resubmit the same package_name and deck."""
        deck_content = "go atlas\nset retry = 1\nend\n"
        fail_count = 0

        def flappy_validator(staging_dir: Path) -> tuple[bool, str | None]:
            nonlocal fail_count
            if fail_count == 0:
                fail_count += 1
                return False, "Transient failure"
            return True, None

        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=flappy_validator,
        )
        try:
            # 1. First attempt fails
            res1 = service.submit_package({
                "package_name": "pkg-flappy",
                "deck_text": deck_content,
            })
            job1 = service.wait_job(res1["job_id"], timeout=5.0)
            self.assertEqual(job1["status"], "failed")

            # 2. Resubmit same package and deck -> creates a fresh new job
            res2 = service.submit_package({
                "package_name": "pkg-flappy",
                "deck_text": deck_content,
            })
            self.assertNotEqual(res1["job_id"], res2["job_id"])
            self.assertEqual(res2["status"], "queued")

            job2 = service.wait_job(res2["job_id"], timeout=5.0)
            self.assertEqual(job2["status"], "registered")
            self.assertTrue((self.packages_dir / "pkg-flappy").is_dir())
        finally:
            service.close()

    def test_rollback_newly_created_dest_on_shard_registration_failure(self) -> None:
        """Contract 4: If shard writing fails after rename, newly created dest_dir is rolled back cleanly."""
        records_artifacts = self.project_root / "records" / "artifacts"
        records_artifacts.mkdir(parents=True, exist_ok=True)

        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            # Inject failure when writing the shard file
            orig_write = Path.write_bytes

            def failing_write_bytes(self: Path, data: bytes) -> int:
                if "records" in self.parts and "artifacts" in self.parts:
                    raise OSError("Injected disk error during shard writing")
                return orig_write(self, data)

            with patch.object(Path, "write_bytes", failing_write_bytes):
                res = service.submit_package({
                    "package_name": "pkg-shard-fail",
                    "deck_text": "go atlas\nend\n",
                })
                job = service.wait_job(res["job_id"], timeout=5.0)

            self.assertEqual(job["status"], "failed")
            self.assertIn("Injected disk error", str(job["error"]))

            # Destination directory must have been rolled back
            dest_dir = self.packages_dir / "pkg-shard-fail"
            self.assertFalse(dest_dir.exists(), "dest_dir should be rolled back and not exist")

            # Staging directory must be cleaned up
            staging_dirs = [
                d.name for d in self.packages_dir.iterdir() if d.name.startswith(".staging")
            ]
            self.assertEqual(staging_dirs, [])
        finally:
            service.close()

    def test_reserved_device_names_and_trailing_dots_rejected(self) -> None:
        """Contract 5: Reserved names (CON, PRN, AUX, NUL, COM1..9, LPT1..9) and trailing dots are rejected."""
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            # Reserved package names
            for bad_pkg in ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9", "pkg.", "bad-"]:
                with self.assertRaises(ContractError, msg=f"Should reject package_name {bad_pkg!r}"):
                    service.submit_package({
                        "package_name": bad_pkg,
                        "deck_text": "go atlas\nend\n",
                    })

            # Reserved deck_file
            for bad_deck in ["con.in", "aux.txt", "nul", "deck.", "deck "]:
                with self.assertRaises(ContractError, msg=f"Should reject deck_file {bad_deck!r}"):
                    service.submit_package({
                        "package_name": "pkg-valid-name",
                        "deck_text": "go atlas\nend\n",
                        "deck_file": bad_deck,
                    })

            # Reserved extra file in files mapping
            for bad_file in ["con.txt", "aux", "nul.dat", "extra.", "prn"]:
                with self.assertRaises(ContractError, msg=f"Should reject extra file {bad_file!r}"):
                    service.submit_package({
                        "package_name": "pkg-valid-name",
                        "deck_text": "go atlas\nend\n",
                        "files": {bad_file: "content"},
                    })

            # Reserved dependency
            for bad_dep in ["com1", "aux.dep", "dep."]:
                with self.assertRaises(ContractError, msg=f"Should reject dependency {bad_dep!r}"):
                    service.submit_package({
                        "package_name": "pkg-valid-name",
                        "deck_text": "go atlas\nend\n",
                        "dependencies": [bad_dep],
                    })
        finally:
            service.close()

    def test_diagnostic_truncation_and_relative_paths(self) -> None:
        """Contract 6: Validation diagnostics sanitize absolute project paths and truncate excessively long output."""
        abs_secret_path = str(self.project_root / "internal" / "secret" / "staging_leak.deck")
        long_noise = "A" * 3000
        raw_error_message = f"Validation syntax error at {abs_secret_path}: {long_noise}"

        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (False, raw_error_message),
        )
        try:
            res = service.submit_package({
                "package_name": "pkg-diag-test",
                "deck_text": "go atlas\nend\n",
            })
            job = service.wait_job(res["job_id"], timeout=5.0)
            self.assertEqual(job["status"], "failed")

            err_str = str(job["error"])
            # 1. Path must be relativized (no absolute project_root string leaked)
            self.assertNotIn(str(self.project_root), err_str)
            # 2. Output must be truncated
            self.assertTrue(len(err_str) <= 1200)
            self.assertIn("[truncated]", err_str)

            # Check log tail as well
            for log_line in job["log_tail"]:
                self.assertNotIn(str(self.project_root), log_line)
        finally:
            service.close()

    def test_startup_cleanup_dangling_staging_dirs(self) -> None:
        """Contract 8: Leftover .staging_* directories are automatically removed on service startup."""
        # Create simulated leftover crash directories
        dangling1 = self.packages_dir / ".staging_crash123_pkgold"
        dangling1.mkdir(parents=True, exist_ok=True)
        (dangling1 / "temp.txt").write_text("unfinished staging", encoding="utf-8")

        dangling2 = self.packages_dir / ".staging_crash456_pkgother"
        dangling2.mkdir(parents=True, exist_ok=True)

        # Existing normal package directory should NOT be touched
        legit_pkg = self.packages_dir / "pkg-legit-existing"
        legit_pkg.mkdir(parents=True, exist_ok=True)
        (legit_pkg / "preserve.txt").write_text("keep me", encoding="utf-8")

        # Startup service
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            self.assertFalse(dangling1.exists(), "dangling staging dir 1 should be removed on startup")
            self.assertFalse(dangling2.exists(), "dangling staging dir 2 should be removed on startup")
            self.assertTrue(legit_pkg.exists(), "normal package dir should remain untouched")
            self.assertTrue((legit_pkg / "preserve.txt").exists())
        finally:
            service.close()

    def test_restart_reverse_lookup_by_content_hash(self) -> None:
        """After service restart, submitting identical content returns registered package."""
        deck_content = "go atlas\nset reboot = 1\nend\n"
        content_hash = "sha256:" + hashlib.sha256(deck_content.encode("utf-8")).hexdigest().lower()

        # Phase 1: create package with first service
        service1 = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            res1 = service1.submit_package({
                "package_name": "pkg-persisted",
                "deck_text": deck_content,
            })
            service1.wait_job(res1["job_id"], timeout=5.0)
        finally:
            service1.close()

        # Phase 2: new service instance (simulating server restart)
        service2 = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            # Query reverse lookup directly
            found = service2.find_registered_package("pkg-persisted", content_hash)
            self.assertIsNotNone(found)
            self.assertEqual(found["package_name"], "pkg-persisted")
            self.assertEqual(found["artifact_id"], "pkg:pkg-persisted")

            # Submit again on clean service -> immediately recognizes registered package
            res2 = service2.submit_package({
                "package_name": "pkg-persisted",
                "deck_text": deck_content,
            })
            self.assertEqual(res2["status"], "registered")
            self.assertEqual(res2["content_hash"], content_hash)

            job2 = service2.get_job(res2["job_id"])
            self.assertIsNotNone(job2)
            self.assertEqual(job2["status"], "registered")
            self.assertEqual(job2["package"]["artifact_id"], "pkg:pkg-persisted")
        finally:
            service2.close()

    def test_reject_invalid_names_and_path_traversal(self) -> None:
        """Invalid package names and path traversal attempts raise ContractError."""
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )
        try:
            invalid_names = [
                "",
                "ab",  # too short (<3)
                "a" * 65,  # too long (>64)
                "-pkg",  # cannot start with hyphen
                "pkg_with_underscore",  # underscores forbidden
                "pkg/nested",  # path separator
                "pkg\\nested",  # backslash
                "../escape",  # relative traversal
                "pkg.dot",  # dot forbidden
                "PKG-UPPER",  # uppercase forbidden
                "pkg space",  # space forbidden
                "con",  # reserved name
                "nul",  # reserved name
                "com1",  # reserved name
                "lpt1",  # reserved name
                "pkg-ending-in-hyphen-",  # trailing hyphen
            ]
            for name in invalid_names:
                with self.assertRaises(ContractError, msg=f"Should reject: {name!r}"):
                    service.submit_package({
                        "package_name": name,
                        "deck_text": "go atlas\nend\n",
                    })

            # deck_text must be a string
            with self.assertRaises(ContractError):
                service.submit_package({"package_name": "pkg-valid", "deck_text": None})
            with self.assertRaises(ContractError):
                service.submit_package({"package_name": "pkg-valid", "deck_text": 1234})
        finally:
            service.close()

    def test_simulate_failure_flag(self) -> None:
        """simulate_failure flag in payload triggers failure and rollback."""
        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),  # Validator would pass, but simulate_failure overrides
        )
        try:
            res = service.submit_package({
                "package_name": "pkg-sim-fail",
                "deck_text": "go atlas\nend\n",
                "simulate_failure": True,
            })
            job = service.wait_job(res["job_id"], timeout=5.0)
            self.assertEqual(job["status"], "failed")
            self.assertIn("Simulated validation failure", str(job["error"]))
            self.assertFalse((self.packages_dir / "pkg-sim-fail").exists())
        finally:
            service.close()

    def test_validate_package_staging_dir_structure(self) -> None:
        """Structural validator correctly checks valid and corrupted staging directories."""
        staging = self.packages_dir / ".staging_test_validator"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            # 1. Missing manifest
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)
            self.assertIn("manifest.json is missing", str(err))

            # 2. Corrupt manifest
            (staging / "manifest.json").write_bytes(b"{corrupt")
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)

            # 3. Missing deck file referenced in manifest
            deck_content = "go atlas\nend\n"
            manifest = {
                "schema_version": 2,
                "deck_file": "deck.in",
                "files": [
                    {
                        "name": "deck.in",
                        "bytes": len(deck_content.encode("utf-8")),
                        "sha256": hashlib.sha256(deck_content.encode("utf-8")).hexdigest(),
                    }
                ],
            }
            (staging / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8") + b"\n")
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)
            self.assertIn("missing: deck.in", str(err))

            # 4. Valid package staging
            (staging / "deck.in").write_bytes(deck_content.encode("utf-8"))
            valid, err = validate_package_staging_dir(staging)
            self.assertTrue(valid)
            self.assertIsNone(err)

            # 5. Size mismatch
            (staging / "deck.in").write_bytes(b"short")
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)
            self.assertIn("size mismatch", str(err))

            # 6. Hash mismatch with same length
            (staging / "deck.in").write_bytes(b"X" * len(deck_content.encode("utf-8")))
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)
            self.assertIn("sha256 mismatch", str(err))

            # 7. Reserved deck filename in manifest
            manifest["deck_file"] = "con.in"
            manifest["files"][0]["name"] = "con.in"
            (staging / "con.in").write_bytes(deck_content.encode("utf-8"))
            (staging / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8") + b"\n")
            valid, err = validate_package_staging_dir(staging)
            self.assertFalse(valid)
            self.assertIn("reserved", str(err).lower())
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def test_serial_worker_processes_jobs_in_fifo_order(self) -> None:
        """Jobs queued in succession are processed serially by the single worker thread."""
        order_processed: list[str] = []

        def slow_validator(staging_dir: Path) -> tuple[bool, str | None]:
            manifest = json.loads((staging_dir / "manifest.json").read_bytes().decode("utf-8"))
            time.sleep(0.05)
            order_processed.append(manifest["package_name"])
            return True, None

        service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=slow_validator,
        )
        try:
            j1 = service.submit_package({"package_name": "pkg-fifo-1", "deck_text": "deck 1"})
            j2 = service.submit_package({"package_name": "pkg-fifo-2", "deck_text": "deck 2"})
            j3 = service.submit_package({"package_name": "pkg-fifo-3", "deck_text": "deck 3"})

            service.wait_job(j3["job_id"], timeout=5.0)
            self.assertEqual(order_processed, ["pkg-fifo-1", "pkg-fifo-2", "pkg-fifo-3"])
        finally:
            service.close()

    def test_default_powershell_validator_integration(self) -> None:
        """Test default powershell validator subprocess or fallback on current repo."""
        repo_root = Path(__file__).resolve().parents[1]
        staging_dir = repo_root / "data" / "inputs" / "packages" / ".staging_ps_unit_test"
        staging_dir.mkdir(parents=True, exist_ok=True)
        try:
            deck = "go atlas\nset x = 1.0\nend\n"
            (staging_dir / "deck.in").write_bytes(deck.encode("utf-8"))
            manifest = {
                "schema_version": 2,
                "artifact_id": "pkg:test-ps-unit",
                "package_name": "test-ps-unit",
                "package_kind": "input-package",
                "created_at": "2026-08-27T00:00:00Z",
                "deck_file": "deck.in",
                "dependencies": [],
                "files": [
                    {
                        "name": "deck.in",
                        "bytes": len(deck.encode("utf-8")),
                        "sha256": hashlib.sha256(deck.encode("utf-8")).hexdigest().lower(),
                    }
                ],
            }
            (staging_dir / "manifest.json").write_bytes(
                json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
            )

            valid, err = default_powershell_validator(staging_dir, repo_root)
            self.assertTrue(valid, f"PS validator failed: {err}")
            self.assertIsNone(err)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)


class PackageLandingHttpServerTests(unittest.TestCase):
    """HTTP endpoint integration tests for POST /api/packages and GET /api/packages/jobs/<id>."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test-status-landing-")
        self.project_root = Path(self.temp_dir.name).resolve()
        self.packages_dir = self.project_root / "data" / "inputs" / "packages"
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.middleware = DemoMiddleware()

        # Dedicated landing service with mock validator
        self.landing_service = PackageLandingService(
            self.project_root,
            packages_dir=self.packages_dir,
            validator=lambda p: (True, None),
        )

        topology = {
            "targets": [{"target_id": "demo-target-a", "host_id": "demo-host-a", "formal_execution": True}],
            "license_pool_groups": {"demo-pool": ["demo-target-a"]},
        }
        policy = DemoPolicy()

        self.server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=self.project_root,
            allow_writes=True,
            demo=True,
            topology=topology,
            policy=policy,
            package_landing=self.landing_service,
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2.0)
        self.temp_dir.cleanup()

    def _post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_post_packages_returns_202_accepted_shape(self) -> None:
        """POST /api/packages returns 202 Accepted with job_id and content_hash."""
        deck_text = "go atlas\nset x = 10\nend\n"
        expected_hash = "sha256:" + hashlib.sha256(deck_text.encode("utf-8")).hexdigest().lower()

        status, payload = self._post("/api/packages", {
            "package_name": "pkg-http-test",
            "deck_text": deck_text,
        })
        self.assertEqual(status, 202)
        self.assertIn("job_id", payload)
        self.assertEqual(payload["content_hash"], expected_hash)
        self.assertTrue(payload["job_id"].startswith("job-pkg-"))

        # Query GET /api/packages/jobs/<job_id>
        job_id = payload["job_id"]
        # Wait for worker to finish
        self.landing_service.wait_job(job_id, timeout=5.0)

        get_status, get_payload = self._get(f"/api/packages/jobs/{job_id}")
        self.assertEqual(get_status, 200)
        self.assertEqual(get_payload["status"], "registered")
        self.assertEqual(get_payload["job_id"], job_id)
        self.assertEqual(get_payload["package_name"], "pkg-http-test")
        self.assertEqual(get_payload["content_hash"], expected_hash)
        self.assertEqual(get_payload["package"]["artifact_id"], "pkg:pkg-http-test")
        self.assertTrue(len(get_payload["log_tail"]) >= 4)

    def test_post_packages_invalid_name_or_traversal_returns_400(self) -> None:
        """POST /api/packages returns 400 when package_name is illegal or tries traversal."""
        bad_payloads = [
            {"package_name": "../traversal", "deck_text": "go atlas\nend\n"},
            {"package_name": "UPPERCASE_NAME", "deck_text": "go atlas\nend\n"},
            {"package_name": "bad/slash", "deck_text": "go atlas\nend\n"},
            {"package_name": "x", "deck_text": "go atlas\nend\n"},
            {"package_name": "con", "deck_text": "go atlas\nend\n"},
            {"package_name": "aux", "deck_text": "go atlas\nend\n"},
            {"package_name": "nul", "deck_text": "go atlas\nend\n"},
            {"package_name": "com1", "deck_text": "go atlas\nend\n"},
            {"package_name": "lpt1", "deck_text": "go atlas\nend\n"},
            {"package_name": "pkg.", "deck_text": "go atlas\nend\n"},
        ]
        for body in bad_payloads:
            status, payload = self._post("/api/packages", body)
            self.assertEqual(status, 400, f"Expected 400 for {body}")
            self.assertIn("error", payload)

    def test_transfer_encoding_rejected_with_400(self) -> None:
        """Contract 7: Requests with Transfer-Encoding header are rejected with 400 Bad Request."""
        # 1. POST /api/packages with Transfer-Encoding header
        status, payload = self._post(
            "/api/packages",
            {"package_name": "pkg-te-test", "deck_text": "go atlas\nend\n"},
            headers={"Transfer-Encoding": "chunked"},
        )
        self.assertEqual(status, 400)
        self.assertIn("Transfer-Encoding", payload.get("error", ""))

        # 2. GET /api/health with Transfer-Encoding header
        get_status, get_payload = self._get(
            "/api/health",
            headers={"Transfer-Encoding": "chunked"},
        )
        self.assertEqual(get_status, 400)
        self.assertIn("Transfer-Encoding", get_payload.get("error", ""))

    def test_get_unknown_job_returns_404(self) -> None:
        """GET /api/packages/jobs/<missing> returns 404."""
        status, payload = self._get("/api/packages/jobs/job-pkg-missing-999")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_writes_disabled_rejects_post_packages_with_405(self) -> None:
        """When allow_writes is False, POST /api/packages is rejected with 405 Method Not Allowed."""
        topology = {
            "targets": [{"target_id": "demo-target-a", "host_id": "demo-host-a", "formal_execution": True}],
            "license_pool_groups": {"demo-pool": ["demo-target-a"]},
        }
        policy = DemoPolicy()
        ro_server = StatusServer(
            ("127.0.0.1", 0),
            middleware=self.middleware,
            project_root=self.project_root,
            allow_writes=False,  # Read-only write gate
            demo=True,
            topology=topology,
            policy=policy,
            package_landing=self.landing_service,
        )
        ro_thread = threading.Thread(target=ro_server.serve_forever, daemon=True)
        ro_thread.start()
        ro_base_url = f"http://127.0.0.1:{ro_server.server_address[1]}"
        try:
            url = f"{ro_base_url}/api/packages"
            data = json.dumps({"package_name": "pkg-ro-test", "deck_text": "go atlas\nend\n"}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5.0)
            self.assertEqual(ctx.exception.code, 405)
        finally:
            ro_server.shutdown()
            ro_server.server_close()
            ro_thread.join(timeout=2.0)

    def test_existing_endpoints_remain_functional(self) -> None:
        """Regression check asserting that existing status endpoints continue to operate."""
        # 1. GET /api/health
        h_status, h_data = self._get("/api/health")
        self.assertEqual(h_status, 200)
        self.assertEqual(h_data["status"], "ok")
        self.assertTrue(h_data["writes_enabled"])

        # 2. GET /api/capacity
        c_status, c_data = self._get("/api/capacity")
        self.assertEqual(c_status, 200)
        self.assertIn("global", c_data)

        # 3. GET /api/overview
        o_status, o_data = self._get("/api/overview")
        self.assertEqual(o_status, 200)
        self.assertIn("studies", o_data)

        # 4. POST /api/packages/parse (existing mutation view)
        p_status, p_data = self._post("/api/packages/parse", {
            "deck_text": "go atlas\nset doping = 1e18\nset length = 0.5\nend\n"
        })
        self.assertEqual(p_status, 200)
        self.assertEqual(len(p_data["parameters"]), 2)
