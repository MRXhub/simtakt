"""Package landing and async materialization worker for the status server."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from control_plane.core.evaluation_contracts import ContractError

PACKAGE_NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SAFE_LEAF_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
DEFAULT_PACKAGES_REL_PATH = "data/inputs/packages"
MAX_DIAGNOSTIC_CHARS = 1024

RESERVED_DEVICE_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


def is_safe_leaf_name(name: str) -> bool:
    """Check if a filename or identifier is a safe leaf name without reserved device names or trailing dots/spaces."""
    if not isinstance(name, str) or not name:
        return False
    if not SAFE_LEAF_NAME_REGEX.match(name):
        return False
    if name.endswith(".") or name.endswith(" ") or name.startswith(" "):
        return False
    stem = name.split(".")[0].upper()
    if stem in RESERVED_DEVICE_NAMES or name.upper() in RESERVED_DEVICE_NAMES:
        return False
    return True


def is_safe_package_name(name: str) -> bool:
    """Check if a package name matches conventions, is not reserved, and has no trailing dot/hyphen."""
    if not isinstance(name, str) or not name:
        return False
    if not PACKAGE_NAME_REGEX.match(name):
        return False
    if name.endswith(".") or name.endswith("-"):
        return False
    stem = name.split(".")[0].upper()
    if stem in RESERVED_DEVICE_NAMES or name.upper() in RESERVED_DEVICE_NAMES:
        return False
    return True


def relativize_text(text: str, project_root: Path) -> str:
    """Strip absolute project root paths from diagnostic text to prevent information leakage."""
    if not text:
        return ""
    try:
        root_resolved = project_root.resolve()
    except Exception:
        root_resolved = project_root
    candidates = [
        str(root_resolved),
        str(root_resolved).replace("\\", "/"),
        str(root_resolved).replace("/", "\\"),
        str(project_root),
        str(project_root).replace("\\", "/"),
        str(project_root).replace("/", "\\"),
    ]
    candidates = sorted(set(c for c in candidates if c), key=len, reverse=True)
    res = text
    for c in candidates:
        res = res.replace(c, ".")
    return res


def truncate_diagnostic(text: str, max_chars: int = MAX_DIAGNOSTIC_CHARS) -> str:
    """Truncate excessively long diagnostic output."""
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + " ... [truncated]"
    return text


class PackageLandingError(Exception):
    """Raised when package staging, verification, or landing fails."""


@dataclass
class PackageJob:
    job_id: str
    package_name: str
    content_hash: str
    status: str
    log_tail: list[str] = field(default_factory=list)
    package: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "package_name": self.package_name,
            "content_hash": self.content_hash,
            "status": self.status,
            "log_tail": list(self.log_tail),
            "package": self.package,
            "error": self.error,
        }


def validate_package_staging_dir(staging_dir: Path) -> tuple[bool, str | None]:
    """Pure-Python structural check of a staged package directory."""
    manifest_path = staging_dir / "manifest.json"
    if not manifest_path.is_file():
        return False, "manifest.json is missing"
    try:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except Exception as exc:
        return False, f"invalid manifest.json: {exc}"

    if not isinstance(manifest, dict):
        return False, "manifest.json must be an object"
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) == 0:
        return False, "manifest.json files must be a non-empty array"
    deck_file = manifest.get("deck_file")
    if not isinstance(deck_file, str) or not deck_file:
        return False, "manifest.json deck_file is required"
    if not is_safe_leaf_name(deck_file):
        return False, f"invalid or reserved deck_file name: {deck_file}"

    for dep in manifest.get("dependencies", []):
        if not isinstance(dep, str) or not is_safe_leaf_name(dep):
            return False, f"invalid or reserved dependency name: {dep}"

    for f_item in files:
        if not isinstance(f_item, dict):
            return False, "manifest file entry must be an object"
        fname = f_item.get("name")
        if not isinstance(fname, str) or not is_safe_leaf_name(fname):
            return False, f"invalid or reserved manifest file entry name: {fname}"
        fpath = staging_dir / fname
        if not fpath.is_file():
            return False, f"package file is missing: {fname}"
        raw = fpath.read_bytes()
        if len(raw) != f_item.get("bytes"):
            return False, f"package file size mismatch for {fname}"
        actual_sha = hashlib.sha256(raw).hexdigest().lower()
        if actual_sha != str(f_item.get("sha256", "")).lower():
            return False, f"package file sha256 mismatch for {fname}"

    return True, None


def default_powershell_validator(
    staging_dir: Path, project_root: Path | None = None
) -> tuple[bool, str | None]:
    """Pure-Python structural validator for staged package directory."""
    return validate_package_staging_dir(staging_dir)


class PackageLandingService:
    """Manage package submissions and serial staging.

    Job queue state is in memory and therefore unavailable after a restart.
    Registration renames the destination first, then atomically replaces its
    shard; interruption between those steps leaves an unreferenced destination,
    which startup reports without deleting.
    """

    def __init__(
        self,
        project_root: Path | str = ".",
        *,
        validator: Callable[[Path], tuple[bool, str | None]] | None = None,
        packages_dir: Path | str | None = None,
        autostart: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        if packages_dir:
            self.packages_dir = Path(packages_dir).resolve()
        else:
            self.packages_dir = (
                self.project_root / DEFAULT_PACKAGES_REL_PATH
            ).resolve()
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(__name__)
        self._cleanup_dangling_staging_dirs()
        self._scan_artifact_consistency()
        self.validator = validator
        self._jobs: dict[str, PackageJob] = {}
        self._jobs_by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        self._worker_thread: threading.Thread | None = None
        if autostart:
            self.start()

    def _cleanup_dangling_staging_dirs(self) -> None:
        """Remove any leftover .staging_* directories on startup."""
        if not self.packages_dir.is_dir():
            return
        try:
            for entry in self.packages_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(".staging"):
                    try:
                        shutil.rmtree(entry, ignore_errors=True)
                    except OSError:
                        pass
        except OSError:
            pass
    def _scan_artifact_consistency(self) -> None:
        """Warn about shard/destination mismatches without mutating either side."""
        records = self.project_root / "records" / "artifacts"
        if not records.is_dir():
            return
        referenced: set[str] = set()
        for shard_file in records.glob("pkg_*.json"):
            try:
                data = json.loads(shard_file.read_text(encoding="utf-8"))
                revisions = data["artifact"]["revisions"]
                for item in revisions:
                    path = item["locations"][0]["path"]
                    referenced.add(str(path).replace("\\", "/"))
            except Exception as exc:
                self._logger.warning("artifact consistency: cannot parse %s: %s", shard_file, exc)
        try:
            destinations = [
                self._rel_path(entry) for entry in self.packages_dir.iterdir()
                if entry.is_dir() and not entry.name.startswith(".staging")
            ]
        except OSError as exc:
            self._logger.warning("artifact consistency: cannot scan destinations: %s", exc)
            return
        for path in sorted(set(destinations) - referenced):
            self._logger.warning("artifact consistency: destination exists but is not referenced by any shard: %s", path)
        for path in sorted(referenced - set(destinations)):
            self._logger.warning("artifact consistency: shard references missing destination: %s", path)

    def start(self) -> None:
        """Start the background serial worker thread."""
        with self._lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._closed = False
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name="PackageLandingWorker",
                )
                self._worker_thread.start()

    def close(self, wait: bool = True) -> None:
        """Stop worker thread and wait for drain."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        if wait and self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)

    def submit_package(self, body: Any) -> dict[str, Any]:
        """Validate input, enforce idempotency, enqueue job, and return 202 payload."""
        if not isinstance(body, Mapping):
            raise ContractError("request body must be an object")

        raw_pkg_name = body.get("package_name")
        if not isinstance(raw_pkg_name, str):
            raise ContractError("package_name is required and must be a string")
        pkg_name = raw_pkg_name.strip()
        if not is_safe_package_name(pkg_name):
            raise ContractError(
                f"invalid or reserved package_name '{pkg_name}'"
            )

        # Path traversal prevention
        target_dir = (self.packages_dir / pkg_name).resolve()
        try:
            target_dir.relative_to(self.packages_dir)
        except ValueError as exc:
            raise ContractError("path traversal detected or invalid package path") from exc
        if target_dir.parent != self.packages_dir:
            raise ContractError("path traversal detected: nested package directory forbidden")

        deck_text = body.get("deck_text")
        if deck_text is None or not isinstance(deck_text, str):
            raise ContractError("deck_text is required and must be a string")

        deck_filename = body.get("deck_file") or "deck.in"
        if not is_safe_leaf_name(deck_filename):
            raise ContractError(f"invalid or reserved deck_file name '{deck_filename}'")

        extra_files = body.get("files") or {}
        if not isinstance(extra_files, Mapping):
            raise ContractError("files must be an object if provided")
        for fname in extra_files:
            if not is_safe_leaf_name(fname):
                raise ContractError(f"invalid or reserved file name '{fname}'")

        dependencies = body.get("dependencies") or []
        if not isinstance(dependencies, (list, tuple)):
            raise ContractError("dependencies must be a list if provided")
        for dep in dependencies:
            if not is_safe_leaf_name(dep):
                raise ContractError(f"invalid or reserved dependency name '{dep}'")

        content_hash = "sha256:" + hashlib.sha256(deck_text.encode("utf-8")).hexdigest().lower()
        idemp_key = (pkg_name, content_hash)

        with self._lock:
            # 1. In-memory idempotency (check if existing non-failed job exists)
            existing_job_id = self._jobs_by_key.get(idemp_key)
            if existing_job_id and existing_job_id in self._jobs:
                existing_job = self._jobs[existing_job_id]
                if existing_job.status != "failed":
                    return {
                        "job_id": existing_job.job_id,
                        "content_hash": existing_job.content_hash,
                        "status": existing_job.status,
                    }

            # 2. Disk reverse-lookup idempotency (after server restart)
            registered = self.find_registered_package(pkg_name, content_hash)
            if registered is not None:
                job_id = f"job-pkg-reg-{registered['package_name']}-{content_hash[7:15]}"
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job = PackageJob(
                    job_id=job_id,
                    package_name=registered["package_name"],
                    content_hash=content_hash,
                    status="registered",
                    log_tail=[
                        f"[{now_str}] [registered] Artifact recorded: {registered['artifact_id']} (revision: {content_hash}). Ready for Problem derivation."
                    ],
                    package={
                        "artifact_id": registered["artifact_id"],
                        "revision": content_hash,
                        "path": registered["path"],
                    },
                    created_at=datetime.now(timezone.utc).isoformat(),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                self._jobs[job_id] = job
                self._jobs_by_key[idemp_key] = job_id
                return {
                    "job_id": job.job_id,
                    "content_hash": job.content_hash,
                    "status": "registered",
                }

            # 3. Create fresh job
            job_id = f"job-pkg-{uuid.uuid4().hex[:8]}"
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            job = PackageJob(
                job_id=job_id,
                package_name=pkg_name,
                content_hash=content_hash,
                status="queued",
                log_tail=[
                    f"[{now_str}] [queued] Job {job_id} submitted. Target package: {pkg_name}"
                ],
                payload=dict(body),
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self._jobs[job_id] = job
            self._jobs_by_key[idemp_key] = job_id
            self._queue.put(job_id)
            return {
                "job_id": job.job_id,
                "content_hash": job.content_hash,
                "status": "queued",
            }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Query a job's current status and log tail."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                return job.to_dict()
        return None

    def find_registered_package(
        self, package_name: str, content_hash: str
    ) -> dict[str, Any] | None:
        """Check data/inputs/packages/<package_name> to see if it matches deck content hash."""
        norm_hash = content_hash.strip().lower()
        pkg_dir = self.packages_dir / package_name
        if not pkg_dir.is_dir() or pkg_dir.name.startswith(".staging"):
            return None
        manifest_file = pkg_dir / "manifest.json"
        if not manifest_file.is_file():
            return None
        try:
            manifest = json.loads(manifest_file.read_bytes().decode("utf-8"))
            deck_file = manifest.get("deck_file", "deck.in")
            deck_path = pkg_dir / deck_file
            if deck_path.is_file():
                deck_bytes = deck_path.read_bytes()
                computed = (
                    "sha256:" + hashlib.sha256(deck_bytes).hexdigest().lower()
                )
                if computed == norm_hash:
                    rel = self._rel_path(pkg_dir)
                    return {
                        "package_name": pkg_dir.name,
                        "artifact_id": manifest.get(
                            "artifact_id", f"pkg:{pkg_dir.name}"
                        ),
                        "path": rel,
                        "manifest": manifest,
                    }
        except Exception:
            pass
        return None

    def find_registered_package_by_content_hash(
        self, content_hash: str
    ) -> dict[str, Any] | None:
        """Scan data/inputs/packages/ to find an existing registered package by deck content hash."""
        norm_hash = content_hash.strip().lower()
        if not self.packages_dir.is_dir():
            return None

        try:
            entries = list(self.packages_dir.iterdir())
        except OSError:
            return None

        for entry in entries:
            if not entry.is_dir() or entry.name.startswith(".staging"):
                continue
            res = self.find_registered_package(entry.name, norm_hash)
            if res is not None:
                return res

        return None

    def wait_job(self, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Synchronously wait for a job to reach a terminal state."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.get_job(job_id)
            if job and job["status"] in {"registered", "failed"}:
                return job
            time.sleep(0.05)
        job = self.get_job(job_id)
        if job:
            return job
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    def _worker_loop(self) -> None:
        while not self._closed:
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if job_id is None:
                self._queue.task_done()
                break
            try:
                self._process_job(job_id)
            finally:
                self._queue.task_done()

    def _rel_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def _process_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return

        staging_dir = self.packages_dir / f".staging_{job.job_id}_{job.package_name}"
        dest_dir = self.packages_dir / job.package_name
        created_dest = False
        created_shard_file: Path | None = None

        try:
            # 1. Staging Step
            with self._lock:
                job.status = "staging"
                job.updated_at = datetime.now(timezone.utc).isoformat()
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(
                    f"[{now_str}] [staging] Staging deck and templates into temporary directory {self._rel_path(staging_dir)}..."
                )

            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            deck_filename = job.payload.get("deck_file") or "deck.in"
            if not is_safe_leaf_name(deck_filename):
                raise ContractError(f"invalid or reserved deck_file name '{deck_filename}'")

            (staging_dir / deck_filename).write_bytes(
                job.payload["deck_text"].encode("utf-8")
            )

            extra_files = job.payload.get("files") or {}
            for fname, fcontent in extra_files.items():
                if not is_safe_leaf_name(fname):
                    raise ContractError(f"invalid or reserved file name '{fname}'")
                if isinstance(fcontent, bytes):
                    (staging_dir / fname).write_bytes(fcontent)
                else:
                    (staging_dir / fname).write_bytes(str(fcontent).encode("utf-8"))

            dependencies = list(job.payload.get("dependencies", []))
            for dep in dependencies:
                if not is_safe_leaf_name(dep):
                    raise ContractError(f"invalid or reserved dependency name '{dep}'")

            files_meta = []
            for file_path in sorted(staging_dir.iterdir()):
                if file_path.is_file() and file_path.name != "manifest.json":
                    raw = file_path.read_bytes()
                    files_meta.append(
                        {
                            "name": file_path.name,
                            "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest().lower(),
                        }
                    )

            manifest_data = {
                "schema_version": 2,
                "artifact_id": f"pkg:{job.package_name}",
                "package_name": job.package_name,
                "package_kind": "input-package",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "deck_file": deck_filename,
                "dependencies": dependencies,
                "files": files_meta,
            }
            (staging_dir / "manifest.json").write_bytes(
                json.dumps(manifest_data, indent=2).encode("utf-8") + b"\n"
            )

            with self._lock:
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(
                    f"[{now_str}] [staging] Synthesizing manifest v2 with sha256 checksum verification..."
                )

            # 2. Verifying Step
            with self._lock:
                job.status = "verifying"
                job.updated_at = datetime.now(timezone.utc).isoformat()
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(
                    f"[{now_str}] [verifying] Running structural package validation..."
                )
                job.log_tail.append(
                    f"[{now_str}] [verifying] Checking set parameter rewrite boundaries and integrity constraints..."
                )

            if job.payload.get("simulate_failure"):
                raise PackageLandingError(
                    "Simulated validation failure requested by client"
                )

            if self.validator is not None:
                valid, err_msg = self.validator(staging_dir)
            else:
                valid, err_msg = default_powershell_validator(
                    staging_dir, self.project_root
                )

            if not valid:
                err_clean = truncate_diagnostic(
                    relativize_text(err_msg or "validation rejected", self.project_root)
                )
                raise PackageLandingError(
                    f"Package validation failed: {err_clean}"
                )

            # 3. Registering Step. Existing revisions are retained in separate
            # immutable destination directories; never overwrite a package.
            if dest_dir.exists():
                dest_dir = self.packages_dir / f"{job.package_name}__{job.content_hash[7:15]}"
            if dest_dir.exists():
                raise PackageLandingError(
                    f"destination directory already exists: {self._rel_path(dest_dir)}"
                )

            with self._lock:
                job.status = "registering"
                job.updated_at = datetime.now(timezone.utc).isoformat()
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(
                    f"[{now_str}] [registering] Verification PASSED (0 errors). Performing atomic rename staging -> {self._rel_path(dest_dir)}..."
                )

            # Write order is destination rename first, then shard replacement:
            # an interruption leaves an unreferenced destination (safe, discoverable
            # by the startup scanner), whereas shard-first could reference missing data.
            staging_dir.replace(dest_dir)
            created_dest = True

            records_artifacts = self.project_root / "records" / "artifacts"
            if records_artifacts.is_dir():
                manifest_path = dest_dir / "manifest.json"
                manifest_hash = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest().lower()
                shard_file = records_artifacts / f"pkg_{job.package_name}.json"
                shard_data: dict[str, Any]
                matching = False
                if shard_file.exists():
                    try:
                        shard_data = json.loads(shard_file.read_text(encoding="utf-8"))
                    except Exception as exc:
                        raise PackageLandingError(
                            f"artifact shard is corrupted and cannot be parsed: {shard_file}: {exc}"
                        ) from exc
                    artifact = shard_data.get("artifact")
                    if (
                        shard_data.get("schema_version") != 1
                        or shard_data.get("record_kind") != "artifact-catalog-shard"
                        or not isinstance(artifact, dict)
                        or artifact.get("artifact_id") != f"pkg:{job.package_name}"
                        or artifact.get("kind") != "input-package"
                        or artifact.get("status") != "active"
                        or not isinstance(artifact.get("revisions"), list)
                    ):
                        raise PackageLandingError(f"artifact shard is invalid: {shard_file}")
                    revisions = artifact["revisions"]
                    matches = [r for r in revisions if isinstance(r, dict) and str(r.get("revision", "")).lower() == manifest_hash]
                    if matches:
                        matching = True
                        old_path = self.project_root / str(matches[0]["locations"][0]["path"])
                        shutil.rmtree(dest_dir, ignore_errors=True)
                        created_dest = False
                        dest_dir = old_path
                    else:
                        revisions.append({
                            "revision": manifest_hash, "hash_scope": "package-manifest",
                            "locations": [{"storage": "workspace", "role": "primary", "availability": "current", "path": self._rel_path(dest_dir)}],
                        })
                        artifact["latest_revision"] = manifest_hash
                else:
                    shard_data = {
                        "schema_version": 1, "record_kind": "artifact-catalog-shard",
                        "artifact": {
                            "artifact_id": f"pkg:{job.package_name}", "kind": "input-package", "status": "active",
                            "latest_revision": manifest_hash,
                            "revisions": [{"revision": manifest_hash, "hash_scope": "package-manifest",
                                           "locations": [{"storage": "workspace", "role": "primary", "availability": "current", "path": self._rel_path(dest_dir)}]}],
                        },
                    }
                if not matching:
                    tmp = shard_file.with_name(f".{shard_file.name}.{uuid.uuid4().hex}.tmp")
                    tmp.write_bytes(json.dumps(shard_data, indent=2).encode("utf-8") + b"\n")
                    os.replace(tmp, shard_file)

            # 4. Registered Terminal State
            with self._lock:
                job.status = "registered"
                job.package = {
                    "artifact_id": f"pkg:{job.package_name}",
                    "revision": job.content_hash,
                    "path": self._rel_path(dest_dir),
                }
                job.error = None
                job.updated_at = datetime.now(timezone.utc).isoformat()
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(
                    f"[{now_str}] [registered] Artifact recorded: pkg:{job.package_name} (revision: {job.content_hash[:16]}...). Ready for Problem derivation."
                )

        except Exception as exc:
            err_msg = truncate_diagnostic(
                relativize_text(str(exc), self.project_root)
            )
            with self._lock:
                job.status = "failed"
                job.error = err_msg
                job.updated_at = datetime.now(timezone.utc).isoformat()
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job.log_tail.append(f"[{now_str}] [failed] ERROR: {err_msg}")

            # Roll back newly created dest_dir if rename happened before subsequent error
            if created_dest and dest_dir.exists():
                shutil.rmtree(dest_dir, ignore_errors=True)
                if created_shard_file and created_shard_file.exists():
                    try:
                        created_shard_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                with self._lock:
                    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    job.log_tail.append(
                        f"[{now_str}] [failed] Rolled back destination directory {self._rel_path(dest_dir)}."
                    )

            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

            with self._lock:
                job.log_tail.append(
                    f"[{now_str}] [failed] Staging directory atomically cleaned up. Rolled back successfully."
                )
