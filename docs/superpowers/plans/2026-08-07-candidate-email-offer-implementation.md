# Candidate Email and Offer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add candidate email extraction and confirmation, transactional interview email, versioned Offer creation and approval, and a login-free Offer response page without changing the existing internal recruitment workflow semantics.

**Architecture:** Extend encrypted `CandidateContact` records with provenance and confirmation metadata, then add a bounded `communications` module backed by the durable job queue. Add a separate `offers` domain for versioning and approval; the public response API is token-scoped and isolated from authenticated recruiting APIs. Deliver the work in four independently releasable milestones.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, encrypted secrets, durable queue workers, `aiosmtplib`, WeasyPrint, React 19, Vite, Node test runner, Playwright smoke tests, Docker Compose.

## Global Constraints

- Candidates never create accounts or log in.
- Interview emails are sent only for initial invitation, reschedule, and cancellation; no timed candidate reminders.
- A fixed enterprise sender is used with the responsible HR user as `Reply-To`.
- The first outbound email requires an HR-confirmed candidate email address.
- LLM output may classify extracted addresses but must never invent an email address.
- Approval completion never sends an Offer automatically.
- Ordinary Offers use the job's default Offer approver; HR-marked special Offers append the fixed organization approval chain.
- Offer acceptance has no electronic-signature meaning.
- Candidate self-service and HR proxy entry remain distinct, immutable audit sources.
- Offer business, approval, delivery, and application states remain separate.
- Public Offer `GET` requests are read-only; state changes require explicit idempotent `POST` requests.
- Public code must not contain Aurora-specific domains, accounts, addresses, candidate data, certificates, or secrets.

## File Structure

### Existing files to modify

- `server/app/recruiting/models.py`: contact confirmation metadata and Offer relationships needed by recruiting projections.
- `server/app/recruiting/api.py`: authorized candidate-email projection and workflow compatibility only; new Offer behavior belongs in `offers`.
- `server/app/recruiting/security.py`: reuse encrypted contact normalization; do not add plaintext persistence.
- `server/app/screening/pipeline.py`: persist deterministically extracted emails after final native/OCR text is available.
- `server/app/interviews/api.py`: enqueue invitation, reschedule, and cancellation deliveries in the same transaction as interview events.
- `server/app/identity/models.py`: job default Offer approver and organization-level special approval configuration references.
- `server/app/queue/payloads.py`: register bounded communications job payloads.
- `server/app/queue/repository.py`: add communications terminal callback types.
- `server/app/worker/main.py`: register email delivery and Offer PDF handlers.
- `server/app/core/settings.py`: email secret key and safe SMTP timeout bounds.
- `server/app/main.py`: register communications, Offer, and public Offer routers; exempt only the token-scoped public response route from session CSRF.
- `server/requirements.txt`, `server/requirements-dev.txt`, `server/Dockerfile`, `THIRD_PARTY_NOTICES.md`: locked mail/PDF dependencies and runtime libraries.
- `frontend/src/App.jsx`: authenticated Offer routes, workbench tasks, and isolated public Offer route.
- `frontend/src/CandidateViews.jsx`: email confirmation and Offer tab entry.
- `frontend/src/JobViews.jsx`: default Offer approver and Offer template fields.
- `frontend/src/SettingsViews.jsx`: email, Offer-template, and special-approver settings.
- `frontend/src/InterviewViews.jsx`: delivery status and resend action without adding reminder controls.
- `frontend/src/appRouter.js`: public `/offer/:token` and authenticated Offer paths.
- `deploy/compose.yaml`, `scripts/community_setup.py`: generic encrypted mail configuration only.

### New bounded modules

