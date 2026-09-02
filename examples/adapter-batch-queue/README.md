# Batch queue adapter example

This example is a worker skeleton for a submit-to-batch-queue execution model. It
uses `FakeBatchQueue`; it never invokes a real scheduler command.

## Processor injection (decide per solver)

| Software | Where processors are injected | Why this adapter must decide |
|---|---|---|
| Silvaco DeckBuild / ATLAS | Deck/input statements (commonly `go`/tool input) | The solver's input language, not a generic scheduler flag, controls parallelism. |
| COMSOL Multiphysics | COMSOL batch command-line options (or model/M-file settings) | A scheduler allocation and COMSOL's own process settings are separate layers. |
| OpenFOAM | `system/decomposeParDict` (`numberOfSubdomains`) and MPI command line (`mpirun -np <N>`) | `numberOfSubdomains` in `decomposeParDict` must match `mpirun -np`; this cross-file/command-line coupling cannot be expressed by a pure JSON template. |
| Synopsys Sentaurus Device / sdevice | Four injection points with precedence: `Math` block, CLI `--threads`, environment variables, and separate assembly/solver thread counts | Multiple injection points and strict override precedence (`--threads` overrides `Math` section, per-operation assembly/linear-solver counts override global counts) mean uncoordinated configurations can silently diverge from scheduler allocations. |

`rewrite_deck_parameters` is the utility intended for the first, deck-rewrite
category; it is not a substitute for a scheduler's processor request. Confirm
the exact syntax for your software/version before implementing submission.

## Real solver command lines and pitfalls

1. **Silvaco DeckBuild / ATLAS**
   - Command pattern:
     ```sh
     deckbuild -run -ascii -outfile <output_log> <input_deck>
     ```
     `-run` enables batch execution; `-ascii` keeps the GUI from opening.
   - Pitfall: Omitting `-outfile` loses the run-time output — including warnings
     and errors — once the batch process exits, and that log is exactly what a
     convergence criterion needs to read.
   - Reference: https://silvaco.com/ and the ATLAS manual mirror at
     https://www.eng.buffalo.edu/~wie/silvaco/atlas_user_manual.pdf

