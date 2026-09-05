# Architecture

## Layered responsibilities

The control plane is organized into layers with explicit boundaries:

- **Core** defines evaluation contracts and dependency-inversion interfaces. It
  contains the data shapes and validation rules shared by every deployment.
- **Evaluation** turns requests into governed preparation plans, ranks work,
  dispatches selected plans, and records lifecycle state. Preparation, the
  scheduling policy, resource observations, and durable receipts live in this
  layer.
- **Data** provides durable evaluation and study repositories. Repository
  implementations may use SQLite or another store while preserving the core
  contracts.
- **Simulation** owns the runtime-neutral worker, gateway, adapter, and session
  protocols. It is the only layer that needs to know how a particular simulation
  runtime is started, observed, recovered, and collected.
- **Web** presents the research workspace and monitoring views. Read-only access
  is the default; `--allow-writes` enables model import, template registration,
  study creation and evaluation submission through the service APIs. Scheduling
  and session execution run in the separate runtime process.

Practical setup: [Docker deployment](DOCKER.md), [SSH endpoints](SSH_SIMULATION.md),
and [adapter integration](ADAPTERS.md). These setup guides are currently in Chinese.

## Runtime assembly from project files

A governed runtime is built by `control_plane.runtime.composition.compose_runtime`
reading three declarations under `project/` — it is **not** wired by
hand-constructing and injecting a set of ports into a service:

- **`project/RUNTIME_COMPONENTS.json`** is the mandatory assembly document
  (`composition.py`). Its `components` must name both a **worker** and a
  **resource_monitor** entry, each with `module`, `factory`, and integer
  `interface_version`; the factory object must expose the required session /
  resource methods. The document's top-level `scheduling_policy`
  (`artifact_id` + `revision`) is the current binding for the active scheduling
  policy artifact (`scheduling_policy.resolve_governed_scheduling_policy`).
- **`project/SIMULATION_ADAPTERS.json`** registers concrete simulator adapters
  (see below). Preparation selects one adapter per problem by capability.
- **`project/EXECUTION_TARGETS.json`** declares the execution targets. Only
  targets that are `active` and `formal_execution: true` participate in
  scheduling (`execution_topology.parse_execution_topology`).

Composition fails closed when any of these guarantees is violated (missing or
malformed components file, required worker/monitor absent, required methods
missing, or — see Deployment guards — a multi-target monitor that cannot provide
a global dispatch lock).

## Preparation does not read PROJECT_STATE

Preparation is governed from the project declarations above, not from a
`PROJECT_STATE.json` envelope. `preparation_phase.PreparationPhase` resolves the
source package from the registered ParameterSchema's `source_package`
(`preparation_phase._make`), and the policy binding comes from
`RUNTIME_COMPONENTS.json.scheduling_policy`. Governance
(`governed_preparation.validate_policy_derived_execution_preparation`) seals the
same choices. The `ControlStore` port and its `PROJECT_STATE.json` reader remain
only for legacy project-level calibration-admission paths; ordinary runtime
preparation never reads it. Full field-by-field detail is in
[`docs/governed-preparation-inputs.md`](governed-preparation-inputs.md).

## Dependency-inversion ports

`control_plane/core/ports.py` defines replaceable storage/observation
boundaries that decouple the control plane from a host project's file layout:

1. **ControlStore** reads governed project control state.
2. **ArtifactStore** resolves a registered artifact record by identity.
3. **TargetCatalog** reads the execution-target catalog.
4. **ResourceMonitor** provides a locked, read-only resource snapshot for a
   scheduling decision, records decision receipts, and — for multi-target
   dispatch — a cross-target `locked_dispatch` lock.

File-backed reference implementations are provided for these ports and can be
swapped for your own storage. Replacing a port means providing an object that
satisfies the corresponding Python `Protocol` and constructing it in the
application composition root. (Note: runtime *components* such as the worker and
resource monitor are themselves concrete instances built from
`RUNTIME_COMPONENTS.json`, and simulator adapters are catalog registrations,
not storage ports.)

## Adding an adapter

Adapters connect a concrete simulator (commercial TCAD, multiphysics, batch
solvers, ...) to the runtime-neutral simulation layer:

1. Implement the **worker** protocol (`control_plane/simulation/worker.py`): it
   starts or resumes a session, observes its state, and collects a durable
   result without accessing the evaluation queue. Optional `terminate_session`
   enables confirmed termination.
2. Implement the **gateway** protocol used by the worker — launching the
   runtime, confirming launch, observing and recovering, validating runner
   receipts, publishing run artifacts, and exposing retry operations.