- `server/app/communications/models.py`: provider config, templates, and delivery records.
- `server/app/communications/schemas.py`: strict API and worker contracts.
- `server/app/communications/extraction.py`: deterministic email extraction and filtering.
- `server/app/communications/security.py`: encrypted SMTP credential and safe delivery rendering helpers.
- `server/app/communications/provider.py`: SMTP provider protocol and `AiosmtplibProvider`.
- `server/app/communications/service.py`: confirmation, template rendering, enqueue, and delivery state transitions.
- `server/app/communications/worker.py`: retryable email delivery job handler.
- `server/app/communications/api.py`: authenticated settings, template, delivery, resend, and candidate email APIs.
- `server/app/offers/models.py`: Offer, version, approval, response, and event records; Task 9 adds access-token persistence.
- `server/app/offers/schemas.py`: internal and public strict contracts.
- `server/app/offers/service.py`: state machine, versioning, approval, sending, expiry, withdrawal, and proxy response.
- `server/app/offers/pdf.py`: deterministic HTML-template-to-PDF rendering.
- `server/app/offers/security.py`: hashed token issue, validation, revocation, and constant-time comparison.
- `server/app/offers/api.py`: authenticated Offer and approval APIs.
- `server/app/offers/public_api.py`: minimum-field token-scoped candidate API.
- `frontend/src/emailSettingsController.js`: settings and test-send API state.
- `frontend/src/offerController.js`: internal Offer commands and projections.
- `frontend/src/OfferViews.jsx`: HR and approver surfaces.
- `frontend/src/PublicOfferView.jsx`: standalone login-free response page.

---

## Milestone 1: Candidate Email and Mail Foundation

### Task 1: Add Contact Provenance and Confirmation

**Files:**
- Create: `server/migrations/versions/0030_candidate_contact_confirmation.py`
- Modify: `server/app/recruiting/models.py`
- Modify: `server/app/recruiting/api.py`
- Test: `server/tests/test_recruiting_api.py`
- Test: `server/tests/test_migrations.py`

**Interfaces:**
- Produces: `GET /api/v1/candidates/{candidate_id}/email`
- Produces: `PUT /api/v1/candidates/{candidate_id}/email` with `If-Match`
- Produces projection `{masked_value, value, source, confirmation_status, confirmed_at, version}` for authorized HR roles.

- [ ] **Step 1: Write failing API and migration tests**

```python
def test_candidate_email_requires_hr_confirmation_before_delivery(client):
    email = client.get(f"/api/v1/candidates/{candidate_id}/email", headers=hr_headers)
    assert email.json()["data"]["confirmation_status"] == "unconfirmed"
    confirmed = client.put(
        f"/api/v1/candidates/{candidate_id}/email",
        json={"value": "candidate@example.test"},
        headers={**hr_headers, "If-Match": '"1"', "Idempotency-Key": "confirm-email"},
    )
    assert confirmed.json()["data"]["confirmation_status"] == "confirmed"
```

- [ ] **Step 2: Verify the tests fail before schema changes**

Run: `python -m pytest -q server/tests/test_recruiting_api.py -k candidate_email server/tests/test_migrations.py`

Expected: FAIL because confirmation columns and endpoints do not exist.

- [ ] **Step 3: Add the forward-only migration and model fields**

```python
source: Mapped[str] = mapped_column(String(32), default="manual")
confirmation_status: Mapped[str] = mapped_column(String(16), default="unconfirmed")
confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
version: Mapped[int] = mapped_column(Integer, default=1)
```

Migration rules: backfill existing rows as `source='legacy'`, `confirmation_status='unconfirmed'`, add tenant-scoped confirmer FK, and add check constraints for `source` and `confirmation_status`.

- [ ] **Step 4: Implement the dedicated no-store HR endpoint**

Decrypt only within the authorized endpoint, audit reads and writes, reject non-email contact types, preserve lookup-hash uniqueness, and never add plaintext to candidate list responses.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -q server/tests/test_recruiting_api.py -k candidate_email server/tests/test_migrations.py`

Commit: `git commit -m "Add candidate email confirmation"`

### Task 2: Extract Emails from Final Resume Text

**Files:**
- Create: `server/app/communications/extraction.py`
- Modify: `server/app/screening/pipeline.py`
- Modify: `server/app/recruiting/profile_jobs.py`
- Test: `server/tests/test_candidate_email_extraction.py`
- Test: `server/tests/test_screening_pipeline.py`

**Interfaces:**
- Produces: `extract_email_candidates(text: str) -> tuple[str, ...]`
- Produces: `persist_extracted_emails(db, *, organization_id, candidate_id, values, source, cipher) -> int`

- [ ] **Step 1: Write extraction and persistence tests**

```python
def test_extract_email_candidates_filters_masked_and_example_addresses():
    assert extract_email_candidates(
        "邮箱 candidate@example.com，备用 c***@mail.com，示例 user@example.test"
    ) == ("candidate@example.com",)