2. **COMSOL Multiphysics**
   - Command pattern:
     ```sh
     comsol batch -inputfile <input.mph> -outputfile <output.mph> -nn <nn> -np <np>
     ```
   - Pitfall: Omitting `-outputfile` directly overwrites the input file, creating data corruption risks. Furthermore, total allocated cores are roughly `nn * np` (`-nn` processes across nodes, `-np` cores per process); misconfiguring either factor results in CPU under-allocation or core oversubscription.
   - Reference: COMSOL Knowledgebase 1001 (https://www.comsol.com/support/knowledgebase/1001) and COMSOL Reference Documentation (https://doc.comsol.com/).

3. **OpenFOAM**
   - Command pattern:
     ```sh
     decomposePar && mpirun -np <N> <solver> -parallel | tee <log_file> && reconstructPar
     ```
   - Pitfall: Reaching `endTime` does not imply numerical convergence (a simulation can finish all time steps without meeting residual convergence criteria). Additionally, running `mpirun ... | tee ...` masks the solver's non-zero exit status in standard shell pipelines unless `set -o pipefail` or `$PIPESTATUS` is used.
   - Reference: https://doc.openfoam.com/2306/tools/parallel/

4. **Synopsys Sentaurus Device / sdevice**
   - Command pattern:
     ```sh
     sdevice my_device.cmd > my_device.log 2>&1
     ```
   - Parallelism syntax: Specify threads within the deck's `Math` section via `Math { NumberOfThreads = 4 }` (legacy spelling: `Number_of_Threads`), or override via CLI with `sdevice --threads 4 my_device.cmd`.
   - Pitfalls and impact on simtakt capacity ledger:
     - **Multi-tier override conflict**: Thread counts can be defined at four distinct levels (`Math` block, CLI `--threads`, environment variables such as `OMP_NUM_THREADS`, and granular assembly vs. linear solver thread options). When these layers conflict, actual core consumption deviates from `requested_processors` booked in the simtakt capacity ledger, causing CPU oversubscription or wasted reservations.
     - **Step-wise parallel licensing**: Sentaurus license consumption scales as a non-linear step function (for example, 2–4 threads consume 1 parallel license token, whereas 5–8 threads consume 2 tokens). Estimating license requirements linearly corrupts capacity accounting, leading to quota exhaustion or license checkout rejections.
     - **Silent stalling under `ParallelLicense (Wait)`**: Configuring `ParallelLicense (Wait)` makes `sdevice` pause indefinitely while waiting for available license tokens rather than failing immediately. Both the underlying queue and simtakt's `observe_session` report the job state as `RUNNING` despite zero compute progress, silently consuming `max_wall_seconds` until hard timeout termination.

### Sentaurus Workbench (SWB) architectural path

Sentaurus Workbench (SWB) provides `gsub` for job submission and `gjob` for execution management, moving experiment nodes through the lifecycle `none` -> `queued` -> `pending` -> `running` -> `done`. Because SWB is itself a complete parameter-sweep and job scheduling engine, its capabilities overlap directly with simtakt's orchestration responsibilities. Implementers must make an upfront architectural decision:
- **Direct solver invocation**: Bypass SWB and invoke `sdevice` directly, giving simtakt full control over parameter variation, scheduling queues, and capacity ledgers.
- **SWB delegation**: Submit jobs through `gsub`, creating a two-tiered scheduling hierarchy where capacity ownership, resource limits, and failure handling must be cleanly partitioned between simtakt and SWB.

## Scheduler status and the two-table trap

Slurm `squeue` is the active view, while `sacct` is the accounting/history view:
`RUNNING`/`PENDING` in `squeue` can become `COMPLETED`, `FAILED`, or `CANCELLED`
in `sacct`, and the job then disappears from `squeue`. Querying historical Slurm
records should use `sacct -X -j <id> --format=State,ExitCode`. Slurm `ExitCode` is
formatted as `exit_code:signal` (for instance, `0:9` indicates termination by signal 9).
A job cancelled via `scancel` transitions to `CANCELLED`, providing verifiable
evidence for termination confirmation.

LSF's `bjobs` active view similarly tracks states such as `PEND`, `RUN`, `DONE`, and `EXIT`.
LSF also supports post-execution tracking with `POST_DONE` and `POST_ERR`; note that
`DONE` indicates solver job completion but does not guarantee that post-processing has
finished.

The dangerous chain is: submit succeeds -> job runs -> job completes -> active
query returns “not found” -> an adapter treats that as `absent` -> dispatch never
collects the accounting result. This example therefore queries active first and
falls back to history; when both are missing it reports `indeterminate` (history
may have expired), not `absent`. A query transport failure is `unreachable`.
Cancellation is also verified by querying after the cancel request rather than
trusting a zero exit status from the cancel command.

Real command references (all are documentation, not calls made here):

* Slurm `squeue`: https://slurm.schedmd.com/squeue.html
* Slurm `sacct`: https://slurm.schedmd.com/sacct.html
* Slurm `scancel`: https://slurm.schedmd.com/scancel.html
* IBM LSF `bjobs`: https://www.ibm.com/docs/en/spectrum-lsf/latest?topic=reference-bjobs
* IBM LSF `bkill`: https://www.ibm.com/docs/en/spectrum-lsf/latest?topic=reference-bkill
* COMSOL batch mode: https://www.comsol.com/support/knowledgebase/1001
* COMSOL reference documentation: https://doc.comsol.com/
* OpenFOAM parallel running: https://doc.openfoam.com/2306/tools/parallel/
* Silvaco: https://silvaco.com/ — ATLAS manual mirror: https://www.eng.buffalo.edu/~wie/silvaco/atlas_user_manual.pdf
* Synopsys Sentaurus: https://www.synopsys.com/ — SWB course notes: https://web.stanford.edu/class/ee212/SWB/swb_2.html

Command syntax and state names vary by release and site configuration; adjust
them for your installed version.

## Deliberate TODOs (not fake implementations)

1. **Submission command construction** — Which executable and input surface does
   this software use, and do processors belong on its command line or in its
   input/deck? A wrong answer can silently run on one core or modify the wrong
   simulation.
2. **Success criterion** — Which output file and exact text indicate numerical
   convergence or divergence? Queue `COMPLETED`/exit 0 is not convergence; a wrong
   criterion immediately sends incomplete results to collection.
3. **Result extraction** — How should output files become `SolverRunRecord` and
   evidence artifacts? Wrong extraction can omit convergence evidence or return
   stale/incomplete data.
 
## Measured solve duration

The queue history record is the source of duration: `fake_queue.py` supplies an
Elapsed-shaped accounting value (for example `00:00:00.250`), and `adapter.py`
parses it into `SolverRunRecord.wall_seconds`. This is the Slurm `sacct Elapsed`
reference semantic, not the submit-to-collect lifetime. In production, fill in
the command and exact output format at the `# TODO(adapter):` marker. Missing
or unparsable accounting yields `wall_seconds=None`; the adapter does not
approximate it with attempt lifetime.

Run the self-contained demonstration with:

```text
python examples/adapter-batch-queue/run_demo.py
```
