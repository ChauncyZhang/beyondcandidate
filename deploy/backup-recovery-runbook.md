# Backup and recovery runbook

## Status and safety boundary

This is the canonical Phase 6C runbook. It defines executable
contracts for paired PostgreSQL and business-object restore points, independent
ledger archives, pruning, isolated restore, and evidence. It is not production
ready until an off-host destination is provisioned, least-privilege identities
are verified, the released real B2B3 CLI and B2B3 Worker are connected, and a
complete real restore drill proves the 24-hour RPO and 4-hour RTO.

Never run restore or drill commands against the `ux09` Compose project or its
volumes. The guard requires `DISPOSABLE_RECOVERY_CONFIRMED=1`, a project named
`ux09-backup-drill-<unique>`, and volumes whose names begin with that exact
project name. The traffic-gate command is permanently closed: it removes any
stale open marker and, after validating signed replay-resistant B2B3 evidence,
writes only traffic-closed evidence. Missing, caller-authored, unsigned,
misbound, or replayed evidence exits with safety code 78. This repository
contains no production traffic-open transition.

## Architecture and trust boundaries

- `deploy/backup/Dockerfile` pins PostgreSQL 16.9, MinIO Client, and rclone by
  immutable tag and digest. Build and record the resulting local image digest.
- `backup.sh` creates one PostgreSQL custom-format dump and one business MinIO
  snapshot under the same `backup_run_id`. The live governance ledger bucket is
  rejected from the business bucket list. Before pairing, it requires a fetched
  complete ledger group and validates schema, active signing-key version/HMAC,
  COMPLETE, archive hash/size/run ID, and cutoff freshness with a dedicated
  verification key.
- `ledger-archive.sh` uses separate source, append, restore, lifecycle, and
  signing-key-history contracts. The business read/append/prune identities must
  have no ledger restore or delete grants.
- `s3-atomic-publisher.py` provides COMPLETE-last publication for allowlisted
  S3-compatible destinations. It uses the pinned `mc` to create one permanent
  lease with provider-native `If-None-Match:*` under the fixed private lease
  prefix, verifies every uploaded size and SHA-256, uploads COMPLETE last, and
  returns the existing run/hash-bound receipt. The bundled rclone adapter still
  exits 78 for stage/publish because copy cannot prove this contract.
- PostgreSQL passwords, MinIO aliases, destination credentials, and signing
  material are protected regular files with one link, no symlink, and mode
  0600 or stricter. The coordinator opens with no-follow semantics, checks the
  opened inode, and gives child processes private 0600 copies. Never pass
  secret values in argv, shell tracing, logs, manifests, reports, or tickets.
- Child processes receive a new strict environment containing only PATH,
  minimal locale/temp/home values, explicitly approved non-sensitive runtime
  values, and process-private secret paths. `PGPASSWORD`, `AWS_*`,
  `RCLONE_CONFIG_PASS`, and password/secret/token/key/credential variables are
  never inherited.
- Every configured staging root must equal its resolved path and cannot be a
  symlink, Windows junction, or other reparse point. Every business bucket is
  validated with the pinned bucket regex before any client call; empty items,
  traversal, separators, and URI-shaped values fail closed.

Production backups must use a destination URI whose host differs from the
application host and whose path is outside PostgreSQL and MinIO data volumes.
Local paths, `file:` URIs, application-host SSH paths, and data-volume paths
fail closed. The application host is not an acceptable off-host destination.

## Installation and prerequisites

1. Provision independent business-backup and ledger-archive failure domains.
   Configure storage-side encryption, versioning/object lock where approved,
   capacity monitoring, and lifecycle policies. Keep DNS and TLS verification
   enabled for remote endpoints.
2. Provision distinct identities for PostgreSQL backup read, business source
   read, business destination append, business prune deletion, business
   restore, ledger source read, ledger archive append, and ledger restore.
   Revoke retired identities after rotation and after temporary restores.
3. Create protected files for `PGPASSFILE`, each `*_CONFIG_FILE`, and signing
   keys, including the dedicated `LEDGER_MANIFEST_VERIFY_KEY_FILE`. Restrict
   ownership to the backup runtime. Do not place them in this repository or in
   Compose environment values.
4. Build the toolchain:

   ```sh
   docker build --pull=false -t ux09-backup:phase6c-foundation deploy/backup
   docker image inspect ux09-backup:phase6c-foundation --format '{{.Id}}'
   ```