```

Cover Unicode punctuation, case-insensitive deduplication, OCR whitespace around `@`, multiple valid addresses, invalid domains, existing cross-candidate uniqueness conflicts, and preservation of confirmed/manual contacts.

- [ ] **Step 2: Verify focused tests fail**

Run: `python -m pytest -q server/tests/test_candidate_email_extraction.py server/tests/test_screening_pipeline.py -k email`

- [ ] **Step 3: Implement deterministic extraction**

Use `email_validator` after a bounded regex candidate scan. Do not call the LLM, do not reconstruct masked local parts, cap candidate count at 10, and label persistence source as `native`, `ocr`, or `manual`.

- [ ] **Step 4: Integrate after enriched text is finalized**

Call persistence from both `ScreeningPipeline.parse_item` and `ResumeProfileJobHandler` so image resumes, OCR PDFs, and profile backfills converge on the same contact behavior. Use the existing `ContactCipher` and skip candidates under deletion governance.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -q server/tests/test_candidate_email_extraction.py server/tests/test_screening_pipeline.py server/tests/test_profile_builder.py`

Commit: `git commit -m "Extract candidate emails from resumes"`

### Task 3: Add SMTP Configuration, Templates, and Durable Delivery

**Files:**
- Create: `server/migrations/versions/0031_email_delivery.py`
- Create: `server/app/communications/models.py`
- Create: `server/app/communications/schemas.py`
- Create: `server/app/communications/security.py`
- Create: `server/app/communications/provider.py`
- Create: `server/app/communications/service.py`
- Create: `server/app/communications/worker.py`
- Create: `server/app/communications/api.py`
- Modify: `server/app/queue/payloads.py`
- Modify: `server/app/queue/repository.py`
- Modify: `server/app/worker/main.py`
- Modify: `server/app/core/settings.py`
- Modify: `server/app/main.py`
- Modify: `server/requirements.txt`
- Modify: `server/requirements-dev.txt`
- Modify: `THIRD_PARTY_NOTICES.md`
- Test: `server/tests/test_communications_api.py`
- Test: `server/tests/test_email_worker.py`
- Test: `server/tests/test_queue.py`

**Interfaces:**
- Produces: `MailProvider.send(message: MailMessage) -> ProviderReceipt`
- Produces: `enqueue_delivery(db, command: DeliveryCommand) -> EmailDelivery`
- Produces queue job `communications.send_email` with `{organization_id, delivery_id}` only.

- [ ] **Step 1: Write model, provider, queue-policy, and retry tests**

```python
async def test_worker_retries_temporary_smtp_failure_without_marking_sent():
    provider = FakeMailProvider(failures=[TemporaryMailError("smtp_timeout")])
    await EmailDeliveryJobHandler(sessions, provider)(job)
    assert stored_delivery.status == "queued"
    assert stored_delivery.safe_error_code == "smtp_timeout"
```

Cover encrypted password readback, TLS modes `starttls` and `tls`, fixed sender, HR reply-to, final failure, idempotent delivery, no raw recipient or SMTP error in logs, and template variable allowlists.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_communications_api.py server/tests/test_email_worker.py server/tests/test_queue.py`

- [ ] **Step 3: Add models and migration**

Create tenant-scoped `email_provider_configs`, `email_templates`, and `email_deliveries`. Store encrypted credentials, template versions, recipient ciphertext, masked recipient, immutable rendered subject/body, business resource identifiers, status, attempts, safe error code, provider receipt ID, and optimistic version.

- [ ] **Step 4: Implement Provider and job handler**

Use `aiosmtplib` with bounded connect/read timeout, no certificate bypass, and typed temporary/permanent failures. Register only opaque IDs in queue payloads and set a maximum of three attempts using existing exponential retry behavior.

- [ ] **Step 5: Implement settings, template, test-send, history, and resend APIs**

Use system/recruiting-admin permission checks, `If-Match`, idempotency keys, no-store responses, safe errors, and existing encrypted-secret response conventions. Test-send must use only saved configuration and a fresh idempotency key.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest -q server/tests/test_communications_api.py server/tests/test_email_worker.py server/tests/test_queue.py server/tests/test_worker.py`

Commit: `git commit -m "Add transactional email delivery"`

