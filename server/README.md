# UX-09 server runtime

Python 3.12 is required. The existing `app/web_app.py` remains a separate local tool.

## Production operations entry point

Use these canonical runbooks for production operations:

- [Deployment, upgrade, and rollback](../deploy/production-operations-runbook.md)
- [Backup and recovery](../deploy/backup-recovery-runbook.md)
- [Observability, alerting, and triage](../deploy/observability/runbook.md)

The local backend and topology gates below validate code and static deployment contracts only.
Environment acceptance must still run against the target Linux host, the real DNS/TLS domain, a
genuinely off-host backup destination with a complete restore drill, and the actual alert receiver.
This Phase 6C foundation is fail-closed and is not production ready; do not open production traffic
until every runbook blocker and environment gate has passed.

## Product Display Branding

`frontend/public/branding.json` is the community runtime default. It configures
`name`, `short_name`, `logo_url` and optional `favicon_url`; the default remains
BeyondCandidate / BC. The browser reads `/branding.json` before mounting the
application and applies it to the shell, login/invitation pages, document title
and favicon. Image paths are same-origin absolute paths, or `null` for a
short-name monogram. External URLs, query/fragment, backslash and traversal are
not permitted. There is no new anonymous API or database branding record.

Compose mounts that same file read-only into proxy, API and worker. The backend
loads `PRODUCT_BRANDING_FILE` at process startup for product-facing Feishu
messages. An explicitly configured unreadable or invalid file is an error;
standalone processes with no branding file use the community default. Website
branding does not change organization/legal names, SMTP sender identity,
cryptographic derivation identifiers, database names or repository paths.

Deployment layers may replace this JSON and provide same-origin logo assets
without modifying public product code. Recreate the affected containers when
changing runtime branding; do not rely on a running process re-reading the file.
Use a new logo filename when replacing an asset to avoid browser image caches.

The real Compose-model regression is `server/tests/test_branding_compose.py`.
`frontend/src/BrandingCsp.test.js` renders null/broken-logo favicons under the
actual public Nginx content security policy in Chromium.
Frontend branding/controller/browser tests participate in the normal Node gate;
backend branding/settings and Feishu tests participate in `server/tests`.

## Local tests

The reproducible test path uses the project's Python 3.12 Docker target:

```powershell
docker build --target test -t ux09-server-test -f server/Dockerfile .
docker run --rm ux09-server-test
```

When a local Python 3.12 interpreter is available, installing
`server/requirements-dev.txt` and running `python -m pytest server/tests` is equivalent.

### Interview Evaluation Timing

Scheduled, rescheduled and confirmed interviews become `pending_feedback` in
API projections when `ends_at <=` the server's current UTC time. The interface
labels these interviews as awaiting evaluation. This is a read-only projection:
it does not confirm attendance, submit feedback, change the application stage,
or send external notifications. Draft and terminal states keep their meaning.
An optional `workflow_status` preserves the stored status only when projection
differs, so existing HR cancellation, no-show and completion controls remain
available without reopening genuinely completed interviews.

| Contract | Durable Regression |
| --- | --- |
| Exact end boundary, UTC equivalence, list filters, personal task classification and read-only feedback access | `server/tests/test_interview_elapsed.py` |
| Awaiting-evaluation label, completed/cancelled preservation and exclusion from upcoming interviews | `frontend/src/interviewController.test.js`, `frontend/src/interviewDateUtils.test.js` |
| Consistent list filter/calendar label, live time-boundary switching, and preserved HR management only for projected statuses | `frontend/src/FrontendClosure.test.js` (`awaiting evaluation`, `open interview calendar and list`) |
| Open-page time-boundary refresh, stale-response protection, preserved pagination and independent partial-failure recovery | `frontend/src/InterviewAutoRefresh.test.js` |

These files participate in normal pytest/Node test discovery. Browser checks use
mocked HTTP responses; they do not modify real interviews or validate Feishu.

### Interview Cancellation And Application Termination

HR may cancel one unfinished interview (including pending evaluation) without
ending an application or inferring a hiring decision. Reasons are required;
submitted feedback and its authors remain historical. Missing or invalid email
configuration produces a manual-contact notice, not a rollback of cancellation.

Terminating recruitment applies only to one job application. It becomes
`withdrawn`, retains history, closes unfinished work, revokes effective Offer
links and stops new forward actions. A hired/accepted application requires an
explicit confirmation that the candidate has not actually joined. Rehiring
creates a new application; it cannot restore old tasks or tokens.