3. Implement the adapter (`control_plane/simulation/adapter_protocol.py`):
   `build_gateway`, `materialize_package`, `validate_package`, and `qualify`.
   Keep tool commands, filesystem details, and credentials inside this adapter.
4. Register the adapter in `project/SIMULATION_ADAPTERS.json` with an
   `adapter_id`, `status` (`active`/`experimental`/`disabled`), `module`,
   `factory`, integer `interface_version`, the non-empty `capabilities` the
   adapter covers, and `resource_defaults` with `processors`, `memory_bytes`,
   and `max_wall_seconds`. `simulation_definition` is optional: when absent the
   preparation binds a content-derived identity
   `configuration.adapter.<adapter_id>` over the canonical catalog-entry JSON
   (`simulation/adapter_catalog.simulation_definition_identity`).
5. Preparation resolves the single runnable adapter whose capabilities are a
   superset of the problem's `simulation_capabilities`. Disabled entries, no
   match, or an ambiguous multiple match all fail closed rather than being
   selected implicitly (`resolve_adapter_for_problem`).

Adapters are not "started up and injected into a dispatcher." Worker and
resource_monitor come from `RUNTIME_COMPONENTS.json`; the adapter is resolved
lazily during preparation when a queued evaluation needs it.

### Package materialization

`adapter.materialize_package(evaluation_input, task)` is an adapter behavior
executed by the worker **when it starts a session** — it is not part of the
preparation phase, and the package it writes is **not** automatically registered
as an artifact. The auditable input baseline is the input-package artifact
referenced by the ParameterSchema `source_package` at an exact revision, which
preparation validated and bound into the immutable preparation.

## Failure and duration semantics

- Session failure classes `unreachable` and `indeterminate` are distinct from
  `absent` and are never collapsed into it (`worker.SESSION_*`). The dispatcher
  observes a reconciling session before applying wall-proof recovery
  (`service.auto_release_wall_budget` / `dispatcher.recover_once`); a pending
  session is only confirmed terminated when the worker returns `terminated` or
  `absent`, and a monitor/worker without `terminate_session` reports
  termination as unconfirmed.
- Workers can include `wall_seconds` / `cpu_seconds` / `peak_rss_bytes` in
  `SolverRunRecord` bodies. When completion feedback is explicitly supplied,
  missing feedback fields are derived by summing wall/CPU durations and taking
  the maximum RSS. With `feedback=None`, the current completion path does not
  create performance feedback; the default dispatcher uses that path. An
  integration can record verified terminal measurements through
  `record_attempt_feedback`. Missing measurements stay `None`. See the
  [adapter guide](ADAPTERS.md) for result and measurement contracts.
- Preparation itself is a lazy queued phase with bounded claims; a bad input
  releases the claim and marks that evaluation `unresolved` without aborting the
  round.

## Wall budgets, orphan recovery, and late harvest

Three runtime behaviors keep an evaluation moving to a terminal state even when
a session outlives its claim, a worker is lost, or two processes race to finish
the same work.  Each is described below from the persisted state transitions in
`control_plane/data/sqlite_evaluation_repository.py` and the dispatcher logic in
`control_plane/evaluation/dispatcher.py`,
`control_plane/evaluation/prepared_dispatcher.py`, and
`control_plane/evaluation/service.py`.

### Learned wall budgets and kill thresholds

Each prepared option carries a wall budget.  A learned resolver
(`wall_budget.resolve_wall_budget`) derives an immutable budget from completed
samples before degrading back to the declared value:

- **Censoring and source degradation.**  Only successful measured samples
  (completed attempts with `af.succeeded = 1` and a non-null `wall_seconds`,
  `repository.list_completed_wall_samples`) feed the learner; the learner reads
  at most 200 per query.  When the most specific grouping
  (`problem + fidelity + target`) has at least `min_budget_samples` samples it is
  used, otherwise the resolver degrades to `problem + fidelity`, then to
  `problem`, and finally falls back to the **declared** source
  (`wall_budget.py:41-54`).  The 95th-percentile-like rank
  (`rank = ceil(0.95 * n) - 1`) and a `1.2 * max` guard plus a `1.0` floor raise
  the budget above the declared value.
- **Automatic widening on kill rate.**  Killed attempts are counted separately
  from samples (`repository.count_wall_budget_kills`, matching
  `failure_class = 'wall-budget-elapsed'`).  When
  `kills / (kills + samples) > kill_rate_widen_threshold` the budget is
  multiplied by `kill_widen_factor` and flagged `widened`
  (`wall_budget.py:55-60`).