### Task 4: Add Email Settings and Candidate Confirmation UI

**Files:**
- Create: `frontend/src/emailSettingsController.js`
- Create: `frontend/src/emailSettingsController.test.js`
- Modify: `frontend/src/SettingsViews.jsx`
- Modify: `frontend/src/CandidateViews.jsx`
- Modify: `frontend/src/candidateController.js`
- Modify: `frontend/src/candidateController.test.js`
- Modify: `frontend/src/product-theme-admin.css`
- Modify: `frontend/src/product-theme-people.css`
- Test: `frontend/src/FrontendClosure.test.js`

**Interfaces:**
- Consumes: Task 1 candidate email endpoints and Task 3 communications settings APIs.
- Produces: `candidateController.getCandidateEmail`, `confirmCandidateEmail`, and `emailSettingsController.testSavedConfiguration`.

- [ ] **Step 1: Write controller and source-level UI tests**

```javascript
test("first outbound email requires an explicitly confirmed address", async () => {
  const email = await controller.getCandidateEmail(candidateId);
  assert.equal(email.confirmationStatus, "unconfirmed");
  await controller.confirmCandidateEmail(candidateId, email.version, "candidate@example.com");
});
```

- [ ] **Step 2: Verify tests fail**

Run: `node --test --test-concurrency=1 src/emailSettingsController.test.js src/candidateController.test.js src/FrontendClosure.test.js`

- [ ] **Step 3: Implement the email settings section**

Add SMTP host, port, TLS mode, username, replace-password control, sender name/address, default reply-to fallback, saved-configuration test, dirty-state navigation guard, and safe error messages. Never return or retain a saved password in frontend state.

- [ ] **Step 4: Implement candidate email confirmation**

Show masked email by default; authorized HR opens a focused confirmation dialog with the decrypted address, source label, multiple-address choice, and explicit confirmation. Keep the action out of list rows to avoid exposing plaintext in bulk views.

- [ ] **Step 5: Run frontend tests/build and commit**

Run: `npm test && npm run build`

Commit: `git commit -m "Add candidate email settings UI"`

## Milestone 2: Interview Transactional Email

### Task 5: Send Interview Invitation, Reschedule, and Cancellation Email

**Files:**
- Create: `server/app/communications/interview_messages.py`
- Modify: `server/app/interviews/api.py`
- Modify: `server/app/interviews/domain.py`
- Modify: `server/app/communications/service.py`
- Test: `server/tests/test_interview_api.py`
- Test: `server/tests/test_email_worker.py`
- Modify: `frontend/src/InterviewViews.jsx`
- Modify: `frontend/src/interviewController.js`
- Test: `frontend/src/InterviewViews.test.js`

**Interfaces:**
- Consumes: `enqueue_delivery` from Task 3 and confirmed email from Task 1.
- Produces message kinds `interview_invitation`, `interview_rescheduled`, and `interview_cancelled`.

- [ ] **Step 1: Write transaction and rendering tests**

Assert create/reschedule/cancel each enqueue exactly one correct delivery, include a valid ICS attachment, never enqueue reminders, and preserve the saved interview when email delivery later fails.

- [ ] **Step 2: Run failing tests**

Run: `python -m pytest -q server/tests/test_interview_api.py -k email server/tests/test_email_worker.py -k interview`

- [ ] **Step 3: Implement message rendering and transactional enqueue**

Resolve candidate email and responsible HR reply-to inside the interview command transaction. Reject scheduling only when no confirmed email exists; a downstream SMTP failure must not roll back an already committed interview.

- [ ] **Step 4: Add delivery status and resend UI**

Display `待发送`, `已发送`, or `发送失败` alongside invitation status. Show “修正邮箱” and “重新发送” only for authorized HR; do not add candidate reminder actions.

- [ ] **Step 5: Run backend/frontend checks and commit**

Run: `python -m pytest -q server/tests/test_interview_api.py server/tests/test_email_worker.py`

Run: `node --test --test-concurrency=1 src/InterviewViews.test.js src/interviewController.test.js && npm run build`

Commit: `git commit -m "Send candidate interview email"`

## Milestone 3: Offer Creation and Internal Approval

### Task 6: Add Offer Domain, Versioning, and Job Approval Defaults