| Contract | Durable Regression |
| --- | --- |
| Single cancellation, pending feedback, history, reason/authorization and manual notification | `server/tests/test_interview_cancellation.py`, `server/tests/test_interview_api.py` |
| Scoped termination, versioned preview, idempotency, cleanup and external acknowledgments | `server/tests/test_application_termination.py`, `server/tests/test_application_termination_core.py` |
| Forward transitions refresh identities cached before a stop and cannot overwrite the terminal row | `server/tests/test_application_termination_stale.py` |
| Offer/approval/token and queued or in-flight mail boundaries | `server/tests/test_offer_termination_boundaries.py`, `server/tests/test_email_termination_boundaries.py` |
| Onboarding/Feishu generation fences and late external results | `server/tests/test_onboarding_termination.py`, `server/tests/test_feishu_termination.py` |
| Late screening results cannot recreate work or advance the old application | `server/tests/test_screening_termination.py` |
| Migration/grants/redaction and actual termination-vs-accept/feedback/reschedule/onboarding lock waits | `server/tests/test_application_termination_postgres.py` |
| Existing calendar serialization, known remote cancellation, old-generation replay | `server/tests/test_feishu_termination_postgres.py` |
| Preview/reason/prearrival confirmation, conflict/refetch preservation, versioned external resolution, independent pending-external workbench and pending-feedback single cancellation at desktop/mobile widths | `frontend/src/RecruitingTermination.test.js`, `frontend/src/terminationController.test.js` |
| Actual candidate/application/interview and accepted-Offer/onboarding entries, manager history without termination privileges, enabled danger-button hover contrast and modal overflow at 1440/390px | `frontend/src/TerminationEntries.test.js` |
| Cancelled approval history says cancelled/no approval needed, preserves original assignee/reason, and retains normal approval labels | `frontend/src/OfferTerminationHistory.test.js` |

These cases run in the normal `python -m pytest server/tests` gate. Focused
development checks may select the files above. The frontend's normal
`src/*.test.js` gate includes termination controller and browser regressions.
From `frontend` in PowerShell, run the focused browser/controller gate with
`$tests = Get-ChildItem src/RecruitingTermination.test.js,src/TerminationEntries.test.js,src/terminationController.test.js,src/OfferTerminationHistory.test.js | Select-Object -ExpandProperty FullName; node --test --test-concurrency=1 $tests`,
then `npm.cmd run build`. These browser cases use intercepted HTTP and local
fixtures; they do not prove deployed API authorization, database locking or
real Feishu/email effects. Hover screenshots are written to
`.tmp/termination-confirm-hover-1440.png` and `.tmp/termination-confirm-hover-390.png`.
Use the host Python 3.12 test environment described above; HTTP fixtures are
isolated SQLite and provider substitutes, not production integrations.

An already-sent or in-flight email cannot be recalled. A Feishu onboarding
instance is not automatically withdrawn: pending external cleanup remains
visible separately in the workbench and terminated application. Unknown or
in-flight creates block manual completion; a new late remote fact reopens
cleanup without restoring recruitment. Manual completion is an audited HR
attestation, not provider verification. External calls in tests are synthetic;
live delivery and Feishu acceptance require separately authorized verification.

For an uncertain onboarding create, **Check Feishu Result** queues a read-only
lookup of the original request UUID; **Refresh Processing Status** reads its
latest facts. The lookup uses durable actual-create attempt times, not the
user's submission time, so queue backlogs and later retries do not move the
original instance outside the adapter's bounded search windows. It never
submits a second approval. A found instance enables the
usual external handling/attestation; a missing result or lookup failure remains
unknown and cannot be acknowledged. Retry the lookup later or contact the
Feishu approval administrator with the displayed request generation. Legacy
requests without an original app/template snapshot and actual-attempt timing,
or a changed app identity,
fail closed: restore/verify the original integration identity with an
administrator rather than starting a new onboarding request or editing status.
The durable queue permits only organization/onboarding/generation IDs for this
read-only topic (`test_termination_reconciliation_payload.py`); worker and API
recovery cases are in `test_onboarding_reconciliation.py`.

For the database gates, set `TERMINATION_POSTGRES_URL` and
`TERMINATION_CALENDAR_POSTGRES_URL` to two separate, initially empty disposable
PostgreSQL databases and run the corresponding test file. Both refuse nonempty
databases, migrate with Alembic and use controlled providers. The caller owns
bounded database/container cleanup. Calendar synchronization retains its existing
serialization: termination waits for an already-running calendar request, then
queues cancellation using the recorded remote ID. Provider HTTP operations have
timeouts, but there is no strict whole-handler deadline across multiple requests.

### Invitation And Password Recovery

The login page's **Forgot Password** action requests a reset email using the
registered email and organization. The existing organization SMTP configuration
and email queue deliver the one-hour, once-only link. The public request always
acknowledges eligible, unknown, inactive and throttled accounts with the same
response; it does not return a token or disclose whether an account exists.
Requesting an email does not change the password, revoke sessions, or invalidate
other valid links. The browser applies a 60-second resend cooldown; the backend
also enforces limits across API workers. Administrator-issued recovery remains
available as a fallback.

