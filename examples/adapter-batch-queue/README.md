# Batch queue adapter example

This example is a worker skeleton for a submit-to-batch-queue execution model. It
uses `FakeBatchQueue`; it never invokes a real scheduler command.

## Processor injection (decide per solver)

| Software | Where processors are injected | Why this adapter must decide |
|---|---|---|
| Silvaco DeckBuild/ATLAS | Deck/input statements (commonly `go`/tool input) | The solver's input language, not a generic scheduler flag, controls parallelism. |
| Sentaurus (Synopsys) | Tool command line or generated parameter/input file, depending on tool | Different Sentaurus tools expose different launch surfaces. |
| COMSOL | COMSOL batch command-line options (or model/M-file settings) | A scheduler allocation and COMSOL's own process settings are separate layers. |

`rewrite_deck_parameters` is the utility intended for the first, deck-rewrite
category; it is not a substitute for a scheduler's processor request. Confirm
the exact syntax for your software/version before implementing submission.

## Scheduler status and the two-table trap

Slurm `squeue` is the active view, while `sacct` is the accounting/history view:
`RUNNING`/`PENDING` in `squeue` can become `COMPLETED`, `FAILED`, or `CANCELLED`
in `sacct`, and the job then disappears from `squeue`. LSF's `bjobs` active view
similarly differs from `bhist`/accounting output (with site/version-specific
state names such as RUN, DONE, EXIT, and PSUSP).

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
* COMSOL batch processing: https://doc.comsol.com/6.2/doc/com.comsol.help.comsol/comsol_ref_running一般.html
* Silvaco command reference (二手镜像，可靠性中等): https://silvaco.com/tcad/atlas/

URLs and state details vary by release and site configuration: **以你的版本为准**.
The Silvaco link above is a secondary mirror/reference and has medium reliability.

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
