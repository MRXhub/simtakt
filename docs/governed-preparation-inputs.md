# Governed Preparation Inputs Specification

## Location
Relative to the project root, the project state configuration file lives at:
`project/PROJECT_STATE.json`

## Minimum Content

Two different minima exist and confusing them is the usual first failure.

**Structural minimum.** `_policy_task` validates only the envelope of the file:
`schema_version`, top-level `status`, a resolvable `scheduling_policy`, and a
matching entry in `active_tasks` with `id`, `kind` and `status`. A file
containing only those fields passes that check.

**Working minimum.** Preparation does not complete with the structural minimum
alone. `_authorization` additionally requires `execution_authorizations` to be a
list, and raises `prepared execution task lacks execution_authorizations`
otherwise. The content below is the form that was actually driven end to end to
a persisted, qualified evaluation, and is what a caller should start from.

Every `artifact_id` and `revision` below is a placeholder. Substitute your own
registered artifact identifiers and their exact SHA-256 revisions; the values
shown will not resolve.

```json
{
  "schema_version": 2,
  "status": "active",
  "scheduling_policy": {
    "artifact_id": "configuration.project-scheduling-policy.<your-id>",
    "revision": "sha256:<64 hex characters>",
    "status": "active"
  },
  "active_tasks": [
    {
      "id": "<your-task-id>",
      "kind": "simulation",
      "status": "approved-prepared-execution",
      "execution_authorizations": [
        {
          "artifact_id": "authorization.<your-id>",
          "revision": "sha256:<64 hex characters>",
          "authorization_kind": "prepared-execution-envelope-v1",
          "status": "active",
          "target_id": "<your-target-id>",
          "expires_at": "2099-01-01T00:00:00+00:00"
        }
      ]
    }
  ]
}
```

`expires_at` is enforced against the wall clock with a margin for the run's
expected duration, so an envelope that expires during a run is refused before
any work starts.

## Field-to-Code References

- `schema_version`: Must equal integer `2`.
  - `src/evaluation/governed_preparation.py:614` (`_policy_task`)
  - `src/evaluation/scheduling_policy.py:309` (`resolve_governed_scheduling_policy`)

- `status`: Must equal string `"active"`.
  - `src/evaluation/governed_preparation.py:614` (`_policy_task`)
  - `src/evaluation/scheduling_policy.py:309` (`resolve_governed_scheduling_policy`)

- `scheduling_policy`: Mapping specifying the active scheduling policy artifact reference.
  - `src/evaluation/scheduling_policy.py:313-333` (`resolve_governed_scheduling_policy`)
  - `scheduling_policy.artifact_id`: Matching pattern `^configuration\.project-scheduling-policy\.[a-z0-9][a-z0-9._-]{0,79}$` (`src/evaluation/scheduling_policy.py:323,326`)
  - `scheduling_policy.revision`: Exact sha256 artifact revision matching `^sha256:[0-9a-f]{64}$` (`src/evaluation/scheduling_policy.py:324,327`)
  - `scheduling_policy.status`: Must equal `"active"` (`src/evaluation/scheduling_policy.py:329`)

- `active_tasks`: List of active task definitions.
  - `src/evaluation/governed_preparation.py:618-622` (`_policy_task`)
  - `active_tasks[].id`: Task identifier matching the preparation request `task_id` (`src/evaluation/governed_preparation.py:623-631`)
  - `active_tasks[].kind`: Must equal `"simulation"` (`src/evaluation/governed_preparation.py:633-639`)
  - `active_tasks[].status`: Must equal `"approved-prepared-execution"` (`src/evaluation/governed_preparation.py:635-639`)

### Further Governed Task Fields
`execution_authorizations` is required, not optional: `_authorization`
(`src/evaluation/governed_preparation.py:153-179`) rejects a task whose
`execution_authorizations` is not a list. Each envelope's `artifact_id` and
`revision` must resolve to an exact active artifact, and its body must agree
with the reference (`:175-220`).

The remaining fields below are cross-validated during preparation
materialization when present:
- `active_tasks[].parallel_efficiency_calibration`: Multi-core calibration sequence definitions (`src/evaluation/governed_preparation.py:643-674`).
- `active_tasks[].approved_packages` / `active_tasks[].source_package`: Package authorization and source package verification (`src/evaluation/governed_preparation.py:440-456`).
- `active_tasks[].simulation_definition`: Simulation deck configuration reference (`src/evaluation/governed_preparation.py:537-548`).
- `active_tasks[].performance_evidence` / `active_tasks[].evidence`: Execution performance evidence records (`src/evaluation/governed_preparation.py:557-573`).
- `active_tasks[].preparation_defaults`: Default budget limits and solver run bounds (`src/evaluation/governed_preparation.py:465-470, 735-745`).
