"""SQLite WAL persistence for the evaluation middleware control plane."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from control_plane.core.evaluation_contracts import (
    ACTIVE_ATTEMPT_STATES,
    CAPACITY_HOLDING_ATTEMPT_STATES,
    HEARTBEATABLE_ATTEMPT_STATES,
    TERMINATION_REQUEST_SOURCE_STATES,
    ContractError,
    attempt_states_sql,
    make_attempt,
    normalize_token,
    validate_algorithm_event,
    validate_algorithm_result,
    validate_algorithm_run,
    validate_attempt,
    validate_candidate,
    validate_evaluation_request,
    validate_observation,
    validate_problem_definition,
    validate_qualification_report,
)
from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    validate_execution_preparation,
)
from control_plane.evaluation.compute_profile import (
    ComputeProfileError,
    MAX_PROFILE_BUCKETS,
    MAX_RECENT_FEEDBACK_PER_BUCKET,
    MAX_SNAPSHOT_IDENTITIES,
    make_capacity_profile_snapshot,
    make_shape_record,
    make_task_class,
    validate_feedback_observation,
    welford_stddev,
    welford_update,
)
from control_plane.evaluation.execution_planning import materialize_session_plan
from control_plane.evaluation.automation_policy import DEFAULT_AUTOMATION_POLICY, most_conservative
from control_plane.evaluation.scheduling import SchedulingError, validate_resource_allocation
_ACTIVE_ATTEMPT_STATES_SQL = attempt_states_sql(ACTIVE_ATTEMPT_STATES)
_CAPACITY_HOLDING_ATTEMPT_STATES_SQL = attempt_states_sql(CAPACITY_HOLDING_ATTEMPT_STATES)
_HEARTBEATABLE_ATTEMPT_STATES_SQL = attempt_states_sql(HEARTBEATABLE_ATTEMPT_STATES)
from control_plane.simulation.session_contracts import validate_simulation_session_plan


SCHEMA_VERSION = 13
_SHA256_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")


def _safe_json_object(raw: str) -> Any:
    """Decode persisted JSON for audit scanning without masking bad budgets."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _normalize_study_artifact_refs(value: Any) -> list[dict[str, str]]:
    """Normalize the small, immutable artifact reference projection used by Study."""

    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError("artifact_refs must be an array")
    refs: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {"artifact_id", "revision"}:
            raise ContractError(
                f"artifact_refs[{index}] must contain artifact_id and revision"
            )
        artifact_id = normalize_token(
            item.get("artifact_id"), f"artifact_refs[{index}].artifact_id"
        )
        revision = str(item.get("revision", "")).strip().lower()
        if not _SHA256_REVISION.fullmatch(revision):
            raise ContractError(
                f"artifact_refs[{index}].revision must be sha256:<64 lowercase hex characters>"
            )
        refs.append({"artifact_id": artifact_id, "revision": revision})
    refs.sort(key=lambda item: (item["artifact_id"], item["revision"]))
    if len({(item["artifact_id"], item["revision"]) for item in refs}) != len(refs):
        raise ContractError("artifact_refs must not contain duplicates")
    return refs