- **Derived proof values.**  The resolver returns `budget_seconds`, and its
  `kill_at_seconds` and `stall_seconds` are ceiling-rounded multiples of the
  budget via `kill_multiplier` (default `1.7`) and `stall_fraction` (default
  `0.25`) (`wall_budget.py:61-69`).
- **Persistence and use.**  A dispatched attempt records its budget in
  `attempts.wall_budget_json`.  If none was stored, launch confirmation writes a
  declared-source budget (`budget_seconds` = declared, `kill_at_seconds` =
  `ceil(1.7 * declared)`, `stall_seconds` = `ceil(0.25 * declared)`)
  (`sqlite_evaluation_repository.py` around `confirm_attempt_start`).  Recovery
  (`service.auto_release_wall_budget`) computes each reconciling attempt's proof
  from the persisted budget's `kill_at_seconds` when present, else from
  `max_wall_seconds * kill_multiplier`, and passes it to
  `repository.auto_release_wall_budget`, which rechecks lifecycle state inside a
  transaction before applying the lost transition. During dispatcher recovery,
  only the candidate observed in that round is eligible; unobserved candidates
  retain their allocation until their own observation round. Invalid persisted
  deadlines are skipped without blocking valid candidates.
  (`service.py` around `auto_release_wall_budget`).

### Orphan sessions and the pending-kill loop

When a reconciling attempt whose session is still running is released as lost,
the control plane records an **orphan session** keyed to the live
`session_ref`, so a still-executing job is neither forgotten nor re-run.

- **Deferred dispatch.**  Before claiming a prepared candidate, the dispatcher
  excludes any evaluation that carries an open orphan last observed `running`
  or `completed`, even after `kill_at`, so it is not double-dispatched while
  the real session is alive or its result awaits harvest (`prepared_dispatcher._orphan_deferred_evaluation_ids`).
  Deferring only skips the round; it performs no Attempt transition.
- **Bounded observation loop.**  `dispatcher.recover_once` runs the orphan loop
  (`_reconcile_open_orphans`) after wall-proof release, processing at most
  `orphan_batch_size` (default `10`) open orphans per round
  (`dispatcher.py:200`).  Each orphan is re-observed via the worker
  (`resume_session`/`observe_session`): `absent` closes it; `running` keeps it
  open (and past `kill_at` requests a worker `terminate_session`); `completed`
  collects it; `unreachable`/`indeterminate` leave it open for a later round
  (`dispatcher._reconcile_one_orphan`).
- **TTL expiry.**  An orphan whose age from `orphan_since`/`created_at` exceeds
  `orphan_ttl_seconds` (default `604800`) triggers a termination request.
  It closes only after an absent observation, confirmed termination, or harvest.
  When a worker lacks `terminate_session`, the orphan is left open with
  `terminate_status = 'unavailable'` and retains its license under the configured
  orphan accounting policy.
- **License accounting.**  Open orphan sessions are counted against license
  capacity: the atomic claim adds `COUNT(*) FROM orphan_sessions WHERE
  status='open'` to the active-allocation count before deciding whether
  `license_sessions` is exhausted (`sqlite_evaluation_repository.py` in
  `claim_prepared_execution`).
- **Persistence and API.**  Orphans live in the `orphan_sessions` table
  (`record_orphan_session`, `get_orphan_session`, `find_orphan_by_session_ref`,
  `list_orphan_sessions`, `update_orphan_session`); the service facade exposes
  them and the web status server serves `GET /api/attempts/orphans`.

### Late harvest and idempotency races

Two code paths can both observe the end of work that only one evaluation should
count: a normal `complete_session` and an orphan `harvest_orphan_session`.  Both
are made idempotent by a single-transaction evaluation-status CAS.

- **First-wins CAS.**  `repository.complete_orphan_attempt` runs entirely inside
  one `BEGIN IMMEDIATE` transaction.  When the evaluation is still harvestable
  (in `_HARVESTABLE_EVALUATION_STATES` = `{requested, deduplicating, queued,
  running, recovering}`) and the attempt is `lost`, it transitions the attempt
  `lost -> completed` with a `late-harvest` event, moves the evaluation to
  `qualifying`, records the orphan `harvested`, and closes it.  The winner then
  flows through the normal qualification path
  (`service.harvest_orphan_session`).
- **`discarded_duplicate`.**  When the evaluation is already resolved, the CAS
  changes no status: it only appends an `AttemptDiscardedDuplicate` state event
  (reason `discarded_duplicate`) and records the orphan `discarded` and closed
  (`sqlite_evaluation_repository.py` in `complete_orphan_attempt`).
