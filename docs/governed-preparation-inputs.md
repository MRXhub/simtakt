# Preparation-Phase Inputs and Failure Rules

This document describes the **inputs** the governed runtime's preparation phase
reads and the **rules** it applies when those inputs are missing or unusable.
It replaces the earlier "Governed Preparation Inputs Specification", which
described a `PROJECT_STATE.json` / execution-authorization envelope that the
current preparation path no longer reads.

Preparation turns one **queued** evaluation into one **immutable execution
preparation** with no caller-provided resource input and no `PROJECT_STATE.json`
read. It is a lazy, bounded queued phase (`preparation_phase.PreparationPhase`)
whose choices are sealed by policy-derived governance
(`governed_preparation.validate_policy_derived_execution_preparation`). Every
technical claim below is grounded in the referenced source.

## The three project files

A governed runtime is assembled from three declarations under `project/`
(`runtime/composition.compose_runtime`). Preparation consumes the same files.

### `project/RUNTIME_COMPONENTS.json` — runtime assembly and policy binding

- `schema_version` must equal `1` (`composition._read_components`).
- `components` is an object or array of entries. Every entry must carry
  `name`, `module`, `factory`, and an integer `interface_version`. The
  assembly **requires** one component named `worker` and one named
  `resource_monitor`; each factory object must expose the corresponding
  session/resource methods (`_read_components`, `compose_runtime`). A
  `dispatcher_id` may be supplied to fix the dispatcher identity.
- Top-level `scheduling_policy` is a `{ "artifact_id", "revision" }` reference
  to the active scheduling-policy artifact. It is the **current policy binding**
  (`scheduling_policy.resolve_governed_scheduling_policy`): the artifact must be
  a registered `configuration` artifact (`expected_kind="configuration"`) at an
  exact sha256 file revision, validated as a `SchedulingPolicy`. The provenance
  field `project_state_revision` is retained for wire/on-disk compatibility and
  now carries the sha256 of the components file that binds the policy
  (`resolve_governed_scheduling_policy`).

### `project/SIMULATION_ADAPTERS.json` — adapter catalog

- `catalog_id` must equal `"simulation-adapters"` and `schema_version` must be
  `1` (`simulation/adapter_catalog.load_catalog`).
- Each `adapters[]` entry **requires** `adapter_id`, `status`, `module`,
  `factory`, integer `interface_version`, non-empty `capabilities` (unique
  strings), and `resource_defaults`.
- `resource_defaults` must include **`processors`**, **`memory_bytes`**, and
  **`max_wall_seconds`** (finite non-negative numbers).
- `status` must be `active`, `experimental`, or `disabled`. Only `active` /
  `experimental` adapters are candidates for scheduling; `disabled` entries are
  never selected (`resolve_adapter_for_problem`).
- `simulation_definition` is **optional**. When an entry does not declare an
  explicit simulation-definition artifact, the runtime binds the option to a
  derived, content-addressed identity
  `configuration.adapter.<adapter_id>` at the sha256 of the canonical catalog
  entry JSON (`simulation_definition_identity`), which governance verifies
  against the loaded catalog rather than resolving a workspace file.

### `project/EXECUTION_TARGETS.json` — execution targets

- `targets[]` is the target list (`execution_topology.ProjectFileTargetCatalog`).
  Each entry carries `target_id`, `status`, and a boolean `formal_execution`;
  optional `host_id` and `license_pool_id` default to "none" and the single
  default license pool respectively.
- Only targets that are `status == "active"` **and** `formal_execution == true`
  participate in scheduling (`parse_execution_topology`). A missing `host_id`
  is retained as `None` and is never inferred to be a private host.

## What a user must provide (beyond the simulator adapter)

Before a queued evaluation can be prepared, a user must supply the following
under `project/` and the workspace artifact registry:

1. A runtime **worker** and a **resource_monitor** component in
   `RUNTIME_COMPONENTS.json` (required to assemble a runnable runtime at all).
2. At least one **formal execution target** in `EXECUTION_TARGETS.json`; when
   more than one formal target is declared, each must have a `host_id` and the
   resource monitor must expose a global **`locked_dispatch`**.
3. An **active input-package artifact** in the artifact registry
   (`records/artifacts/<id>.json`, kind `input-package`, at a resolvable exact
   revision), referenced by the ParameterSchema `source_package`.
4. A registered **ParameterSchema** whose `source_package` points at that exact
   revision (`preparation_phase._make` reads `schema.source_package`).
5. An **active SchedulingPolicy artifact** referenced by
   `RUNTIME_COMPONENTS.json.scheduling_policy` (a `configuration` artifact at an
   exact file-hash revision).
6. A **problem** whose `parameter_schema_revision` matches the schema and whose
   `simulation_capabilities` is covered by exactly one runnable adapter.

## Data path: template → adapter → plan

Preparation is adapter/policy-driven, with the schema's `source_package` as the
auditable input baseline (`preparation_phase._make`):

1. From a claimed queued evaluation it loads the problem/candidate and the
   registered ParameterSchema.
2. It resolves the adapter whose capabilities cover the problem's
   `simulation_capabilities` (`resolve_adapter_for_problem`).
3. It reads the ParameterSchema `source_package` (`artifact_id` + exact
   `revision`) and verifies the workspace artifact resolves with
   `expected_kind="input-package"` (`workspace_artifacts.resolve_workspace_artifact`).
