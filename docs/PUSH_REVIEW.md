# Publication review — 2026-09-05

The publication includes the previously reviewed recovery work through `bc104d9`, subsequent runtime and package-import fixes, the revised Web workspace, and bilingual documentation. The remote baseline before publication was `43e0fb3` on `origin/main`.

## Confirmed fixes

| Area | Trigger and correction |
| --- | --- |
| Termination after restart | A resumed batch worker could lack its saved session binding. Restore the persisted execution plan and allocation before termination; keep termination pending if reconstruction fails. |
| Preparation failures | An uninitialized logger could mask the original error and interrupt claim release. Initialize the logger and verify that subsequent queued work continues. |
| Package registration | Import returned a deck hash while the registry stored a manifest hash. Return the registered manifest revision and always create the registry directory. |
| Package revisions | Directory scans could override the latest registry entry. Prefer the registry revision and load the matching model file. |
| Import idempotency | Equal model text with different attachments could collide. Include filenames, all file contents and dependencies in the submission fingerprint. |
| Restarted imports | Reuse could miss later revisions. Resolve historical revisions from the registry and verify their files. |
| Filename collisions | Attachments could overwrite reserved package files. Reject reserved names and case-insensitive duplicate names. |
| Import shutdown | Accepted jobs could remain queued while new jobs were still accepted. Drain accepted work and reject submissions after shutdown begins. |
| Web editing | Polling and delayed responses could clear drafts or retain outdated previews. Preserve active forms and invalidate stale previews. |
| Template bindings | Real projects could fall back to example schemas or the wrong version. Resolve the study's actual template and retain exact version bindings. |

The UI combines model parameters and evaluation configuration into one template editor, uses persisted display names, and moves internal identifiers and raw records into expandable details. A read-only template-options endpoint exposes configured adapter metadata without loading solver code.

## Publication checks

- `python -m unittest discover tests`: **441 tests, 0 failures, 1 skip** on Windows with Python 3.14.
- `node tests/browser_workbench_smoke.cjs`: **21 real Chrome checks passed**, using a real SQLite database and the minimal test adapter.
- Earlier responsive verification of the same UI covered **72 combinations**: nine routes, two languages and four viewport widths, with no document overflow.
- README commands, documentation links, Python/JavaScript syntax and staged whitespace are checked before publication.

Browser evidence is generated under ignored `tmp/browser-smoke-*` directories. The final local run used `tmp/browser-smoke-mg2KS1`; full Python output is in `tmp/release-unittest.log`. These local artifacts are not distributed with the source. Re-run the commands above to generate your own evidence.

## Scope

Checks cover repository transactions, dispatch and recovery, preparation, package registration, and the browser request-to-result workflow. They are not an exhaustive proof that the project contains no defects. Python 3.10 compatibility receives a syntax check; the full suite was run on Python 3.14. Commercial solvers and real remote scheduling systems were not deployed or exercised in this review.