Anonymous intake never queries or locks a user before acknowledging the request.
For an existing organization it persists an encrypted email and an ID-only
`identity.password_recovery` job, regardless of account eligibility. The worker
checks eligibility and per-user limits later, then clears the temporary email
on completion or terminal failure. Expiry remains one hour from the original
request, not from delayed worker processing. Revision `0044_password_recovery`
adds this intake table; drain recovery jobs before rolling back to an older
worker that cannot handle them.

Invitation links activate a pending member once and expire after 48 hours. An
already active member must log in rather than reuse that invitation. If the
password is forgotten, an authorized administrator can open **Settings >
Organization > Members > Reset Password** and generate a one-time link valid for
one hour. Share that link with the member securely; it is shown only in the
current dialog. Issuing it does not change the password or revoke sessions.
Consuming it replaces the password, clears login lockout, revokes every old
session, and invalidates other outstanding reset links. Administrators cannot
use this action for themselves; the current-password change flow is unchanged.

| Contract | Durable Regression |
| --- | --- |
| Login recovery preserves email, never submits a typed password, handles manual organization entry, and offers safe failure/retry and mobile layout | `frontend/src/forgotPassword.test.js`, `frontend/src/ForgotPasswordUi.test.js` |
| Anonymous requests do not disclose account state, preserve existing links, enforce server-side limits, and queue only opaque IDs; SMTP rendering produces a usable once-only link without persisting it | `server/tests/test_password_recovery.py` |
| Concurrent recovery requests respect shared network/user limits; a worker waiting for the user lock rereads token expiry before sending | `server/tests/test_password_recovery_postgres.py` |
| Activation remains once-only; consumed invitations offer login recovery; successful activation removes the token before external navigation | `frontend/src/PasswordResetUi.test.js`, `frontend/src/InvitationUi.test.js`, `server/tests/test_password_invitations.py`, `server/tests/test_invitation_reissue.py` |
| Active-member reset requires current administrator authority and tenant scope; issuance preserves the account; token purposes cannot cross-use; consumption revokes sessions and clears lockout | `server/tests/test_password_reset.py`, `frontend/src/passwordReset.test.js` |
| Privilege or password changes invalidate older reset links; a concurrent old-session password change cannot overwrite a completed reset | `server/tests/test_password_reset.py`, `server/tests/test_password_reset_postgres.py` |
| Confirmation, safe errors, link encoding, password entry and return to login work at desktop and mobile widths | `frontend/src/PasswordResetUi.test.js` |
| Forward migration preserves existing invitations and grants; concurrent consumption succeeds only once | `server/tests/test_password_reset_postgres.py` |

Focused entries (included in the normal frontend/backend test discovery):

```powershell
python -m pytest server/tests/test_password_reset.py server/tests/test_password_invitations.py server/tests/test_invitation_reissue.py server/tests/test_identity.py server/tests/test_identity_management.py -q
python -m pytest server/tests/test_password_recovery.py server/tests/test_communications_api.py -q
python -m pytest server/tests/test_queue.py server/tests/test_queue_review.py server/tests/test_worker.py -q
cd frontend
node --test --test-concurrency=1 src/passwordReset.test.js src/PasswordResetUi.test.js src/invitation.test.js src/InvitationUi.test.js
node --test --test-concurrency=1 src/forgotPassword.test.js src/ForgotPasswordUi.test.js
```

The browser tests use mocked HTTP responses; they do not issue real reset links
or validate a live Feishu authorization. The API tests use isolated SQLite.
For migration and locking verification, set `POSTGRES_SMOKE_URL` to a disposable,
initially empty PostgreSQL database and run
`python -m pytest server/tests/test_password_reset_postgres.py -q`. This test
leaves its synthetic data in that disposable database and refuses a nonempty DB.
Do not run it against a shared or production database. Revision
`0043_password_reset` is forward-only. Before rolling back to code that predates
purpose checks, expire all outstanding password-reset tokens through an
explicitly authorized operational action.

The separate self-service concurrency check requires
`PASSWORD_RECOVERY_POSTGRES_URL` pointing to an initially empty disposable
PostgreSQL database. Run
`python -m pytest server/tests/test_password_recovery_postgres.py -q` from the
repository root; it upgrades that isolated database and retains synthetic data.
It does not contact a real mailbox or production database.

### Member Removal And Unified Handover

Administrators use **Settings > Organization > Members > Remove Member And
Handover**. Removal retains the user row and original historical attribution,
disables login, revokes sessions, expires activation/reset grants, and prevents
bootstrap from silently restoring the account. Administrator hierarchy, tenant
scope, self-removal and last-active-administrator protections remain enforced.