4. It picks a runnable active formal target whose processors at least meet the
   adapter's `resource_defaults.processors` (falling back to the adapter's
   declared `target_id` or `"default"`).
5. It takes the adapter's `resource_defaults` as the option's
   `processors`/`memory_bytes` and derives the `simulation_definition` (explicit
   catalog artifact or content identity). The preparation's wall-time budget
   `max_wall_seconds` (and `command_timeout_seconds`) comes from
   `resource_defaults.max_wall_seconds`. With no measured evidence yet the
   performance profile is uncalibrated and the scheduler falls back to these
   adapter defaults.
6. The result is one **immutable execution preparation** (option set,
   performance-profile snapshot, budget) committed under the claim.

`materialize_package` is **not** part of this path: it is an adapter behavior
the worker runs at `start_session`, writing a package into its own configured
directory. Its product is **not** auto-registered as an artifact; the auditable
input baseline remains the schema-referenced `input-package` artifact at its
exact preparation-bound revision. (`pkg:`/`pkg.` identifiers are a
compatibility namespace used when listing landed packages — e.g. scanning legacy
`data/inputs/packages` synthesizes `pkg:<package_name>` when a manifest carries
no `artifact_id` (`evaluation/service.py`). They are not the recommended or
required identifier of a governed source package.)

## Failure disposition rules

One malformed or unavailable input must not abort a runtime round.

- **Preparation is a lazy queue phase.** `PreparationPhase` claims queued
  evaluations within a bounded window
  (`window_limit`/`lookahead`, lease-bounded). If any of package / adapter /
  schema / target is unusable, the claim is released
  (`release_preparation_claim`) and **that** evaluation is marked `unresolved`
  with a stored reason; the round continues (`preparation_phase.prepare_once`).
- **`unreachable` / `indeterminate` are not `absent`.** Session start and
  observation outcomes keep these as distinct classes
  (`simulation/worker.SESSION_START_OUTCOMES` / `SESSION_OBSERVATIONS`); they are
  never collapsed into `absent`.
- **Observe before wall-proof recovery.** The dispatcher observes a reconciling
  session before applying wall-budget recovery (`dispatcher.recover_once`,
  `service.auto_release_wall_budget`), which uses the persisted immutable budget
  (`max_wall_seconds`, `command_timeout_seconds`) — not a fabricated clock.
- **Termination is only confirmed when the worker says so.** A pending session
  is confirmed terminated only when the worker returns `terminated` or
  `absent`; a worker/monitor lacking `terminate_session` reports termination as
  unconfirmed (the composition summary labels it accordingly).
- Recovery paths requeue a `recovering` evaluation back to `queued`, and
  deterministic/budget failures transition it to `unresolved` with evidence.

## Duration reporting path

Measured solve durations reach the control plane through worker collection:

- Each worker-collected `SolverRunRecord` carries optional `wall_seconds`,
  `cpu_seconds`, and `peak_rss_bytes`
  (`simulation/session_contracts.make_solver_run_record`); missing values remain
  `None`.
- Completion feedback writes these into the `attempt_feedback` row; across
  multiple solver records, wall and cpu are summed and RSS takes the maximum
  (`evaluation/service.py` aggregation; `sqlite_evaluation_repository`
  `attempt_feedback` table).
- This feedback feeds the per-task compute profile
  (`evaluation/compute_profile.py`), which the scheduler uses for capacity
  estimation. The measurements come from the worker's own record — the attempt
  lifecycle clock is not used to estimate solve duration.

## Deployment guards

A runtime refuses to assemble, and preparation refuses to schedule, rather than
silently degrade:

- Missing/malformed `RUNTIME_COMPONENTS.json`, a missing `worker` /
  `resource_monitor`, or a missing required component method aborts assembly
  (`composition`).
- More than one formal target with any missing `host_id` is fail-closed
  (`execution_topology.ensure_formal_targets_ready`); a multi-target resource
  monitor without `locked_dispatch` rejects composition/dispatch
  (`composition`, `prepared_dispatcher`).
- A `disabled` adapter, a problem with **no** unique capability match, a
  policy revision that does not resolve as an exact active artifact, capacity
  overruns, and non-active/non-formal execution targets are never implicitly
  scheduled.

## Field-to-code references

- Runtime assembly and required components — `runtime/composition.py`.
- Policy binding from `RUNTIME_COMPONENTS.json.scheduling_policy` —
  `evaluation/scheduling_policy.py:resolve_governed_scheduling_policy`.
- Adapter catalog required fields / `resource_defaults` /
  `simulation_definition` identity — `simulation/adapter_catalog.py`.
- Lazy preparation and per-evaluation failure isolation —
  `evaluation/preparation_phase.py`.
- Policy-derived governance sealing the choices —
  `evaluation/governed_preparation.py:validate_policy_derived_execution_preparation`.
- Source-package artifact resolution —
  `core/workspace_artifacts.py:resolve_workspace_artifact`.
- Execution-target topology and readiness —
  `evaluation/execution_topology.py`.
- Worker/session outcome classes — `simulation/worker.py`.
- Solver-run duration records and feedback — `simulation/session_contracts.py`,
  `evaluation/service.py`, `evaluation/compute_profile.py`.