**Files:**
- Create: `server/migrations/versions/0032_offer_workflow.py`
- Create: `server/app/offers/models.py`
- Create: `server/app/offers/schemas.py`
- Create: `server/app/offers/service.py`
- Modify: `server/app/identity/models.py`
- Modify: `server/app/recruiting/schemas.py`
- Modify: `server/app/recruiting/service.py`
- Test: `server/tests/test_offer_service.py`
- Test: `server/tests/test_recruiting_api.py`
- Test: `server/tests/test_migrations.py`

**Interfaces:**
- Produces: `OfferCommand`, `OfferVersionCommand`, `submit_offer`, `decide_approval`, `withdraw_offer`, and `expire_due_offers`.
- Adds job fields `offer_approver_id` and `offer_template_id`.

- [ ] **Step 1: Write state-machine and tenant-isolation tests**

```python
def test_special_offer_appends_fixed_approvers_after_job_default():
    offer = submit_offer(db, command=special_offer)
    assert [step.assignee_id for step in offer.approvals] == [job.offer_approver_id, *organization.special_offer_approver_ids]
```

Cover draft edits, immutable submitted versions, sequential approval, rejection with reason, stale `If-Match`, withdrawal, expiry, application-stage mapping, and cross-tenant rejection.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_offer_service.py server/tests/test_recruiting_api.py -k offer server/tests/test_migrations.py`

- [ ] **Step 3: Add models and migration**

Create `offer_templates`, `offers`, `offer_versions`, `offer_approvals`, `offer_responses`, and `offer_events`; add job default approver/template fields and an ordered organization special-approver table. Use database check constraints for all status enums and one-current-version uniqueness. Access tokens are deliberately deferred to Task 9.

- [ ] **Step 4: Implement state transitions**

Keep Offer, approval, email delivery, and application status independent. Only accepted responses move `passed -> hired`; only declined responses move `passed -> withdrawn`; expiry keeps `passed`.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -q server/tests/test_offer_service.py server/tests/test_recruiting_api.py -k offer server/tests/test_migrations.py`

Commit: `git commit -m "Add versioned Offer workflow"`

### Task 7: Add Offer Templates and Deterministic PDF Generation

**Files:**
- Create: `server/app/offers/pdf.py`
- Modify: `server/app/offers/service.py`
- Modify: `server/Dockerfile`
- Modify: `server/requirements.txt`
- Modify: `server/requirements-dev.txt`
- Modify: `THIRD_PARTY_NOTICES.md`
- Test: `server/tests/test_offer_pdf.py`
- Create: `server/tests/test_dependency_licenses.py`

**Interfaces:**
- Produces: `render_offer_pdf(template_html: str, variables: Mapping[str, str]) -> bytes`.

- [ ] **Step 1: Write rendering and safety tests**

Verify Chinese text extraction, stable page count, missing required variables, HTML escaping, blocked external URLs, maximum output size, and deterministic rendering of identical inputs.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_offer_pdf.py server/tests/test_dependency_licenses.py`

- [ ] **Step 3: Add WeasyPrint runtime and renderer**

Pin WeasyPrint, add only required Debian runtime libraries, disallow network fetches with a custom URL fetcher, embed approved local fonts/assets, and store generated PDF through the existing private object-storage abstraction.

- [ ] **Step 4: Run PDF and image-build tests and commit**

Run: `python -m pytest -q server/tests/test_offer_pdf.py server/tests/test_dependency_licenses.py`

Run: `docker build --target test -t beyondcandidate-server-test-offers -f server/Dockerfile .`

Commit: `git commit -m "Generate Offer PDFs from templates"`

### Task 8: Add Authenticated Offer APIs and Internal UI

**Files:**
- Create: `server/app/offers/api.py`
- Modify: `server/app/main.py`
- Test: `server/tests/test_offer_api.py`
- Create: `frontend/src/offerController.js`
- Create: `frontend/src/offerController.test.js`
- Create: `frontend/src/OfferViews.jsx`
- Modify: `frontend/src/CandidateViews.jsx`
- Modify: `frontend/src/JobViews.jsx`
- Modify: `frontend/src/SettingsViews.jsx`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/roleCapabilities.js`
- Test: `frontend/src/OfferViews.test.js`
- Test: `frontend/src/roleCapabilities.test.js`

