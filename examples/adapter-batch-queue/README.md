# Batch queue adapter example

This example is a worker skeleton for a submit-to-batch-queue execution model. It
uses `FakeBatchQueue`; it never invokes a real scheduler command.

## Processor injection (decide per solver)

| Software | Where processors are injected | Why this adapter must decide |
|---|---|---|
| Silvaco DeckBuild/ATLAS | Deck/input statements (commonly `go`/tool input) | The solver's input language, not a generic scheduler flag, controls parallelism. |
| COMSOL | COMSOL batch command-line options (or model/M-file settings) | A scheduler allocation and COMSOL's own process settings are separate layers. |
| OpenFOAM | `system/decomposeParDict` (`numberOfSubdomains`) and MPI command line (`mpirun -np <N>`) | `numberOfSubdomains` in `decomposeParDict` must match `mpirun -np`; this cross-file/command-line coupling cannot be expressed by a pure JSON template. |

`rewrite_deck_parameters` is the utility intended for the first, deck-rewrite
category; it is not a substitute for a scheduler's processor request. Confirm
the exact syntax for your software/version before implementing submission.

## Real solver command lines and pitfalls

1. **Silvaco DeckBuild / ATLAS**
   - Command pattern:
     ```sh
     deckbuild -run <input_deck> -outfile <output_log>
     ```
   - Pitfall: Using `-outfile` can drop logs or lose unbuffered output if the process terminates abruptly.
   - Reference: Silvaco Official Documentation (https://silvaco.com/) and ATLAS manual secondary mirror (https://www.eng.buffalo.edu/~wie/silvaco/atlas_user_manual.pdf).

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
   - Reference: OpenFOAM User Guide (https://www.openfoam.com/documentation/user-guide/).

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
* COMSOL Knowledgebase 1001 (Running COMSOL in batch mode): https://www.comsol.com/support/knowledgebase/1001
* COMSOL Reference Documentation: https://doc.comsol.com/
* Silvaco Official Documentation: https://silvaco.com/
* Silvaco ATLAS User Manual (medium reliability, secondary mirror): https://www.eng.buffalo.edu/~wie/silvaco/atlas_user_manual.pdf
* OpenFOAM User Guide: https://www.openfoam.com/documentation/user-guide/

URLs and state details vary by release and site configuration: **consult your installed version and manual**.
The Silvaco Buffalo link above is a secondary mirror/reference and has medium reliability.

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

Run the self-contained demonstration with:

```text
python examples/adapter-batch-queue/run_demo.py
```
