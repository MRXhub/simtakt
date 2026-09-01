# Local-process SimulationWorker

This example launches a solver as a real child process, persists a PID plus a random
launch token, and uses the solver's log (not merely its exit code) to decide convergence.
It is intentionally a teaching adapter; the three `TODO` comments in `adapter.py` are
questions that must be answered before production use.

| Concern | Windows | POSIX |
|---|---|---|
| Start | `Popen` with argv list and `creationflags` | argv list and `start_new_session=True` |
| Kill tree | `taskkill /T /F /PID` | `killpg(SIGTERM)`, then `SIGKILL` |
| Exit code | zero is only process success, not numerical convergence | same |
| Paths/encoding | path length and file locks matter; logs may be code pages | UTF-8 is common but not guaranteed; decode with replacement |

`proc.terminate()` on Windows only kills the parent process: its children become
orphans and may continue consuming a solver license. Never use `shell=True`; it changes
which process is tracked and makes quoting and tree ownership ambiguous. Redirecting
stdout/stderr to files avoids pipe-buffer deadlocks. Keep paths below the historical 260
character limit where possible, and close files before replacing/deleting them because
Windows locks open files.

PID reuse threatens `resume_session`: a new, unrelated process can acquire the old PID.
The PID file and process existence therefore are insufficient. This adapter requires the
persisted random token to match the PID file as well, and never relaunches on resume.

The convergence TODO asks which file, exact text, and encoding are authoritative. The
demo currently uses `CONVERGED:` in the log and tolerant UTF-8 decoding; the fake solver's
`diverge` mode exits zero without that marker. Result extraction is also a TODO: the
example returns contract-shaped records but production must define how `result.json`
becomes a SolverRunRecord and evidence artifact.

Run exactly from the repository root:

```text
python examples/adapter-local-process/run_demo.py
```

Expected key output includes `normal observe=completed`, `diverge observe=indeterminate`,
and `tree terminate=terminated ... child_alive=False` (the latter is direct PID liveness
verification, not inference from `taskkill`'s return code).
