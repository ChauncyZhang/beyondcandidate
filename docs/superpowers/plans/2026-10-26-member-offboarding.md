# Member Offboarding Implementation Plan

> **For agentic workers:** Use the approved spec and bounded ownership below.
> Main integrates all slices and runs one combined independent R2 review.

**Goal:** Revoke a departing member's access, hand unfinished work to one eligible
successor, and preserve historical attribution and visible unresolved work.

**Architecture:** Keep `users` and historical foreign keys. Add one durable
offboarding record per source member; separate account revocation from retryable
domain handover. Background producers resolve operational ownership without
rewriting the original requester.

**Tech Stack:** Existing FastAPI/SQLAlchemy/Alembic/PostgreSQL and React/Vite.

**Spec:** `docs/superpowers/specs/2026-10-26-member-offboarding-design.md`

## Constraints and Review Focus

- Baseline: `195e4f5f6292238c58af96f48ec20b887340d635`; working tree clean except
  the proposed spec. Product changes belong in this public checkout.
- No physical user/business deletion, implicit role/scope copying, historical
  reattribution, remote writes during verification, commit, push or deployment.
- Late screening completion must not recreate removed ownership.
- Terminal hired applications with unfinished onboarding remain operational.
- Stale previews and concurrent administrator/target changes must fail safely.
- Ineligible successor must not prevent the explicit revoke-only action.
- Existing external instances and original interview feedback stay attributed
  correctly and appear as unresolved work instead of being silently rewritten.

## Shared Interfaces

- New `MemberOffboarding` in `server/app/identity/offboarding_models.py` stores
  tenant/source/successor/removing-actor IDs, removal/update times, version,
  `pending` or `completed` status, and unresolved item snapshots. Composite FKs
  enforce tenant alignment. Migration `0045_member_offboarding` is additive.
- `GET /api/v1/settings/users/{id}/offboarding?successor_id=<uuid>` returns
  `data` with `user_id`, `user_name`, `removed_at`, `successor_user_id`,
  `handover_status` (`not_started`/`pending`/`completed`), `preview_version`,
  `counts`, `items`, and `successors`.
- Each item has `category`, `resource_type`, `resource_id`, `title`,
  `status` (`transferable`/`blocked`), `reason_code`, `reason`, and optional
  local `href`. Counts have `category`, `label`, `total`, `transferable`, `blocked`.
  Successor entries have `id`, `display_name`, `eligible`, and nullable `reason`.
- `POST` to the same URL accepts `successor_user_id` (nullable),
  `preview_version`, and `revoke_only` (default false). It returns the current
  detail plus `transferred_count`. A stale preview returns 409
  `offboarding_preview_changed`. Revoke-only does not execute domain transfer.
- Settings user entries gain nullable `offboarding` summary with `removed_at`,
  `successor_user_id`, `handover_status`, and `pending_count`; retain old fields.
- Domain module `identity/offboarding_handover.py` exposes
  `build_handover_plan(db, organization_id, source_user, successor_user, *, now,
  lock=False)` returning `items`, `counts`, and a stable `fingerprint`;
  `apply_handover(db, plan, *, actor_user_id, trace_id, now)` mutates only eligible
  planned responsibilities in the caller transaction and returns a count.
- Producer helper `identity/offboarding_resolver.py` exposes
  `resolve_operational_user_id(db, organization_id, user_id, *, job_id=None)`.
  It locks the selected users; active original users are unchanged. Removed
  users resolve through the durable successor chain with active/tenant/role/
  access checks. Missing destination or cycles raise `OperationalOwnerUnavailable`.

## Tasks

### 1. Identity, Persistence and Revocation (Main)

- [x] Add failing API tests for preview, revocation, invite/reset/session login
  protection, RBAC/tenant/self/last-admin guards, stale preview and retry.
- [x] Add model/migration with grants; implement settings endpoints, locked
  authorization recheck, session/grant invalidation, durable pending state.
- [x] Guard bootstrap from restoring a removed account and add its regression.
- [x] Run focused identity tests and isolated PostgreSQL migration/locking tests.

### 2. Domain Handover (Backend Worker)

- [x] Add failing domain regressions for live jobs/candidates/applications,
  open review/current-round approval, talent, interview coordination and
  unfinished onboarding, preserving historical actors and decisions.
- [x] Implement shared domain interface with explicit eligibility, version-aware
  locked execution, visible blocked/external/default-configuration items.
- [x] Verify partial handover and repeat execution do not duplicate effects.

### 3. Background Ownership (Worker)

- [x] Add resolver and producer regressions including a late task, missing
  destination, chain/cycle and immutable requester attribution.
- [x] Wire screening/routing/talent operational creation to the shared resolver.
- [x] Run focused screening/routing/talent regressions without weakening fixtures.

### 4. Administrator UI (Frontend Worker)

- [x] Add API/controller regressions and member removal drawer with one successor,
  preview, revoke-only, pending/removed filters, explanations and retry.
- [x] Preserve existing invite/reset/member configuration workflows.
- [x] Run focused frontend tests and build; main verifies an actual browser flow.

### 5. Combined Gate (Main)

- [x] Integrate and inspect worker deltas; run affected existing tests and full
  frontend suite, public-tree check, compile and diff checks.
- [x] Independent no-history R2 review of the full scoped change; repair in-scope
  blockers and recheck affected contracts with the same reviewer.
- [x] Update existing server test/runbook mapping with commands and evidence limits.
- [x] Report verified local outcome and remaining manual external-system work.

## Local Verification Result

The independent R2 review passed after one repair recheck. The two reproduced
findings (inherited background batches and stale completed summaries after
successor scope changes) have durable RED-to-GREEN cases. Final combined backend
integration passed 97 tests; the isolated PostgreSQL migration/concurrency gate
passed 3 tests. Full frontend discovery passed 718 tests before the final narrow
layout repair; the affected API/controller/browser batch then passed 56 tests,
and the final desktop/mobile layout batch passed 6 tests with a successful build.

Existing external instances retain manual follow-up; this does not provide a
remote transfer or a new manual-acknowledgment endpoint. No real member was
removed, and no commit, push, migration of production, or deployment was performed.
