# Task 3 Report: Transactional Email Delivery

## Status

Complete. The reusable transactional email foundation is implemented and ready for the later interview and Offer email tasks. No interview- or Offer-specific sending behavior was added.

## Backend behavior delivered

- Added tenant-scoped, single-row SMTP provider configuration with encrypted password storage, masked readback, optimistic versions, and `starttls`/`tls`-only validation.
- Added versioned email templates with a strict variable allowlist, missing/unknown variable rejection, escaped body substitutions, and header-injection protection.
- Added durable email deliveries containing encrypted recipient data, masked recipient display, immutable rendered subject/body snapshots, provider/template versions, business resource identifiers, attempts, safe errors, and opaque receipts.
- Added transaction-friendly, business-key-idempotent `enqueue_delivery` behavior and an exact `{organization_id, delivery_id}` queue payload with three total attempts.
- Added aiosmtplib SMTP delivery with bounded timeout, mandatory certificate verification, temporary/permanent safe error classification, retry-state preservation, and idempotent terminal failure notification/audit behavior.
- Added system-admin SMTP settings and test-send APIs, recruiting-admin template/history/resend APIs, `If-Match` and `Idempotency-Key` handling, no-store responses, status filtering, and stable cursor pagination.
- Registered the API router, worker handler, payload policy, and allowlisted terminal callback.

## Data and migration

- Added reversible revision `0031_email_delivery`, directly following `0030_candidate_contact_confirmation`.
- Added `email_provider_configs`, `email_templates`, and `email_deliveries` with explicit tenant keys, composite foreign keys, unique constraints, state/version checks, and a history index.
- Updated the shared migration-head assertion and expected table set. This is the sole owned-file exception: `server/tests/test_migrations.py` was required by the explicit migration-head clarification.

## Dependency and security notes

- Pinned `aiosmtplib==5.1.2` in runtime requirements and installed that exact version into the isolated `server/.venv` for verification. `requirements-dev.txt` already includes runtime dependencies via `-r requirements.txt`, so no duplicate pin was added there.
- Recorded aiosmtplib 5.1.2 as MIT-licensed in `THIRD_PARTY_NOTICES.md`; installed package metadata was also checked as MIT.
- The fixed enterprise sender comes only from process settings. Each delivery stores the responsible HR name/email as Reply-To.
- Recipient plaintext and raw SMTP exception text are excluded from deliveries, queue payloads, audit metadata, and logs. Provider receipts are locally generated opaque UUIDs because SMTP responses can contain recipient data.

## Verification

- TDD red phase: the focused tests initially failed during collection because the communications modules and queue policy did not exist.
- `server/.venv/Scripts/python.exe -m pytest -q server/tests/test_communications_api.py server/tests/test_email_worker.py server/tests/test_queue.py server/tests/test_worker.py` -> 25 passed.
- Combined focused/settings/head gate -> 93 passed, with one existing Alembic `path_separator` deprecation warning.
- Isolated `0031` SQLite upgrade/downgrade exercise -> passed.
- Python compileall for communications, queue, worker, settings, and migration -> passed.
- `git diff --check` -> passed.

## Remaining risk

- PostgreSQL migration smoke was not run because `POSTGRES_SMOKE_URL` was not configured. Revision 0031 was instead exercised through an isolated upgrade/downgrade and the Alembic head test.
- Full offline SQL generation cannot pass the repository's pre-existing revision 0016 because that migration calls `MockConnection.scalar`; this failure occurs before revision 0031 and was not changed in Task 3.