One active same-tenant successor receives eligible operational responsibilities.
Roles and global department scope are not copied. Any job or talent-pool grant
addition is explicit in the preview. Ineligible work remains visible in the
removed member's pending-handover view. **Revoke Access Only** does not activate a
new successor mapping, even if a successor was selected in the preview.

Candidate notes, feedback authors, decided approvals, uploaded-file authors and
original screening requesters are not rewritten. Existing Feishu instance
identities and calendar snapshots are not transferred through direct database
edits. Their manual follow-up items do not mean the remote transfer was completed.
Durable pending snapshots contain routing references, not copied candidate names
or content; previews calculate current responsibility and eligibility again.

Late screening and talent producers resolve the latest valid successor while
retaining original authorship. An unavailable destination or a lock conflict is a
retryable ownership failure, not a successful completion or an arbitrary fallback
to an administrator. An ownership-blocked LLM attempt returns to a retryable state
rather than becoming a false success on its next execution.

| Contract | Durable Regression |
| --- | --- |
| Revocation, current RBAC/tenant checks, stale previews, partial handover, history, revoke-only, pending visibility and failed-write rollback | `server/tests/test_member_offboarding.py` |
| Bound and unbound Feishu login cannot reactivate a removed member | `server/tests/test_member_offboarding_feishu.py` |
| Live responsibilities, explicit grants, current approval rounds, historical preservation, blocked external work and scoped fingerprints | `server/tests/test_member_handover.py` |
| Late parsing/routing/talent work, successor chains, unavailable destinations and LLM retry recovery | `server/tests/test_member_offboarding_producers.py` |
| API/controller race handling, explicit confirmation, pending/resume, errors and desktop/mobile layout | `frontend/src/memberOffboarding.test.js`, `frontend/src/MemberOffboardingUi.test.js`, `frontend/src/MemberStatusFilterUi.test.js` |
| Confirmation text contrast on normal/hover/focus and unchanged disabled hover, at desktop/mobile widths; pointer/focus never submits | `frontend/src/MemberOffboardingUi.test.js` (`offboarding confirmation stays readable`); local mocked API browser coverage, not a production removal |
| Forward migration preserves rows/grants; reciprocal removal and producer-lock races are safe | `server/tests/test_member_offboarding_postgres.py` |

Normal discovery includes the new tests. Focused entries from the repository root:

```powershell
python -m pytest server/tests/test_member_offboarding.py server/tests/test_member_offboarding_feishu.py server/tests/test_member_handover.py server/tests/test_member_offboarding_producers.py server/tests/test_identity_management.py server/tests/test_bootstrap.py -q
cd frontend
node --test --test-concurrency=1 src/memberOffboarding.test.js src/MemberOffboardingUi.test.js src/MemberStatusFilterUi.test.js
```

On Windows with a Node version that does not expand the test glob, the full
frontend gate can be run as
`node --test --test-concurrency=1 (Get-ChildItem src/*.test.js | Select-Object -ExpandProperty FullName)`.
Browser gates mock HTTP; API gates use isolated SQLite and fake Feishu providers.
They do not remove real accounts or modify a live Feishu instance.

For the named migration/locking risk, set `MEMBER_OFFBOARDING_POSTGRES_URL` to an
initially empty disposable PostgreSQL database and run
`python -m pytest server/tests/test_member_offboarding_postgres.py -q`. It refuses
nonempty databases and retains synthetic data for caller-owned disposal. Revision
`0045_member_offboarding` is additive and forward-only. A rollback to workers
without successor resolution requires stopping or draining affected screening
work; do not downgrade the database or restore removed accounts as a workaround.

### Interview adjacency regression

Ordinary scheduling uses zero implicit buffer. A 14:00-15:00 interview may touch
an existing 15:00-16:00 interview; actual overlap remains forbidden. Explicit
buffer values on availability/conflict queries retain their existing meaning.

| Contract | Durable regression |
| --- | --- |
| New scheduling and rescheduling allow touching endpoints; 60/90-minute durations still determine actual overlap; saving does not bypass conflicts | `frontend/src/InterviewAdjacent.test.js` |
| Default buffer is zero; explicitly requested buffer is preserved | `frontend/src/interviewController.test.js`, `server/tests/test_interview_adjacency_api.py` |
| Authoritative create/update and calendar conflicts agree with selectable slots; real overlap stays blocked | `server/tests/test_interview_adjacency_api.py`, existing `server/tests/test_interview_api.py` |

Frontend focused entry (from `frontend`):
`node --test --test-concurrency=1 src/InterviewAdjacent.test.js src/interviewController.test.js src/InterviewScheduleState.test.js`.
The normal frontend test gate includes these `src/*.test.js` cases.
Backend focused entry (from the repository root with the test dependencies):
`python -m pytest server/tests/test_interview_adjacency_api.py server/tests/test_interview_api.py server/tests/test_interview_domain.py -q`.
These backend cases run in the normal `server/tests` gate. API tests use isolated
SQLite and synthetic calendar providers; they do not verify live Feishu calendar
data or replace the existing PostgreSQL concurrency gate.