class RepositoryError(RuntimeError):
    """Raised when persisted state violates a middleware invariant."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    actual = _utc_now() if value is None else value
    if actual.tzinfo is None:
        raise RepositoryError("timestamps must be timezone-aware")
    return actual.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _preparation_provenance(
    value: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {
        "artifact_id",
        "revision",
        "project_state_revision",
    }:
        raise RepositoryError("execution preparation provenance is invalid")
    artifact_id = str(value.get("artifact_id", ""))
    revision = str(value.get("revision", "")).lower()
    state_revision = str(value.get("project_state_revision", "")).lower()
    if (
        not re.fullmatch(
            r"configuration\.project-scheduling-policy\."
            r"[a-z0-9][a-z0-9._-]{0,79}",
            artifact_id,
        )
        or not _SHA256_REVISION.fullmatch(revision)
        or not _SHA256_REVISION.fullmatch(state_revision)
    ):
        raise RepositoryError("execution preparation provenance is invalid")
    return {
        "artifact_id": artifact_id,
        "revision": revision,
        "project_state_revision": state_revision,
    }


class SQLiteEvaluationRepository:
    """Atomic fact store; simulator files remain in the governed artifact store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        if not self.path.parent.is_dir():
            raise RepositoryError(f"database parent directory does not exist: {self.path.parent}")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RepositoryError("SQLite database did not enter WAL mode")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_documents (
                    revision TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS problem_definitions (
                    problem_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (problem_id, revision)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    problem_id TEXT NOT NULL,
                    problem_revision TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (problem_id, problem_revision)
                        REFERENCES problem_definitions(problem_id, revision)
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    candidate_id TEXT NOT NULL,
                    fidelity TEXT NOT NULL,
                    requested_outputs_json TEXT NOT NULL,
                    evidence_profile TEXT NOT NULL,
                    independence_requirement TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    simulation_adapter TEXT NOT NULL,
                    numerical_profile TEXT NOT NULL,
                    checkpoint_parent_attempt_id TEXT,
                    status TEXT NOT NULL,
                    termination_state TEXT,
                    failure_class TEXT,
                    artifact_ids_json TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    session_ref TEXT,
                    last_heartbeat_at TEXT,
                    execution_preparation_id TEXT,
                    execution_preparation_json TEXT,
                    selected_execution_option_id TEXT,
                    execution_plan_id TEXT,
                    execution_plan_json TEXT,
                    allocation_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (evaluation_id, attempt_number),
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(evaluation_id),
                    FOREIGN KEY (checkpoint_parent_attempt_id) REFERENCES attempts(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS preparation_claims (
                    claim_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    controller_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(evaluation_id)
                );

                CREATE TABLE IF NOT EXISTS qualification_reports (
                    qualification_report_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(evaluation_id),
                    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS observations (
                    observation_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    observation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(evaluation_id),
                    FOREIGN KEY (candidate_id) REFERENCES candidates(candidate_id)
                );

                CREATE TABLE IF NOT EXISTS algorithm_runs (
                    algorithm_run_id TEXT PRIMARY KEY,
                    algorithm_id TEXT NOT NULL,
                    algorithm_revision TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    problem_revision TEXT NOT NULL,
                    run_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    terminal_status TEXT,
                    archive_bundle_revision TEXT,
                    archive_artifact_id TEXT,
                    archive_revision TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    archived_at TEXT,
                    FOREIGN KEY (problem_id, problem_revision)
                        REFERENCES problem_definitions(problem_id, revision)
                );

                CREATE TABLE IF NOT EXISTS algorithm_events (
                    algorithm_event_id TEXT PRIMARY KEY,
                    algorithm_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    run_status TEXT NOT NULL,
                    input_observation_ids_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (algorithm_run_id, sequence),
                    UNIQUE (algorithm_run_id, event_key),
                    FOREIGN KEY (algorithm_run_id)
                        REFERENCES algorithm_runs(algorithm_run_id)
                );

                CREATE TABLE IF NOT EXISTS algorithm_results (
                    algorithm_result_id TEXT PRIMARY KEY,
                    algorithm_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    algorithm_id TEXT NOT NULL,
                    algorithm_revision TEXT NOT NULL,
                    problem_id TEXT NOT NULL,
                    problem_revision TEXT NOT NULL,
                    result_type TEXT NOT NULL,
                    input_observation_ids_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (algorithm_run_id, sequence),
                    FOREIGN KEY (algorithm_run_id)
                        REFERENCES algorithm_runs(algorithm_run_id),
                    FOREIGN KEY (problem_id, problem_revision)
                        REFERENCES problem_definitions(problem_id, revision)
                );

                CREATE TABLE IF NOT EXISTS state_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS outbox_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_dispatch
                    ON attempts(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_attempts_lease
                    ON attempts(status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS idx_evaluations_queue
                    ON evaluations(status, updated_at, created_at, evaluation_id);
                CREATE INDEX IF NOT EXISTS idx_preparation_claims_expiry
                    ON preparation_claims(expires_at);
                CREATE INDEX IF NOT EXISTS idx_observations_candidate
                    ON observations(candidate_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_algorithm_events_run
                    ON algorithm_events(algorithm_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_algorithm_results_run
                    ON algorithm_results(algorithm_run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
                    ON outbox_events(published_at, sequence);
                """
            )
            existing = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if existing is None or int(existing["version"]) in set(range(1, SCHEMA_VERSION + 1)):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(attempts)")
                    }
                    if "termination_state" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN termination_state TEXT"
                        )
                    if "session_ref" not in columns:
                        connection.execute("ALTER TABLE attempts ADD COLUMN session_ref TEXT")
                    if "execution_plan_id" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN execution_plan_id TEXT"
                        )
                    if "execution_plan_json" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN execution_plan_json TEXT"
                        )
                    if "allocation_json" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN allocation_json TEXT"
                        )
                    if "execution_preparation_id" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN execution_preparation_id TEXT"
                        )
                    if "execution_preparation_json" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN execution_preparation_json TEXT"
                        )
                    if "selected_execution_option_id" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN selected_execution_option_id TEXT"
                        )
                    if "feedback_json" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN feedback_json TEXT"
                        )
                    if "feedback_recorded_at" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN feedback_recorded_at TEXT"
                        )
                    if "last_heartbeat_at" not in columns:
                        connection.execute(
                            "ALTER TABLE attempts ADD COLUMN last_heartbeat_at TEXT"
                        )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS attempt_feedback (
                            attempt_id TEXT PRIMARY KEY, task_class_key TEXT NOT NULL,
                            target_id TEXT NOT NULL, profile_revision TEXT NOT NULL,
                            processors INTEGER NOT NULL, succeeded INTEGER NOT NULL,
                            wall_seconds REAL, cpu_seconds REAL, busy_seconds REAL,
                            rss_bytes INTEGER, created_at TEXT NOT NULL,
                            FOREIGN KEY (attempt_id) REFERENCES attempts(attempt_id))"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS task_shape_stats (
                            task_class_key TEXT NOT NULL, target_id TEXT NOT NULL,
                            profile_revision TEXT NOT NULL, processors INTEGER NOT NULL,
                            sample_count INTEGER NOT NULL, success_count INTEGER NOT NULL,
                            failure_count INTEGER NOT NULL, wall_samples INTEGER NOT NULL,
                            wall_mean_seconds REAL, wall_m2_seconds REAL,
                            cpu_samples INTEGER NOT NULL, cpu_mean_seconds REAL,
                            cpu_m2_seconds REAL, busy_samples INTEGER NOT NULL,
                            busy_mean_seconds REAL, busy_m2_seconds REAL,
                            rss_samples INTEGER NOT NULL, rss_mean_bytes REAL,
                            rss_m2_bytes REAL, updated_at TEXT NOT NULL,
                            PRIMARY KEY (task_class_key, target_id, profile_revision, processors))"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_task_shape_stats_class "
                        "ON task_shape_stats(task_class_key, target_id, profile_revision, processors)"
                    )
                    # Study registration is an additive migration.  Keep it in the
                    # versioned transaction so existing control-plane rows are never
                    # rewritten or copied.
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS studies (
                            study_id TEXT PRIMARY KEY,
                            problem_id TEXT NOT NULL,
                            problem_revision TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            metadata_json TEXT NOT NULL,
                            algorithm_run_id TEXT,
                            artifact_refs_json TEXT NOT NULL,
                            automation_profile TEXT NOT NULL DEFAULT 'assisted',
                            FOREIGN KEY (problem_id, problem_revision)
                                REFERENCES problem_definitions(problem_id, revision)
                        )"""
                    )
                    study_columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(studies)")
                    }
                    if "automation_profile" not in study_columns:
                        connection.execute(
                            "ALTER TABLE studies ADD COLUMN automation_profile TEXT NOT NULL DEFAULT 'assisted'"
                        )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS study_evaluations (
                            study_id TEXT NOT NULL,
                            evaluation_id TEXT NOT NULL,
                            PRIMARY KEY (study_id, evaluation_id),
                            FOREIGN KEY (study_id) REFERENCES studies(study_id),
                            FOREIGN KEY (evaluation_id)
                                REFERENCES evaluations(evaluation_id)
                        )"""
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_studies_problem "
                        "ON studies(problem_id, problem_revision, created_at, study_id)"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_study_evaluations_evaluation "
                        "ON study_evaluations(evaluation_id, study_id)"
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS schema_documents (
                            revision TEXT PRIMARY KEY,
                            kind TEXT NOT NULL,
                            canonical_json TEXT NOT NULL,
                            registered_at TEXT NOT NULL
                        )"""
                    )
                    if existing is None:
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (SCHEMA_VERSION, _iso()),
                        )
                    elif int(existing["version"]) != SCHEMA_VERSION:
                        if int(existing["version"]) < 12:
                            connection.execute(
                                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                                (12, _iso()),
                            )
                        connection.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (SCHEMA_VERSION, _iso()),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            elif int(existing["version"]) != SCHEMA_VERSION:
                raise RepositoryError(
                    f"unsupported evaluation database schema: {existing['version']}"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_session_ref
                ON attempts(session_ref) WHERE session_ref IS NOT NULL
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                expected_index_sql = (
                    "CREATE UNIQUE INDEX idx_attempts_active_preparation "
                    "ON attempts(execution_preparation_id) "
                    "WHERE execution_preparation_id IS NOT NULL "
                    f"AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})"
                )
                existing_index = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' "
                    "AND name='idx_attempts_active_preparation'"
                ).fetchone()
                normalize_sql = lambda sql: re.sub(r"\s+", " ", str(sql or "")).strip().lower()
                if normalize_sql(existing_index["sql"] if existing_index else "") != normalize_sql(
                    expected_index_sql
                ):
                    connection.execute(
                        "DROP INDEX IF EXISTS idx_attempts_execution_preparation"
                    )
                    connection.execute(
                        "DROP INDEX IF EXISTS idx_attempts_active_preparation"
                    )
                    try:
                        connection.execute(expected_index_sql)
                    except sqlite3.DatabaseError as exc:
                        raise RepositoryError(
                            "failed to rebuild idx_attempts_active_preparation "
                            f"with expected predicate: {exc}"
                        ) from exc
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _state_event(
        connection: sqlite3.Connection,
        *,
        aggregate_type: str,
        aggregate_id: str,
        from_status: str | None,
        to_status: str,
        event_type: str,
        payload: Mapping[str, Any],
        publish: bool = True,
        created_at: str | None = None,
    ) -> None:
        timestamp = _iso() if created_at is None else created_at
        event_id = f"event:{uuid.uuid4()}"
        payload_json = canonical_json(dict(payload))
        connection.execute(
            """
            INSERT INTO state_events(
                event_id, aggregate_type, aggregate_id, from_status, to_status,
                event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                aggregate_type,
                aggregate_id,
                from_status,
                to_status,
                event_type,
                payload_json,
                timestamp,
            ),
        )
        if publish:
            connection.execute(
                """
                INSERT INTO outbox_events(
                    event_id, event_type, aggregate_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, event_type, aggregate_id, payload_json, timestamp),
            )

    @classmethod
    def _transition_evaluation(
        cls,
        connection: sqlite3.Connection,
        *,
        evaluation_id: str,
        expected: Sequence[str],
        target: str,
        event_type: str,
        payload: Mapping[str, Any],
        observation_id: str | None = None,
        publish: bool = True,
        created_at: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT status FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
        current = str(row["status"])
        if current not in expected:
            allowed = ", ".join(expected)
            raise RepositoryError(
                f"Evaluation {evaluation_id} is {current}; expected one of: {allowed}"
            )
        timestamp = _iso() if created_at is None else created_at
        connection.execute(
            """
            UPDATE evaluations
            SET status = ?, observation_id = COALESCE(?, observation_id), updated_at = ?
            WHERE evaluation_id = ? AND status = ?
            """,
            (target, observation_id, timestamp, evaluation_id, current),
        )
        cls._state_event(
            connection,
            aggregate_type="evaluation",
            aggregate_id=evaluation_id,
            from_status=current,
            to_status=target,
            event_type=event_type,
            payload=payload,
            publish=publish,
            created_at=timestamp,
        )

    @classmethod
    def _transition_attempt(
        cls,
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        expected: Sequence[str],
        target: str,
        event_type: str,
        payload: Mapping[str, Any],
        publish: bool = True,
        created_at: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT status FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Attempt: {attempt_id}")
        current = str(row["status"])
        if current not in expected:
            allowed = ", ".join(expected)
            raise RepositoryError(
                f"Attempt {attempt_id} is {current}; expected one of: {allowed}"
            )
        timestamp = _iso() if created_at is None else created_at
        connection.execute(
            "UPDATE attempts SET status = ?, updated_at = ? WHERE attempt_id = ? AND status = ?",
            (target, timestamp, attempt_id, current),
        )
        cls._state_event(
            connection,
            aggregate_type="attempt",
            aggregate_id=attempt_id,
            from_status=current,
            to_status=target,
            event_type=event_type,
            payload=payload,
            publish=publish,
            created_at=timestamp,
        )

    def register_schema_document(
        self, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Idempotently register one canonical ParameterSchema document."""
        from control_plane.evaluation.parameter_schema import (
            compute_schema_revision,
            validate_parameter_schema,
        )

        canonical = validate_parameter_schema(document)
        revision = compute_schema_revision(canonical)
        kind = str(canonical.get("kind", "parameter-schema"))
        raw_json = canonical_json(canonical)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT revision, kind, canonical_json, registered_at FROM schema_documents WHERE revision = ?",
                (revision,),
            ).fetchone()
            if row is not None:
                schema_obj = json.loads(str(row["canonical_json"]))
                extracts = schema_obj.get("extracts") if isinstance(schema_obj, dict) else None
                extract_names = (
                    [str(e["name"]) for e in extracts if isinstance(e, dict) and "name" in e]
                    if isinstance(extracts, list)
                    else []
                )
                return {
                    "revision": str(row["revision"]),
                    "kind": str(row["kind"]),
                    "canonical_json": str(row["canonical_json"]),
                    "registered_at": str(row["registered_at"]),
                    "schema": schema_obj,
                    "extract_names": extract_names,
                }
            now = _iso()
            connection.execute(
                """
                INSERT INTO schema_documents(revision, kind, canonical_json, registered_at)
                VALUES (?, ?, ?, ?)
                """,
                (revision, kind, raw_json, now),
            )
            extracts = canonical.get("extracts")
            extract_names = (
                [str(e["name"]) for e in extracts if isinstance(e, dict) and "name" in e]
                if isinstance(extracts, list)
                else []
            )
            return {
                "revision": revision,
                "kind": kind,
                "canonical_json": raw_json,
                "registered_at": now,
                "schema": canonical,
                "extract_names": extract_names,
            }

    def get_schema_document(self, revision: str) -> dict[str, Any]:
        """Fetch one ParameterSchema document by its stable revision hash."""
        rev = str(revision).strip().lower()
        if not _SHA256_REVISION.fullmatch(rev):
            raise RepositoryError(f"unknown Schema: {revision}")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT revision, kind, canonical_json, registered_at FROM schema_documents WHERE revision = ?",
                (rev,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Schema: {revision}")
            schema_obj = json.loads(str(row["canonical_json"]))
            extracts = schema_obj.get("extracts") if isinstance(schema_obj, dict) else None
            extract_names = (
                [str(e["name"]) for e in extracts if isinstance(e, dict) and "name" in e]
                if isinstance(extracts, list)
                else []
            )
            return {
                "revision": str(row["revision"]),
                "kind": str(row["kind"]),
                "canonical_json": str(row["canonical_json"]),
                "registered_at": str(row["registered_at"]),
                "schema": schema_obj,
                "extract_names": extract_names,
            }

    def list_schema_documents(self) -> list[dict[str, Any]]:
        """List all registered ParameterSchema documents."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT revision, kind, canonical_json, registered_at FROM schema_documents ORDER BY registered_at DESC"
            ).fetchall()
            results = []
            for r in rows:
                schema_obj = json.loads(str(r["canonical_json"]))
                extracts = schema_obj.get("extracts") if isinstance(schema_obj, dict) else None
                extract_names = (
                    [str(e["name"]) for e in extracts if isinstance(e, dict) and "name" in e]
                    if isinstance(extracts, list)
                    else []
                )
                params = schema_obj.get("parameters") if isinstance(schema_obj, dict) else None
                parameter_count = len(params) if isinstance(params, list) else 0
                results.append(
                    {
                        "revision": str(r["revision"]),
                        "kind": str(r["kind"]),
                        "registered_at": str(r["registered_at"]),
                        "extract_names": extract_names,
                        "parameter_count": parameter_count,
                    }
                )
            return results
    def list_problems(self) -> list[dict[str, Any]]:
        """List all registered ProblemDefinition records."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT definition_json FROM problem_definitions ORDER BY created_at, problem_id"
            ).fetchall()
            return [json.loads(row["definition_json"]) for row in rows]

    def register_problem(self, definition: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_problem_definition(definition)
        definition_json = canonical_json(normalized)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT definition_json FROM problem_definitions
                WHERE problem_id = ? AND revision = ?
                """,
                (normalized["problem_id"], normalized["revision"]),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO problem_definitions(
                        problem_id, revision, definition_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        normalized["problem_id"],
                        normalized["revision"],
                        definition_json,
                        _iso(),
                    ),
                )
            elif existing["definition_json"] != definition_json:
                raise RepositoryError("ProblemDefinition identity collision")
        return normalized

    @staticmethod
    def _normalize_study(
        *,
        study_id: Any,
        problem_id: Any,
        problem_revision: Any,
        metadata: Any,
        algorithm_run_id: Any,
        artifact_refs: Any,
        automation_profile: Any = "assisted",
    ) -> dict[str, Any]:
        normalized_metadata = {} if metadata is None else json.loads(canonical_json(metadata))
        if not isinstance(normalized_metadata, Mapping):
            raise ContractError("metadata must be an object or null")
        revision = str(problem_revision).strip().lower()
        if not _SHA256_REVISION.fullmatch(revision):
            raise ContractError(
                "problem_revision must be sha256:<64 lowercase hex characters>"
            )
        return {
            "study_id": normalize_token(study_id, "study_id"),
            "problem_id": normalize_token(problem_id, "problem_id"),
            "problem_revision": revision,
            "metadata": dict(normalized_metadata),
            "algorithm_run_id": (
                None
                if algorithm_run_id is None
                else normalize_token(algorithm_run_id, "algorithm_run_id")
            ),
            "artifact_refs": _normalize_study_artifact_refs(artifact_refs),
            "automation_profile": (
                str(automation_profile).strip().lower()
                if isinstance(automation_profile, str)
                and str(automation_profile).strip().lower() in {"autonomous", "assisted", "manual"}
                else (_ for _ in ()).throw(
                    ContractError("automation_profile must be autonomous, assisted, or manual")
                )
            ),
        }

    @staticmethod
    def _study_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "study_id": str(row["study_id"]),
            "problem_id": str(row["problem_id"]),
            "problem_revision": str(row["problem_revision"]),
            "created_at": str(row["created_at"]),
            "metadata": json.loads(row["metadata_json"]),
            "algorithm_run_id": row["algorithm_run_id"],
            "artifact_refs": json.loads(row["artifact_refs_json"]),
            "automation_profile": str(row["automation_profile"] or "assisted"),
        }

    def create_study(
        self,
        *,
        study_id: str,
        problem_id: str,
        problem_revision: str,
        metadata: Mapping[str, Any] | None = None,
        algorithm_run_id: str | None = None,
        artifact_refs: Sequence[Mapping[str, Any]] = (),
        automation_profile: str = "assisted",
    ) -> dict[str, Any]:
        normalized = self._normalize_study(
            study_id=study_id,
            problem_id=problem_id,
            problem_revision=problem_revision,
            metadata=metadata,
            algorithm_run_id=algorithm_run_id,
            artifact_refs=artifact_refs,
            automation_profile=automation_profile,
        )
        metadata_json = canonical_json(normalized["metadata"])
        artifact_refs_json = canonical_json(normalized["artifact_refs"])
        with self._transaction() as connection:
            problem = connection.execute(
                """SELECT 1 FROM problem_definitions
                   WHERE problem_id = ? AND revision = ?""",
                (normalized["problem_id"], normalized["problem_revision"]),
            ).fetchone()
            if problem is None:
                raise RepositoryError(
                    "Study references an unregistered ProblemDefinition"
                )
            existing = connection.execute(
                "SELECT * FROM studies WHERE study_id = ?",
                (normalized["study_id"],),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["problem_id"] == normalized["problem_id"]
                    and existing["problem_revision"] == normalized["problem_revision"]
                    and existing["metadata_json"] == metadata_json
                    and existing["algorithm_run_id"] == normalized["algorithm_run_id"]
                    and existing["artifact_refs_json"] == artifact_refs_json
                    and str(existing["automation_profile"] or "assisted")
                    == normalized["automation_profile"]
                )
                if not same:
                    raise RepositoryError("Study identity collision")
                return self._study_record(existing)
            created_at = _iso()
            connection.execute(
                """INSERT INTO studies(
                    study_id, problem_id, problem_revision, created_at,
                    metadata_json, algorithm_run_id, artifact_refs_json,
                    automation_profile
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    normalized["study_id"],
                    normalized["problem_id"],
                    normalized["problem_revision"],
                    created_at,
                    metadata_json,
                    normalized["algorithm_run_id"],
                    artifact_refs_json,
                    normalized["automation_profile"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM studies WHERE study_id = ?",
                (normalized["study_id"],),
            ).fetchone()
            assert row is not None
            return self._study_record(row)

    def get_study(self, study_id: str) -> dict[str, Any]:
        normalized_id = normalize_token(study_id, "study_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM studies WHERE study_id = ?", (normalized_id,)
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Study: {normalized_id}")
        return self._study_record(row)

    def _assert_study(
        self, connection: sqlite3.Connection, study_id: str
    ) -> sqlite3.Row:
        normalized_id = normalize_token(study_id, "study_id")
        row = connection.execute(
            "SELECT * FROM studies WHERE study_id = ?", (normalized_id,)
        ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Study: {normalized_id}")
        return row

    def _associate_study_evaluation(
        self, connection: sqlite3.Connection, study_id: str, evaluation_id: str
    ) -> None:
        self._assert_study(connection, study_id)
        exists = connection.execute(
            "SELECT 1 FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
        ).fetchone()
        if exists is None:
            raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
        connection.execute(
            """INSERT OR IGNORE INTO study_evaluations(study_id, evaluation_id)
               VALUES (?, ?)""",
            (study_id, evaluation_id),
        )

    @staticmethod
    def _attempt_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "evaluation_id": str(row["evaluation_id"]),
            "attempt_number": int(row["attempt_number"]),
            "status": str(row["status"]),
            "failure_class": row["failure_class"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _wait_fields(connection: sqlite3.Connection, evaluation_id: str, status: str) -> dict[str, Any]:
        row = connection.execute("SELECT 1 FROM attempts WHERE evaluation_id=? AND status='reconciling' LIMIT 1", (evaluation_id,)).fetchone()
        if row is not None:
            return {"wait_reason": "reconciling", "wait_since": None}
        if status not in {"queued", "recovering"}:
            return {"wait_reason": None, "wait_since": None}
        rows = connection.execute("SELECT status, failure_class, execution_plan_json, updated_at, created_at FROM attempts WHERE evaluation_id=? ORDER BY attempt_number DESC, created_at DESC", (evaluation_id,)).fetchall()
        if status == "recovering":
            for row in rows:
                if row["failure_class"]:
                    return {"wait_reason": str(row["failure_class"]), "wait_since": str(row["updated_at"])}
        if status == "queued":
            for row in rows:
                if row["failure_class"]:
                    return {
                        "wait_reason": "requeued-after:" + str(row["failure_class"]),
                        "wait_since": str(row["updated_at"]),
                    }
        return {"wait_reason": None, "wait_since": None}

    def get_study_status(self, study_id: str) -> dict[str, Any]:
        normalized_id = normalize_token(study_id, "study_id")
        with closing(self._connect()) as connection:
            study_row = connection.execute(
                "SELECT * FROM studies WHERE study_id = ?", (normalized_id,)
            ).fetchone()
            if study_row is None:
                raise RepositoryError(f"unknown Study: {normalized_id}")
            evaluation_rows = connection.execute(
                """SELECT e.* FROM evaluations e
                   JOIN study_evaluations se ON se.evaluation_id = e.evaluation_id
                   WHERE se.study_id = ?
                   ORDER BY e.created_at, e.evaluation_id""",
                (normalized_id,),
            ).fetchall()
            evaluations: list[dict[str, Any]] = []
            for evaluation_row in evaluation_rows:
                attempts = connection.execute(
                    """SELECT attempt_id, evaluation_id, attempt_number, status,
                              failure_class, created_at, updated_at
                       FROM attempts WHERE evaluation_id = ?
                       ORDER BY attempt_number, created_at, attempt_id""",
                    (evaluation_row["evaluation_id"],),
                ).fetchall()
                evaluation = self._evaluation_record(evaluation_row)
                evaluation.update(self._wait_fields(connection, str(evaluation_row["evaluation_id"]), str(evaluation_row["status"])))
                evaluations.append({**evaluation, "attempts": [self._attempt_summary(row) for row in attempts]})
        return {"study": self._study_record(study_row), "evaluations": evaluations}

    def list_studies(self, problem_id: str | None = None) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            if problem_id is None:
                rows = connection.execute(
                    "SELECT * FROM studies ORDER BY created_at, study_id"
                ).fetchall()
            else:
                normalized_id = normalize_token(problem_id, "problem_id")
                rows = connection.execute(
                    """SELECT * FROM studies WHERE problem_id = ?
                       ORDER BY created_at, study_id""",
                    (normalized_id,),
                ).fetchall()
        return [self._study_record(row) for row in rows]

    def list_study_overviews(self, limit: int | None = None) -> dict[str, Any]:
        """Return the repository-wide, read-only study status rollup."""
        if limit is not None and limit <= 0:
            raise ContractError("limit must be a positive integer")
        active_statuses = {
            "requested", "deduplicating", "queued", "running", "recovering",
            "qualifying",
        }
        with closing(self._connect()) as connection:
            study_rows = connection.execute(
                "SELECT * FROM studies ORDER BY study_id"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for study in study_rows:
                evaluation_rows = connection.execute(
                    """SELECT e.evaluation_id, e.status, e.updated_at
                       FROM evaluations e
                       JOIN study_evaluations se ON se.evaluation_id=e.evaluation_id
                       WHERE se.study_id=?""",
                    (study["study_id"],),
                ).fetchall()
                status_counts: dict[str, int] = {}
                waiting: list[tuple[str, str | None, str]] = []
                last_activity: str | None = None
                for evaluation in evaluation_rows:
                    status = str(evaluation["status"])
                    status_counts[status] = status_counts.get(status, 0) + 1
                    updated = str(evaluation["updated_at"])
                    if last_activity is None or updated > last_activity:
                        last_activity = updated
                    wait = self._wait_fields(
                        connection, str(evaluation["evaluation_id"]), status
                    )
                    if wait["wait_reason"] is not None:
                        waiting.append((
                            str(evaluation["evaluation_id"]),
                            wait["wait_since"],
                            str(wait["wait_reason"]),
                        ))
                active_count = sum(
                    count for status, count in status_counts.items()
                    if status in active_statuses
                )
                oldest_wait = None
                if waiting:
                    with_since = [item for item in waiting if item[1] is not None]
                    selected = min(with_since, key=lambda item: item[1]) if with_since else min(waiting, key=lambda item: item[0])
                    oldest_wait = {
                        "evaluation_id": selected[0],
                        "wait_reason": selected[2],
                        "wait_since": selected[1],
                    }
                result.append({
                    "study_id": str(study["study_id"]),
                    "problem_id": str(study["problem_id"]),
                    "problem_revision": study["problem_revision"],
                    "created_at": str(study["created_at"]),
                    "algorithm_run_id": study["algorithm_run_id"],
                    "automation_profile": study["automation_profile"],
                    "evaluation_count": len(evaluation_rows),
                    "status_counts": {key: value for key, value in status_counts.items() if value > 0},
                    "active_count": active_count,
                    "waiting_count": len(waiting),
                    "oldest_wait": oldest_wait,
                    "last_activity_at": last_activity,
                })
        result.sort(key=lambda item: (
            item["active_count"] > 0,
            item["last_activity_at"] or item["created_at"],
        ), reverse=True)
        if limit is not None:
            result = result[:limit]
        return {"study_count": len(study_rows), "studies": result}

    def list_problem_evaluations(
        self, problem_id: str, problem_revision: str | None = None
    ) -> list[dict[str, Any]]:
        normalized_id = normalize_token(problem_id, "problem_id")
        revision = None
        if problem_revision is not None:
            revision = str(problem_revision).strip().lower()
            if not _SHA256_REVISION.fullmatch(revision):
                raise ContractError(
                    "problem_revision must be sha256:<64 lowercase hex characters>"
                )
        with closing(self._connect()) as connection:
            if revision is None:
                rows = connection.execute(
                    """SELECT e.* FROM evaluations e
                       JOIN candidates c ON c.candidate_id = e.candidate_id
                       WHERE c.problem_id = ?
                       ORDER BY e.created_at, e.evaluation_id""",
                    (normalized_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT e.* FROM evaluations e
                       JOIN candidates c ON c.candidate_id = e.candidate_id
                       WHERE c.problem_id = ? AND c.problem_revision = ?
                       ORDER BY e.created_at, e.evaluation_id""",
                    (normalized_id, revision),
                ).fetchall()
            evaluations = []
            for row in rows:
                rec = self._evaluation_record(row)
                rec.update(self._wait_fields(connection, str(row["evaluation_id"]), str(row["status"])))
                evaluations.append(rec)
            return evaluations

    def list_evaluations(
        self, problem_id: str | None = None, problem_revision: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the immutable Evaluation projection for one Problem lineage, or all Evaluations."""
        with closing(self._connect()) as connection:
            if problem_id is None:
                rows = connection.execute(
                    "SELECT * FROM evaluations ORDER BY created_at, evaluation_id"
                ).fetchall()
            elif problem_revision is None:
                normalized_id = normalize_token(problem_id, "problem_id")
                rows = connection.execute(
                    """SELECT e.* FROM evaluations e
                       JOIN candidates c ON c.candidate_id = e.candidate_id
                       WHERE c.problem_id = ?
                       ORDER BY e.created_at, e.evaluation_id""",
                    (normalized_id,),
                ).fetchall()
            else:
                normalized_id = normalize_token(problem_id, "problem_id")
                revision = str(problem_revision).strip().lower()
                if not _SHA256_REVISION.fullmatch(revision):
                    raise ContractError(
                        "problem_revision must be sha256:<64 lowercase hex characters>"
                    )
                rows = connection.execute(
                    """SELECT e.* FROM evaluations e
                       JOIN candidates c ON c.candidate_id = e.candidate_id
                       WHERE c.problem_id = ? AND c.problem_revision = ?
                       ORDER BY e.created_at, e.evaluation_id""",
                    (normalized_id, revision),
                ).fetchall()
            return [self._evaluation_record(row) for row in rows]

    def associate_study_evaluation(self, study_id: str, evaluation_id: str) -> None:
        normalized_study_id = normalize_token(study_id, "study_id")
        with self._transaction() as connection:
            self._associate_study_evaluation(
                connection, normalized_study_id, evaluation_id
            )

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection, candidate: Mapping[str, Any]
    ) -> None:
        registered = connection.execute(
            """
            SELECT 1 FROM problem_definitions
            WHERE problem_id = ? AND revision = ?
            """,
            (candidate["problem_id"], candidate["problem_revision"]),
        ).fetchone()
        if registered is None:
            raise RepositoryError("Candidate references an unregistered ProblemDefinition")
        candidate_json = canonical_json(candidate)
        existing = connection.execute(
            "SELECT candidate_json FROM candidates WHERE candidate_id = ?",
            (candidate["candidate_id"],),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO candidates(
                    candidate_id, problem_id, problem_revision, candidate_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate["candidate_id"],
                    candidate["problem_id"],
                    candidate["problem_revision"],
                    candidate_json,
                    _iso(),
                ),
            )
        elif existing["candidate_json"] != candidate_json:
            raise RepositoryError("Candidate identity collision")

    def submit_evaluation(
        self,
        candidate: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        study_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_candidate = validate_candidate(candidate)
        normalized_request = validate_evaluation_request(request)
        normalized_study_id = (
            None if study_id is None else normalize_token(study_id, "study_id")
        )
        if normalized_request["candidate_id"] != normalized_candidate["candidate_id"]:
            raise RepositoryError("EvaluationRequest references a different Candidate")

        with self._transaction() as connection:
            if normalized_study_id is not None:
                self._assert_study(connection, normalized_study_id)
            self._insert_candidate(connection, normalized_candidate)
            existing = connection.execute(
                "SELECT * FROM evaluations WHERE idempotency_key = ?",
                (normalized_request["idempotency_key"],),
            ).fetchone()
            if existing is not None:
                if normalized_study_id is not None:
                    self._associate_study_evaluation(
                        connection, normalized_study_id, str(existing["evaluation_id"])
                    )
                return self._evaluation_record(existing)

            reusable = None
            if normalized_request["independence_requirement"] == "normal":
                reusable = connection.execute(
                    """
                    SELECT o.observation_id
                    FROM observations o
                    JOIN evaluations source ON source.evaluation_id = o.evaluation_id
                    WHERE o.candidate_id = ?
                      AND source.fidelity = ?
                      AND source.requested_outputs_json = ?
                      AND source.evidence_profile = ?
                    ORDER BY o.created_at, o.observation_id
                    LIMIT 1
                    """,
                    (
                        normalized_request["candidate_id"],
                        normalized_request["fidelity"],
                        canonical_json(normalized_request["requested_outputs"]),
                        normalized_request["evidence_profile"],
                    ),
                ).fetchone()

            now = _iso()
            evaluation_id = normalized_request["evaluation_id"]
            connection.execute(
                """
                INSERT INTO evaluations(
                    evaluation_id, idempotency_key, candidate_id, fidelity,
                    requested_outputs_json, evidence_profile,
                    independence_requirement, priority, request_json, status,
                    observation_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', NULL, ?, ?)
                """,
                (
                    evaluation_id,
                    normalized_request["idempotency_key"],
                    normalized_request["candidate_id"],
                    normalized_request["fidelity"],
                    canonical_json(normalized_request["requested_outputs"]),
                    normalized_request["evidence_profile"],
                    normalized_request["independence_requirement"],
                    normalized_request["priority"],
                    canonical_json(normalized_request),
                    now,
                    now,
                ),
            )
            self._state_event(
                connection,
                aggregate_type="evaluation",
                aggregate_id=evaluation_id,
                from_status=None,
                to_status="requested",
                event_type="EvaluationRequested",
                payload={"candidate_id": normalized_request["candidate_id"]},
                created_at=now,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=evaluation_id,
                expected=("requested",),
                target="deduplicating",
                event_type="EvaluationDeduplicationStarted",
                payload={},
                publish=False,
            )
            if reusable is None:
                self._transition_evaluation(
                    connection,
                    evaluation_id=evaluation_id,
                    expected=("deduplicating",),
                    target="queued",
                    event_type="EvaluationQueued",
                    payload={},
                    publish=False,
                )
            else:
                observation_id = str(reusable["observation_id"])
                self._transition_evaluation(
                    connection,
                    evaluation_id=evaluation_id,
                    expected=("deduplicating",),
                    target="qualified",
                    event_type="EvaluationDeduplicated",
                    payload={"observation_id": observation_id},
                    observation_id=observation_id,
                )
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
            assert row is not None
            if normalized_study_id is not None:
                self._associate_study_evaluation(
                    connection, normalized_study_id, evaluation_id
                )
            return self._evaluation_record(row)

    def schedule_attempt(
        self,
        *,
        evaluation_id: str,
        simulation_adapter: str,
        numerical_profile: str,
        checkpoint_parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            attempt = self._schedule_attempt_in_transaction(
                connection,
                evaluation_id=evaluation_id,
                simulation_adapter=simulation_adapter,
                numerical_profile=numerical_profile,
                checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
                attempt_id=attempt_id,
            )
            if attempt["execution_preparation_id"] is not None:
                raise RepositoryError(
                    "legacy scheduling cannot adopt a prepared Attempt"
                )
            return attempt

    def cancel_planned_attempt(
        self, attempt_id: str, reason: str
    ) -> dict[str, Any]:
        """Cancel one unstarted Attempt without inventing execution history."""

        if not isinstance(reason, str) or not reason.strip():
            raise RepositoryError("cancellation reason is required")
        explanation = reason.strip()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, e.status AS evaluation_status,
                       (SELECT COUNT(*) FROM attempts sibling
                        WHERE sibling.evaluation_id = a.evaluation_id)
                           AS evaluation_attempt_count
                FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")

            status = str(row["status"])
            if status not in {"planned", "cancelled"}:
                self._attempt_record(row)
                raise RepositoryError("only a planned Attempt can be cancelled")
            try:
                artifact_ids = json.loads(row["artifact_ids_json"])
            except json.JSONDecodeError as exc:
                raise RepositoryError(
                    "Attempt artifacts contain invalid persisted JSON"
                ) from exc
            if (
                row["failure_class"] is not None
                or row["lease_owner"] is not None
                or row["lease_expires_at"] is not None
                or row["session_ref"] is not None
                or row["execution_preparation_id"] is not None
                or row["execution_preparation_json"] is not None
                or row["allocation_json"] is not None
                or row["selected_execution_option_id"] is not None
                or artifact_ids
            ):
                raise RepositoryError(
                    "Attempt with execution facts cannot be cancelled"
                )
            attempt = self._attempt_record(row)
            if attempt["status"] == "cancelled":
                if (
                    row["evaluation_status"] != "cancelled"
                    or int(row["evaluation_attempt_count"]) != 1
                ):
                    raise RepositoryError(
                        "cancelled Attempt and Evaluation state is inconsistent"
                    )
                event = connection.execute(
                    """
                    SELECT payload_json FROM state_events
                    WHERE aggregate_type = 'attempt' AND aggregate_id = ?
                      AND event_type = 'AttemptCancelled'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (attempt_id,),
                ).fetchone()
                expected_payload = {
                    "evaluation_id": attempt["evaluation_id"],
                    "reason": explanation,
                }
                if event is None or json.loads(event["payload_json"]) != expected_payload:
                    raise RepositoryError(
                        "cancelled Attempt was replayed with a different reason"
                    )
                return attempt

            if row["evaluation_status"] != "queued":
                raise RepositoryError(
                    "planned Attempt can be cancelled only for a queued Evaluation"
                )
            if int(row["evaluation_attempt_count"]) != 1:
                raise RepositoryError(
                    "planned Attempt can be cancelled only when it is the Evaluation's sole Attempt"
                )

            timestamp = _iso()
            payload = {
                "evaluation_id": attempt["evaluation_id"],
                "reason": explanation,
            }
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("planned",),
                target="cancelled",
                event_type="AttemptCancelled",
                payload=payload,
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=attempt["evaluation_id"],
                expected=("queued",),
                target="cancelled",
                event_type="EvaluationCancelled",
                payload={"attempt_id": attempt_id, "reason": explanation},
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def _schedule_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        evaluation_id: str,
        simulation_adapter: str,
        numerical_profile: str,
        checkpoint_parent_attempt_id: str | None,
        attempt_id: str | None,
    ) -> dict[str, Any]:
        if attempt_id is not None:
            existing = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                record = self._attempt_record(existing)
                expected = {
                    "evaluation_id": evaluation_id,
                    "simulation_adapter": simulation_adapter,
                    "numerical_profile": numerical_profile,
                    "checkpoint_parent_attempt_id": checkpoint_parent_attempt_id,
                }
                if any(record[key] != value for key, value in expected.items()):
                    raise RepositoryError(
                        "Attempt identity was replayed with different inputs"
                    )
                return record
        evaluation = connection.execute(
            "SELECT status FROM evaluations WHERE evaluation_id = ?",
            (evaluation_id,),
        ).fetchone()
        if evaluation is None:
            raise RepositoryError("Attempt references an unknown Evaluation")
        if evaluation["status"] != "queued":
            raise RepositoryError("Attempt can be scheduled only for a queued Evaluation")
        active = connection.execute(
            f"""
            SELECT * FROM attempts
            WHERE evaluation_id = ?
              AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
            """,
            (evaluation_id,),
        ).fetchone()
        if active is not None:
            record = self._attempt_record(active)
            expected = {
                "simulation_adapter": simulation_adapter,
                "numerical_profile": numerical_profile,
                "checkpoint_parent_attempt_id": checkpoint_parent_attempt_id,
            }
            if all(record[key] == value for key, value in expected.items()):
                return record
            raise RepositoryError("Evaluation already has a different active Attempt")
        expected_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()[0]
        )
        normalized = make_attempt(
            evaluation_id=evaluation_id,
            attempt_number=expected_number,
            simulation_adapter=simulation_adapter,
            numerical_profile=numerical_profile,
            checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
            attempt_id=attempt_id,
        )
        parent = normalized["checkpoint_parent_attempt_id"]
        if parent is not None:
            parent_row = connection.execute(
                "SELECT evaluation_id FROM attempts WHERE attempt_id = ?", (parent,)
            ).fetchone()
            if parent_row is None or parent_row["evaluation_id"] != normalized["evaluation_id"]:
                raise RepositoryError("checkpoint parent must belong to the same Evaluation")
        now = _iso()
        connection.execute(
            """
            INSERT INTO attempts(
                attempt_id, evaluation_id, attempt_number, simulation_adapter,
                numerical_profile, checkpoint_parent_attempt_id, status,
                failure_class, artifact_ids_json, lease_owner, lease_expires_at,
                session_ref, execution_plan_id, execution_plan_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'planned', NULL, '[]', NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (
                normalized["attempt_id"],
                normalized["evaluation_id"],
                normalized["attempt_number"],
                normalized["simulation_adapter"],
                normalized["numerical_profile"],
                parent,
                now,
                now,
            ),
        )
        self._state_event(
            connection,
            aggregate_type="attempt",
            aggregate_id=normalized["attempt_id"],
            from_status=None,
            to_status="planned",
            event_type="AttemptScheduled",
            payload={"evaluation_id": normalized["evaluation_id"]},
            created_at=now,
        )
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (normalized["attempt_id"],)
        ).fetchone()
        assert row is not None
        return self._attempt_record(row)

    def bind_attempt_plan(
        self,
        attempt_id: str,
        execution_plan_id: str,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan_id = normalize_token(execution_plan_id, "execution_plan_id")
        plan_json = canonical_json(plan)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["execution_preparation_json"] is not None:
                raise RepositoryError(
                    "legacy SessionPlan cannot be bound to a prepared Attempt"
                )
            if row["execution_plan_id"] is not None or row["execution_plan_json"] is not None:
                if (
                    row["execution_plan_id"] == plan_id
                    and row["execution_plan_json"] == plan_json
                ):
                    return self._attempt_record(row)
                raise RepositoryError("Attempt execution plan cannot be overwritten")
            if row["status"] != "planned":
                raise RepositoryError("session plan must be bound before an Attempt is leased")
            timestamp = _iso()
            connection.execute(
                """
                UPDATE attempts
                SET execution_plan_id = ?, execution_plan_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (plan_id, plan_json, timestamp, attempt_id),
            )
            self._state_event(
                connection,
                aggregate_type="attempt",
                aggregate_id=attempt_id,
                from_status="planned",
                to_status="planned",
                event_type="AttemptPlanBound",
                payload={"execution_plan_id": plan_id},
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def create_prepared_attempt(
        self,
        preparation: Mapping[str, Any],
        *,
        checkpoint_parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
        governance_provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or replay one fully prepared Attempt in a single transaction."""

        try:
            normalized = validate_execution_preparation(preparation)
        except ExecutionOptionError as exc:
            raise RepositoryError("execution preparation is invalid") from exc
        identity = normalized["preparation_id"]
        encoded = canonical_json(normalized)
        provenance = _preparation_provenance(governance_provenance)
        with self._transaction() as connection:
            return self._create_prepared_attempt_in_transaction(
                connection,
                normalized=normalized,
                encoded=encoded,
                checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
                attempt_id=attempt_id,
                governance_provenance=provenance,
            )

    def _create_prepared_attempt_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        normalized: Mapping[str, Any],
        encoded: str,
        checkpoint_parent_attempt_id: str | None,
        attempt_id: str | None,
        governance_provenance: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        identity = str(normalized["preparation_id"])
        existing = connection.execute(
            f"""
            SELECT a.*, e.candidate_id AS evaluation_candidate_id
            FROM attempts a
            JOIN evaluations e ON e.evaluation_id = a.evaluation_id
            WHERE a.execution_preparation_id = ?
              AND a.status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
            """,
            (identity,),
        ).fetchone()
        if existing is not None:
            if (
                existing["execution_preparation_json"] != encoded
                or existing["checkpoint_parent_attempt_id"]
                != checkpoint_parent_attempt_id
                or (attempt_id is not None and existing["attempt_id"] != attempt_id)
            ):
                raise RepositoryError(
                    "execution preparation identity was replayed with different inputs"
                )
            return self.get_attempt(existing["attempt_id"], connection=connection)

        evaluation = connection.execute(
            "SELECT candidate_id FROM evaluations WHERE evaluation_id = ?",
            (normalized["evaluation_id"],),
        ).fetchone()
        if evaluation is None:
            raise RepositoryError("execution preparation references an unknown Evaluation")
        if evaluation["candidate_id"] != normalized["candidate_id"]:
            raise RepositoryError(
                "execution preparation references a different Candidate"
            )
        active = connection.execute(
            f"""
            SELECT attempt_id, execution_preparation_id FROM attempts
            WHERE evaluation_id = ?
              AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
            """,
            (normalized["evaluation_id"],),
        ).fetchone()
        if active is not None:
            if active["execution_preparation_id"] is None:
                raise RepositoryError(
                    "prepared execution cannot adopt an Attempt from the legacy path"
                )
            raise RepositoryError(
                "Evaluation already has a different prepared Attempt"
            )

        attempt = self._schedule_attempt_in_transaction(
            connection,
            evaluation_id=normalized["evaluation_id"],
            simulation_adapter=normalized["simulation_proxy"],
            numerical_profile=normalized["numerical_profile"],
            checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
            attempt_id=attempt_id,
        )
        return self._bind_execution_preparation_in_transaction(
            connection,
            attempt["attempt_id"],
            identity,
            normalized,
            encoded,
            governance_provenance,
        )

    def _bind_execution_preparation_in_transaction(
        self,
        connection: sqlite3.Connection,
        attempt_id: str,
        identity: str,
        normalized: Mapping[str, Any],
        encoded: str,
        governance_provenance: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT a.*, e.candidate_id AS evaluation_candidate_id
            FROM attempts a
            JOIN evaluations e ON e.evaluation_id = a.evaluation_id
            WHERE a.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Attempt: {attempt_id}")
        if row["execution_preparation_id"] is not None:
            if (
                row["execution_preparation_id"] == identity
                and row["execution_preparation_json"] == encoded
            ):
                return self.get_attempt(attempt_id, connection=connection)
            raise RepositoryError("Attempt execution preparation cannot be overwritten")
        if (
            row["status"] != "planned"
            or row["execution_plan_json"] is not None
            or row["allocation_json"] is not None
            or row["selected_execution_option_id"] is not None
        ):
            raise RepositoryError("execution preparation must be bound before scheduling")
        if (
            normalized["evaluation_id"] != row["evaluation_id"]
            or normalized["candidate_id"] != row["evaluation_candidate_id"]
            or normalized["simulation_proxy"] != row["simulation_adapter"]
            or normalized["numerical_profile"] != row["numerical_profile"]
        ):
            raise RepositoryError(
                "execution preparation does not match the scheduled Attempt"
            )
        collision = connection.execute(
            f"""
            SELECT attempt_id FROM attempts
            WHERE execution_preparation_id = ? AND attempt_id <> ?
              AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
            """,
            (identity, attempt_id),
        ).fetchone()
        if collision is not None:
            raise RepositoryError(
                "execution preparation is already bound to a different Attempt"
            )
        timestamp = _iso()
        connection.execute(
            """
            UPDATE attempts
            SET execution_preparation_id = ?,
                execution_preparation_json = ?, updated_at = ?
            WHERE attempt_id = ? AND status = 'planned'
              AND execution_preparation_json IS NULL
            """,
            (identity, encoded, timestamp, attempt_id),
        )
        payload: dict[str, Any] = {"preparation_id": identity}
        if governance_provenance is not None:
            payload["governance"] = dict(governance_provenance)
        self._state_event(
            connection,
            aggregate_type="attempt",
            aggregate_id=attempt_id,
            from_status="planned",
            to_status="planned",
            event_type="AttemptExecutionPrepared",
            payload=payload,
            created_at=timestamp,
        )
        return self.get_attempt(attempt_id, connection=connection)

    def list_queued_evaluations(
        self,
        limit: int | None = None,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """List queue members that do not already occupy a preparation slot."""

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise RepositoryError("queue limit must be a positive integer or None")
        timestamp = _iso(_utc_now() if now is None else now)
        statement = f"""
            SELECT e.* FROM evaluations e
            LEFT JOIN preparation_claims pc ON pc.evaluation_id = e.evaluation_id
            WHERE e.status = 'queued'
              AND (pc.claim_id IS NULL OR pc.expires_at <= ?)
              AND NOT EXISTS (
                  SELECT 1 FROM attempts a
                  WHERE a.evaluation_id = e.evaluation_id
                    AND a.status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
              )
            ORDER BY e.created_at, e.evaluation_id
        """
        parameters: tuple[Any, ...] = (timestamp,)
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (*parameters, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [
            {**self._evaluation_record(row), "queued_since": str(row["created_at"])}
            for row in rows
        ]

    @classmethod
    def _expire_preparation_claims_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        timestamp: str,
    ) -> None:
        stale = connection.execute(
            f"""
            SELECT pc.*, e.status AS evaluation_status
            FROM preparation_claims pc
            JOIN evaluations e ON e.evaluation_id = pc.evaluation_id
            WHERE pc.expires_at <= ?
               OR e.status <> 'queued'
               OR EXISTS (
                   SELECT 1 FROM attempts a
                   WHERE a.evaluation_id = pc.evaluation_id
                     AND a.status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
               )
            ORDER BY pc.created_at, pc.claim_id
            """,
            (timestamp,),
        ).fetchall()
        for row in stale:
            if row["expires_at"] <= timestamp:
                event_type = "EvaluationPreparationClaimExpired"
                reason = "claim-lease-expired"
            elif row["evaluation_status"] != "queued":
                event_type = "EvaluationPreparationClaimReleased"
                reason = "evaluation-not-queued"
            else:
                event_type = "EvaluationPreparationClaimReleased"
                reason = "active-attempt-exists"
            connection.execute(
                "DELETE FROM preparation_claims WHERE claim_id = ?",
                (row["claim_id"],),
            )
            cls._state_event(
                connection,
                aggregate_type="evaluation",
                aggregate_id=str(row["evaluation_id"]),
                from_status=str(row["evaluation_status"]),
                to_status=str(row["evaluation_status"]),
                event_type=event_type,
                payload={
                    "claim_id": str(row["claim_id"]),
                    "controller_id": str(row["controller_id"]),
                    "reason": reason,
                },
                publish=False,
                created_at=timestamp,
            )

    @staticmethod
    def _preparation_window_occupancy_in_transaction(
        connection: sqlite3.Connection,
    ) -> int:
        prepared_attempts = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM attempts
                WHERE execution_preparation_id IS NOT NULL
                  AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
                """
            ).fetchone()[0]
        )
        active_claims = int(
            connection.execute("SELECT COUNT(*) FROM preparation_claims").fetchone()[0]
        )
        return prepared_attempts + active_claims

    def preparation_window_occupancy(
        self, *, now: datetime | None = None
    ) -> int:
        """Return valid claims plus nonterminal prepared Attempts."""

        timestamp = _iso(_utc_now() if now is None else now)
        with self._transaction() as connection:
            self._expire_preparation_claims_in_transaction(
                connection, timestamp=timestamp
            )
            return self._preparation_window_occupancy_in_transaction(connection)

    def claim_preparation_slots(
        self,
        ordered_evaluation_ids: Sequence[str],
        *,
        controller_id: str,
        window_limit: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically reserve at most the unoccupied rolling-window slots."""

        if isinstance(ordered_evaluation_ids, (str, bytes, bytearray)) or not isinstance(
            ordered_evaluation_ids, Sequence
        ):
            raise RepositoryError("ordered evaluation IDs must be an array")
        evaluation_ids = [
            normalize_token(value, "evaluation_id") for value in ordered_evaluation_ids
        ]
        if len(evaluation_ids) != len(set(evaluation_ids)):
            raise RepositoryError("ordered evaluation IDs must be unique")
        controller = normalize_token(controller_id, "controller_id")
        if (
            isinstance(window_limit, bool)
            or not isinstance(window_limit, int)
            or window_limit < 0
        ):
            raise RepositoryError("window_limit must be a nonnegative integer")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        current = _utc_now() if now is None else now
        timestamp = _iso(current)
        expires_at = _iso(current + timedelta(seconds=lease_seconds))

        with self._transaction() as connection:
            self._expire_preparation_claims_in_transaction(
                connection, timestamp=timestamp
            )
            occupied = self._preparation_window_occupancy_in_transaction(connection)
            available = max(0, window_limit - occupied)
            claimed: list[dict[str, Any]] = []

            for evaluation_id in evaluation_ids:
                evaluation = connection.execute(
                    "SELECT * FROM evaluations WHERE evaluation_id = ?",
                    (evaluation_id,),
                ).fetchone()
                if evaluation is None:
                    raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
                existing = connection.execute(
                    "SELECT * FROM preparation_claims WHERE evaluation_id = ?",
                    (evaluation_id,),
                ).fetchone()
                if existing is not None:
                    if existing["controller_id"] == controller:
                        claimed.append(
                            {
                                **self._evaluation_record(evaluation),
                                "queued_since": str(evaluation["created_at"]),
                                "claim_id": str(existing["claim_id"]),
                                "controller_id": controller,
                                "claim_expires_at": str(existing["expires_at"]),
                            }
                        )
                    continue
                if available == 0 or evaluation["status"] != "queued":
                    continue
                active = connection.execute(
                    f"""
                    SELECT 1 FROM attempts
                    WHERE evaluation_id = ?
                      AND status IN ({_ACTIVE_ATTEMPT_STATES_SQL})
                    """,
                    (evaluation_id,),
                ).fetchone()
                if active is not None:
                    continue
                claim_id = f"preparation-claim:{uuid.uuid4()}"
                connection.execute(
                    """
                    INSERT INTO preparation_claims(
                        claim_id, evaluation_id, controller_id, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (claim_id, evaluation_id, controller, expires_at, timestamp),
                )
                self._state_event(
                    connection,
                    aggregate_type="evaluation",
                    aggregate_id=evaluation_id,
                    from_status="queued",
                    to_status="queued",
                    event_type="EvaluationPreparationClaimed",
                    payload={
                        "claim_id": claim_id,
                        "controller_id": controller,
                        "expires_at": expires_at,
                        "window_limit": window_limit,
                    },
                    publish=False,
                    created_at=timestamp,
                )
                claimed.append(
                    {
                        **self._evaluation_record(evaluation),
                        "queued_since": str(evaluation["created_at"]),
                        "claim_id": claim_id,
                        "controller_id": controller,
                        "claim_expires_at": expires_at,
                    }
                )
                available -= 1
            return claimed

    def commit_preparation_claim(
        self,
        claim_id: str,
        controller_id: str,
        preparation: Mapping[str, Any],
        *,
        checkpoint_parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
        governance_provenance: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically consume one claim and create its fully prepared Attempt."""

        claim_identity = normalize_token(claim_id, "claim_id")
        controller = normalize_token(controller_id, "controller_id")
        try:
            normalized = validate_execution_preparation(preparation)
        except ExecutionOptionError as exc:
            raise RepositoryError("execution preparation is invalid") from exc
        encoded = canonical_json(normalized)
        provenance = _preparation_provenance(governance_provenance)
        timestamp = _iso(_utc_now() if now is None else now)

        with self._transaction() as connection:
            claim = connection.execute(
                "SELECT * FROM preparation_claims WHERE claim_id = ?",
                (claim_identity,),
            ).fetchone()
            if claim is None:
                replay = connection.execute(
                    """
                    SELECT a.*, s.payload_json AS claim_payload_json
                    FROM attempts a
                    JOIN state_events s
                      ON s.aggregate_type = 'evaluation'
                     AND s.aggregate_id = a.evaluation_id
                     AND s.event_type = 'EvaluationPreparationClaimCommitted'
                    WHERE a.execution_preparation_id = ?
                    ORDER BY a.attempt_number DESC, s.sequence DESC
                    """,
                    (normalized["preparation_id"],),
                ).fetchall()
                for row in replay:
                    payload = json.loads(row["claim_payload_json"])
                    if (
                        payload.get("claim_id") == claim_identity
                        and payload.get("controller_id") == controller
                        and payload.get("attempt_id") == row["attempt_id"]
                        and payload.get("preparation_id")
                        == normalized["preparation_id"]
                        and row["execution_preparation_json"] == encoded
                        and row["checkpoint_parent_attempt_id"]
                        == checkpoint_parent_attempt_id
                        and (attempt_id is None or row["attempt_id"] == attempt_id)
                    ):
                        return self._attempt_record(row)
                raise RepositoryError(f"unknown preparation claim: {claim_identity}")
            if claim["controller_id"] != controller:
                raise RepositoryError("preparation claim belongs to another controller")
            if claim["expires_at"] <= timestamp:
                raise RepositoryError("preparation claim has expired")
            if claim["evaluation_id"] != normalized["evaluation_id"]:
                raise RepositoryError("preparation claim references a different Evaluation")
            evaluation = connection.execute(
                "SELECT status FROM evaluations WHERE evaluation_id = ?",
                (claim["evaluation_id"],),
            ).fetchone()
            if evaluation is None or evaluation["status"] != "queued":
                raise RepositoryError("preparation claim requires a queued Evaluation")

            attempt = self._create_prepared_attempt_in_transaction(
                connection,
                normalized=normalized,
                encoded=encoded,
                checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
                attempt_id=attempt_id,
                governance_provenance=provenance,
            )
            deleted = connection.execute(
                """
                DELETE FROM preparation_claims
                WHERE claim_id = ? AND controller_id = ?
                """,
                (claim_identity, controller),
            )
            if deleted.rowcount != 1:
                raise RepositoryError("preparation claim changed before commit")
            self._state_event(
                connection,
                aggregate_type="evaluation",
                aggregate_id=str(claim["evaluation_id"]),
                from_status="queued",
                to_status="queued",
                event_type="EvaluationPreparationClaimCommitted",
                payload={
                    "claim_id": claim_identity,
                    "controller_id": controller,
                    "preparation_id": normalized["preparation_id"],
                    "attempt_id": attempt["attempt_id"],
                },
                publish=False,
                created_at=timestamp,
            )
            return attempt

    def release_preparation_claim(
        self,
        claim_id: str,
        controller_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Release one uncommitted claim; repeated release is a no-op."""

        claim_identity = normalize_token(claim_id, "claim_id")
        controller = normalize_token(controller_id, "controller_id")
        explanation = str(reason).strip()
        if not explanation:
            raise RepositoryError("claim release reason is required")
        timestamp = _iso(_utc_now() if now is None else now)
        with self._transaction() as connection:
            claim = connection.execute(
                """
                SELECT pc.*, e.status AS evaluation_status
                FROM preparation_claims pc
                JOIN evaluations e ON e.evaluation_id = pc.evaluation_id
                WHERE pc.claim_id = ?
                """,
                (claim_identity,),
            ).fetchone()
            if claim is None:
                return False
            if claim["controller_id"] != controller:
                raise RepositoryError("preparation claim belongs to another controller")
            connection.execute(
                "DELETE FROM preparation_claims WHERE claim_id = ?",
                (claim_identity,),
            )
            self._state_event(
                connection,
                aggregate_type="evaluation",
                aggregate_id=str(claim["evaluation_id"]),
                from_status=str(claim["evaluation_status"]),
                to_status=str(claim["evaluation_status"]),
                event_type="EvaluationPreparationClaimReleased",
                payload={
                    "claim_id": claim_identity,
                    "controller_id": controller,
                    "reason": explanation,
                },
                publish=False,
                created_at=timestamp,
            )
            return True

    def retire_unstarted_preparation(
        self,
        attempt_id: str,
        preparation_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Retire a revoked unstarted Preparation without cancelling its Evaluation."""

        attempt_identity = normalize_token(attempt_id, "attempt_id")
        preparation_identity = normalize_token(preparation_id, "preparation_id")
        explanation = str(reason).strip()
        if not explanation:
            raise RepositoryError("preparation retirement reason is required")
        timestamp = _iso(_utc_now() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, e.status AS evaluation_status
                FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.attempt_id = ?
                """,
                (attempt_identity,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_identity}")
            payload = {
                "evaluation_id": str(row["evaluation_id"]),
                "preparation_id": preparation_identity,
                "reason": explanation,
            }
            if row["status"] == "cancelled":
                event = connection.execute(
                    """
                    SELECT payload_json FROM state_events
                    WHERE aggregate_type = 'attempt' AND aggregate_id = ?
                      AND event_type = 'AttemptPreparationRetired'
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (attempt_identity,),
                ).fetchone()
                if event is None or json.loads(event["payload_json"]) != payload:
                    raise RepositoryError(
                        "retired Preparation was replayed with different inputs"
                    )
                return self._attempt_record(row)
            if row["status"] != "planned":
                raise RepositoryError("only a planned Preparation can be retired")
            if row["evaluation_status"] != "queued":
                raise RepositoryError("Preparation retirement requires a queued Evaluation")
            if (
                row["execution_preparation_id"] != preparation_identity
                or row["execution_preparation_json"] is None
            ):
                raise RepositoryError("Attempt references a different Preparation")
            try:
                artifacts = json.loads(row["artifact_ids_json"])
            except json.JSONDecodeError as exc:
                raise RepositoryError(
                    "Attempt artifacts contain invalid persisted JSON"
                ) from exc
            if (
                row["failure_class"] is not None
                or row["lease_owner"] is not None
                or row["lease_expires_at"] is not None
                or row["session_ref"] is not None
                or row["selected_execution_option_id"] is not None
                or row["execution_plan_id"] is not None
                or row["execution_plan_json"] is not None
                or row["allocation_json"] is not None
                or artifacts
            ):
                raise RepositoryError(
                    "started or allocated Preparation cannot be retired"
                )
            self._transition_attempt(
                connection,
                attempt_id=attempt_identity,
                expected=("planned",),
                target="cancelled",
                event_type="AttemptPreparationRetired",
                payload=payload,
                created_at=timestamp,
            )
            return self.get_attempt(attempt_identity, connection=connection)

    def list_prepared_scheduling_candidates(
        self, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return only the new post-scheduling-plan control path."""

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise RepositoryError("candidate limit must be a positive integer or None")
        statement = """
            SELECT a.*, e.priority AS evaluation_priority,
                   e.created_at AS evaluation_queued_since
            FROM attempts a
            JOIN evaluations e ON e.evaluation_id = a.evaluation_id
            WHERE a.status = 'planned' AND e.status = 'queued'
              AND a.execution_preparation_json IS NOT NULL
              AND a.selected_execution_option_id IS NULL
              AND a.execution_plan_json IS NULL
              AND a.allocation_json IS NULL
            ORDER BY a.created_at, a.attempt_id
        """
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            statement += " LIMIT ?"
            parameters = (limit,)
        with closing(self._connect()) as connection:
            rows = connection.execute(statement, parameters).fetchall()
        return [
            {
                **self._attempt_record(row),
                "priority": str(row["evaluation_priority"]),
                "queued_since": str(row["evaluation_queued_since"]),
            }
            for row in rows
        ]

    def list_scheduling_candidates(self, limit: int = 32) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise RepositoryError("candidate limit must be a positive integer")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.status = 'planned' AND e.status = 'queued'
                  AND a.execution_preparation_json IS NULL
                  AND a.execution_plan_json IS NOT NULL
                  AND a.allocation_json IS NULL
                ORDER BY a.created_at, a.attempt_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._attempt_record(row) for row in rows]

    def list_stale_reconciling_attempts(
        self,
        stale_seconds: int = 3600,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """List reconciling Attempts whose latest state event is too old.

        The state-events journal is the durable clock for this report.  In
        particular, expiring a reconciliation lease deliberately does not
        update the Attempt, so a dead reconciliation remains discoverable.
        This method is read-only and never changes lifecycle state.
        """
        if (
            isinstance(stale_seconds, bool)
            or not isinstance(stale_seconds, int)
            or stale_seconds < 0
        ):
            raise RepositoryError("stale_seconds must be a non-negative integer")
        current = _utc_now() if now is None else now
        if current.tzinfo is None:
            raise RepositoryError("timestamps must be timezone-aware")
        timestamp = _iso(current)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT a.*, latest.created_at AS reconciling_since
                FROM attempts a
                JOIN (
                    SELECT aggregate_id, MAX(sequence) AS sequence
                    FROM state_events
                    WHERE aggregate_type = 'attempt'
                    GROUP BY aggregate_id
                ) latest_sequence
                  ON latest_sequence.aggregate_id = a.attempt_id
                JOIN state_events latest
                  ON latest.aggregate_type = 'attempt'
                 AND latest.aggregate_id = latest_sequence.aggregate_id
                 AND latest.sequence = latest_sequence.sequence
                WHERE a.status = 'reconciling'
                  AND a.allocation_json IS NOT NULL
                ORDER BY latest.created_at, a.attempt_id
                """
            ).fetchall()

        stale: list[dict[str, Any]] = []
        for row in rows:
            try:
                since = datetime.fromisoformat(str(row["reconciling_since"]))
            except (TypeError, ValueError) as exc:
                raise RepositoryError(
                    "Attempt state event contains an invalid timestamp"
                ) from exc
            if since.tzinfo is None:
                raise RepositoryError("Attempt state event timestamp lacks timezone")
            age = (current.astimezone(timezone.utc) - since.astimezone(timezone.utc)).total_seconds()
            if age <= stale_seconds:
                continue
            try:
                allocation = validate_resource_allocation(
                    json.loads(row["allocation_json"])
                )
            except (json.JSONDecodeError, ExecutionOptionError, ContractError, SchedulingError) as exc:
                raise RepositoryError("stale Attempt allocation is invalid") from exc
            stale.append(
                {
                    "attempt_id": str(row["attempt_id"]),
                    "evaluation_id": str(row["evaluation_id"]),
                    "target_id": allocation["target_id"],
                    "processors": allocation["processors"],
                    "memory_bytes": allocation["memory_bytes"],
                    "reconciling_since": str(row["reconciling_since"]),
                    "age_seconds": age,
                }
            )
        return stale

    def list_reconciling_attempts_for_wall_proof(self) -> list[dict[str, Any]]:
        """Return reconciling Attempts and their durable claim clock.

        Attempt row ``updated_at`` is not a suitable clock: reconciliation
        lease expiry and heartbeats can change it independently of launch.
        The first AttemptLeased state event is the durable claim instant (with
        AttemptStarted as a compatibility fallback for older rows).
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE status = 'reconciling' AND allocation_json IS NOT NULL
                ORDER BY created_at, attempt_id
                """
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            for row in rows:
                events = connection.execute(
                    """
                    SELECT event_type, created_at FROM state_events
                    WHERE aggregate_type = 'attempt' AND aggregate_id = ?
                      AND event_type IN ('AttemptLeased', 'AttemptStarted')
                    ORDER BY sequence
                    """,
                    (row["attempt_id"],),
                ).fetchall()
                claim_event = next(
                    (event for event in events if event["event_type"] == "AttemptLeased"),
                    None,
                )
                if claim_event is None:
                    claim_event = next(
                        (event for event in events if event["event_type"] == "AttemptStarted"),
                        None,
                    )
                candidates.append(
                    {
                        "attempt_id": str(row["attempt_id"]),
                        "evaluation_id": str(row["evaluation_id"]),
                        "execution_preparation": (
                            None
                            if row["execution_preparation_json"] is None
                            else _safe_json_object(row["execution_preparation_json"])
                        ),
                        "execution_plan": (
                            None
                            if row["execution_plan_json"] is None
                            else _safe_json_object(row["execution_plan_json"])
                        ),
                        "claimed_at": (
                            None if claim_event is None else str(claim_event["created_at"])
                        ),
                    }
                )
        return candidates

    def has_reconciling_attempts_for_wall_proof(self) -> bool:
        """Cheap read-only hint for whether wall-budget recovery may apply."""
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT 1 FROM attempts
                WHERE status = 'reconciling' AND allocation_json IS NOT NULL
                LIMIT 1
                """
            ).fetchone() is not None

    def auto_release_wall_budget(
        self,
        proof_seconds_by_attempt: Mapping[str, int],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Release only reconciling Attempts whose caller-provided proof elapsed.

        The proof budget is deliberately supplied by the caller from the
        immutable plan/preparation.  This transaction rechecks lifecycle state
        before applying the existing lost transition, so a concurrent observer
        cannot be terminated inside its proof window.
        """
        if not isinstance(proof_seconds_by_attempt, Mapping):
            raise RepositoryError("proof_seconds_by_attempt must be a mapping")
        for attempt_id, proof_seconds in proof_seconds_by_attempt.items():
            if (
                isinstance(proof_seconds, bool)
                or not isinstance(proof_seconds, int)
                or proof_seconds < 1
            ):
                raise RepositoryError(
                    f"proof_seconds for {attempt_id} must be a positive integer"
                )
        current = _utc_now() if now is None else now
        if current.tzinfo is None:
            raise RepositoryError("timestamps must be timezone-aware")
        timestamp = _iso(current)
        if not self.has_reconciling_attempts_for_wall_proof():
            return []
        records: list[dict[str, Any]] = []
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE status = 'reconciling' AND allocation_json IS NOT NULL
                ORDER BY created_at, attempt_id
                """
            ).fetchall()
            for row in rows:
                attempt_id = str(row["attempt_id"])
                persisted_budget = None
                for budget_source in (
                    row["execution_preparation_json"],
                    row["execution_plan_json"],
                ):
                    decoded = (
                        None if budget_source is None else _safe_json_object(budget_source)
                    )
                    if isinstance(decoded, Mapping) and isinstance(decoded.get("budget"), Mapping):
                        persisted_budget = decoded["budget"]
                        break
                if not isinstance(persisted_budget, Mapping) or not {
                    "max_wall_seconds",
                    "command_timeout_seconds",
                }.issubset(persisted_budget):
                    records.append(
                        {
                            "attempt_id": attempt_id,
                            "evaluation_id": str(row["evaluation_id"]),
                            "status": "skipped",
                            "reason": "budget-unavailable",
                            "budget_status": "unreadable",
                            "source": "auto:wall-proof",
                        }
                    )
                    continue
                if attempt_id not in proof_seconds_by_attempt:
                    records.append(
                        {
                            "attempt_id": attempt_id,
                            "evaluation_id": str(row["evaluation_id"]),
                            "status": "skipped",
                            "reason": "proof_seconds-not-supplied",
                            "source": "auto:wall-proof",
                        }
                    )
                    continue
                event = connection.execute(
                    """
                    SELECT event_type, created_at FROM state_events
                    WHERE aggregate_type = 'attempt' AND aggregate_id = ?
                      AND event_type IN ('AttemptLeased', 'AttemptStarted')
                    ORDER BY sequence
                    """,
                    (attempt_id,),
                ).fetchall()
                claim_event = next(
                    (item for item in event if item["event_type"] == "AttemptLeased"),
                    None,
                )
                if claim_event is None:
                    claim_event = next(
                        (item for item in event if item["event_type"] == "AttemptStarted"),
                        None,
                    )
                if claim_event is None:
                    records.append(
                        {
                            "attempt_id": attempt_id,
                            "evaluation_id": str(row["evaluation_id"]),
                            "status": "skipped",
                            "reason": "claim-time-unavailable",
                            "source": "auto:wall-proof",
                        }
                    )
                    continue
                try:
                    claimed = datetime.fromisoformat(str(claim_event["created_at"]))
                except (TypeError, ValueError) as exc:
                    raise RepositoryError(
                        "Attempt claim event contains an invalid timestamp"
                    ) from exc
                if claimed.tzinfo is None:
                    raise RepositoryError("Attempt claim event timestamp lacks timezone")
                age = (
                    current.astimezone(timezone.utc)
                    - claimed.astimezone(timezone.utc)
                ).total_seconds()
                proof_seconds = proof_seconds_by_attempt[attempt_id]
                if age <= proof_seconds:
                    # A live proof window is intentionally absent from the
                    # processing receipt: no lifecycle action occurred.
                    continue
                payload = {
                    "reason": "wall-budget-elapsed",
                    "source": "auto:wall-proof",
                    "failure_class": "wall-budget-elapsed",
                    "proof_seconds": proof_seconds,
                    "claimed_at": str(claim_event["created_at"]),
                    "age_seconds": age,
                }
                self._mark_attempt_lost_in_transaction(
                    connection,
                    row,
                    failure_class="wall-budget-elapsed",
                    payload=payload,
                    created_at=timestamp,
                )
                records.append(
                    {
                        "attempt_id": attempt_id,
                        "evaluation_id": str(row["evaluation_id"]),
                        "status": "released",
                        "new_status": "lost",
                        "claimed_at": str(claim_event["created_at"]),
                        "age_seconds": age,
                        "proof_seconds": proof_seconds,
                        "source": "auto:wall-proof",
                        "reason": "wall-budget-elapsed",
                    }
                )
        return records

    @staticmethod
    def _attempt_budget(row: sqlite3.Row) -> dict[str, Any] | None:
        for encoded in (row["execution_preparation_json"], row["execution_plan_json"]):
            value = None if encoded is None else _safe_json_object(encoded)
            if isinstance(value, Mapping) and isinstance(value.get("budget"), Mapping):
                budget = value["budget"]
                if {"max_wall_seconds", "command_timeout_seconds"}.issubset(budget):
                    return {
                        "max_wall_seconds": budget.get("max_wall_seconds"),
                        "command_timeout_seconds": budget.get("command_timeout_seconds"),
                    }
        return None

    @staticmethod
    def _attempt_shape(row: sqlite3.Row) -> tuple[str, str, str, int] | None:
        encoded = row["execution_preparation_json"]
        selected_id = row["selected_execution_option_id"]
        value = None if encoded is None else _safe_json_object(encoded)
        if not isinstance(value, Mapping) or not selected_id:
            return None
        options = value.get("execution_option_set", {}).get("options", [])
        selected = next((item for item in options if isinstance(item, Mapping) and item.get("option_id") == selected_id), None)
        if not isinstance(selected, Mapping) or not isinstance(selected.get("simulation_definition"), Mapping):
            return None
        definition = selected["simulation_definition"]
        try:
            task_class = make_task_class(
                simulation_definition_artifact_id=definition["artifact_id"],
                simulation_definition_revision=definition["revision"],
                numerical_profile=value["numerical_profile"],
                recovery_profile_revision=value["recovery_profile_revision"],
            )["key"]
            return (task_class, str(selected["target_id"]), str(definition["revision"]), int(selected["processors"]))
        except (KeyError, TypeError, ValueError, ComputeProfileError):
            return None


    @staticmethod
    def _effective_automation_profile(connection: sqlite3.Connection, evaluation_id: str, default: str) -> str:
        rows = connection.execute(
            """SELECT s.automation_profile FROM studies s
               JOIN study_evaluations se ON se.study_id=s.study_id
               WHERE se.evaluation_id=?""", (evaluation_id,)
        ).fetchall()
        return most_conservative([str(row["automation_profile"] or default) for row in rows], default)

    def has_recovering_evaluations(self) -> bool:
        """Cheap read-only hint for whether recovery triage may apply."""
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM evaluations WHERE status = 'recovering' LIMIT 1"
            ).fetchone() is not None

    def auto_requeue_recovering(
        self, *, now: datetime | None = None,
        automation_policy: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply infrastructure recovery and timeout triage atomically."""
        policy = DEFAULT_AUTOMATION_POLICY if automation_policy is None else automation_policy
        platform = policy.get("platform", {})
        profiles = policy.get("profiles", {})
        default_profile = str(policy.get("default_profile", "assisted"))
        requeue_limit = int(platform.get("requeue_limit", 2))
        tier1_min = int(platform.get("tier1_min_samples", 20))
        timestamp = _iso(_utc_now() if now is None else now)
        if not self.has_recovering_evaluations():
            return []
        results: list[dict[str, Any]] = []
        with self._transaction() as connection:
            evaluations = connection.execute(
                "SELECT * FROM evaluations WHERE status = 'recovering' ORDER BY updated_at, evaluation_id"
            ).fetchall()
            for evaluation in evaluations:
                evaluation_id = str(evaluation["evaluation_id"])
                attempts = connection.execute(
                    "SELECT * FROM attempts WHERE evaluation_id=? ORDER BY attempt_number, created_at, attempt_id",
                    (evaluation_id,),
                ).fetchall()
                attempt = attempts[-1] if attempts else None
                if attempt is None:
                    continue
                failure = str(attempt["failure_class"] or "")
                if failure in {"remote-session-not-found", "wall-budget-elapsed"}:
                    event_rows = connection.execute(
                        """SELECT payload_json FROM state_events
                           WHERE aggregate_type='evaluation' AND aggregate_id=?
                             AND from_status='recovering' AND to_status='queued'""", (evaluation_id,)
                    ).fetchall()
                    automatic_count = sum(
                        1 for item in event_rows
                        if _safe_json_object(item["payload_json"]) and _safe_json_object(item["payload_json"]).get("source") == "auto:requeue"
                    )
                    if automatic_count >= requeue_limit:
                        continue
                    reason = f"automatic recovery whitelist: {failure}"
                    payload = {
                        "reason": reason, "source": "auto:requeue", "failure_class": failure,
                        "attempt_id": str(attempt["attempt_id"]), "automatic_requeue_count": automatic_count + 1,
                        "rule": "infrastructure-whitelist", "tier": 0,
                    }
                    self._transition_evaluation(connection, evaluation_id=evaluation_id,
                        expected=("recovering",), target="queued", event_type="RecoveryPlanned",
                        payload=payload, created_at=timestamp)
                    results.append({"evaluation_id": evaluation_id, "attempt_id": str(attempt["attempt_id"]),
                        "source": "auto:requeue", "reason": reason, "automatic_requeue_count": automatic_count + 1,
                        "status": "queued", "rule": "infrastructure-whitelist", "tier": 0,
                        "action": "requeued", "input": {"failure_class": failure}})
                    continue
                if failure != "timeout":
                    # Preserve the old fail-closed behavior for operator and
                    # unknown failures: they require an explicit decision.
                    continue
                timeout_attempts = [row for row in attempts if row["failure_class"] == "timeout"]
                profile_name = self._effective_automation_profile(connection, evaluation_id, default_profile)
                profile = profiles.get(profile_name, {})
                budget = self._attempt_budget(attempt)
                evidence = [{"attempt_id": str(row["attempt_id"]), "timestamp": str(row["updated_at"]),
                             "budget": self._attempt_budget(row)} for row in timeout_attempts]
                shape = self._attempt_shape(attempt)
                stats = None
                tier = 0
                if shape is not None:
                    stat_row = connection.execute(
                        """SELECT * FROM task_shape_stats WHERE task_class_key=? AND target_id=?
                           AND profile_revision=? AND processors=?""", shape
                    ).fetchone()
                    if stat_row is not None and int(stat_row["sample_count"]) >= tier1_min:
                        tier = 1
                        mean = stat_row["wall_mean_seconds"]
                        stddev = welford_stddev(stat_row["wall_m2_seconds"], int(stat_row["wall_samples"]))
                        p95 = None if mean is None else float(mean) + 1.645 * float(stddev or 0.0)
                        stats = {"task_class_key": shape[0], "target_id": shape[1], "profile_revision": shape[2],
                                 "processors": shape[3], "sample_count": int(stat_row["sample_count"]),
                                 "mean": mean, "variance": None if stddev is None else float(stddev) ** 2,
                                 "stddev": stddev, "p95": p95, "budget": budget}
                input_summary = {"failure_class": failure, "timeout_count": len(timeout_attempts),
                                 "timeout_timestamps": [item["timestamp"] for item in evidence],
                                 "budget": budget, "profile": profile_name}
                if stats is not None:
                    input_summary["statistics"] = stats
                report_reasons: list[str] = []
                if stats is not None and stats["p95"] is not None and budget is not None and stats["p95"] >= 0.9 * float(budget["max_wall_seconds"]):
                    report_reasons.append("systemic-budget-pressure")
                if len(timeout_attempts) >= 2:
                    rule = "tier0-deterministic-timeout"
                    report_reasons.append("deterministic-timeout")
                    if profile.get("pathological_point") == "skip-and-mark":
                        action = "marked-unresolved"
                        self._transition_evaluation(connection, evaluation_id=evaluation_id,
                            expected=("recovering",), target="unresolved", event_type="EvaluationUnresolved",
                            payload={"reason": "deterministic-timeout; evidence=" + canonical_json(evidence),
                                     "rule": rule, "tier": tier, "evidence": evidence}, created_at=timestamp)
                    else:
                        action = "held"
                elif bool(profile.get("timeout_transient_rerun", False)):
                    rule = "tier0-first-timeout-rerun"
                    action = "requeued"
                    event_rows = connection.execute(
                        """SELECT payload_json FROM state_events WHERE aggregate_type='evaluation' AND aggregate_id=?
                           AND from_status='recovering' AND to_status='queued'""", (evaluation_id,)).fetchall()
                    automatic_count = sum(1 for item in event_rows if (_safe_json_object(item["payload_json"]) or {}).get("source") == "auto:requeue")
                    if automatic_count >= requeue_limit:
                        action = "held"
                        rule = "tier0-timeout-rerun-limit"
                    else:
                        payload = {"reason": "transient timeout", "source": "auto:requeue", "failure_class": failure,
                                   "rule": rule, "tier": tier, "attempt_id": str(attempt["attempt_id"]),
                                   "automatic_requeue_count": automatic_count + 1, "budget": budget}
                        if stats is not None: payload["statistics"] = stats
                        self._transition_evaluation(connection, evaluation_id=evaluation_id,
                            expected=("recovering",), target="queued", event_type="RecoveryPlanned",
                            payload=payload, created_at=timestamp)
                else:
                    rule = "tier0-first-timeout-held"
                    action = "held"
                    report_reasons.append("manual-timeout-review")
                result = {"evaluation_id": evaluation_id, "rule": rule, "tier": tier, "action": action,
                          "input": input_summary, "evidence": evidence, "report": bool(report_reasons) or action != "requeued"}
                if report_reasons: result["report_reasons"] = report_reasons
                results.append(result)
        return results

    def operator_requeue(
        self, evaluation_id: str, reason: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        return self.plan_recovery(
            evaluation_id,
            reason,
            source="operator:requeue",
            now=now,
        )

    def list_active_allocations(
        self, target_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List active allocations for one target, or all targets when omitted."""
        target = None if target_id is None else normalize_token(target_id, "target_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT allocation_json, execution_preparation_json FROM attempts
                WHERE status IN ({_CAPACITY_HOLDING_ATTEMPT_STATES_SQL})
                  AND allocation_json IS NOT NULL
                ORDER BY created_at, attempt_id
                """
            ).fetchall()
        allocations = []
        for row in rows:
            allocation = validate_resource_allocation(
                json.loads(row["allocation_json"])
            )
            if target is None or allocation["target_id"] == target:
                active = {
                    "attempt_id": allocation["attempt_id"],
                    "target_id": allocation["target_id"],
                    "processors": allocation["processors"],
                    "memory_bytes": allocation["memory_bytes"],
                    "resource_key": allocation["resource_key"],
                }
                preparation_json = row["execution_preparation_json"]
                if preparation_json is not None:
                    try:
                        preparation = validate_execution_preparation(
                            json.loads(preparation_json)
                        )
                    except (json.JSONDecodeError, ExecutionOptionError) as exc:
                        raise RepositoryError(
                            "active allocation has invalid execution preparation"
                        ) from exc
                    calibration = preparation.get("calibration")
                    if (
                        isinstance(calibration, Mapping)
                        and calibration.get("target_isolation") == "exclusive"
                    ):
                        active["exclusive_target"] = True
                allocations.append(active)
        return allocations

    def claim_scheduled_session(
        self,
        attempt_id: str,
        dispatcher_id: str,
        lease_seconds: int,
        allocation: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        dispatcher = str(dispatcher_id).strip()
        if not dispatcher:
            raise RepositoryError("dispatcher_id is required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        normalized = validate_resource_allocation(allocation)
        if normalized["attempt_id"] != attempt_id:
            raise RepositoryError("allocation does not match the selected Attempt")
        current = _utc_now() if now is None else now
        expires = current + timedelta(seconds=lease_seconds)
        timestamp = _iso(current)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.attempt_id = ?
                  AND a.status = 'planned' AND e.status = 'queued'
                  AND a.execution_preparation_json IS NULL
                  AND a.execution_plan_json IS NOT NULL
                  AND a.allocation_json IS NULL
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                return None
            plan = json.loads(row["execution_plan_json"])
            if (
                plan.get("attempt_id") != attempt_id
                or plan.get("target_id") != normalized["target_id"]
                or plan.get("resources", {}).get("requested_processors")
                != normalized["processors"]
            ):
                raise RepositoryError(
                    "allocation does not match the bound session plan"
                )

            connection.execute(
                """
                UPDATE attempts
                SET lease_owner = ?, lease_expires_at = ?, session_ref = ?,
                    allocation_json = ?, last_heartbeat_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'planned'
                  AND allocation_json IS NULL
                """,
                (
                    dispatcher,
                    _iso(expires),
                    normalized["session_ref"],
                    canonical_json(normalized),
                    timestamp,
                    timestamp,
                    attempt_id,
                ),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("planned",),
                target="leased",
                event_type="AttemptLeased",
                payload={
                    "dispatcher_id": dispatcher,
                    "lease_expires_at": _iso(expires),
                    "allocation_id": normalized["allocation_id"],
                    "decision_id": normalized["decision"]["decision_id"],
                },
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=str(row["evaluation_id"]),
                expected=("queued",),
                target="running",
                event_type="EvaluationRunning",
                payload={"attempt_id": attempt_id},
                publish=False,
                created_at=timestamp,
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("leased",),
                target="running",
                event_type="AttemptStarted",
                payload={
                    "dispatcher_id": dispatcher,
                    "session_ref": normalized["session_ref"],
                    "execution_plan_id": str(row["execution_plan_id"]),
                    "allocation_id": normalized["allocation_id"],
                },
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def claim_prepared_execution(
        self,
        attempt_id: str,
        dispatcher_id: str,
        lease_seconds: int,
        *,
        preparation_id: str,
        selected_option_id: str,
        session_plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        license_sessions: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Atomically select an option, materialize its plan, and allocate it."""

        dispatcher = str(dispatcher_id).strip()
        if not dispatcher:
            raise RepositoryError("dispatcher_id is required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        preparation_identity = normalize_token(preparation_id, "preparation_id")
        option_identity = normalize_token(selected_option_id, "selected_option_id")
        plan = validate_simulation_session_plan(session_plan)
        normalized_allocation = validate_resource_allocation(allocation)
        if (
            plan["attempt_id"] != attempt_id
            or normalized_allocation["attempt_id"] != attempt_id
        ):
            raise RepositoryError(
                "plan and allocation must match the selected Attempt"
            )
        if license_sessions is not None and (
            isinstance(license_sessions, bool)
            or not isinstance(license_sessions, int)
            or license_sessions < 1
        ):
            raise RepositoryError("license_sessions must be a positive integer")
        current = _utc_now() if now is None else now
        expires = current + timedelta(seconds=lease_seconds)
        timestamp = _iso(current)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.attempt_id = ?
                  AND a.status = 'planned' AND e.status = 'queued'
                  AND a.execution_preparation_json IS NOT NULL
                  AND a.selected_execution_option_id IS NULL
                  AND a.execution_plan_json IS NULL
                  AND a.allocation_json IS NULL
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                return None
            if row["execution_preparation_id"] != preparation_identity:
                raise RepositoryError("claim references a different preparation")
            try:
                preparation = validate_execution_preparation(
                    json.loads(row["execution_preparation_json"])
                )
            except ExecutionOptionError as exc:
                raise RepositoryError(
                    "stored execution preparation is invalid"
                ) from exc
            options = {
                item["option_id"]: item
                for item in preparation["execution_option_set"]["options"]
            }
            selected_option = options.get(option_identity)
            if selected_option is None:
                raise RepositoryError(
                    "selected option is not part of the stored preparation"
                )
            expected_plan = materialize_session_plan(
                attempt_id=attempt_id,
                preparation=preparation,
                selected_option=selected_option,
            )
            if plan != expected_plan:
                raise RepositoryError(
                    "session plan was not materialized from the selected option"
                )
            decision = normalized_allocation["decision"]
            profiles = {
                item["execution_option_id"]: item
                for item in preparation["performance_profile_snapshot"]["profiles"]
            }
            if (
                decision.get("selected_execution_option_set_id")
                != preparation["execution_option_set"]["option_set_id"]
                or decision.get("selected_execution_option") != selected_option
                or decision.get("selected_performance_profile_snapshot_id")
                != preparation["performance_profile_snapshot"][
                    "profile_snapshot_id"
                ]
                or decision.get("selected_performance_profile")
                != profiles[option_identity]
                or normalized_allocation["target_id"]
                != selected_option["target_id"]
                or normalized_allocation["processors"]
                != selected_option["processors"]
                or normalized_allocation["memory_bytes"]
                != selected_option["memory_bytes"]
            ):
                raise RepositoryError(
                    "allocation decision does not match the stored preparation"
                )

            if license_sessions is not None:
                active_count = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*) FROM attempts
                        WHERE status IN ({_CAPACITY_HOLDING_ATTEMPT_STATES_SQL})
                          AND allocation_json IS NOT NULL
                        """
                    ).fetchone()[0]
                )
                if active_count >= license_sessions:
                    raise RepositoryError(
                        "license sessions exhausted: "
                        f"{active_count} active allocations, limit {license_sessions}"
                    )

            updated = connection.execute(
                """
                UPDATE attempts
                SET selected_execution_option_id = ?,
                    execution_plan_id = ?, execution_plan_json = ?,
                    lease_owner = ?, lease_expires_at = ?, session_ref = ?,
                    allocation_json = ?, last_heartbeat_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'planned'
                  AND execution_preparation_id = ?
                  AND selected_execution_option_id IS NULL
                  AND execution_plan_json IS NULL
                  AND allocation_json IS NULL
                """,
                (
                    option_identity,
                    plan["plan_id"],
                    canonical_json(plan),
                    dispatcher,
                    _iso(expires),
                    normalized_allocation["session_ref"],
                    canonical_json(normalized_allocation),
                    timestamp,
                    timestamp,
                    attempt_id,
                    preparation_identity,
                ),
            )
            if updated.rowcount != 1:
                return None
            self._state_event(
                connection,
                aggregate_type="attempt",
                aggregate_id=attempt_id,
                from_status="planned",
                to_status="planned",
                event_type="AttemptPlanMaterialized",
                payload={
                    "preparation_id": preparation_identity,
                    "selected_execution_option_id": option_identity,
                    "execution_plan_id": plan["plan_id"],
                },
                created_at=timestamp,
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("planned",),
                target="leased",
                event_type="AttemptLeased",
                payload={
                    "dispatcher_id": dispatcher,
                    "lease_expires_at": _iso(expires),
                    "allocation_id": normalized_allocation["allocation_id"],
                    "decision_id": decision["decision_id"],
                },
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=str(row["evaluation_id"]),
                expected=("queued",),
                target="running",
                event_type="EvaluationRunning",
                payload={"attempt_id": attempt_id},
                publish=False,
                created_at=timestamp,
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("leased",),
                target="starting",
                event_type="AttemptStarting",
                payload={
                    "dispatcher_id": dispatcher,
                    "session_ref": normalized_allocation["session_ref"],
                    "execution_plan_id": plan["plan_id"],
                    "allocation_id": normalized_allocation["allocation_id"],
                },
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def confirm_attempt_start(
        self,
        attempt_id: str,
        dispatcher_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """CAS-confirm that the worker accepted a durably claimed session."""
        dispatcher = str(dispatcher_id).strip()
        if not dispatcher:
            raise RepositoryError("dispatcher_id is required")
        current = _utc_now() if now is None else now
        timestamp = _iso(current)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["status"] != "starting":
                raise RepositoryError(
                    f"Attempt {attempt_id} is {row['status']}; launch confirmation requires starting"
                )
            if row["lease_owner"] != dispatcher:
                raise RepositoryError("launch confirmation requires the claiming dispatcher")
            updated = connection.execute(
                """
                UPDATE attempts SET status = 'running', updated_at = ?
                WHERE attempt_id = ? AND status = 'starting' AND lease_owner = ?
                """,
                (timestamp, attempt_id, dispatcher),
            )
            if updated.rowcount != 1:
                raise RepositoryError("Attempt changed before launch confirmation")
            self._state_event(
                connection,
                aggregate_type="attempt",
                aggregate_id=attempt_id,
                payload={
                    "dispatcher_id": dispatcher,
                    "reason": "launch_confirmed",
                    "outcome": "launch_confirmed",
                },
                from_status="starting",
                to_status="running",
                event_type="AttemptStarted",
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def lease_next_attempt(
        self,
        worker_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        worker = str(worker_id).strip()
        if not worker:
            raise RepositoryError("worker_id is required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        current = _utc_now() if now is None else now
        expires = current + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.* FROM attempts a
                JOIN evaluations e ON e.evaluation_id = a.evaluation_id
                WHERE a.status = 'planned' AND e.status = 'queued'
                  AND a.execution_preparation_json IS NULL
                ORDER BY a.created_at, a.attempt_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            attempt_id = str(row["attempt_id"])
            evaluation_id = str(row["evaluation_id"])
            timestamp = _iso(current)
            connection.execute(
                """
                UPDATE attempts
                SET lease_owner = ?, lease_expires_at = ?, last_heartbeat_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'planned'
                """,
                (worker, _iso(expires), timestamp, timestamp, attempt_id),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("planned",),
                target="leased",
                event_type="AttemptLeased",
                payload={"worker_id": worker, "lease_expires_at": _iso(expires)},
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=evaluation_id,
                expected=("queued",),
                target="running",
                event_type="EvaluationRunning",
                payload={"attempt_id": attempt_id},
                publish=False,
                created_at=timestamp,
            )
            leased = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert leased is not None
            return self._attempt_record(leased)

    def has_reconciliation_candidate(self) -> bool:
        """Cheap read-only hint for whether an unleased reconciliation exists."""
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                SELECT 1 FROM attempts
                WHERE status = 'reconciling' AND lease_owner IS NULL
                LIMIT 1
                """
            ).fetchone() is not None

    def lease_next_reconciliation(
        self,
        observer_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        observer = str(observer_id).strip()
        if not observer:
            raise RepositoryError("observer_id is required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        current = _utc_now() if now is None else now
        expires = current + timedelta(seconds=lease_seconds)
        if not self.has_reconciliation_candidate():
            return None
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM attempts
                WHERE status = 'reconciling' AND lease_owner IS NULL
                ORDER BY updated_at, attempt_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            attempt_id = str(row["attempt_id"])
            timestamp = _iso(current)
            connection.execute(
                """
                UPDATE attempts
                SET lease_owner = ?, lease_expires_at = ?
                WHERE attempt_id = ? AND status = 'reconciling'
                  AND lease_owner IS NULL
                """,
                (observer, _iso(expires), attempt_id),
            )
            self._state_event(
                connection,
                aggregate_type="attempt",
                aggregate_id=attempt_id,
                from_status="reconciling",
                to_status="reconciling",
                event_type="AttemptReconciliationLeased",
                payload={
                    "observer_id": observer,
                    "lease_expires_at": _iso(expires),
                },
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def release_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        *,
        reason: str = "capacity-wait",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        explanation = normalize_token(reason, "reason")
        with self._transaction() as connection:
            row = self._owned_attempt(connection, attempt_id, worker_id, now=now)
            if row["status"] != "leased":
                raise RepositoryError("only an unstarted leased Attempt can be released")
            timestamp = _iso(_utc_now() if now is None else now)
            connection.execute(
                """
                UPDATE attempts
                SET lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE attempt_id = ?
                """,
                (timestamp, attempt_id),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("leased",),
                target="planned",
                event_type="AttemptLeaseReleased",
                payload={"worker_id": worker_id, "reason": explanation},
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=str(row["evaluation_id"]),
                expected=("running",),
                target="queued",
                event_type="EvaluationDispatchDeferred",
                payload={"attempt_id": attempt_id, "reason": explanation},
                publish=False,
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def start_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        *,
        session_ref: str | None = None,
        execution_plan_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if (session_ref is None) != (execution_plan_id is None):
            raise RepositoryError(
                "session_ref and execution_plan_id must be supplied together"
            )
        normalized_session_ref = (
            None if session_ref is None else normalize_token(session_ref, "session_ref")
        )
        normalized_plan_id = (
            None
            if execution_plan_id is None
            else normalize_token(execution_plan_id, "execution_plan_id")
        )
        with self._transaction() as connection:
            row = self._owned_attempt(connection, attempt_id, worker_id, now=now)
            if row["status"] != "leased":
                raise RepositoryError("only a leased Attempt can start")
            if normalized_session_ref is not None:
                if (
                    row["execution_plan_id"] != normalized_plan_id
                    or row["execution_plan_json"] is None
                ):
                    raise RepositoryError(
                        "session plan must be durably bound before the Attempt starts"
                    )
                connection.execute(
                    """
                    UPDATE attempts SET session_ref = ? WHERE attempt_id = ?
                    """,
                    (normalized_session_ref, attempt_id),
                )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("leased",),
                target="running",
                event_type="AttemptStarted",
                payload={
                    "worker_id": worker_id,
                    "session_ref": normalized_session_ref,
                    "execution_plan_id": normalized_plan_id,
                },
                created_at=_iso(_utc_now() if now is None else now),
            )
            return self.get_attempt(attempt_id, connection=connection)

    def heartbeat(
        self,
        attempt_id: str,
        worker_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
            raise RepositoryError("lease_seconds must be a positive integer")
        current = _utc_now() if now is None else now
        with self._transaction() as connection:
            row = self._owned_attempt(connection, attempt_id, worker_id, now=current)
            if row["status"] not in HEARTBEATABLE_ATTEMPT_STATES:
                raise RepositoryError("terminal Attempt cannot renew a lease")
            connection.execute(
                """
                UPDATE attempts SET lease_expires_at = ?, last_heartbeat_at = ?, updated_at = ? WHERE attempt_id = ?
                """,
                (_iso(current + timedelta(seconds=lease_seconds)), _iso(current), _iso(current), attempt_id),
            )
            return self.get_attempt(attempt_id, connection=connection)

    def begin_collection(
        self, attempt_id: str, worker_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._owned_attempt(connection, attempt_id, worker_id, now=now)
            if row["status"] != "running":
                raise RepositoryError("only a running Attempt can begin collection")
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("running",),
                target="collecting",
                event_type="AttemptCollecting",
                payload={},
                publish=False,
                created_at=_iso(_utc_now() if now is None else now),
            )
            return self.get_attempt(attempt_id, connection=connection)

    def complete_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        artifact_ids: Sequence[str],
        *,
        now: datetime | None = None,
        _validated_session_result: bool = False,
        feedback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_artifacts = [str(item).strip() for item in artifact_ids]
        artifacts = sorted(raw_artifacts)
        if (
            not artifacts
            or any(not item for item in artifacts)
            or len(artifacts) != len(set(artifacts))
        ):
            raise RepositoryError("completed Attempt requires evidence artifact IDs")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if (
                row["execution_preparation_json"] is not None
                and not _validated_session_result
            ):
                raise RepositoryError(
                    "prepared Attempt completion requires a validated SessionResult"
                )
            if row["status"] == "completed":
                if json.loads(row["artifact_ids_json"]) != artifacts:
                    raise RepositoryError("terminal Attempt receipt cannot be overwritten")
                if feedback is not None:
                    if not isinstance(feedback, Mapping) or not bool(feedback.get("success")):
                        raise RepositoryError("completed Attempt feedback must be successful")
                    self._record_attempt_feedback_in_transaction(
                        connection, attempt_id, row, feedback,
                        _iso(_utc_now() if now is None else now),
                    )
                return self._attempt_record(row)
            self._owned_attempt(connection, attempt_id, worker_id, now=now)
            if row["status"] != "collecting":
                raise RepositoryError("Attempt must collect before completion")
            timestamp = _iso(_utc_now() if now is None else now)
            connection.execute(
                """
                UPDATE attempts SET artifact_ids_json = ?, failure_class = NULL, updated_at = ?
                WHERE attempt_id = ?
                """,
                (canonical_json(artifacts), timestamp, attempt_id),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("collecting",),
                target="completed",
                event_type="AttemptCompleted",
                payload={"artifact_ids": artifacts},
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=str(row["evaluation_id"]),
                expected=("running",),
                target="qualifying",
                event_type="QualificationStarted",
                payload={"attempt_id": attempt_id},
                created_at=timestamp,
            )
            if feedback is not None:
                if not isinstance(feedback, Mapping) or not bool(feedback.get("success")):
                    raise RepositoryError("completed Attempt feedback must be successful")
                self._record_attempt_feedback_in_transaction(
                    connection, attempt_id, row, feedback, timestamp
                )
            return self.get_attempt(attempt_id, connection=connection)

    @staticmethod
    def _feedback_preparation(row: sqlite3.Row) -> dict[str, Any]:
        encoded = row["execution_preparation_json"]
        selected = row["selected_execution_option_id"]
        if encoded is None or selected is None:
            raise RepositoryError("attempt feedback requires a selected prepared option")
        try:
            preparation = validate_execution_preparation(json.loads(encoded))
        except (json.JSONDecodeError, ExecutionOptionError) as exc:
            raise RepositoryError("attempt execution preparation is invalid") from exc
        options = preparation["execution_option_set"]["options"]
        if not any(item["option_id"] == selected for item in options):
            raise RepositoryError("selected execution option is invalid")
        return preparation

    @staticmethod
    def _feedback_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "attempt_id": str(row["attempt_id"]),
            "task_class_key": str(row["task_class_key"]),
            "target_id": str(row["target_id"]),
            "profile_revision": str(row["profile_revision"]),
            "processors": int(row["processors"]),
            "success": bool(row["succeeded"]),
            "wall_seconds": row["wall_seconds"],
            "cpu_seconds": row["cpu_seconds"],
            "busy_seconds": row["busy_seconds"],
            "rss_bytes": row["rss_bytes"],
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _shape_record(row: sqlite3.Row) -> dict[str, Any]:
        return make_shape_record(
            task_class_key=str(row["task_class_key"]), target_id=str(row["target_id"]),
            profile_revision=str(row["profile_revision"]), processors=int(row["processors"]),
            sample_count=int(row["sample_count"]), success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]), successful_wall_samples=int(row["wall_samples"]),
            successful_wall_mean_seconds=row["wall_mean_seconds"],
            successful_wall_stddev_seconds=welford_stddev(row["wall_m2_seconds"], int(row["wall_samples"])),
            cpu_samples=int(row["cpu_samples"]), cpu_mean_seconds=row["cpu_mean_seconds"],
            cpu_stddev_seconds=welford_stddev(row["cpu_m2_seconds"], int(row["cpu_samples"])),
            busy_samples=int(row["busy_samples"]), busy_mean_seconds=row["busy_mean_seconds"],
            busy_stddev_seconds=welford_stddev(row["busy_m2_seconds"], int(row["busy_samples"])),
            rss_samples=int(row["rss_samples"]), rss_mean_bytes=row["rss_mean_bytes"],
            rss_stddev_bytes=welford_stddev(row["rss_m2_bytes"], int(row["rss_samples"])),
        )

    @staticmethod
    def _feedback_update_stats(
        connection: sqlite3.Connection, bucket: tuple[str, str, str, int],
        observation: Mapping[str, Any], timestamp: str,
    ) -> None:
        task_class, target, revision, processors = bucket
        row = connection.execute(
            "SELECT * FROM task_shape_stats WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=?",
            bucket,
        ).fetchone()
        values = {
            "sample_count": 0 if row is None else int(row["sample_count"]),
            "success_count": 0 if row is None else int(row["success_count"]),
            "failure_count": 0 if row is None else int(row["failure_count"]),
        }
        values["sample_count"] += 1
        values["success_count"] += int(observation["success"])
        values["failure_count"] += int(not observation["success"])
        metrics = (("wall", "wall_seconds", observation["success"]), ("cpu", "cpu_seconds", True), ("busy", "busy_seconds", True), ("rss", "rss_bytes", True))
        for prefix, source, eligible in metrics:
            count_key = prefix + "_samples"
            mean_key = prefix + "_mean_seconds" if prefix != "rss" else "rss_mean_bytes"
            m2_key = prefix + "_m2_seconds" if prefix != "rss" else "rss_m2_bytes"
            count = 0 if row is None else int(row[count_key])
            mean = None if row is None else row[mean_key]
            m2 = None if row is None else row[m2_key]
            value = observation[source]
            if eligible and value is not None:
                mean, m2 = welford_update(mean, m2, count, float(value))
                count += 1
            values.update({count_key: count, mean_key: mean, m2_key: m2})
        if row is None:
            connection.execute(
                "INSERT INTO task_shape_stats(task_class_key,target_id,profile_revision,processors,sample_count,success_count,failure_count,wall_samples,wall_mean_seconds,wall_m2_seconds,cpu_samples,cpu_mean_seconds,cpu_m2_seconds,busy_samples,busy_mean_seconds,busy_m2_seconds,rss_samples,rss_mean_bytes,rss_m2_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_class,target,revision,processors,values["sample_count"],values["success_count"],values["failure_count"],values["wall_samples"],values["wall_mean_seconds"],values["wall_m2_seconds"],values["cpu_samples"],values["cpu_mean_seconds"],values["cpu_m2_seconds"],values["busy_samples"],values["busy_mean_seconds"],values["busy_m2_seconds"],values["rss_samples"],values["rss_mean_bytes"],values["rss_m2_bytes"],timestamp),
            )
        else:
            connection.execute(
                "UPDATE task_shape_stats SET sample_count=?,success_count=?,failure_count=?,wall_samples=?,wall_mean_seconds=?,wall_m2_seconds=?,cpu_samples=?,cpu_mean_seconds=?,cpu_m2_seconds=?,busy_samples=?,busy_mean_seconds=?,busy_m2_seconds=?,rss_samples=?,rss_mean_bytes=?,rss_m2_bytes=?,updated_at=? WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=?",
                (values["sample_count"],values["success_count"],values["failure_count"],values["wall_samples"],values["wall_mean_seconds"],values["wall_m2_seconds"],values["cpu_samples"],values["cpu_mean_seconds"],values["cpu_m2_seconds"],values["busy_samples"],values["busy_mean_seconds"],values["busy_m2_seconds"],values["rss_samples"],values["rss_mean_bytes"],values["rss_m2_bytes"],timestamp,*bucket),
            )

    def _record_attempt_feedback_in_transaction(
        self, connection: sqlite3.Connection, attempt_id: str, row: sqlite3.Row,
        observation: Mapping[str, Any], timestamp: str,
    ) -> dict[str, Any]:
        feedback = validate_feedback_observation(observation)
        marker = row["feedback_json"]
        if marker is not None:
            try:
                stored = validate_feedback_observation(json.loads(marker))
            except (json.JSONDecodeError, ComputeProfileError) as exc:
                raise RepositoryError("Attempt feedback marker is invalid") from exc
            if stored != feedback:
                raise RepositoryError("conflicting Attempt feedback already exists")
            return {"attempt_id": attempt_id, **stored, "created_at": row["feedback_recorded_at"]}
        existing = connection.execute("SELECT * FROM attempt_feedback WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing is not None:
            stored = self._feedback_record(existing)
            if {key: stored[key] for key in feedback} != dict(feedback):
                raise RepositoryError("conflicting Attempt feedback already exists")
            return stored
        preparation = self._feedback_preparation(row)
        selected = next(item for item in preparation["execution_option_set"]["options"] if item["option_id"] == row["selected_execution_option_id"])
        definition = selected["simulation_definition"]
        task_class = make_task_class(simulation_definition_artifact_id=definition["artifact_id"], simulation_definition_revision=definition["revision"], numerical_profile=preparation["numerical_profile"], recovery_profile_revision=preparation["recovery_profile_revision"])["key"]
        bucket = (task_class, str(selected["target_id"]), str(definition["revision"]), int(selected["processors"]))
        exists = connection.execute("SELECT 1 FROM task_shape_stats WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=?", bucket).fetchone()
        if exists is None and connection.execute("SELECT COUNT(*) FROM task_shape_stats").fetchone()[0] >= MAX_PROFILE_BUCKETS:
            old = connection.execute("SELECT task_class_key,target_id,profile_revision,processors FROM task_shape_stats ORDER BY updated_at,task_class_key,target_id,profile_revision,processors LIMIT 1").fetchone()
            old_bucket = tuple(old)
            connection.execute("DELETE FROM task_shape_stats WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=?", old_bucket)
            connection.execute("DELETE FROM attempt_feedback WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=?", old_bucket)
        connection.execute("INSERT INTO attempt_feedback(attempt_id,task_class_key,target_id,profile_revision,processors,succeeded,wall_seconds,cpu_seconds,busy_seconds,rss_bytes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (attempt_id,*bucket,int(feedback["success"]),feedback["wall_seconds"],feedback["cpu_seconds"],feedback["busy_seconds"],feedback["rss_bytes"],timestamp))
        connection.execute("UPDATE attempts SET feedback_json=?,feedback_recorded_at=? WHERE attempt_id=?", (canonical_json(feedback),timestamp,attempt_id))
        self._feedback_update_stats(connection,bucket,feedback,timestamp)
        connection.execute("DELETE FROM attempt_feedback WHERE attempt_id IN (SELECT attempt_id FROM attempt_feedback WHERE task_class_key=? AND target_id=? AND profile_revision=? AND processors=? ORDER BY created_at DESC,attempt_id DESC LIMIT -1 OFFSET ?)", (*bucket,MAX_RECENT_FEEDBACK_PER_BUCKET))
        stored = connection.execute("SELECT * FROM attempt_feedback WHERE attempt_id=?", (attempt_id,)).fetchone()
        if stored is None:
            raise RepositoryError("feedback ledger evicted its newest entry")
        return self._feedback_record(stored)

    def record_attempt_feedback(
        self, attempt_id: str, *, success: bool, wall_seconds: float | None = None,
        cpu_seconds: float | None = None, busy_seconds: float | None = None,
        rss_bytes: int | None = None, now: datetime | None = None,
    ) -> dict[str, Any]:
        observation = validate_feedback_observation({"success": success, "wall_seconds": wall_seconds, "cpu_seconds": cpu_seconds, "busy_seconds": busy_seconds, "rss_bytes": rss_bytes})
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["status"] not in {"completed", "failed"}:
                raise RepositoryError("Attempt feedback requires a terminal Attempt")
            if (row["status"] == "completed") != observation["success"]:
                raise RepositoryError("Attempt feedback success does not match terminal status")
            return self._record_attempt_feedback_in_transaction(connection, attempt_id, row, observation, _iso(_utc_now() if now is None else now))

    def get_attempt_feedback(self, attempt_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is not None and row["feedback_json"] is not None:
                try:
                    return {"attempt_id": attempt_id, **validate_feedback_observation(json.loads(row["feedback_json"])), "created_at": row["feedback_recorded_at"]}
                except (json.JSONDecodeError, ComputeProfileError) as exc:
                    raise RepositoryError("Attempt feedback marker is invalid") from exc
            stored = connection.execute("SELECT * FROM attempt_feedback WHERE attempt_id=?", (attempt_id,)).fetchone()
        return None if stored is None else self._feedback_record(stored)

    def list_task_shape_statistics(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM task_shape_stats ORDER BY task_class_key,target_id,profile_revision,processors").fetchall()
        return [self._shape_record(row) for row in rows]

    def budget_proposals(self, *, tier2_min_samples: int = 30) -> list[dict[str, Any]]:
        """Build read-only wall-budget proposals from mature shape statistics."""
        if isinstance(tier2_min_samples, bool) or not isinstance(tier2_min_samples, int) or tier2_min_samples < 1:
            raise RepositoryError("tier2_min_samples must be a positive integer")
        with closing(self._connect()) as connection:
            stats = connection.execute(
                "SELECT * FROM task_shape_stats WHERE sample_count >= ? ORDER BY task_class_key,target_id,profile_revision,processors",
                (tier2_min_samples,),
            ).fetchall()
            completed = connection.execute(
                """SELECT * FROM attempts WHERE status='completed' AND execution_preparation_json IS NOT NULL
                   ORDER BY updated_at DESC, attempt_id DESC"""
            ).fetchall()
            proposals: list[dict[str, Any]] = []
            for row in stats:
                shape = (str(row["task_class_key"]), str(row["target_id"]), str(row["profile_revision"]), int(row["processors"]))
                mean = row["wall_mean_seconds"]
                stddev = welford_stddev(row["wall_m2_seconds"], int(row["wall_samples"]))
                if mean is None:
                    p95 = None
                    recommendation = None
                else:
                    p95 = float(mean) + 1.645 * float(stddev or 0.0)
                    recommendation = int(math.ceil(1.5 * p95 / 60.0) * 60)
                current_budget = None
                for attempt in completed:
                    if self._attempt_shape(attempt) == shape:
                        current_budget = self._attempt_budget(attempt)
                        break
                proposals.append({
                    "shape": {"task_class_key": shape[0], "target_id": shape[1], "profile_revision": shape[2], "processors": shape[3]},
                    "sample_count": int(row["sample_count"]), "mean": mean, "p95": p95,
                    "current_budget": None if current_budget is None else current_budget,
                    "suggested_max_wall_seconds": recommendation,
                })
        return proposals

    def capacity_counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS n FROM evaluations GROUP BY status").fetchall()
            reconciling = connection.execute("SELECT COUNT(*) FROM attempts WHERE status='reconciling'").fetchone()[0]
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        return {"queued": counts.get("queued", 0), "recovering": counts.get("recovering", 0), "reconciling": int(reconciling)}

    def get_capacity_profile_snapshot(self, relevant_identities: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
        if len(relevant_identities) > MAX_SNAPSHOT_IDENTITIES:
            raise RepositoryError("capacity profile identity set exceeds bound")
        identities = set()
        for identity in relevant_identities:
            if not isinstance(identity, Mapping):
                raise RepositoryError("capacity profile identity is invalid")
            try:
                key = make_task_class(simulation_definition_artifact_id=identity["simulation_definition_artifact_id"], simulation_definition_revision=identity["simulation_definition_revision"], numerical_profile=identity["numerical_profile"], recovery_profile_revision=identity["recovery_profile_revision"], user_class_key=identity.get("user_class_key"))["key"]
                identities.add((key, str(identity["target_id"]).strip(), str(identity["simulation_definition_revision"]).lower()))
            except (KeyError, TypeError, ValueError, ComputeProfileError) as exc:
                raise RepositoryError("capacity profile identity is invalid") from exc
        if not identities:
            return make_capacity_profile_snapshot([])
        where = " OR ".join("(task_class_key=? AND target_id=? AND profile_revision=?)" for _ in identities)
        params = [part for identity in sorted(identities) for part in identity]
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM task_shape_stats WHERE " + where + " ORDER BY task_class_key,target_id,profile_revision,processors", params).fetchall()
        try:
            return make_capacity_profile_snapshot([self._shape_record(row) for row in rows])
        except ComputeProfileError as exc:
            raise RepositoryError("persisted task shape statistics are inconsistent") from exc

    @classmethod
    def _mark_attempt_lost_in_transaction(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        failure_class: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        """Apply the existing lost transition and its Evaluation semantics."""
        attempt_id = str(row["attempt_id"])
        termination_state = (
            "requested"
            if str(row["status"]) in TERMINATION_REQUEST_SOURCE_STATES
            else None
        )
        connection.execute(
            """
            UPDATE attempts
            SET failure_class = ?, termination_state = ?, lease_owner = NULL,
                lease_expires_at = NULL, updated_at = ?
            WHERE attempt_id = ?
            """,
            (failure_class, termination_state, created_at, attempt_id),
        )
        cls._transition_attempt(
            connection,
            attempt_id=attempt_id,
            expected=("reconciling",),
            target="lost",
            event_type="AttemptLost",
            payload=payload,
            created_at=created_at,
        )
        recovery_payload = {
            "attempt_id": attempt_id,
            "failure_class": failure_class,
        }
        for key in ("source", "reason", "proof_seconds", "claimed_at", "age_seconds"):
            if key in payload:
                recovery_payload[key] = payload[key]
        cls._transition_evaluation(
            connection,
            evaluation_id=str(row["evaluation_id"]),
            expected=("running",),
            target="recovering",
            event_type="EvaluationRecovering",
            payload=recovery_payload,
            publish=False,
            created_at=created_at,
        )

    def force_lost_attempt(
        self,
        attempt_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Explicitly terminate one reconciling Attempt at operator request."""
        explanation = str(reason).strip()
        if not explanation:
            raise RepositoryError("force-lost reason is required")
        timestamp = _iso(_utc_now() if now is None else now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            old_status = str(row["status"])
            if old_status != "reconciling":
                raise RepositoryError(
                    f"Attempt {attempt_id} is {old_status}; force-lost requires reconciling"
                )
            released_allocation = None
            if row["allocation_json"] is not None:
                try:
                    allocation = validate_resource_allocation(
                        json.loads(row["allocation_json"])
                    )
                except (json.JSONDecodeError, ContractError, ExecutionOptionError, SchedulingError) as exc:
                    raise RepositoryError("Attempt allocation is invalid") from exc
                released_allocation = {
                    "attempt_id": allocation["attempt_id"],
                    "target_id": allocation["target_id"],
                    "processors": allocation["processors"],
                    "memory_bytes": allocation["memory_bytes"],
                    "resource_key": allocation["resource_key"],
                }
            failure_class = "operator-force-lost"
            self._mark_attempt_lost_in_transaction(
                connection,
                row,
                failure_class=failure_class,
                payload={
                    "reason": explanation,
                    "source": "operator:force-lost",
                    "operator_source": "operator:force-lost",
                    "failure_class": failure_class,
                },
                created_at=timestamp,
            )
            attempt = self.get_attempt(attempt_id, connection=connection)
            evaluation = self.get_evaluation(
                str(row["evaluation_id"]), connection=connection
            )
            return {
                "attempt_id": attempt_id,
                "old_status": old_status,
                "new_status": str(attempt["status"]),
                "released_allocation": released_allocation,
                "evaluation": evaluation,
            }

    def fail_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        failure_class: str,
        artifact_ids: Sequence[str] = (),
        *,
        now: datetime | None = None,
        feedback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        failure = str(failure_class).strip()
        raw_artifacts = [str(item).strip() for item in artifact_ids]
        artifacts = sorted(raw_artifacts)
        if (
            not failure
            or any(not item for item in artifacts)
            or len(artifacts) != len(set(artifacts))
        ):
            raise RepositoryError("failed Attempt requires a failure class and valid artifacts")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["status"] in {"failed", "lost"}:
                same = (
                    row["status"] == "failed"
                    and row["failure_class"] == failure
                    and json.loads(row["artifact_ids_json"]) == artifacts
                )
                if not same:
                    raise RepositoryError("terminal Attempt receipt cannot be overwritten")
                if feedback is not None:
                    if not isinstance(feedback, Mapping) or bool(feedback.get("success")):
                        raise RepositoryError("failed Attempt feedback must be unsuccessful")
                    self._record_attempt_feedback_in_transaction(
                        connection, attempt_id, row, feedback,
                        _iso(_utc_now() if now is None else now),
                    )
                return self._attempt_record(row)
            if row["status"] == "completed":
                raise RepositoryError("terminal Attempt receipt cannot be overwritten")
            self._owned_attempt(connection, attempt_id, worker_id, now=now)
            timestamp = _iso(_utc_now() if now is None else now)
            termination_state = (
                "requested"
                if str(row["status"]) in TERMINATION_REQUEST_SOURCE_STATES
                else None
            )
            connection.execute(
                """
                UPDATE attempts
                SET failure_class = ?, artifact_ids_json = ?, termination_state = ?,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (failure, canonical_json(artifacts), termination_state, timestamp, attempt_id),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("starting", "leased", "running", "collecting", "reconciling"),
                target="failed",
                event_type="AttemptFailed",
                payload={"failure_class": failure, "artifact_ids": artifacts},
                created_at=timestamp,
            )
            self._transition_evaluation(
                connection,
                evaluation_id=str(row["evaluation_id"]),
                expected=("running",),
                target="recovering",
                event_type="EvaluationRecovering",
                payload={"attempt_id": attempt_id, "failure_class": failure},
                publish=False,
                created_at=timestamp,
            )
            if feedback is not None:
                if not isinstance(feedback, Mapping) or bool(feedback.get("success")):
                    raise RepositoryError("failed Attempt feedback must be unsuccessful")
                self._record_attempt_feedback_in_transaction(
                    connection, attempt_id, row, feedback, timestamp
                )
            return self.get_attempt(attempt_id, connection=connection)

    def mark_attempt_reconciling(
        self,
        attempt_id: str,
        worker_id: str,
        artifact_ids: Sequence[str] = (),
        *,
        reason: str = "proxy-session-indeterminate",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        artifacts = sorted(str(item).strip() for item in artifact_ids)
        explanation = str(reason).strip()
        if (
            not explanation
            or any(not item for item in artifacts)
            or len(artifacts) != len(set(artifacts))
        ):
            raise RepositoryError("reconciling Attempt requires a reason and valid artifacts")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["status"] == "reconciling":
                if json.loads(row["artifact_ids_json"]) != artifacts:
                    raise RepositoryError("reconciling Attempt evidence cannot be overwritten")
                return self._attempt_record(row)
            self._owned_attempt(connection, attempt_id, worker_id, now=now)
            if row["status"] not in {"starting", "running", "collecting"}:
                raise RepositoryError("only a started Attempt can require reconciliation")
            timestamp = _iso(_utc_now() if now is None else now)
            connection.execute(
                """
                UPDATE attempts
                SET artifact_ids_json = ?, failure_class = NULL, updated_at = ?
                WHERE attempt_id = ?
                """,
                (canonical_json(artifacts), timestamp, attempt_id),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=(str(row["status"]),),
                target="reconciling",
                event_type="AttemptReconciliationRequired",
                payload={"reason": explanation, "artifact_ids": artifacts},
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def reconcile_attempt(
        self,
        attempt_id: str,
        observer_id: str,
        session_ref: str,
        observed_status: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observer = str(observer_id).strip()
        if not observer:
            raise RepositoryError("observer_id is required")
        remote_status = str(observed_status).strip().lower()
        if remote_status not in {
            "running",
            "completed",
            "absent",
            "unreachable",
            "indeterminate",
            "unknown",
        }:
            raise RepositoryError(
                "observed_status must be running, completed, absent, "
                "unreachable, or indeterminate"
            )
        if remote_status == "unknown":
            remote_status = "indeterminate"
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise RepositoryError("lease_seconds must be a positive integer")
        normalized_session_ref = normalize_token(session_ref, "session_ref")
        current = _utc_now() if now is None else now
        timestamp = _iso(current)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown Attempt: {attempt_id}")
            if row["status"] == "reconciling":
                row = self._owned_attempt(
                    connection, attempt_id, observer, now=current
                )
            if row["session_ref"] != normalized_session_ref:
                raise RepositoryError("remote session identity does not match the Attempt")
            replay_status = {
                "running": "running",
                "completed": "collecting",
                "absent": "lost",
            }.get(remote_status)
            if row["status"] != "reconciling":
                if replay_status == row["status"]:
                    return self._attempt_record(row)
                raise RepositoryError("only a reconciling Attempt can accept an observation")
            if remote_status in {"unreachable", "indeterminate"}:
                # A read-only observation that proves no state change must not
                # refresh the Attempt age or manufacture a transition.
                return self.get_attempt(attempt_id, connection=connection)
            if remote_status == "absent":
                failure = "remote-session-not-found"
                self._mark_attempt_lost_in_transaction(
                    connection,
                    row,
                    failure_class=failure,
                    payload={
                        "observer_id": observer,
                        "session_ref": normalized_session_ref,
                        "failure_class": failure,
                    },
                    created_at=timestamp,
                )
                return self.get_attempt(attempt_id, connection=connection)

            target = "running" if remote_status == "running" else "collecting"
            connection.execute(
                """
                UPDATE attempts
                SET lease_owner = ?, lease_expires_at = ?, failure_class = NULL,
                    updated_at = ?
                WHERE attempt_id = ?
                """,
                (
                    observer,
                    _iso(current + timedelta(seconds=lease_seconds)),
                    timestamp,
                    attempt_id,
                ),
            )
            self._transition_attempt(
                connection,
                attempt_id=attempt_id,
                expected=("reconciling",),
                target=target,
                event_type="AttemptReconciled",
                payload={
                    "observer_id": observer,
                    "session_ref": normalized_session_ref,
                    "observed_status": remote_status,
                },
                created_at=timestamp,
            )
            return self.get_attempt(attempt_id, connection=connection)

    def has_expired_leases(self, *, now: datetime | None = None) -> bool:
        """Cheap read-only hint for whether lease expiry may apply."""
        current = _utc_now() if now is None else now
        if current.tzinfo is None:
            raise RepositoryError("timestamps must be timezone-aware")
        with closing(self._connect()) as connection:
            return connection.execute(
                f"""
                SELECT 1 FROM attempts
                WHERE status IN ({_CAPACITY_HOLDING_ATTEMPT_STATES_SQL})
                  AND lease_expires_at <= ?
                LIMIT 1
                """,
                (_iso(current),),
            ).fetchone() is not None

    def expire_leases(self, *, now: datetime | None = None) -> list[str]:
        if not self.has_expired_leases(now=now):
            return []
        current = _utc_now() if now is None else now
        timestamp = _iso(current)
        expired_ids: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM attempts
                WHERE status IN ({_CAPACITY_HOLDING_ATTEMPT_STATES_SQL})
                  AND lease_expires_at <= ?
                ORDER BY lease_expires_at, attempt_id
                """,
                (timestamp,),
            ).fetchall()
            for row in rows:
                attempt_id = str(row["attempt_id"])
                before_start = str(row["status"]) == "leased"
                if str(row["status"]) == "reconciling":
                    connection.execute(
                        """
                        UPDATE attempts
                        SET lease_owner = NULL, lease_expires_at = NULL
                        WHERE attempt_id = ?
                        """,
                        (attempt_id,),
                    )
                    expired_ids.append(attempt_id)
                    continue
                failure = "worker-lease-expired-before-start" if before_start else None
                connection.execute(
                    """
                    UPDATE attempts
                    SET failure_class = ?, lease_owner = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (failure, timestamp, attempt_id),
                )
                self._transition_attempt(
                    connection,
                    attempt_id=attempt_id,
                    expected=(str(row["status"]),),
                    target="lost" if before_start else "reconciling",
                    event_type=(
                        "AttemptLost" if before_start else "AttemptReconciliationRequired"
                    ),
                    payload={
                        "reason": "worker-lease-expired",
                        "session_ref": row["session_ref"],
                        "failure_class": failure,
                    },
                    created_at=timestamp,
                )
                if before_start:
                    self._transition_evaluation(
                        connection,
                        evaluation_id=str(row["evaluation_id"]),
                        expected=("running",),
                        target="recovering",
                        event_type="EvaluationRecovering",
                        payload={"attempt_id": attempt_id, "failure_class": failure},
                        publish=False,
                        created_at=timestamp,
                    )
                expired_ids.append(attempt_id)
        return expired_ids

    def plan_recovery(
        self,
        evaluation_id: str,
        reason: str,
        *,
        source: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        explanation = str(reason).strip()
        if not explanation:
            raise RepositoryError("recovery reason is required")
        payload: dict[str, Any] = {"reason": explanation}
        if source is not None:
            normalized_source = str(source).strip()
            if not normalized_source:
                raise RepositoryError("recovery source is required")
            payload["source"] = normalized_source
        with self._transaction() as connection:
            self._transition_evaluation(
                connection,
                evaluation_id=evaluation_id,
                expected=("recovering",),
                target="queued",
                event_type="RecoveryPlanned",
                payload=payload,
                created_at=_iso(_utc_now() if now is None else now),
            )
            return self.get_evaluation(evaluation_id, connection=connection)

    def mark_unresolved(self, evaluation_id: str, reason: str) -> dict[str, Any]:
        explanation = str(reason).strip()
        if not explanation:
            raise RepositoryError("unresolved reason is required")
        with self._transaction() as connection:
            self._transition_evaluation(
                connection,
                evaluation_id=evaluation_id,
                expected=("recovering",),
                target="unresolved",
                event_type="EvaluationUnresolved",
                payload={"reason": explanation},
            )
            return self.get_evaluation(evaluation_id, connection=connection)

    def record_qualification(
        self,
        report: Mapping[str, Any],
        observation: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        normalized_report = validate_qualification_report(report)
        normalized_observation = (
            None if observation is None else validate_observation(observation)
        )
        if (normalized_report["status"] == "qualified") != (
            normalized_observation is not None
        ):
            raise RepositoryError("only a qualified report must include an Observation")
        if normalized_observation is not None:
            for key in ("evaluation_id", "candidate_id", "qualifier_revision", "metric_schema_revision"):
                if normalized_observation[key] != normalized_report[key]:
                    raise RepositoryError(f"Observation {key} does not match its report")

        report_json = canonical_json(normalized_report)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT report_json FROM qualification_reports
                WHERE qualification_report_id = ?
                """,
                (normalized_report["qualification_report_id"],),
            ).fetchone()
            if existing is not None:
                if existing["report_json"] != report_json:
                    raise RepositoryError("QualificationReport identity collision")
                if normalized_observation is None:
                    return self.get_evaluation(
                        normalized_report["evaluation_id"], connection=connection
                    )
                stored = self.get_observation(
                    normalized_observation["observation_id"], connection=connection
                )
                if stored != normalized_observation:
                    raise RepositoryError("Qualification replay lacks its Observation")
                return stored

            evaluation = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?",
                (normalized_report["evaluation_id"],),
            ).fetchone()
            if evaluation is None or evaluation["status"] != "qualifying":
                raise RepositoryError("Qualification requires an Evaluation in qualifying state")
            if evaluation["candidate_id"] != normalized_report["candidate_id"]:
                raise RepositoryError("QualificationReport references a different Candidate")
            problem = connection.execute(
                """
                SELECT p.definition_json
                FROM candidates c
                JOIN problem_definitions p
                  ON p.problem_id = c.problem_id AND p.revision = c.problem_revision
                WHERE c.candidate_id = ?
                """,
                (normalized_report["candidate_id"],),
            ).fetchone()
            if problem is None or json.loads(problem["definition_json"])[
                "metric_schema_revision"
            ] != normalized_report["metric_schema_revision"]:
                raise RepositoryError(
                    "QualificationReport metric schema does not match the ProblemDefinition"
                )
            placeholders = ",".join("?" for _ in normalized_report["attempt_ids"])
            attempt_rows = connection.execute(
                f"""
                SELECT attempt_id, status, evaluation_id, artifact_ids_json FROM attempts
                WHERE attempt_id IN ({placeholders})
                """,
                tuple(normalized_report["attempt_ids"]),
            ).fetchall()
            if len(attempt_rows) != len(normalized_report["attempt_ids"]) or any(
                row["evaluation_id"] != normalized_report["evaluation_id"]
                or row["status"] != "completed"
                for row in attempt_rows
            ):
                raise RepositoryError(
                    "QualificationReport Attempts must be completed members of the Evaluation"
                )
            attempt_artifacts = {
                artifact
                for row in attempt_rows
                for artifact in json.loads(row["artifact_ids_json"])
            }
            if not set(normalized_report["evidence_artifact_ids"]).issubset(
                attempt_artifacts
            ):
                raise RepositoryError(
                    "QualificationReport evidence is not attached to its Attempts"
                )
            now = _iso()
            connection.execute(
                """
                INSERT INTO qualification_reports(
                    qualification_report_id, evaluation_id, candidate_id,
                    status, report_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_report["qualification_report_id"],
                    normalized_report["evaluation_id"],
                    normalized_report["candidate_id"],
                    normalized_report["status"],
                    report_json,
                    now,
                ),
            )
            if normalized_observation is not None:
                observation_json = canonical_json(normalized_observation)
                collision = connection.execute(
                    "SELECT observation_json FROM observations WHERE observation_id = ?",
                    (normalized_observation["observation_id"],),
                ).fetchone()
                if collision is None:
                    connection.execute(
                        """
                        INSERT INTO observations(
                            observation_id, evaluation_id, candidate_id,
                            observation_json, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_observation["observation_id"],
                            normalized_observation["evaluation_id"],
                            normalized_observation["candidate_id"],
                            observation_json,
                            now,
                        ),
                    )
                elif collision["observation_json"] != observation_json:
                    raise RepositoryError("Observation identity collision")
                self._transition_evaluation(
                    connection,
                    evaluation_id=normalized_report["evaluation_id"],
                    expected=("qualifying",),
                    target="qualified",
                    event_type="ObservationQualified",
                    payload={"observation_id": normalized_observation["observation_id"]},
                    observation_id=normalized_observation["observation_id"],
                    created_at=now,
                )
                return normalized_observation

            if normalized_report["status"] == "ambiguous":
                target = "ambiguous"
                event_type = "ObservationAmbiguous"
            elif normalized_report["recoverable"]:
                target = "recovering"
                event_type = "QualificationEvidenceGap"
            else:
                target = "unresolved"
                event_type = "EvaluationUnresolved"
            self._transition_evaluation(
                connection,
                evaluation_id=normalized_report["evaluation_id"],
                expected=("qualifying",),
                target=target,
                event_type=event_type,
                payload={
                    "qualification_report_id": normalized_report[
                        "qualification_report_id"
                    ],
                    "issues": normalized_report["issues"],
                },
                created_at=now,
            )
            return self.get_evaluation(
                normalized_report["evaluation_id"], connection=connection
            )

    @staticmethod
    def _owned_attempt(
        connection: sqlite3.Connection,
        attempt_id: str,
        worker_id: str,
        *,
        now: datetime | None,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Attempt: {attempt_id}")
        if row["lease_owner"] != str(worker_id).strip():
            raise RepositoryError("worker does not own the Attempt lease")
        current = _iso(_utc_now() if now is None else now)
        if row["lease_expires_at"] is None or row["lease_expires_at"] <= current:
            raise RepositoryError("Attempt lease has expired")
        return row

    @staticmethod
    def _evaluation_record(row: sqlite3.Row) -> dict[str, Any]:
        request = json.loads(row["request_json"])
        return {
            **request,
            "status": str(row["status"]),
            "observation_id": row["observation_id"],
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> dict[str, Any]:
        base = {
            "contract_version": 1,
            "attempt_id": str(row["attempt_id"]),
            "evaluation_id": str(row["evaluation_id"]),
            "attempt_number": int(row["attempt_number"]),
            "simulation_adapter": str(row["simulation_adapter"]),
            "numerical_profile": str(row["numerical_profile"]),
            "checkpoint_parent_attempt_id": row["checkpoint_parent_attempt_id"],
            "status": str(row["status"]),
            "termination_state": row["termination_state"],
            "failure_class": row["failure_class"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
        }
        validate_attempt(base)
        return {
            **base,
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "session_ref": row["session_ref"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "execution_preparation_id": row["execution_preparation_id"],
            "execution_preparation": (
                None
                if row["execution_preparation_json"] is None
                else json.loads(row["execution_preparation_json"])
            ),
            "selected_execution_option_id": row[
                "selected_execution_option_id"
            ],
            "execution_plan_id": row["execution_plan_id"],
            "execution_plan": (
                None
                if row["execution_plan_json"] is None
                else json.loads(row["execution_plan_json"])
            ),
            "allocation": (
                None
                if row["allocation_json"] is None
                else json.loads(row["allocation_json"])
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT candidate_json FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            return None if row is None else json.loads(row["candidate_json"])

    def get_evaluation(
        self, evaluation_id: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
        else:
            with closing(self._connect()) as local:
                row = local.execute(
                    "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
                ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
        return self._evaluation_record(row)

    def get_evaluation_input(self, evaluation_id: str) -> dict[str, Any]:
        """Return one immutable problem/candidate/Evaluation read model."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    p.definition_json,
                    c.candidate_json,
                    e.candidate_id AS indexed_candidate_id,
                    e.request_json,
                    e.status,
                    e.observation_id,
                    e.created_at,
                    e.updated_at
                FROM evaluations e
                JOIN candidates c ON c.candidate_id = e.candidate_id
                JOIN problem_definitions p
                  ON p.problem_id = c.problem_id AND p.revision = c.problem_revision
                WHERE e.evaluation_id = ?
                """,
                (evaluation_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
        try:
            problem = validate_problem_definition(json.loads(row["definition_json"]))
            candidate = validate_candidate(json.loads(row["candidate_json"]))
            request = validate_evaluation_request(json.loads(row["request_json"]))
        except (ContractError, json.JSONDecodeError) as exc:
            raise RepositoryError("evaluation input contains invalid persisted JSON") from exc
        if (
            candidate["candidate_id"] != row["indexed_candidate_id"]
            or candidate["problem_id"] != problem["problem_id"]
            or candidate["problem_revision"] != problem["revision"]
            or request["candidate_id"] != candidate["candidate_id"]
        ):
            raise RepositoryError("evaluation input lineage is inconsistent")
        return {
            "problem": problem,
            "candidate": candidate,
            "evaluation": {
                **request,
                "status": str(row["status"]),
                "observation_id": row["observation_id"],
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            },
        }

    def get_attempt(
        self, attempt_id: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        else:
            with closing(self._connect()) as local:
                row = local.execute(
                    "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
                ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Attempt: {attempt_id}")
        return self._attempt_record(row)

    def list_evaluation_attempts(
        self, evaluation_id: str
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
            if exists is None:
                raise RepositoryError(f"unknown Evaluation: {evaluation_id}")
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE evaluation_id = ?
                ORDER BY attempt_number, created_at, attempt_id
                """,
                (evaluation_id,),
            ).fetchall()
        return [self._attempt_record(row) for row in rows]

    def list_attempt_ids(self) -> list[str]:
        """Return the complete Attempt identity set without execution details."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_id"
            ).fetchall()
        return [str(row["attempt_id"]) for row in rows]

    def get_observation(
        self, observation_id: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if connection is not None:
            row = connection.execute(
                "SELECT observation_json FROM observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        else:
            with closing(self._connect()) as local:
                row = local.execute(
                    "SELECT observation_json FROM observations WHERE observation_id = ?",
                    (observation_id,),
                ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Observation: {observation_id}")
        return json.loads(row["observation_json"])

    def get_qualified_sample(self, observation_id: str) -> dict[str, Any]:
        """Return one immutable problem/candidate/Observation read model."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT p.definition_json, c.candidate_json, o.observation_json
                FROM observations o
                JOIN candidates c ON c.candidate_id = o.candidate_id
                JOIN problem_definitions p
                  ON p.problem_id = c.problem_id AND p.revision = c.problem_revision
                WHERE o.observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown Observation: {observation_id}")
        try:
            problem = validate_problem_definition(json.loads(row["definition_json"]))
            candidate = validate_candidate(json.loads(row["candidate_json"]))
            observation = validate_observation(json.loads(row["observation_json"]))
        except (ContractError, json.JSONDecodeError) as exc:
            raise RepositoryError("qualified sample contains invalid persisted JSON") from exc
        if (
            candidate["problem_id"] != problem["problem_id"]
            or candidate["problem_revision"] != problem["revision"]
            or observation["candidate_id"] != candidate["candidate_id"]
            or observation["metric_schema_revision"]
            != problem["metric_schema_revision"]
        ):
            raise RepositoryError("qualified sample lineage is inconsistent")
        return {
            "problem": problem,
            "candidate": candidate,
            "observation": observation,
        }

    @staticmethod
    def _validate_algorithm_observations(
        connection: sqlite3.Connection,
        observation_ids: Sequence[str],
        *,
        problem_id: str,
        problem_revision: str,
        label: str,
    ) -> None:
        if not observation_ids:
            return
        placeholders = ",".join("?" for _ in observation_ids)
        rows = connection.execute(
            f"""
            SELECT o.observation_id, c.problem_id, c.problem_revision
            FROM observations o
            JOIN candidates c ON c.candidate_id = o.candidate_id
            WHERE o.observation_id IN ({placeholders})
            """,
            tuple(observation_ids),
        ).fetchall()
        if len(rows) != len(observation_ids):
            raise RepositoryError(f"{label} references an unknown Observation")
        if any(
            row["problem_id"] != problem_id
            or row["problem_revision"] != problem_revision
            for row in rows
        ):
            raise RepositoryError(
                f"{label} Observation belongs to a different problem"
            )

    @staticmethod
    def _algorithm_run_record(row: sqlite3.Row) -> dict[str, Any]:
        archive = (
            {
                "bundle_revision": str(row["archive_bundle_revision"]),
                "artifact_id": str(row["archive_artifact_id"]),
                "revision": str(row["archive_revision"]),
                "archived_at": str(row["archived_at"]),
            }
            if row["archive_artifact_id"] is not None
            else None
        )
        return {
            **validate_algorithm_run(json.loads(row["run_json"])),
            "status": str(row["status"]),
            "terminal_status": (
                str(row["terminal_status"])
                if row["terminal_status"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "completed_at": (
                str(row["completed_at"]) if row["completed_at"] is not None else None
            ),
            "archive": archive,
        }

    def register_algorithm_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_algorithm_run(run)
        run_json = canonical_json(normalized)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (normalized["algorithm_run_id"],),
            ).fetchone()
            if existing is not None:
                if existing["run_json"] != run_json:
                    raise RepositoryError("AlgorithmRun identity collision")
                return self._algorithm_run_record(existing)
            problem = connection.execute(
                """
                SELECT 1 FROM problem_definitions
                WHERE problem_id = ? AND revision = ?
                """,
                (normalized["problem_id"], normalized["problem_revision"]),
            ).fetchone()
            if problem is None:
                raise RepositoryError(
                    "AlgorithmRun references an unregistered ProblemDefinition"
                )
            created_at = _iso()
            connection.execute(
                """
                INSERT INTO algorithm_runs(
                    algorithm_run_id, algorithm_id, algorithm_revision,
                    problem_id, problem_revision, run_json, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    normalized["algorithm_run_id"],
                    normalized["algorithm_id"],
                    normalized["algorithm_revision"],
                    normalized["problem_id"],
                    normalized["problem_revision"],
                    run_json,
                    created_at,
                    created_at,
                ),
            )
            self._state_event(
                connection,
                aggregate_type="algorithm-run",
                aggregate_id=normalized["algorithm_run_id"],
                from_status=None,
                to_status="active",
                event_type="AlgorithmRunRegistered",
                payload={"configuration_revision": normalized["configuration_revision"]},
                created_at=created_at,
            )
            row = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (normalized["algorithm_run_id"],),
            ).fetchone()
            assert row is not None
            return self._algorithm_run_record(row)

    def list_algorithm_runs(self) -> list[dict[str, Any]]:
        """Return all algorithm run records ordered by algorithm_run_id."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM algorithm_runs ORDER BY algorithm_run_id"
            ).fetchall()
            return [self._algorithm_run_record(row) for row in rows]

    def get_algorithm_run(self, algorithm_run_id: str) -> dict[str, Any]:
        run_id = normalize_token(algorithm_run_id, "algorithm_run_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown AlgorithmRun: {run_id}")
        return self._algorithm_run_record(row)

    @staticmethod
    def _algorithm_event_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **validate_algorithm_event(json.loads(row["event_json"])),
            "sequence": int(row["sequence"]),
            "created_at": str(row["created_at"]),
        }

    def record_algorithm_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_algorithm_event(event)
        event_json = canonical_json(normalized)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT event_json, sequence, created_at
                FROM algorithm_events WHERE algorithm_event_id = ?
                """,
                (normalized["algorithm_event_id"],),
            ).fetchone()
            if existing is not None:
                if existing["event_json"] != event_json:
                    raise RepositoryError("AlgorithmEvent identity collision")
                return self._algorithm_event_record(existing)
            same_key = connection.execute(
                """
                SELECT algorithm_event_id FROM algorithm_events
                WHERE algorithm_run_id = ? AND event_key = ?
                """,
                (normalized["algorithm_run_id"], normalized["event_key"]),
            ).fetchone()
            if same_key is not None:
                raise RepositoryError("AlgorithmEvent event_key collision")
            run = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (normalized["algorithm_run_id"],),
            ).fetchone()
            if run is None:
                raise RepositoryError(
                    "AlgorithmEvent references an unregistered AlgorithmRun"
                )
            current_status = str(run["status"])
            if current_status == "archived":
                raise RepositoryError("AlgorithmRun is archived")
            if current_status in {"completed", "blocked", "failed"}:
                raise RepositoryError(
                    "AlgorithmRun is terminal; only exact event replay is allowed"
                )
            self._validate_algorithm_observations(
                connection,
                normalized["input_observation_ids"],
                problem_id=str(run["problem_id"]),
                problem_revision=str(run["problem_revision"]),
                label="AlgorithmEvent",
            )
            next_status = normalized["run_status"]
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM algorithm_events WHERE algorithm_run_id = ?
                    """,
                    (normalized["algorithm_run_id"],),
                ).fetchone()[0]
            )
            created_at = _iso()
            connection.execute(
                """
                INSERT INTO algorithm_events(
                    algorithm_event_id, algorithm_run_id, sequence, event_key,
                    event_type, run_status, input_observation_ids_json,
                    artifact_ids_json, event_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["algorithm_event_id"],
                    normalized["algorithm_run_id"],
                    sequence,
                    normalized["event_key"],
                    normalized["event_type"],
                    normalized["run_status"],
                    canonical_json(normalized["input_observation_ids"]),
                    canonical_json(normalized["artifact_ids"]),
                    event_json,
                    created_at,
                ),
            )
            completed_at = run["completed_at"]
            terminal_status = run["terminal_status"]
            if next_status in {"completed", "blocked", "failed"}:
                terminal_status = next_status
                completed_at = completed_at or created_at
            else:
                terminal_status = None
                completed_at = None
            connection.execute(
                """
                UPDATE algorithm_runs
                SET status = ?, terminal_status = ?, updated_at = ?, completed_at = ?
                WHERE algorithm_run_id = ?
                """,
                (
                    next_status,
                    terminal_status,
                    created_at,
                    completed_at,
                    normalized["algorithm_run_id"],
                ),
            )
            self._state_event(
                connection,
                aggregate_type="algorithm-run",
                aggregate_id=normalized["algorithm_run_id"],
                from_status=current_status,
                to_status=next_status,
                event_type="AlgorithmEventRecorded",
                payload={
                    "algorithm_event_id": normalized["algorithm_event_id"],
                    "sequence": sequence,
                    "event_type": normalized["event_type"],
                },
                created_at=created_at,
            )
            return {
                **normalized,
                "sequence": sequence,
                "created_at": created_at,
            }

    def list_algorithm_events(
        self, algorithm_run_id: str
    ) -> list[dict[str, Any]]:
        run_id = normalize_token(algorithm_run_id, "algorithm_run_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT event_json, sequence, created_at
                FROM algorithm_events
                WHERE algorithm_run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [self._algorithm_event_record(row) for row in rows]

    @staticmethod
    def _algorithm_result_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            **validate_algorithm_result(json.loads(row["result_json"])),
            "sequence": int(row["sequence"]),
            "created_at": str(row["created_at"]),
        }

    def record_algorithm_result(
        self, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Append one idempotent algorithm result with Observation lineage."""

        normalized = validate_algorithm_result(result)
        result_json = canonical_json(normalized)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT result_json, sequence, created_at
                FROM algorithm_results WHERE algorithm_result_id = ?
                """,
                (normalized["algorithm_result_id"],),
            ).fetchone()
            if existing is not None:
                if existing["result_json"] != result_json:
                    raise RepositoryError("AlgorithmResult identity collision")
                return self._algorithm_result_record(existing)

            run = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (normalized["algorithm_run_id"],),
            ).fetchone()
            if run is None:
                raise RepositoryError(
                    "AlgorithmResult references an unregistered AlgorithmRun"
                )
            if run["status"] == "archived":
                raise RepositoryError("AlgorithmRun is archived")
            if run["status"] in {"completed", "blocked", "failed"}:
                terminal_event = connection.execute(
                    """
                    SELECT event_json FROM algorithm_events
                    WHERE algorithm_run_id = ? ORDER BY sequence DESC LIMIT 1
                    """,
                    (normalized["algorithm_run_id"],),
                ).fetchone()
                if terminal_event is not None:
                    event_payload = json.loads(terminal_event["event_json"]).get(
                        "payload", {}
                    )
                    if (
                        isinstance(event_payload, Mapping)
                        and event_payload.get("terminal_result_policy")
                        == "forbidden"
                    ):
                        raise RepositoryError(
                            "AlgorithmRun terminal event forbids results"
                        )
            if (
                run["algorithm_id"] != normalized["algorithm_id"]
                or run["algorithm_revision"] != normalized["algorithm_revision"]
                or run["problem_id"] != normalized["problem_id"]
                or run["problem_revision"] != normalized["problem_revision"]
            ):
                raise RepositoryError(
                    "AlgorithmResult lineage differs from its AlgorithmRun"
                )

            observation_ids = normalized["input_observation_ids"]
            self._validate_algorithm_observations(
                connection,
                observation_ids,
                problem_id=normalized["problem_id"],
                problem_revision=normalized["problem_revision"],
                label="AlgorithmResult",
            )

            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM algorithm_results WHERE algorithm_run_id = ?
                    """,
                    (normalized["algorithm_run_id"],),
                ).fetchone()[0]
            )
            created_at = _iso()
            connection.execute(
                """
                INSERT INTO algorithm_results(
                    algorithm_result_id, algorithm_run_id, sequence,
                    algorithm_id, algorithm_revision, problem_id,
                    problem_revision, result_type, input_observation_ids_json,
                    result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["algorithm_result_id"],
                    normalized["algorithm_run_id"],
                    sequence,
                    normalized["algorithm_id"],
                    normalized["algorithm_revision"],
                    normalized["problem_id"],
                    normalized["problem_revision"],
                    normalized["result_type"],
                    canonical_json(observation_ids),
                    result_json,
                    created_at,
                ),
            )
            self._state_event(
                connection,
                aggregate_type="algorithm-result",
                aggregate_id=normalized["algorithm_result_id"],
                from_status=None,
                to_status="recorded",
                event_type="AlgorithmResultRecorded",
                payload={
                    "algorithm_run_id": normalized["algorithm_run_id"],
                    "sequence": sequence,
                    "result_type": normalized["result_type"],
                    "input_observation_ids": observation_ids,
                },
                created_at=created_at,
            )
            return {
                **normalized,
                "sequence": sequence,
                "created_at": created_at,
            }

    def get_algorithm_result(self, algorithm_result_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT result_json, sequence, created_at
                FROM algorithm_results WHERE algorithm_result_id = ?
                """,
                (algorithm_result_id,),
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown AlgorithmResult: {algorithm_result_id}")
        return self._algorithm_result_record(row)

    def list_algorithm_results(
        self, algorithm_run_id: str
    ) -> list[dict[str, Any]]:
        run_id = normalize_token(algorithm_run_id, "algorithm_run_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT result_json, sequence, created_at
                FROM algorithm_results
                WHERE algorithm_run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        return [self._algorithm_result_record(row) for row in rows]

    def _algorithm_run_bundle(
        self, connection: sqlite3.Connection, algorithm_run_id: str
    ) -> dict[str, Any]:
        run = connection.execute(
            "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
            (algorithm_run_id,),
        ).fetchone()
        if run is None:
            raise RepositoryError(f"unknown AlgorithmRun: {algorithm_run_id}")
        event_rows = connection.execute(
            """
            SELECT event_json, sequence, created_at
            FROM algorithm_events
            WHERE algorithm_run_id = ? ORDER BY sequence
            """,
            (algorithm_run_id,),
        ).fetchall()
        result_rows = connection.execute(
            """
            SELECT result_json, sequence, created_at
            FROM algorithm_results
            WHERE algorithm_run_id = ? ORDER BY sequence
            """,
            (algorithm_run_id,),
        ).fetchall()
        body = {
            "schema_version": 1,
            "bundle_kind": "algorithm-run-archive",
            "algorithm_run": validate_algorithm_run(json.loads(run["run_json"])),
            "terminal_status": (
                str(run["terminal_status"])
                if run["terminal_status"] is not None
                else None
            ),
            "created_at": str(run["created_at"]),
            "completed_at": (
                str(run["completed_at"]) if run["completed_at"] is not None else None
            ),
            "events": [
                self._algorithm_event_record(row) for row in event_rows
            ],
            "results": [
                self._algorithm_result_record(row) for row in result_rows
            ],
        }
        digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        return {**body, "bundle_revision": f"sha256:{digest}"}

    def export_algorithm_run(self, algorithm_run_id: str) -> dict[str, Any]:
        """Return a deterministic, read-only archive bundle for one whole run."""

        run_id = normalize_token(algorithm_run_id, "algorithm_run_id")
        with closing(self._connect()) as connection:
            return self._algorithm_run_bundle(connection, run_id)

    def archive_algorithm_run(
        self,
        algorithm_run_id: str,
        *,
        bundle_revision: str,
        archive_artifact_id: str,
        archive_revision: str,
    ) -> dict[str, Any]:
        """Mark a terminal run archived after its exact bundle is governed."""

        run_id = normalize_token(algorithm_run_id, "algorithm_run_id")
        artifact_id = normalize_token(archive_artifact_id, "archive_artifact_id")
        bundle = str(bundle_revision).strip().lower()
        revision = str(archive_revision).strip().lower()
        if not _SHA256_REVISION.fullmatch(bundle):
            raise RepositoryError("bundle_revision must be a SHA-256 revision")
        if not _SHA256_REVISION.fullmatch(revision):
            raise RepositoryError("archive_revision must be a SHA-256 revision")
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RepositoryError(f"unknown AlgorithmRun: {run_id}")
            if run["status"] == "archived":
                if (
                    run["archive_bundle_revision"] == bundle
                    and run["archive_artifact_id"] == artifact_id
                    and run["archive_revision"] == revision
                ):
                    return self._algorithm_run_record(run)
                raise RepositoryError("AlgorithmRun has a different archive binding")
            if run["terminal_status"] not in {"completed", "blocked", "failed"}:
                raise RepositoryError("AlgorithmRun must be terminal before archival")
            expected = self._algorithm_run_bundle(connection, run_id)
            if expected["bundle_revision"] != bundle:
                raise RepositoryError(
                    "algorithm run changed after the archive bundle was exported"
                )
            archived_at = _iso()
            connection.execute(
                """
                UPDATE algorithm_runs
                SET status = 'archived', archive_bundle_revision = ?,
                    archive_artifact_id = ?, archive_revision = ?,
                    archived_at = ?, updated_at = ?
                WHERE algorithm_run_id = ?
                """,
                (
                    bundle,
                    artifact_id,
                    revision,
                    archived_at,
                    archived_at,
                    run_id,
                ),
            )
            self._state_event(
                connection,
                aggregate_type="algorithm-run",
                aggregate_id=run_id,
                from_status=str(run["status"]),
                to_status="archived",
                event_type="AlgorithmRunArchived",
                payload={
                    "bundle_revision": bundle,
                    "archive_artifact_id": artifact_id,
                    "archive_revision": revision,
                },
                created_at=archived_at,
            )
            archived = connection.execute(
                "SELECT * FROM algorithm_runs WHERE algorithm_run_id = ?",
                (run_id,),
            ).fetchone()
            assert archived is not None
            return self._algorithm_run_record(archived)

    def state_events(self, aggregate_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM state_events
                WHERE aggregate_id = ? ORDER BY sequence
                """,
                (aggregate_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def read_outbox(self, *, unpublished_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE published_at IS NULL" if unpublished_only else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM outbox_events {where} ORDER BY sequence"
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]

    def mark_outbox_published(
        self, event_id: str, *, now: datetime | None = None
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT published_at FROM outbox_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown outbox event: {event_id}")
            if row["published_at"] is None:
                connection.execute(
                    "UPDATE outbox_events SET published_at = ? WHERE event_id = ?",
                    (_iso(_utc_now() if now is None else now), event_id),
                )

    def journal_mode(self) -> str:
        with closing(self._connect()) as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
