# Phase 6C backup scheduling design

## Objective

Run the existing ledger archive followed by the paired PostgreSQL and business
object backup publication every 12 hours by default. A backup becomes fresh
only after the newly published business group has been fetched through the
read-only destination client and has passed the existing complete-group
validation contract.

## Topology

A Linux systemd timer starts a oneshot service. The service runs the immutable
`backup-tool` Compose service from `deploy/compose.backup.yaml`; scheduling is
kept out of the container so host restart catch-up is provided by
`Persistent=true`. The host environment file is used only for Compose
interpolation and file-path configuration; secret values are mounted as 0400
files. The host state path is fixed to `/var/lib/ux09-backup` so a configuration
error cannot make the root-owned systemd preflight change ownership of another
directory. The container acquires a non-blocking `fcntl` lock on that
bind-mounted state before any remote operation, so systemd starts, manual
Compose starts, and duplicate timer delivery cannot overlap.

The oneshot coordinator generates non-PII ledger and business run IDs, invokes
the existing `ledger-archive` command, fetches and verifies that immutable
ledger group, invokes the existing `backup` command, then fetches and validates
the exact business complete group. Only that final restore-aware validation
updates freshness state.

## Configuration and failure behavior

The systemd renderer accepts configurable calendar, randomized delay, service
timeout, deployment root, Compose environment file, and unit output directory.
The Compose overlay requires immutable backup image coordinates, application
and storage endpoints, clients, secret-file paths, and retention inputs through
`${VAR:?message}` interpolation. Defaults are limited to the requested 12-hour
calendar, 15-minute randomized delay, and 18-hour freshness threshold. Invalid
or missing values fail before publication; credentials remain mounted files and
are never logged.

Every coordinator stage emits one-line JSON with bounded fields: timestamp,
event, stage, run ID, result, and exit code. Child output is not copied into
coordinator error events. Any validation or child failure exits non-zero and
leaves the previous successful freshness state unchanged. A non-PII pending-run
record is written before remote work; failures preserve its ledger/business run
IDs, last stage, and workspace. Publisher exit 75/76 marks reconciliation as
required, and every later schedule fails closed until an operator reconciles
the same run and archives the evidence.

## Freshness signal and alert

After remote validation, the coordinator fetches and validates the independent
ledger group paired with every business point counted toward redundancy. Only
then does it atomically write Prometheus textfile metrics on the shared
scheduler-state volume. Node exporter exposes the last valid restore-point Unix
timestamp and configured threshold. A Prometheus
recording rule computes `ux09_backup_last_success_age_seconds`; critical alerts
fire when the age exceeds the configured threshold or when either source metric
is absent. The `freshness-check` command applies the same threshold and exits
non-zero for missing, malformed, future-dated, or stale state.

## Rollback

Disable and stop `ux09-backup.timer`, preserve the state volume and all remote
leases/groups, then remove the backup Compose overlay from observability and
restore the previous alert rules. Do not delete or republish an ambiguous run.
Reconcile publisher exit 76 with the existing read-only procedure before
reenabling one canary and then the timer.

## Verification scope

Focused tests cover lock exclusion, stage failure propagation, restore-aware
freshness updates, stale/missing checks, secret-safe structured logs, systemd
render validation, Compose fail-closed interpolation, and Prometheus rules.
`deploy/e2e/**` is explicitly outside this change and is not modified or run.