## Screening document parsing and OCR

Scope: resume upload, parsing, OCR rendering, and screening/profile enrichment in the
public product repository.

- Symptom: image resumes or image-only PDFs upload successfully but later produce empty
  parsed text, skip OCR, or fail with a generic parse/render error.
- Cause: the document pipeline has several separate contracts: upload preflight type
  detection, parser `parser_version`/quality classification, the isolated OCR renderer,
  enrichment routing, resume/profile persistence, and the frontend MIME/display states.
  Updating only one layer can leave later workers with no safe OCR path.
- Working fix: for JPG/PNG support or scanned-PDF changes, update the parser and
  `validate_upload_preflight` together, keep PDFium as the renderability authority for
  damaged but renderable image PDFs, route image inputs through
  `IsolatedOcrRenderer.render_image`, and keep `used_ocr` plus
  `ocr_rules`/`ocr_llm` profile source persistence in sync.
- Verification method: run the focused tests for the changed layer first, including
  `python -m pytest server/tests/test_screening.py server/tests/test_ocr_rendering.py server/tests/test_resume_enrichment.py server/tests/test_screening_api.py server/tests/test_migrations.py -q`,
  then run the normal backend gate described above before publishing.

## Migrations

From the repository root, set `DATABASE_URL` to the async PostgreSQL URL for the
bootstrap/migration owner (`POSTGRES_USER`), not the runtime application role:

```powershell
python -m alembic -c server/alembic.ini upgrade head
```

The Phase 0 revision is intentionally empty; domain tables arrive in later tasks.

Alembic never creates roles or stores database passwords. PostgreSQL bootstrap owns the
schema, while API and worker processes connect as the separate `APP_DB_USER`. On a fresh
Compose volume, `deploy/postgres/provision-app-role.sh` creates that login with
`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS` and establishes default
table/sequence privileges. After every migration on an existing volume, rerun the safe,
idempotent grant reconciliation before restarting API/worker processes:

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres `
  sh /docker-entrypoint-initdb.d/10-provision-app-role.sh
```

The application role receives DML on current non-audit tables, append-only access to audit
tables, and no function grants. The separate `GOVERNANCE_DB_USER` login receives no table or
sequence privileges and inherits only the fixed, no-login `ux09_governance_executor` boundary,
which can execute `redact_candidate_data(uuid, uuid, uuid)`. Keep the owner, application, and
governance usernames and passwords pairwise distinct. The provisioning script rejects shared
values, removes stale inbound/outbound governance memberships, and reconciles all three roles
idempotently after migrations. Reconciliation also removes any inherited `ADMIN OPTION` before
restoring the single ordinary executor membership, so the governance login cannot delegate it.

The API container receives none of the governance database, object-deletion, ledger, or signing
settings. The general worker continues to use `DATABASE_URL` for normal queue work and receives a
separate `GOVERNANCE_DATABASE_URL` only for the registered
`governance.delete_candidate` handler.

## Governance deletion execution

The deletion job payload is exactly `organization_id`, `deletion_request_id`, and
`request_version`. Execution locks Candidate before DeletionRequest, revalidates the approved
version, legal hold, active applications, candidate version, and private manifest, then commits
`executing`, exact object checkpoints, and the started audit before any object call.
Before that first checkpoint mutation, the worker constructs and validates the complete canonical
v2 ledger evidence, including manifest/count bounds and exact typed bucket/key locations. Invalid
or oversized evidence fails closed without changing objects, candidates, requests, or checkpoints.

The same Candidate lock is followed by ordered ScreeningItem locks. A matching parse, score, or
LLM job with a live queue lease makes deletion retry before execution side effects; matching
queued jobs are cancelled, while running jobs (including expired leases) are never cancelled.
This pairs with the screening-side active-deletion guard: queue claim may happen first, but no
provider or scoring work can begin after deletion reaches approved or executing. The common lock
order is Candidate, ScreeningItem, then queue job.

New report exports persist exact candidate membership. Export preparation and finalization are
short transactions around an out-of-transaction MinIO write; a generation token prevents a late
writer from reviving a failed export. Deletion cancels only matching queued exports and retries
while a matching export or queue job is running. Exports created before revision 0017 have no
membership rows and are never guessed to belong to a candidate.

Resume and matching export objects are deleted outside database transactions. Each successful or
failed attempt is checkpointed in its own transaction; missing objects count as deleted. Database
redaction starts only after every checkpoint is deleted. The redaction checksum, tombstone time,
manifest hash, and exact checkpoint keys form stable ledger input across retries.