- **Win-then-kill duplicates.**  The same rule protects the reverse race: a late
  `complete_session` for an evaluation another attempt already settled still
  completes the attempt but, when the evaluation is no longer `running`, skips
  terminal evaluation and writes `discarded_duplicate` instead of reopening
  qualification (`service.complete_session`, `_skip_terminal_evaluation` in
  `repository.complete_attempt`).

### Scheduling-policy recovery fields (defaults)

The governed scheduling policy (`scheduling_policy.validate_scheduling_policy`)
accepts these optional fields; the defaults are applied when a field is absent:

| Field | Default | Meaning |
|-------|---------|---------|
| `kill_multiplier` | `1.7` | Budget multiplier producing `kill_at_seconds` |
| `stall_fraction` | `0.25` | Fraction of budget used as the stall threshold |
| `min_budget_samples` | `5` | Minimum samples before a learned source is trusted |
| `kill_rate_widen_threshold` | `0.10` | Kill-rate ratio above which the budget auto-widens |
| `kill_widen_factor` | `1.5` | Multiplier applied when the budget auto-widens |
| `reconcile_hold_seconds` | `1800` | Hold window applied during automatic release |
| `orphan_ttl_seconds` | `604800` | Time an open orphan may remain before TTL expiry |
| `orphans_hold_license` | `true` | Whether open orphans count against license capacity |
| `orphan_batch_size` | `10` | Open orphans observed per recovery round |

### Not implemented / pending

1. **Progress (stall) probe.**  `stall_seconds` is computed and persisted with
   every attempt's wall budget, but no adapter-side method feeds it: zombie
   detection is wall-time only.  A progress signal is software-specific
   (output-file mtime, solver log timestamps, scheduler accounting), so it will
   be an optional adapter method probed with `getattr`, like
   `terminate_session`; the contract is not yet fixed.
2. **Target normalisation of learned budgets.**  Samples are keyed by
   `(problem_revision, fidelity, target)` and degrade to coarser keys when
   sparse; there is no cross-target speed normalisation.  Planned direction: an
   operator-declared per-target baseline plus a selectable estimation mode.

Earlier drafts of this section listed three wiring gaps (learned budget not
called at launch, `orphans_hold_license` not consulted, production orphans
without `kill_at`).  All three are closed: `confirm_attempt_start` calls
`resolve_wall_budget`, the atomic claim honours the flag, and auto-release
writes `kill_at` from the persisted budget.

## Deployment guards

A runtime refuses to start (rather than degrade) on several conditions
(`composition.compose_runtime`, `execution_topology.ensure_formal_targets_ready`,
`prepared_dispatcher`):

- `project/RUNTIME_COMPONENTS.json` is missing, not valid JSON, lacks
  `schema_version == 1`, or does not declare a `worker` and a `resource_monitor`
  with the required methods.
- More than one formal execution target is declared without each carrying a
  `host_id` (missing host identity is never inferred to be a private host), or
  the resource monitor does not provide `locked_dispatch` for the multi-target
  case.
- A disabled adapter is never selected; a problem whose capabilities match no
  adapter or more than one adapter is not implicitly scheduled; a non-active /
  non-formal target is not scheduled; and policy/revision or capacity-out-of-
  range choices fail closed.

## Demo architecture

The demo (`examples/minimal-runtime/`) uses the same composition boundary as a
deployed application. It builds a disposable workspace and SQLite database,
registers a real input-package artifact and a ParameterSchema referencing it,
registers a problem, and then calls `compose_runtime` so that the genuine
runtime assembles the worker and resource monitor from
`project/RUNTIME_COMPONENTS.json` and drives a real preparation →
dispatch → session → collection → qualification loop. A fixed-quota resource
monitor and a fake worker/adapter emulate runtime launch and receipt handling
without an external simulator, host, or license service. The read-only web
status server can consume the same repository projections. A production
composition replaces the example worker/monitor through the same project-file
mechanism and adapter registration.

## The lifecycle of one evaluation

`requested` → `queued` → (lazy preparation) → scheduled & dispatched → session
running → `qualifying` → `qualified` / `unresolved`. Recovery or wall-proof
failure can requeue an evaluation to `queued`, and one bad input is isolated as
`unresolved`. See the [project README](../README.md) section *The lifecycle of
one evaluation* and the [governed preparation inputs](governed-preparation-inputs.md)
document for the detailed inputs and rules.
