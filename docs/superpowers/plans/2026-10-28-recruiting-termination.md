# Recruiting Termination Implementation Plan

> Execute the approved design in the sibling specs directory. Local code and tests only; no commit, push, deployment or live provider writes.

**Goal:** Cancel an individual interview or terminate one job application without deleting history, leaking active work, or reactivating through a late worker.
**Baseline:** 76669aa57de1a6e9c25cc58c7cb2e8e76a875b03; only the approved design was untracked. R2: authorization, persistence, worker races and external facts.
**Verification:** Focused service/API/browser regressions, existing affected suites and build, isolated PostgreSQL migration/actual-lock checks, then one independent read-only combined QA.

## Shared Contract

- New module `server.app.recruiting.termination` exposes read-only `application_is_terminated(db, organization_id, application_id) -> bool` and `record_external_fact(db, organization_id, application_id, *, kind, resource_id, state, reference=None, generation=None)`. The latter is a no-op without a termination record; updates are made under candidate/application locks. States: `in_flight`, `unknown`, `needs_action`, `resolved`. A genuinely new unresolved fact reopens external cleanup. Existing acknowledgment is preserved in history.
- Persistent `ApplicationTermination` stores org/application/candidate/job IDs, actor, source stage, reason, timestamp, version, external status/items and resolution history; no copied PII. Application becomes `withdrawn`. Do not change OnboardingRecord's existing lifecycle states.
- `GET /api/v1/applications/{id}/termination` returns `{data:{application_id,candidate_id,job_id,version,stage,preview_version,counts,external_items,requires_not_onboarded_confirmation,allowed_actions:{terminate,confirm_external,reconcile_external},termination}}`. `counts` keys: review_tasks/interviews/feedback_tasks/offers/approvals/queued_messages/onboarding. `termination` is null or `{id,version,source_stage,reason_text,terminated_at,external_status,external_items,resolution_history}`. External items contain kind/resource_id/state/reference/generation. Read-only, existing job scope and MANAGE_CANDIDATE authorization.
- `POST` same URL accepts `{preview_version,reason_text,confirm_not_onboarded:false}` plus application `If-Match` and `Idempotency-Key`. Same response. Reasons 1..2000 trimmed characters. Preview fingerprints affected dependent state; stale ->409 `termination_preview_changed`; stale resource ->409 `resource_version_conflict`; invalid terminal ->409 `application_already_terminal`; missing prearrival confirmation ->422 `not_onboarded_confirmation_required`.
- `POST /api/v1/applications/{id}/termination/external-resolution` accepts `{reason_text}`, termination `If-Match`, idempotency; returns same shape. In-flight/unknown ->409 `external_resolution_pending`.
- `POST /api/v1/applications/{id}/termination/reconcile-external` accepts `{reason_text}`, termination `If-Match`, idempotency; derives original unknown onboarding generations from scoped stored facts and queues `feishu.approval.onboarding.reconcile` (org/onboarding/generation IDs only). Its handler only looks up original UUIDs using the pre-create approval/app/start snapshot; it never creates an approval. Missing/changed identity or no definitive result stays unknown. HR explicitly refreshes the result; queued is not completed. This closes the independent-review recovery finding without allowing acknowledgment of unknown results.
- `GET /api/v1/application-terminations/pending` returns `{data:{items:[{application_id,candidate_id,candidate_name,job_id,job_title,version,terminated_at,external_items}],total}}` using the same HR/job scope. Independent workbench section, not a new existing strict task group.
- Main owns interview API + new `server.app.interviews.cancellation.cancel_interview_record(db, interview, *, actor_user_id, trace_id, reason_text, notify_candidate=False, request=None)`; caller holds candidate/application/interview locks. Cancels unfinished participant tasks only, preserves submitted ones, records events/audit, queues calendar cancellation. Single cancel may enqueue candidate notice; failure is reported without undoing local stop. Does not advance application. Core calls with notify_candidate=False.
- All forward guards check termination only after existing candidate/application governance locks; terminal history remains readable. No new permissions or provider cancellation API.

## Ownership And Tasks

- [x] Core: termination model/migration0046, helpers/coordinator/router, main/env registrations, migration head assertion and core tests. Lock candidate -> application -> resources; existing hired only through explicit prearrival termination.
- [x] Offers/mail: effective Offer cleanup, pending approvals, token invalidation, forward fences and SMTP preflight/late facts; dedicated and existing tests passed. Combined review pending.
- [x] Onboarding/Feishu: forward fences, generation-preserving in-flight/unknown/new remote facts and manual-cleanup sequencing; dedicated tests and actual calendar lock gate passed.
- [x] Frontend: shared preview/confirmation modal, candidate + application + interview + Offer/onboarding entries, independent pending-external workbench section; desktop/mobile browser tests and build passed.
- [x] Main: interview single-cancel helper/API/feedback guards and screening producer guards implemented; regression and test mapping added. Combined integration/review remains below.
- [x] Integrate all contracts, run narrow affected regressions/build and isolated PostgreSQL checks. No production contacts or real provider calls.
- [x] Combined auto-review R2 using no-history reviewer; stale Interview identities, retention recalculation and unknown-request recovery findings repaired and independently rechecked. Final timing recheck passed at scoped digest `F335967BE98E2D6DD2B5B5B7C8D525260DA5191FFA1C27116274E6EB49EF73D1`.

## Local Verification Result

Combined feature gate: 114 passed; existing interview gate: 139 passed;
recruiting consumers: 166 passed. Final timing/onboarding/API selection: 99
passed; independent timing selection: 18 passed plus the adapted HTTP transport
counterexample. Frontend desktop/mobile, production build and independent
browser checks passed. Isolated PostgreSQL migration/grants/redaction and actual
lock-contention gates passed. A native Windows parser-process exception check
and timing-sensitive queue runner were verified in the repository's Linux test
image without weakening existing assertions. Final independent verdict: PASS
for this approved local scope. No commit, push, deployment or live-provider
acceptance was performed.

## Evidence Limits

SMTP or Feishu already in flight cannot be recalled. Tests use controlled providers; manual external acknowledgment is attestation, not remote verification. Existing submitted approval is not proof of employee arrival. New applications must not revive old tokens/tasks.