The worker writes the canonical signed ledger last, reads it back through independent signature
verification, and accepts an existing object only when it matches exactly. Only then does one
short Candidate-before-Request transaction persist the ledger receipt, mark the request completed,
and append the completed audit. Completed re-entry verifies the ledger without mutating the
request or `recovery_generation`. A dead-letter callback matches only the exact tenant, request,
version, and executing state; it does not acquire a Candidate lock while the queue row is locked.

## Compose

Copy `deploy/.env.example` to `deploy/.env`, replace every `change-me` value, then run:

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
```

Offer 邮件链路还要求 `EMAIL_ENCRYPTION_KEY` 使用独立的 32 字节 padded base64url 密钥，
并将 `OFFER_PUBLIC_BASE_URL` 设置为候选人实际访问的站点 origin。生产环境必须使用 HTTPS，
例如 `https://recruiting.example.com`，不能包含路径、查询参数或账号信息。SMTP 主机、账号和
密码由系统管理员在网页的邮件设置中保存；Compose 环境只固定发件人、加密密钥和公开地址。

Compose first runs the one-shot `minio-provision` service. MinIO root credentials exist only in
the MinIO server and that provisioner; API and worker runtime use the ordinary application object
credential. The deletion credential can list/delete only configured resume/export prefixes and
cannot read or write objects. The ledger credential can list/read/write only its ledger prefix and
cannot delete or access resume/export objects. Report exports use the ordinary object bucket under
`exports/`, so `GOVERNANCE_EXPORT_BUCKET` must match `OBJECT_STORAGE_BUCKET` and the deletion
prefix must remain `exports/` unless the report storage contract changes with it.
All configured governance prefixes are non-empty relative paths ending in `/`. Retired MinIO
users are ignored only when `mc` explicitly reports that the user does not exist; authentication,
network, or CLI failures stop provisioning without printing the retired key or command output.

The baseline Compose file must keep service logs bounded. A repeated production risk is that
long-running API, worker, PostgreSQL, MinIO, ClamAV, or backup containers use Docker's default
unbounded `json-file` logs until the host disk fills and the stack fails in unrelated ways. Keep the
shared `x-bounded-logging` anchor on runtime and one-shot services, and verify changes with
`docker compose --env-file deploy/.env.example -f deploy/compose.yaml config --quiet` plus a config
readback that each service still resolves `logging.options.max-size` and `max-file`.

`server.app.governance.storage` provides the delete-only adapter and canonical signed ledger v1/v2.
V1 remains readable only for B2B2 completion/redelivery. New deletions write v2, which adds the
original request facts, canonical private manifest, recovery generation, and exact typed
bucket/key descriptors needed for restore recovery. Ledger writes verify an existing object before
accepting idempotent re-entry; malformed, tampered, mismatched, non-canonical, or applicable v1
evidence fails with a stable non-identifying code. Recovery validates every discovered ledger and
the restored database state before it creates a run, checkpoint, or queue job.

## Retention and restore recovery operations

Seed the first daily retention sweep explicitly after deployment (Alembic never schedules jobs):

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm worker `
  python -m server.app.governance.retention_sweep --scheduled-date 2026-07-16
```

Each tenant/date has a stable dedupe key. A sweep claims a bounded batch with `SKIP LOCKED`,
recomputes due facts under lock, excludes active applications/holds/open requests, creates only
non-PII `requested` deletion evidence, and schedules the next UTC date. Configure the bounds with
`GOVERNANCE_RETENTION_SWEEP_BATCH_SIZE` (1-1000, default 100).

Restore recovery is an operator-only CLI and has no HTTP/OpenAPI route. Preserve the ledger bucket
outside the restored data plane, restore PostgreSQL and data objects first, then run:

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm worker `
  python -m server.app.governance.redelete_after_restore `
  --restore-id 00000000-0000-4000-8000-000000000001 `
  --restored-at 2026-07-15T00:00:00Z
```

The CLI accepts only those two arguments. Before its first durable recovery mutation it validates
the separate application/governance PostgreSQL identities, function execute privilege, and the
actual redaction boundary. It proves delete capability with disposable, scoped non-business
canaries and validates ledger list access, all bounded v1/v2 ledger pages, signatures, canonical
evidence, and restored organization plus exact signed organization/candidate pairs in bounded
queries. It then creates one durable checkpoint and queue job per applicable v2 ledger. Concurrent
same-ID calls serialize in PostgreSQL: the same timestamp is idempotent, while another timestamp
fails closed. Workers re-read the exact ledger checksum, repair only minimum non-PII request and
artifact evidence, delete only signed objects, invoke the frozen redaction routine, and increment
the request generation once per restore. Configure the discovery ceiling with
`GOVERNANCE_RECOVERY_MAX_LEDGERS` (1-100000, default 10000). Stop recovery workers before restoring
again; never restore or overwrite the independently preserved ledger bucket.

