# Phase 3 restart-recovery acceptance

This harness starts an isolated copy of the production-shaped Compose stack, seeds only synthetic data, drives the real React/Vite client with Playwright, and proves that a run queued while the worker is stopped survives browser closure and an API restart without duplicate durable facts.

## Safety and isolation

- Every invocation uses a unique Compose project, host ports, PostgreSQL volume, MinIO volume, and browser profile.
- Test credentials and encryption keys are generated in process memory and are not written to the repository.
- The harness never targets an existing Compose project and never uses production environment files.
- Successful runs remove containers and volumes. Failed runs keep them only when `-KeepOnFailure` is supplied.

## Acceptance contract

1. Migrate an empty PostgreSQL database and create the private MinIO bucket.
2. Seed one recruiting administrator and one job with immutable JD and rule versions.
3. Stop the worker, upload 100 distinct synthetic TXT resumes through the browser UI, and persist the generated run ID in the application's own recent-task local storage.
4. Prove all 100 items are durable and unprocessed, close the browser, restart `api`, start `worker`, and reopen the same persistent browser profile.
5. Resume the same run from the recent-task banner and wait for a terminal result.
6. Query PostgreSQL for the run and require exactly 100 items, candidates, `new` applications, and screening results, with no duplicate item IDs or queue dedupe keys; then stat all 100 recorded objects in the private MinIO bucket.

## Run

From the repository root:

```powershell
PowerShell -ExecutionPolicy Bypass -File deploy/e2e/run-recovery.ps1
```

Keep the isolated stack after a failure:

```powershell
PowerShell -ExecutionPolicy Bypass -File deploy/e2e/run-recovery.ps1 -KeepOnFailure
```

The script prints the generated Compose project name and diagnostic commands. Do not copy generated credentials from process environments or container inspection into tickets or logs.

## Rollback and cleanup

This harness has no production rollback action. Its rollback is deletion of the unique Compose project and volumes:

```powershell
docker compose -p <printed-project> -f deploy/compose.yaml -f deploy/e2e/compose.yaml down --volumes --remove-orphans
```

Production application rollback must retain PostgreSQL and MinIO data, deploy the previous compatible image, and must not run an automatic Alembic downgrade.

## Final UX-09 F-01 through F-06 gate

`run-final.ps1` is the real React/Vite + FastAPI + PostgreSQL + MinIO + ClamAV + Worker browser gate. It runs every named flow at 1280x720 and 390x844, uses only synthetic `example.test` identities, and creates a unique Compose project, host ports, database, buckets, volumes, Chromium profiles, and artifact directory.

From the repository root:

```powershell
$env:DISPOSABLE_E2E_CONFIRMED = '1'
PowerShell -ExecutionPolicy Bypass -File deploy/e2e/run-final.ps1
```

Retain the isolated stack after a failure only when live diagnosis is required:

```powershell
$env:DISPOSABLE_E2E_CONFIRMED = '1'
PowerShell -ExecutionPolicy Bypass -File deploy/e2e/run-final.ps1 -KeepOnFailure
```

Run the focused contract independently:

```powershell
node --test tests/e2e/final-runner-contract.test.cjs
```

The final gate is all-or-nothing: any blocked, failed, or partially reached flow produces exit code 2 and must be reported as incomplete, never PASS. Failure evidence is written under `.tmp/e2e-artifacts/<project>/` and includes a trace, screenshot, sanitized DOM/URL, console/page errors, and failed request method/path/status. Tracing is paused while login credentials or resume upload bodies are entered. Artifact directories are untracked and must be scanned before sharing; never commit `.tmp`.

The latest recorded real run remains blocked: F-01 create/publish succeeded but immediate edit returned HTTP 409; desktop F-02 ended with 18/18 non-retryable failures and no candidates; mobile navigation was outside the 390x844 interactive viewport. F-03 through F-06 therefore did not reach their required real prerequisites. See `.superpowers/sdd/task-final-e2e-report.md` for exact evidence and the artifact privacy finding.