5. Verify PostgreSQL 16 compatibility, destination TLS/DNS, free staging
   capacity for a full dump plus object snapshot, and clock synchronization.
   Provisioning of production roles/policies remains outside this independent
   slice and requires the B2B shared-file release.
6. Set `BACKUP_ATOMIC_PUBLISHER` and, when approved for the independent ledger
   destination, `LEDGER_ATOMIC_PUBLISHER` to
   `/opt/ux09-backup/s3-atomic-publisher.py`. The protected `mc` config must
   contain alias `s3` for `s3://BUCKET/PREFIX`, or the URI authority alias for
   `minio://ALIAS/BUCKET/PREFIX`. Do not use `--insecure` or place endpoint or
   credential values in environment variables.

## Required secret-file and adapter contract

The coordinator invokes one executable path per client variable; shell command
strings are rejected. Client arguments contain only secret-file paths, safe
endpoint identifiers, run IDs, and local evidence paths.

- `BUSINESS_SNAPSHOT_CLIENT`: `snapshot --config-file FILE --buckets LIST
  --output PATH --inventory PATH`. Inventory entries contain only a keyed/path
  hash, content hash, and size; raw object keys and filenames are forbidden.
- `BACKUP_ATOMIC_PUBLISHER`: `publish-complete-group --lease-config-file FILE
  --destination URI --run-id ID --source PATH --receipt PATH`. It must perform
  cross-process/cross-host conditional creation; exactly one publisher may win
  a run ID and a loser must not overwrite payload, manifest, or COMPLETE.
  `s3-atomic-publisher.py` exits 75 for an existing lease/complete group, 74 for
  a provider failure before the COMPLETE commit phase, 76 when commit status is
  unknown, and 78 for rejected input. Exit 76 covers any ambiguous COMPLETE PUT
  and every COMPLETE stat/get or local receipt failure after that PUT. Its
  messages are deliberately generic; investigate through aggregate provider
  metrics without copying object names, local filenames, client stderr, or
  config contents into logs.
- `BACKUP_DESTINATION_CLIENT`: signed `catalog`, verified
  `fetch-complete-group`, and complete-group `delete-complete-group` only.
- Reference validation is bundled and pinned. The coordinator executes the
  fixed SQL query, binds its trusted expected count and query fingerprint to
  the independently captured inventory, and requires `checked == expected`
  with zero mismatches. Zero references require an explicit trusted zero.
- `LEDGER_ARCHIVE_CLIENT`: `snapshot`, `select-latest-complete`,
  `fetch-complete-group`, and `restore-verified`. The consumer verifies strict
  schema, active key version/history, HMAC, COMPLETE, archive hash, run binding,
  and cutoff freshness. A separate proof client emits a signed proof bound to
  archive run, business run, and recovery generation; exit zero is not proof.
- `BUSINESS_RESTORE_CLIENT`: restores the verified snapshot with the temporary
  business restore identity; it has no access to the ledger archive.

`destination-rclone.sh` remains read/catalog/delete only and
`minio-business.sh` prevalidates every tar member before extraction. The
publisher adapter is implemented, but production enablement still requires a
real off-host provider endpoint, reviewed least-privilege policies, TLS,
encryption/lifecycle behavior, and provider-specific conditional-write proof.
The disposable MinIO race is local evidence only and is not off-host evidence.

## First paired backup

1. Run the independent ledger archive first. Its manifest must record the
   archive cutoff, entry count, archive hash, lifecycle policy version, and its
   active signing-key version. The independent key history retains all versions
   and has exactly one active version; replacing one unversioned key is forbidden.
   Fetch the immutable complete archive group with a read-only identity and set
   `LEDGER_PAIRING_GROUP_PATH` to that group. A bare manifest is never accepted.
2. Export a retention-policy snapshot from
   `retention_policies.backup_window_days` with its policy version. Supply that
   exact value as `BACKUP_WINDOW_DAYS`; no default is permitted.
3. Set a non-PII `BACKUP_RUN_ID`, UTC cutoff, pinned image digest, business
   buckets excluding the live ledger, protected secret-file paths, off-host
   destination, application hostname, client executable paths,
   `LEDGER_PAIRING_GROUP_PATH`, and `LEDGER_MANIFEST_VERIFY_KEY_FILE`.
4. Run `deploy/backup/backup.sh`. It must finish `pg_restore --list`, dump and
   snapshot hashes, complete object inventory, and the pinned reference proof
   before atomically writing local manifest/COMPLETE and invoking the external
   atomic publisher. Missing lease support or an invalid receipt fails closed.
