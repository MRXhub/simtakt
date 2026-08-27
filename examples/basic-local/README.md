# basic-local

A minimal, dependency-free example of the control-plane simulation
worker/gateway protocol running against **local short jobs** instead of a remote
TCAD cluster.

This example exists so the platform's scheduling and session lifecycle can be
exercised end-to-end without any external solver, license, or VM. It is a
reference implementation only and requires no external solver.

## Layout

| File | Purpose |
| --- | --- |
| `local_adapter.py` | Reference implementation of `SimulationGateway` and `SimulationWorker`. Each "session" is a short local job: it sleeps briefly, computes a small value, and writes a result file into its durable session directory. |
| `run_demo.py` | Assembly script: in-memory SQLite control store, built-in `TargetCatalog`/`ControlStore` fixtures, a pure local scheduler, and dispatch of several local jobs through the adapter. |
| `README.md` | This file. |

## Run the demo

From the repository root:

```console
$ python examples/basic-local/run_demo.py
control store: sqlite at .../control.sqlite
worker sessions root: .../sessions
[step 1] scheduling local-session-1 (attempt=attempt:...)
[step 1] dispatching local job on local.target-main
[step 1] observation: running
[step 1] observation after wait: completed
[step 1] collected result status=completed artifact=artifact:...

=== dispatch summary ===
  20260826-120000-001  local-session-1  local.target-main  processors=2  completed  artifact:...
...
3/3 local jobs completed (exit 0)
web console: start it in demo mode with `python -m control_plane.web.status_server --demo`
```

The process exits `0` once every dispatched session completes. Pass `--jobs N`
to dispatch a different number of jobs:

```console
$ python examples/basic-local/run_demo.py --jobs 5
```

## Expected output

- A control-store path (a temporary SQLite database) and the worker sessions
  root are printed first.
- For each dispatched job the demo prints a `[step N]` line at scheduling,
  dispatch, observation (both the immediate and post-wait state), and final
  collected result.
- A `=== dispatch summary ===` block lists every completed run with its run id,
  session ref, target, processor count, observation, and artifact id.
- The final line reports `N/N local jobs completed (exit 0)`.

## Web console

The same control plane can be viewed in a browser using the self-contained web
status console in demo mode:

```console
$ python -m control_plane.web.status_server --demo
```

## Tests

The end-to-end test lives in the repository's `tests/` directory:

```console
$ python -m unittest discover tests
```

It runs the demo in-process (no interactive input) and directly exercises the
local worker/gateway round trip.