**Interfaces:**
- Produces internal endpoints under `/api/v1/offers` and `/api/v1/offer-approvals`.
- Produces frontend methods `createOffer`, `updateDraft`, `submitApproval`, `approve`, `requestChanges`, `send`, `withdraw`, and `listHistory`.

- [ ] **Step 1: Write API/controller/permission tests**

Cover HR draft ownership, approver-only decisions at the current step, mandatory special reason, approved-but-unsent state, version conflict refresh, and visibility of sensitive compensation only to authorized roles.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_offer_api.py`

Run: `node --test --test-concurrency=1 src/offerController.test.js src/OfferViews.test.js src/roleCapabilities.test.js`

- [ ] **Step 3: Implement strict APIs**

Use idempotency, `If-Match`, safe problem codes, audit events, tenant scope, and no-store. Sending must require an approved current version, confirmed candidate email, and an explicit HR command.

- [ ] **Step 4: Implement internal surfaces**

Add candidate Offer tab, job default approver/template fields, organization template and special-approver settings, workbench approval tasks, HR send/withdraw/version actions, and an explicit “特殊 Offer” switch with required explanation.

- [ ] **Step 5: Run focused and frontend regression checks, then commit**

Run: `python -m pytest -q server/tests/test_offer_api.py server/tests/test_offer_service.py`

Run: `npm test && npm run build`

Commit: `git commit -m "Add internal Offer management"`

## Milestone 4: Candidate Response and HR Proxy Entry

### Task 9: Add Token-Scoped Public Offer API

**Files:**
- Create: `server/migrations/versions/0033_offer_access_tokens.py`
- Modify: `server/app/offers/models.py`
- Create: `server/app/offers/security.py`
- Create: `server/app/offers/public_api.py`
- Modify: `server/app/main.py`
- Modify: `deploy/nginx/default.conf`
- Modify: `deploy/nginx/production.conf.template`
- Test: `server/tests/test_public_offer_api.py`
- Test: `server/tests/test_nginx_routes.py`

**Interfaces:**
- Produces: `GET /api/public/v1/offers/{token}`.
- Produces: `POST /api/public/v1/offers/{token}/responses` with `{decision, expected_start_date, reason_text}`.

- [ ] **Step 1: Write token, scanner-safety, expiry, and concurrency tests**

```python
def test_public_get_never_changes_offer_state(client):
    for _ in range(3):
        assert client.get(f"/api/public/v1/offers/{token}").status_code == 200
    assert load_offer().status == "sent"
```

Cover hashed token storage, constant-time validation, revoked/superseded/expired responses, no PII in URL or logs, idempotent duplicate POST, accept-versus-decline race, and minimum public projection.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_public_offer_api.py server/tests/test_nginx_routes.py`

- [ ] **Step 3: Implement token issue and public routes**

