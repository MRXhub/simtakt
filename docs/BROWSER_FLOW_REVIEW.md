# Browser workflow validation — 2026-09-05

**21 checks passed in real Chrome**, against an isolated SQLite project and the minimal test adapter. A browser-submitted evaluation reached `qualified`, its attempt reached `completed`, and its allocation was released. Result persistence and template version bindings survived page reloads.

## Reproduce

Install Node.js, Playwright and Chrome as described in the [README](../README.en.md#development-and-validation), then run from the repository root:

```bash
node tests/browser_workbench_smoke.cjs
```

The script starts its own test backend, drives forms and navigation in Chrome, and closes both when finished. `PYTHON` selects the Python executable; `BROWSER_CHANNEL` selects the browser channel. Evidence is retained under `tmp/browser-smoke-*` and is excluded from source control.

## Verified behavior

| Area | Checks |
| --- | --- |
| Empty project | Real registries do not show example schemas. |
| Model import | Model text is parsed, stored and registered. |
| Template save | A partial registration failure preserves the draft; retry uses the same identity without creating duplicate schema versions. |
| Editing | Polling, sidebar collapse and background/foreground transitions preserve active input and previews. Editing invalidates outdated previews. |
| Study and run | A named study supplies the actual template; invalid parameters cannot be submitted; a valid request is stored once. |
| Runtime | Preparation, scheduling, execution, collection and qualification complete using real SQLite and the minimal test adapter. |
| Persistence | Results survive reload; older studies retain their template revision while new studies can select the new version. |
| Monitoring | Overview, compute resources, run performance and algorithm views load without JavaScript errors. |
| Connectivity | Going offline marks cached data as stale; reconnecting clears the stale notice. |
| Technical details | Internal hashes, UUIDs and raw error details require explicit expansion. |
| Languages | Chinese and English navigation and forms remain usable. |
| Navigation | Desktop collapse persists; mobile drawer, dismissal and keyboard behavior work. |

The final publication run produced **21 checks and 0 JavaScript errors**, with evidence in the local directory `tmp/browser-smoke-mg2KS1`. Earlier checks of the same UI covered **72 route/language/viewport combinations** at widths 1440, 1024, 390 and 320 pixels. Wide tables keep their own horizontal scrolling; the document does not overflow.

## Terminology and measurement semantics

The [terminology guide](UI_TERMINOLOGY.md) records the Chinese/English names and disclosure rules. Run performance reports historical resource measurements; it does not render simulation geometry. Missing values are not treated as zero or unlimited capacity. Sample counts include recorded successes and failures, while mean wall time uses successful executions with a recorded duration.

## Validation boundary

This is browser-driven integration testing, including form actions and SQLite/runtime verification. It uses a test adapter, not a commercial solver. Screenshots in `docs/images/` show the actual interface against a local example project. Remote deployment, solver numerical correctness and commercial license services require separate integration validation.