5. Fetch the published group with a read-only identity and rerun manifest,
   marker, hash, inventory, and `pg_restore --list` validation. Record only the
   non-PII evidence location and result.

No publisher exit code by itself proves whether the remote COMPLETE object is
present. On exit 76, or when an exit 75/74 leaves remote state in doubt, stop the
scheduler and preserve the lease and group. Do not republish, delete the lease,
overwrite objects, or allocate a replacement run ID. Reconcile the same run ID
with a new, known-absent local receipt path:

```text
s3-atomic-publisher.py reconcile-complete-group \
  --lease-config-file FILE --destination URI --run-id ID \
  --receipt RECONCILED_RECEIPT
```

Reconciliation is read-only against the provider: it uses stat/get to download
the permanent lease and exact group into a private directory, validates the
manifest, hashes, sizes, run binding, and COMPLETE, then creates the local
receipt with no-replace semantics. A valid receipt proves that the original run
committed. If reconciliation exits 76, status remains unknown: preserve all
remote state and escalate to provider-backed investigation; do not bypass the
lease with a new run ID. Exit 75 must never be worked around with
list-then-write or manual overwrite.

Schedule ledger archive followed by paired backup at least every 12 hours. On
the approved Linux host, render and verify the supplied units, then enable the
timer only after a successful canary:

```sh
UX09_DEPLOY_ROOT=/opt/ux09 \
BACKUP_SYSTEMD_ENV_FILE=/etc/ux09/backup.env \
deploy/backup/render-systemd-units.sh
systemctl daemon-reload
systemctl start ux09-backup.service
systemctl enable --now ux09-backup.timer
systemctl list-timers ux09-backup.timer
```

The renderer requires a regular, non-symlink environment file with mode 0600
or stricter and one link. It verifies the units with `systemd-analyze`, uses
`OnCalendar=*-*-* 00,12:00:00`, `Persistent=true`, randomized delay, and the
coordinator's non-blocking `fcntl` lock. The coordinator advances freshness
only after fetching and validating the exact remote complete group and at least
two valid restore points. Every counted business restore point must also have
its independently stored ledger group fetched and revalidated for signature,
manifest hash, run ID, and cutoff binding. Alert after 18 hours and page after
the 24-hour RPO. Do not infer freshness from process success or a dump file
alone.

The scheduler writes `/var/lib/ux09-backup/pending-run.json` before the first
remote operation. Any failure preserves that file and the matching `runs/`
workspace; exit 75/76 during either publish stage marks the state
`reconciliation_required`. Later timer deliveries fail closed while the file
exists. Stop the timer, reconcile the exact recorded ledger or business run ID,
fetch and validate the resulting complete group, archive the non-PII incident
evidence, and only then remove `pending-run.json` and its matching workspace.
Never clear it merely to make the next timer run.

## Complete-group-only prune

Run `prune.sh` only after catalog validation. The catalog verifies manifest
HMAC, canonical schema, COMPLETE, payload hashes/sizes, inventory, and the
custom dump list before ordering by remote COMPLETE metadata. It fails closed
when the latest point is incomplete/invalid, any complete point disagrees with the current
`backup_window_days`, the catalog is malformed, or fewer than two valid points
exist. It ignores incomplete staging groups and deletes only an entire valid
complete group older than the policy cutoff while preserving the newest two
valid points. Ledger archives use their independent lifecycle and identity;
business prune credentials must be unable to list, restore, or delete them.

## Backup selection

Choose the newest complete point at or before the incident cutoff that passes
marker, schema, image/version compatibility, dump list, hashes, inventory, and
reference gates. Reject an invalid latest point and escalate; do not silently
fall back. Confirm its ledger archive is at least as fresh as the selected
business point. Record only run IDs, timestamps, aggregate counts, hashes, and
gate results—never object keys, candidate/request identifiers, filenames,
content, credentials, or PII.

## Isolated restore and recovery drill

Use a unique project and new volumes. Replace the syntactically valid example
image/digest with the reviewed immutable artifact, and point the catalog at a
freshly generated, signature-verified catalog. These variables, preflight,
Compose config, and drill commands are one copyable sequence:

```sh
export BACKUP_DRILL_PROJECT=ux09-backup-drill-$(date -u +%Y%m%d%H%M%S)
export COMPOSE_PROJECT_NAME=$BACKUP_DRILL_PROJECT
export DISPOSABLE_RECOVERY_CONFIRMED=1
export RECOVERY_VOLUME_NAMES=$BACKUP_DRILL_PROJECT-postgres-data,$BACKUP_DRILL_PROJECT-minio-data
export BACKUP_IMAGE=registry.example.test/ux09-backup:phase6c-foundation
export BACKUP_IMAGE_DIGEST=sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
export BACKUP_CATALOG_FILE="$PWD/verified-backup-catalog.json"
export BACKUP_WINDOW_DAYS=30

python3 deploy/backup/backupctl.py preflight-drill
docker compose -f deploy/compose.backup-drill.yaml config --quiet
docker compose -f deploy/compose.backup-drill.yaml up -d postgres minio backup-tool
docker compose -f deploy/compose.backup-drill.yaml cp \
  "$BACKUP_CATALOG_FILE" backup-tool:/work/verified-backup-catalog.json
docker compose -f deploy/compose.backup-drill.yaml exec \
  -e B2B3_CLI_COMMAND=/released/b2b3-cli \
  -e B2B3_WORKER_COMMAND=/released/b2b3-worker \
  backup-tool /opt/ux09-backup/drill.sh
```

`preflight-drill` strictly validates the image repository/tag and exact
`sha256:` plus 64-lowercase-hex digest, verifies disposable project/volume
isolation, and reuses complete-group retention checks. An invalid latest point,
policy mismatch, malformed catalog, fewer than two valid points, mutable image
reference, or malformed digest fails before Compose/drill execution. The final
drill command still exits fail closed in this foundation because real B2B3 is
not implemented; it cannot open traffic.

The drill sequence is strict:

1. Seed synthetic candidate/application/resume/feedback rows and business
   objects, create and validate one paired restore point, then add post-cutoff
   canaries.
2. Complete a real B2B2 deletion and independently preserve its ledger.
3. Start RTO timing. Restore verifies the selected business group, chooses the
   latest complete independent ledger archive, validates its signature, hash,
   freshness, and requires a signed restore proof before recording
   `ledger_restored_first=true`. It then restores PostgreSQL and prevalidated
   business objects into the new disposable volumes.
4. Verify Alembic head, database permissions, aggregate row/object counts,
   hashes, and references. Recovery evidence still records `traffic_open=false`.
5. Set `B2B3_CLI_COMMAND` and `B2B3_WORKER_COMMAND` only to the released real
   implementations. Run them to prevalidate, checkpoint, advance recovery
   generation, re-delete restored PII, and process Worker jobs. This foundation
   intentionally does not implement or fake either interface.
6. Require B2B3 evidence for tombstones, absent objects, safe audit counts,
   generation, repeat idempotency, tamper failure, and checkpoint reclaim.
7. Run `traffic-gate.sh` with the independently signed B2B3 evidence and verify
   that it removes any stale open marker and writes the bound traffic-closed
   drill evidence. It exits 78 for missing, invalid, replayed, or misbound
   evidence. It never creates an open marker. Then run HTTPS
   readiness/read-only smoke while traffic remains closed and prove
   `failure_at - backup_cutoff <= 24 hours` and total RTO `<= 4 hours`.

Until step 7 completes, do not publish ports, remove the traffic-closed marker,
or route production traffic. A successful drill still requires the released
real B2B3 implementations and independent signing identity; repository tests
and caller-authored evidence do not satisfy that requirement.

## Full-host recovery

Rebuild the host from approved immutable artifacts, restore networking/TLS/DNS,
mount only new empty data volumes, and deploy the pinned compatible application
version with traffic disabled. Restore ledger first, then the selected paired
group and run all isolated verification. The foundation stops closed; only a
future reviewed signed B2B3 traffic protocol may authorize a progressive
rollout. Abort on any identity overlap, marker/hash/schema
failure, reference mismatch, permission drift, RPO/RTO breach, or B2B3 gap.

## Evidence and escalation

Retain signed manifests, COMPLETE hashes, tool versions/digests, run/cutoff
times, sizes, aggregate object counts, reference mismatch count, ledger
freshness, prune decisions, measured RPO/RTO, B2B3 safe counts, and every gate
in the independent evidence destination. If validation fails, preserve the
incomplete group for forensic review without marking it complete, keep traffic
closed, stop prune, and escalate to the incident commander, database owner,
storage owner, and governance/B2B3 owner. Never paste secret files or raw data
into incident channels.

After a drill, use `docker compose -p "$BACKUP_DRILL_PROJECT" -f
deploy/compose.backup-drill.yaml down -v` only after confirming the project name
again. Production project or volume deletion is never an accepted recovery
step.