Issue a 256-bit token only when an approved Offer is sent, store only SHA-256 hash, scope it to one Offer version, and add `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, a restrictive CSP, rate limits, and generic invalid-link responses.

- [ ] **Step 4: Implement atomic response recording**

Lock the Offer and current token row, validate current version and deadline, insert one immutable response, transition the application through the existing workflow service, revoke the token for further mutations, and enqueue confirmation mail plus HR/Feishu notification.

- [ ] **Step 5: Run focused security tests and commit**

Run: `python -m pytest -q server/tests/test_public_offer_api.py server/tests/test_nginx_routes.py server/tests/test_application_workflow_actions.py`

Commit: `git commit -m "Add secure candidate Offer responses"`

### Task 10: Add the Standalone Candidate Offer Page

**Files:**
- Create: `frontend/src/publicOfferController.js`
- Create: `frontend/src/publicOfferController.test.js`
- Create: `frontend/src/PublicOfferView.jsx`
- Create: `frontend/src/PublicOfferView.test.js`
- Create: `frontend/src/product-theme-public-offer.css`
- Modify: `frontend/src/App.jsx`
- Modify: `frontend/src/appRouter.js`
- Modify: `frontend/index.html`
- Test: `frontend/src/appRouter.test.js`

**Interfaces:**
- Consumes: Task 9 public endpoints.
- Produces standalone `/offer/:token` route that never bootstraps an employee session.

- [ ] **Step 1: Write controller and UI-state tests**

Cover loading, active, already accepted, already declined, expired, withdrawn, superseded, invalid link, PDF download, required start date, optional decline reason, double-submit lock, and 390/768/1280 widths.

- [ ] **Step 2: Verify tests fail**

Run: `node --test --test-concurrency=1 src/publicOfferController.test.js src/PublicOfferView.test.js src/appRouter.test.js`

- [ ] **Step 3: Implement isolated routing and API state**

Do not call `/api/v1/me`, do not render employee navigation, do not persist tokens outside the route, and clear sensitive response data after submission. Use explicit confirmation before the final POST.

- [ ] **Step 4: Implement the accessible candidate page**

Show enterprise identity, greeting, job, location, deadline, responsible HR contact, Offer PDF, accept and decline actions, expected-start-date input, and final immutable result. Do not show AI data, interview feedback, internal approval, or employee identifiers.

- [ ] **Step 5: Run tests/build and commit**

Run: `npm test && npm run build`

Commit: `git commit -m "Add candidate Offer confirmation page"`

### Task 11: Add HR Proxy Response and Result Notifications

**Files:**
- Modify: `server/app/offers/api.py`
- Modify: `server/app/offers/service.py`
- Modify: `server/app/integrations/feishu/worker.py`
- Modify: `server/app/notifications/api.py`
- Test: `server/tests/test_offer_api.py`
- Test: `server/tests/test_feishu_notifications.py`
- Modify: `frontend/src/OfferViews.jsx`
- Modify: `frontend/src/offerController.js`
- Test: `frontend/src/OfferViews.test.js`

**Interfaces:**
- Produces: `POST /api/v1/offers/{offer_id}/proxy-responses` with `{decision, expected_start_date, channel, communicated_at, note}`.

- [ ] **Step 1: Write proxy-source and notification tests**

Assert channel and communication time are required, accepted responses require start date, source is `hr_proxy`, candidate confirmation mail is queued, responsible HR receives one notification, and concurrent public response/proxy entry yields one final result.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -q server/tests/test_offer_api.py -k proxy server/tests/test_feishu_notifications.py -k offer`

- [ ] **Step 3: Implement proxy command and audit projection**

Reuse the same atomic response service as public submission with a different source contract. Record actor, channel, communicated time, note, application version, and Offer version without impersonating the candidate.

- [ ] **Step 4: Implement HR dialog and result timeline**

Add “代候选人确认”, accept/decline, expected start date, channel, time, optional note, explicit confirmation, and immutable “由 HR 代为登记” timeline labels.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -q server/tests/test_offer_api.py server/tests/test_feishu_notifications.py server/tests/test_notification_api.py`

Run: `node --test --test-concurrency=1 src/OfferViews.test.js src/offerController.test.js && npm run build`

Commit: `git commit -m "Add HR Offer response entry"`

### Task 12: Complete Regression, Documentation, and Release Gates

**Files:**
- Modify: `README.md`
- Modify: `server/README.md`
- Modify: `deploy/.env.example`
- Modify: `scripts/community_setup.py`
- Create: `docs/candidate-email-and-offer-operations.md`
- Test: `server/tests/test_production_topology.py`
- Test: `scripts/check_public_tree.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces operator documentation and release evidence only; no new business behavior.

- [ ] **Step 1: Add setup and operations documentation**

Document SMTP setup/test, sender and reply-to behavior, template management, failed-delivery recovery, Offer approval, token revocation, expiry, and rollback compatibility. Never include real credentials or candidate information.

- [ ] **Step 2: Run full frontend verification**

Run: `cd frontend && npm ci --no-audit --no-fund && npm test && npm run build`

- [ ] **Step 3: Build and run full backend verification**

Run: `docker build --target test -t beyondcandidate-server-test-offers -f server/Dockerfile .`

Run: `docker run --rm beyondcandidate-server-test-offers`

- [ ] **Step 4: Run public-boundary and diff checks**

Run: `python scripts/check_public_tree.py`

Run: `git diff --check`

- [ ] **Step 5: Run local Compose smoke**

Verify migration head, SMTP test with a local fake SMTP service, interview invitation rendering, ordinary and special approval, Offer send, public accept, HR proxy decline, expiry, revoked token, and failed-delivery retry without using production data.

- [ ] **Step 6: Commit documentation and release gate**

Commit: `git commit -m "Document candidate email and Offer operations"`