Validate the example topology without starting services:

```powershell
docker compose --env-file deploy/.env.example -f deploy/compose.yaml config --quiet
```

The backend verification gate is deliberately layered because the test image does not contain a
Docker CLI. Run the backend suite inside the PostgreSQL-enabled test container with exactly:

```text
python -m pytest server/tests --ignore=server/tests/test_production_topology.py --ignore=server/tests/test_observability_topology.py -q
```

Run both Docker/Compose topology files on the host, where the Docker CLI is available:

```powershell
python -m pytest server/tests/test_production_topology.py server/tests/test_observability_topology.py -q
```

These two commands form the complete gate; do not describe the container command alone as the
standard full backend suite.

Only Nginx publishes a host port. Development uses HTTP on `http://localhost:8080`.

Outbox delivery is at-least-once. Every allowlisted topic handler receives the durable
`outbox_events.id` UUID as its mandatory idempotency key. External side effects must use
that key for deduplication because a crash after the side effect and before publication
can replay the same event. The system does not claim externally exactly-once delivery.
When shutdown arrives while a database claim is in flight, the worker does not start the
returned item. It intentionally leaves the lease untouched; database-time lease expiry
and stale-claim recovery make the same durable ID available to another worker.

Feishu notifications must stay on the same outbox contract. Schedule only allowlisted
`feishu.notification.send` events after confirming the organization integration is enabled,
store only tenant/user/application/interview identifiers in the payload, and let the worker
resolve candidate, job, interview, origin, current recipient binding, and the Chinese Card 2.0
payload at delivery time. Keep card actions as `open_url` links to the platform and use the outbox
UUID as the Feishu message idempotency key. Disabled configs,
unbound recipients, stale feedback assignments, or completed feedback are successful no-ops;
provider/network failures remain retryable without logging provider response bodies or private
candidate data. Verify this path with `python -m pytest server/tests/test_feishu_notifications.py`.

Feishu interview calendar sync is also an outbox-generation contract. A recurring failure mode is
that a stale create/update event races a reschedule and creates a duplicate Feishu event or
overwrites the newer meeting URL. Include the sync row generation in the outbox payload, lock the
interview and sync row while persisting the provider result, and return without provider calls when
the generation is stale. Video interviews should let Feishu create the native meeting and persist the
returned meeting URL; onsite and phone interviews must clear any old URL. Add the candidate email as
an attendee only after decrypting it at delivery time. Verify this path with:

```powershell
python -m pytest server/tests/test_feishu_contract.py server/tests/test_feishu_sync.py server/tests/test_interview_api.py
```

Interview email delivery depends on the same versioned calendar state. A recurring failure mode is
that a video-interview email is sent before Feishu has returned the native meeting URL, or an older
invite is sent after a reschedule/cancellation. Before sending invitation and reschedule email,
materialize any current safe `https` meeting URL into the plain-text body, HTML action links, and ICS
attachment. If the matching Feishu sync row is still pending, retry only within the bounded job
window; if no safe meeting URL is available at the final attempt, terminate the delivery without
sending placeholder meeting details. Compare the delivery ICS sequence with the current interview sequence
and mark superseded deliveries `cancelled` before any provider call. Temporary SMTP failures should
become terminal after the configured real send-attempt ceiling instead of retrying forever. Verify
this path with:

```powershell
python -m pytest server/tests/test_email_worker.py server/tests/test_interview_api.py -q
```

Interview availability queries must also fail open around Feishu, not around internal conflicts. A
slow or unavailable Feishu freebusy call should have a short total timeout and a bounded concurrency
slot, then return internal ATS busy windows with external status `unknown`; it must not hold the
schedule UI for provider latency or hide existing local interview conflicts. Verify this with:

```powershell
python -m pytest server/tests/test_feishu_availability.py -q
```

Candidate detail interview actions are application-scoped. A recurring failure mode is that the
candidate detail page only sees the global interview list's first page, so an existing interview for
the selected application cannot be rescheduled or given feedback, or another application for the
same candidate leaks into the history. Keep `GET /api/v1/interviews` filterable by
`application_id`, include that filter in the signed cursor facts, have the frontend controller page
through every interview for the selected application, and derive candidate history/actions from
matching server-backed `candidateId` plus `applicationId`. Verify this with:

```powershell
python -m pytest server/tests/test_interview_api.py -q
cd frontend
node --test --test-concurrency=1 src/interviewController.test.js src/AppInterviewIntegration.test.js src/ScreeningViews.test.js
```

Production TLS must terminate upstream or use externally managed certificates mounted into
Nginx with a production-specific server block. Never expose API, PostgreSQL, or MinIO ports.

