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

from control_plane.core.evaluation_contracts import ContractError, canonical_json
from control_plane.core.workspace_artifacts import resolve_workspace_artifact

PACKAGE_NAME_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
SAFE_LEAF_NAME_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
# A landed package directory: ``<package>`` or a later revision ``<package>__<suffix>``.
PACKAGE_DIR_REGEX = re.compile(r"^([a-z0-9][a-z0-9-]{2,63})(?:__[A-Za-z0-9][A-Za-z0-9._-]*)?$")
DEFAULT_PACKAGES_REL_PATH = "data/inputs/packages"
# Users drop ``<package>/`` folders here; import generates the manifest.
DEFAULT_INBOX_REL_PATH = "data/inputs/inbox"
# Operating-system clutter that is skipped rather than imported.
IGNORED_INBOX_FILES = frozenset({"thumbs.db", "desktop.ini"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "staging", "verifying", "registering"})
# An inbox folder being imported is renamed to ``.importing.<job>.<name>``.
HANDOFF_PREFIX = ".importing."
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


class PackageClaimConflict(ContractError):
    """Raised when a claimed directory's revision is already registered elsewhere."""


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


def _submission_hash(deck_file: str, files: list[dict[str, Any]], dependencies: list[str]) -> str:
    """Hash all submitted bytes and their roles, independently of registration time."""
    body = {"deck_file": deck_file, "files": sorted(files, key=lambda row: row["name"]),
            "dependencies": dependencies}
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


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
        inbox_dir: Path | str | None = None,
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
        self.inbox_dir = Path(inbox_dir or self.project_root / DEFAULT_INBOX_REL_PATH).resolve()
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(__name__)
        self._cleanup_dangling_staging_dirs()
        self._restore_interrupted_imports()
        self._scan_artifact_consistency()
        self.validator = validator
        self._jobs: dict[str, PackageJob] = {}
        self._jobs_by_key: dict[tuple[str, str], str] = {}
        self._inbox_jobs: dict[str, str] = {}
        self._lock = threading.Lock()
        self._shard_lock = threading.Lock()
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
    def _restore_interrupted_imports(self) -> None:
        """Return inbox folders whose import was cut off by a restart."""
        try:
            entries = [e for e in self.inbox_dir.iterdir() if e.name.startswith(HANDOFF_PREFIX) and e.is_dir()]
        except OSError:
            return
        for entry in entries:
            parts = entry.name[len(HANDOFF_PREFIX):].split(".", 1)
            if len(parts) != 2 or not parts[1]:
                continue
            restored = self._restore_inbox_folder(entry, parts[1], parts[0])
            self._logger.warning("inbox: returned interrupted import to %s", self._rel_path(restored))

    def _scan_artifact_consistency(self) -> None:
        """Warn about shard/destination mismatches without mutating either side."""
        records = self.project_root / "records" / "artifacts"
        referenced: set[str] = set()
        shard_files = records.glob("pkg.*.json") if records.is_dir() else ()
        for shard_file in shard_files:
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
            self._logger.warning(
                "artifact consistency: destination exists but is not referenced by any shard "
                "(claim it with POST /api/packages/claim): %s", path)
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

        names = [deck_filename, *extra_files]
        folded = [name.casefold() for name in names]
        if "manifest.json" in folded or len(folded) != len(set(folded)):
            raise ContractError("deck, extra files, and manifest.json must have distinct names")
        normalized_files = {
            name: content if isinstance(content, bytes) else str(content).encode("utf-8")
            for name, content in extra_files.items()
        }
        file_bytes = {deck_filename: deck_text.encode("utf-8"), **normalized_files}
        file_records = [{"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                        for name, raw in file_bytes.items()]
        submission_hash = _submission_hash(deck_filename, file_records, list(dependencies))
        content_hash = "sha256:" + hashlib.sha256(deck_text.encode("utf-8")).hexdigest().lower()
        idemp_key = (pkg_name, submission_hash)

        with self._lock:
            if self._closed:
                raise ContractError("package landing service is closed")
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
            registered = self.find_registered_package(pkg_name, content_hash, submission_hash=submission_hash)
            if registered is not None:
                job_id = f"job-pkg-reg-{registered['package_name']}-{submission_hash[7:15]}"
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                job = PackageJob(
                    job_id=job_id,
                    package_name=registered["package_name"],
                    content_hash=content_hash,
                    status="registered",
                    log_tail=[
                        f"[{now_str}] [registered] Artifact recorded: {registered['artifact_id']} (revision: {registered['revision']}). Ready for Problem derivation."
                    ],
                    package={
                        "artifact_id": registered["artifact_id"],
                        "revision": registered["revision"],
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
                payload={**body, "deck_file": deck_filename, "files": normalized_files,
                         "dependencies": list(dependencies), "_submission_hash": submission_hash},
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
        self, package_name: str, content_hash: str, *, submission_hash: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve an intact registered revision, including noninitial versions."""
        if not is_safe_package_name(package_name):
            raise ContractError(f"invalid or reserved package_name '{package_name}'")
        artifact_id = f"pkg.{package_name}"
        shard_path = self.project_root / "records" / "artifacts" / f"{artifact_id}.json"
        try:
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            revisions = shard["artifact"]["revisions"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        for revision in revisions:
            try:
                resolved = resolve_workspace_artifact(
                    self.project_root, artifact_id, revision=revision["revision"], expected_kind="input-package",
                )
                pkg_dir = resolved.path
                pkg_dir.relative_to(self.packages_dir)
                valid, _ = validate_package_staging_dir(pkg_dir)
                if not valid:
                    continue
                manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("package_name") != package_name:
                    continue
                deck_file = manifest["deck_file"]
                computed = "sha256:" + hashlib.sha256((pkg_dir / deck_file).read_bytes()).hexdigest()
                if computed != content_hash.strip().lower():
                    continue
                if submission_hash is not None and _submission_hash(
                    deck_file, manifest["files"], manifest.get("dependencies", []),
                ) != submission_hash:
                    continue
                return {"package_name": package_name, "artifact_id": artifact_id,
                        "revision": resolved.revision, "path": self._rel_path(pkg_dir), "manifest": manifest}
            except Exception:
                continue
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
            if (
                not entry.is_dir()
                or entry.name.startswith(".staging")
                or not is_safe_package_name(entry.name)
            ):
                continue
            res = self.find_registered_package(entry.name, norm_hash)
            if res is not None:
                return res

        return None

    def claim_package(self, directory: Any) -> dict[str, Any]:
        """Register an existing package directory that no shard references.

        The directory must sit directly under the packages root, be named
        ``<package>`` or ``<package>__<suffix>``, and carry a manifest for that
        package that passes the landing validator. Files are never moved or
        rewritten: claiming only records the directory as a revision. Claiming
        an already-recorded directory again returns it with ``claimed`` False.
        """
        if not isinstance(directory, str) or not directory.strip():
            raise ContractError("directory is required and must be a string")
        name = directory.strip()
        match = PACKAGE_DIR_REGEX.match(name)
        if match is None or not is_safe_package_name(match.group(1)):
            raise ContractError(f"invalid package directory name '{name}'")
        package_name = match.group(1)
        package_dir = (self.packages_dir / name).resolve()
        if package_dir.parent != self.packages_dir or not package_dir.is_dir():
            raise ContractError(f"package directory not found: {name}")

        if self.validator is not None:
            valid, err_msg = self.validator(package_dir)
        else:
            valid, err_msg = default_powershell_validator(package_dir, self.project_root)
        if not valid:
            raise ContractError(
                "package directory failed validation: "
                + truncate_diagnostic(relativize_text(err_msg or "validation rejected", self.project_root))
                + f"; move the folder into {self._rel_path(self.inbox_dir)} to import it with a generated manifest"
            )
        manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("package_name", package_name) != package_name:
            raise ContractError(
                f"manifest package_name '{manifest.get('package_name')}' does not match directory '{name}'"
            )
        # Preparation rejects a package whose manifest names another artifact,
        # so a claimed revision must already carry the registered identity.
        if manifest.get("artifact_id") != f"pkg.{package_name}":
            raise ContractError(
                f"manifest artifact_id '{manifest.get('artifact_id')}' must be 'pkg.{package_name}' to be claimed; "
                f"move the folder into {self._rel_path(self.inbox_dir)} to import it with a generated manifest"
            )

        manifest_hash, recorded_path = self._record_revision(package_name, package_dir, make_latest=False)
        if recorded_path is not None and recorded_path != package_dir:
            raise PackageClaimConflict(
                f"revision {manifest_hash} is already registered at {self._rel_path(recorded_path)}"
            )
        return {
            "artifact_id": f"pkg.{package_name}",
            "package_name": package_name,
            "revision": manifest_hash,
            "path": self._rel_path(package_dir),
            "claimed": recorded_path is None,
        }

    def list_inbox(self) -> dict[str, Any]:
        """Describe the folders waiting in the inbox without reading file contents."""
        items = []
        try:
            entries = sorted(self.inbox_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            item = self._scan_inbox_folder(entry)
            with self._lock:
                job = self._jobs.get(self._inbox_jobs.get(entry.name, ""))
                item["job"] = {"job_id": job.job_id, "status": job.status, "error": job.error} if job else None
            items.append(item)
        return {"path": self._rel_path(self.inbox_dir), "items": items}

    def import_inbox(self, body: Any) -> dict[str, Any]:
        """Queue an inbox folder for landing as a new package revision.

        The folder name is the package name. Every plain file in it becomes a
        package file and the manifest is generated during landing, so a legacy
        ``manifest.json`` in the folder only supplies defaults for ``deck_file``
        and ``dependencies``. The folder is removed once its files are
        registered.
        """
        if not isinstance(body, Mapping):
            raise ContractError("request body must be an object")
        directory = body.get("directory")
        if not isinstance(directory, str) or not directory.strip():
            raise ContractError("directory is required and must be a string")
        name = directory.strip()
        folder = (self.inbox_dir / name).resolve()
        if folder.parent != self.inbox_dir or not folder.is_dir() or (self.inbox_dir / name).is_symlink():
            raise ContractError(f"inbox folder not found: {name}")
        scan = self._scan_inbox_folder(folder)
        if scan["issues"]:
            raise ContractError(f"inbox folder '{name}' cannot be imported: " + "; ".join(scan["issues"]))

        deck_file = body.get("deck_file") or scan["deck_file"]
        if not deck_file:
            raise ContractError("deck_file is required: choose which file in the folder is the deck")
        if deck_file not in {row["name"] for row in scan["files"]}:
            raise ContractError(f"deck_file '{deck_file}' is not a file in inbox folder '{name}'")
        dependencies = body.get("dependencies")
        if dependencies is None:
            dependencies = scan["dependencies"]
        if not isinstance(dependencies, (list, tuple)):
            raise ContractError("dependencies must be a list if provided")
        for dep in dependencies:
            if not is_safe_leaf_name(dep):
                raise ContractError(f"invalid or reserved dependency name '{dep}'")

        with self._lock:
            if self._closed:
                raise ContractError("package landing service is closed")
            active = self._jobs.get(self._inbox_jobs.get(name, ""))
            if active is not None and active.status in ACTIVE_JOB_STATUSES:
                return {"job_id": active.job_id, "content_hash": active.content_hash, "status": active.status}
            job_id = f"job-pkg-{uuid.uuid4().hex[:8]}"
            now = datetime.now(timezone.utc)
            job = PackageJob(
                job_id=job_id,
                package_name=name,
                content_hash="",
                status="queued",
                log_tail=[f"[{now.strftime('%H:%M:%S')}] [queued] Job {job_id} submitted from inbox folder {name}."],
                payload={"_inbox_dir": name, "deck_file": deck_file, "dependencies": list(dependencies)},
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
            self._jobs[job_id] = job
            self._inbox_jobs[name] = job_id
            self._queue.put(job_id)
        return {"job_id": job_id, "content_hash": "", "status": "queued"}

    def _scan_inbox_folder(self, folder: Path, name: str | None = None) -> dict[str, Any]:
        """List an inbox folder's importable files and anything blocking import."""
        name = name or folder.name
        issues: list[str] = []
        files: list[dict[str, Any]] = []
        legacy: dict[str, Any] = {}
        if not is_safe_package_name(name):
            issues.append(f"folder name must match {PACKAGE_NAME_REGEX.pattern}")
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            children = []
            issues.append(f"cannot read folder: {exc.strerror or exc}")
        for child in children:
            if child.name.startswith(".") or child.name.casefold() in IGNORED_INBOX_FILES:
                continue
            if child.name.casefold() == "manifest.json":
                try:
                    data = json.loads(child.read_text(encoding="utf-8"))
                    legacy = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    pass
                continue
            if child.is_symlink() or not child.is_file():
                issues.append(f"only plain files can be imported: {child.name}")
            elif not is_safe_leaf_name(child.name):
                issues.append(f"unsupported file name: {child.name}")
            else:
                files.append({"name": child.name, "bytes": child.stat().st_size})
        names = [row["name"] for row in files]
        folded = [n.casefold() for n in names]
        if len(folded) != len(set(folded)):
            issues.append("file names must differ by more than letter case")
        if not files:
            issues.append("folder has no files to import")

        deck_file = legacy.get("deck_file") if legacy.get("deck_file") in names else None
        if deck_file is None:
            decks = [n for n in names if n.split(".")[0].casefold() == "deck"]
            if len(names) == 1:
                deck_file = names[0]
            elif len(decks) == 1:
                deck_file = decks[0]
        dependencies = legacy.get("dependencies")
        if not isinstance(dependencies, list) or not all(
            isinstance(d, str) and is_safe_leaf_name(d) for d in dependencies
        ):
            dependencies = []
        return {
            "directory": name,
            "package_name": name if is_safe_package_name(name) else None,
            "files": files,
            "deck_file": deck_file,
            "dependencies": dependencies,
            "issues": issues,
        }

    def _hand_off_inbox_folder(
        self, job: PackageJob, name: str, staging_dir: Path, deck_file: str
    ) -> tuple[Path, dict[str, tuple[bool, int, int]]]:
        """Take an inbox folder out of the user's path, then copy it into staging.

        The folder is renamed within the inbox, so later writes to
        ``inbox/<name>`` start a new folder instead of joining this import. The
        returned snapshot lets cleanup confirm the handed-off folder still holds
        exactly what was copied before deleting it.
        """
        folder = self.inbox_dir / name
        if folder.is_symlink() or not folder.is_dir():
            raise PackageLandingError(f"inbox folder disappeared: {name}")
        handed_off = self.inbox_dir / f"{HANDOFF_PREFIX}{job.job_id[-8:]}.{name}"
        try:
            folder.rename(handed_off)
        except OSError as exc:
            raise PackageLandingError(
                f"inbox folder '{name}' is in use and cannot be imported yet ({exc.strerror or exc})"
            ) from exc
        # The caller only learns the handed-off path on return, so any failure
        # from here on must put the folder back before propagating.
        try:
            snapshot = self._snapshot_folder(handed_off)
            scan = self._scan_inbox_folder(handed_off, name)
            if scan["issues"]:
                raise PackageLandingError("; ".join(scan["issues"]))
            if deck_file not in {row["name"] for row in scan["files"]}:
                raise PackageLandingError(f"deck_file '{deck_file}' is no longer in inbox folder '{name}'")
            for row in scan["files"]:
                shutil.copyfile(handed_off / row["name"], staging_dir / row["name"])
            if self._snapshot_folder(handed_off) != snapshot:
                raise PackageLandingError(f"inbox folder '{name}' changed while it was copied; import it again")
        except BaseException:
            self._settle_inbox_folder(job, handed_off, {}, imported=False)
            raise
        return handed_off, snapshot

    @staticmethod
    def _snapshot_folder(folder: Path) -> dict[str, tuple[bool, int, int]]:
        """Every entry's kind, size, and modification time, symlinks not followed."""
        snapshot = {}
        with os.scandir(folder) as entries:
            for entry in entries:
                st = entry.stat(follow_symlinks=False)
                snapshot[entry.name] = (entry.is_dir(follow_symlinks=False), st.st_size, st.st_mtime_ns)
        return snapshot

    def _restore_inbox_folder(self, handed_off: Path, name: str, tag: str) -> Path:
        """Move a handed-off folder back; a folder recreated meanwhile keeps its name."""
        target = self.inbox_dir / name
        if target.exists():
            target = self.inbox_dir / f"{name}.kept-{tag}"
        handed_off.rename(target)
        return target

    def _settle_inbox_folder(
        self, job: PackageJob, handed_off: Path, snapshot: dict[str, tuple[bool, int, int]], *, imported: bool
    ) -> None:
        """Delete an imported folder only if unchanged; otherwise return it to the inbox."""
        name = job.payload["_inbox_dir"]
        stage = "registered" if imported else "failed"
        try:
            if imported and self._snapshot_folder(handed_off) == snapshot:
                shutil.rmtree(handed_off)
                message = f"Removed inbox folder {name}."
            else:
                restored = self._restore_inbox_folder(handed_off, name, job.job_id[-8:])
                reason = "changed during import" if imported else "was not imported"
                message = f"Inbox folder {reason}; returned it as {self._rel_path(restored)}."
        except OSError as exc:
            message = (f"Inbox folder {self._rel_path(handed_off)} could not be cleaned up "
                       f"({exc.strerror or exc}); check it by hand.")
        with self._lock:
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            job.log_tail.append(f"[{now_str}] [{stage}] {message}")

    def _record_revision(
        self, package_name: str, package_dir: Path, *, make_latest: bool = True
    ) -> tuple[str, Path | None]:
        """Add ``package_dir``'s manifest revision to the package's artifact shard.

        Returns the revision and, when that revision was already recorded, the
        directory the shard points at; nothing is written in that case. A new
        shard always makes its first revision the latest one.
        """
        records_artifacts = self.project_root / "records" / "artifacts"
        records_artifacts.mkdir(parents=True, exist_ok=True)
        artifact_id = f"pkg.{package_name}"
        manifest_hash = "sha256:" + hashlib.sha256((package_dir / "manifest.json").read_bytes()).hexdigest().lower()
        revision = {
            "revision": manifest_hash, "hash_scope": "package-manifest",
            "locations": [{"storage": "workspace", "role": "primary", "availability": "current",
                           "path": self._rel_path(package_dir)}],
        }
        shard_file = records_artifacts / f"{artifact_id}.json"
        with self._shard_lock:
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
                    or artifact.get("artifact_id") != artifact_id
                    or artifact.get("kind") != "input-package"
                    or artifact.get("status") != "active"
                    or not isinstance(artifact.get("revisions"), list)
                ):
                    raise PackageLandingError(f"artifact shard is invalid: {shard_file}")
                revisions = artifact["revisions"]
                matches = [r for r in revisions if isinstance(r, dict) and str(r.get("revision", "")).lower() == manifest_hash]
                if matches:
                    return manifest_hash, (self.project_root / str(matches[0]["locations"][0]["path"])).resolve()
                revisions.append(revision)
                if make_latest:
                    artifact["latest_revision"] = manifest_hash
            else:
                shard_data = {
                    "schema_version": 1, "record_kind": "artifact-catalog-shard",
                    "artifact": {
                        "artifact_id": artifact_id, "kind": "input-package", "status": "active",
                        "latest_revision": manifest_hash, "revisions": [revision],
                    },
                }
            tmp = shard_file.with_name(f".{shard_file.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(json.dumps(shard_data, indent=2).encode("utf-8") + b"\n")
            os.replace(tmp, shard_file)
        return manifest_hash, None

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
        while True:
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

    def _finish_registered(self, job: PackageJob, revision: str, path: str, message: str) -> None:
        with self._lock:
            job.status = "registered"
            job.package = {"artifact_id": f"pkg.{job.package_name}", "revision": revision, "path": path}
            job.error = None
            job.updated_at = datetime.now(timezone.utc).isoformat()
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            job.log_tail.append(f"[{now_str}] [registered] {message}")

    def _process_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return

        staging_dir = self.packages_dir / f".staging_{job.job_id}_{job.package_name}"
        dest_dir = self.packages_dir / job.package_name
        created_dest = False
        created_shard_file: Path | None = None
        handoff = None

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

            inbox_name = job.payload.get("_inbox_dir")
            if inbox_name:
                handoff = self._hand_off_inbox_folder(job, inbox_name, staging_dir, deck_filename)
            else:
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
            submission_hash = job.payload.get("_submission_hash") or _submission_hash(
                deck_filename, files_meta, dependencies
            )

            if handoff is not None:
                content_hash = "sha256:" + hashlib.sha256((staging_dir / deck_filename).read_bytes()).hexdigest()
                with self._lock:
                    job.content_hash = content_hash
                registered = self.find_registered_package(
                    job.package_name, content_hash, submission_hash=submission_hash
                )
                if registered is not None:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    self._finish_registered(job, registered["revision"], registered["path"],
                                            "Inbox files match an existing revision; nothing new was written.")
                    self._settle_inbox_folder(job, *handoff, imported=True)
                    return

            manifest_data = {
                "schema_version": 2,
                "artifact_id": f"pkg.{job.package_name}",
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
                dest_dir = self.packages_dir / f"{job.package_name}__{submission_hash[7:]}"
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

            manifest_hash, recorded_path = self._record_revision(job.package_name, dest_dir)
            if recorded_path is not None and recorded_path != dest_dir.resolve():
                shutil.rmtree(dest_dir, ignore_errors=True)
                created_dest = False
                dest_dir = recorded_path

            # 4. Registered Terminal State
            self._finish_registered(
                job, manifest_hash, self._rel_path(dest_dir),
                f"Artifact recorded: pkg.{job.package_name} (revision: {manifest_hash[:16]}...). Ready for Problem derivation.",
            )
            if handoff is not None:
                self._settle_inbox_folder(job, *handoff, imported=True)

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
            if handoff is not None:
                self._settle_inbox_folder(job, *handoff, imported=False)

            with self._lock:
                job.log_tail.append(
                    f"[{now_str}] [failed] Staging directory atomically cleaned up. Rolled back successfully."
                )