Readiness probes run concurrently and cancel siblings after a failure. The worker dependency
cycle is bounded by `READINESS_TIMEOUT_SECONDS` (5 seconds by default). MinIO uses explicit
1-second connect, 3-second read, and 4-second total network deadlines with retries disabled.
Cancelling the async wrapper does not stop a running OS thread instantly; the underlying MinIO
network deadline provides the finite bound required for worker process shutdown.

## Production requirements

Set `APP_ENVIRONMENT=production`, distinct non-placeholder PostgreSQL owner and application
passwords, distinct MinIO
access and secret keys, and explicit HTTPS CORS origins. Startup rejects placeholders and
wildcard CORS. Credential values beginning with `change-me`, `placeholder`, or `replace-me`
(including suffixed example values) are rejected without echoing the value. Buckets are private and must be provisioned separately; the application never
enables anonymous/public access. Rotate secrets through the deployment environment, not Git.

Rotate governance credentials through the deployment secret store. Stop the governance deletion
consumer, rerun PostgreSQL role and MinIO policy provisioning with the new values, deploy worker
secrets atomically, and run the real PostgreSQL and MinIO smoke tests before resuming work. When
rotating MinIO access-key identities, set `PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY` and/or
`PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY` for the provisioning run; the provisioner removes those
retired users after attaching the new least-privilege policies. Production requires the independent
ledger signing key to contain at least 32 UTF-8 bytes, no whitespace or placeholder text, and
non-trivial character diversity.
Signing-key rotation requires explicit ledger key history/versioning; it is not a safe online
single-key swap because prior ledgers must remain independently verifiable.

Candidate contacts require separate high-entropy 32-byte base64url
`CONTACT_ENCRYPTION_KEY` and `CONTACT_LOOKUP_SECRET` values. Generate them independently;
the encryption value is a Fernet key and neither value is committed. Rotation is an offline
maintenance boundary: stop writes, decrypt and re-encrypt all
contacts, recompute lookup hashes, reconcile row counts and duplicate constraints, then deploy
both new values atomically. Online dual-key rotation is outside Phase 2.

LLM provider API keys use another independent high-entropy 32-byte base64url
`LLM_CONFIG_ENCRYPTION_KEY`. Deployment operators may define platform-wide providers and models
with `LLM_PROVIDER_ALLOWLIST_JSON`. System administrators can also add organization-scoped,
OpenAI-compatible providers from **Settings > AI settings > Add Provider**. The application stores
the Provider ID, display name, Base URL, and model allowlist in PostgreSQL; API keys remain
encrypted separately and are never returned. For example, a platform-wide provider can still be
defined as:

```text
LLM_PROVIDER_ALLOWLIST_JSON={"openai":{"base_url":"https://api.openai.com/v1","models":["gpt-4.1-mini"]}}
```

Production provider URLs must use HTTPS on port 443. Literal private, loopback, link-local, and
reserved addresses are rejected when a provider is added. Connection tests and screening calls
resolve and validate every DNS answer, pin one public address for the TLS request, and reject
redirects. The settings connection test sends only a constant health-check prompt; it never sends
JD or resume content.

## LLM-only screening flow

This section describes new screening tasks in the server ATS. The local desktop tool documented in
the repository root is a separate legacy product; its local rule scoring and optional LLM mode do
not describe this server flow.

For a new server screening task, the LLM is the only scoring source. It returns five bounded
dimensions totaling 100 points: core capability (35), experience depth (25), role seniority (20),
transferability (10), and explicit constraints (10). The server derives the recommendation and
route from that total:

- 85-100: `优先评审`; automatically enter hiring-manager review.
- 60-84: `建议评审`; automatically enter hiring-manager review.
- 0-59: `暂缓`; automatically enter the system-managed AI deferred talent pool.

After the final LLM attempt fails, the server fails open to hiring-manager review with `score=null`
and displays `AI评分不可用`. It never invents a score, routes the candidate from a legacy rule
result, or creates an HR manual-review step. Status changes are driven by these routing actions;
HR users do not edit screening status manually.

Parsing failures, malware detection, unsupported formats, missing files, candidate-creation
failures, and similar pre-routing failures remain technical failures. They do not become an LLM
recommendation or an automatic talent decision.

Historical rule results and prompt v1 records remain read-only compatibility data. New prompts,
scores, routing decisions, and reports do not use them. Current LLM facts are exposed without
prompt text, provider bodies, input hashes, or API keys.

An empty LLM job allowlist applies to every job in the tenant; a non-empty list limits evaluation
to those job IDs.

Before evaluation, the worker removes recognized email/phone values, labeled name/address fields,
and the known candidate display name, then applies strict input limits. This deterministic
redaction is not a general DLP guarantee. Deploy only an approved provider that satisfies the
organization's privacy and data-processing requirements.

Candidate notes are append-only MVP facts, matching JD, rule, resume, stage-event, and
candidate-event history. Corrections create a new note/event instead of editing prior text.
