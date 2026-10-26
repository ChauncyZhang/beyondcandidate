# Member Offboarding and Unified Handover

Status: approved by the user on 2026-10-26; implementation authorized, delivery
actions and production changes are not authorized.

## Intent and Decisions

An administrator must be able to remove a departing colleague's access without
losing unfinished recruiting work or changing the authorship of completed work.
The user selected one successor for all transferable responsibilities, rather
than a separate successor for each category.

The member action is named "移除成员并交接". Removal is logical offboarding,
not physical deletion of the user row or candidate/business records.

## Approaches

- Physical deletion is unsuitable: operational and historical records reference
  the user, and deleting or relabeling them would lose attribution.
- Disabling access alone protects login but strands tasks and future assignments.
- Logical removal plus a durable handover to one successor is selected. It
  preserves history and gives administrators a visible unresolved-work list.

## Access Revocation

- System administrators and recruiting administrators follow the existing member
  management hierarchy; recruiting administrators cannot remove administrators.
- Reject self-removal and removing the last active system administrator. Check
  current authorization again after acquiring the relevant database locks.
- Disable the account, revoke sessions, invalidate outstanding activation/reset
  grants, and ensure password and Feishu login cannot reactivate it.
- Revocation must not depend on the successor being eligible for every task.
  Offer "仅收回账号，稍后交接" when complete handover is unavailable.
- Removed users remain discoverable in an administrator filter with their
  removal time, chosen successor, handover status, and unresolved items.
- Ordinary invitation, reset, or bootstrap behavior must not silently restore an
  offboarded account. Reactivation and reusing its email are outside this scope.

## One Successor, No Implicit Privilege Escalation

- The successor must be a different active user in the same organization.
- Do not copy the departing user's roles, department scope, or credentials.
- Check existing domain eligibility and access for each responsibility. Move
  only eligible work; list ineligible items with an actionable explanation.
- Administrators may fix ordinary role/access configuration through its existing
  controls, then retry handover to the same single successor.
- Transfer responsibility assignments only. Any necessary collaborator or pool
  grant change must be shown explicitly in the preview and stay within the
  administrator's existing authority; do not silently broaden global scope.

## Responsibility and History Boundary

| Category | Handover | Retain unchanged |
| --- | --- | --- |
| Jobs and candidates | Ongoing job ownership, live candidate stewardship, operational collaboration assignments | JD/rule authors, original events, closed historical responsibility |
| Applications | Nonterminal application follow-up; accepted/hired applications with unfinished onboarding also remain operational work | Terminal hiring/rejection/withdrawal decisions and their actors |
| Manager reviews | Open tasks after job-review eligibility checks | Closed/cancelled tasks and recorded conclusions |
| Internal Offer approval | Undecided steps of the current live round, after eligibility and duplicate/self-approval checks | Approved/rejected decisions, reasons, times, submitted version authors |
| Talent | Eligible pool stewardship and membership follow-up | Source, contact restrictions, retention rules and original records |
| Interviews | Active coordination; future participant replacement through normal scheduling rules | Creators, feedback authors, submitted/amended evaluations |
| Onboarding | Ready-to-submit follow-up and access to manage it | Existing submission initiator, generation and remote instance identity |

Already-conducted interviews missing the departing person's feedback are not
automatically reassigned as if the successor attended. Keep the original draft
and authorship, and expose a manual-resolution item for the administrator.

Feishu instances already submitted and ambiguous in-flight/failed submissions
must not have their initiator or fingerprint rewritten. Show them as external
follow-up/reconciliation items, without resubmitting or claiming remote transfer.
Calendar participant changes use the existing scheduling/sync path, not direct
mutation of remote IDs or stored invitation snapshots.

Future default approver configuration referring to the departing user must be
identified. Offer defaults may be replaced only when eligible. Feishu fixed
approver changes retain the existing revalidation requirement and must not be
silently marked valid.

## Durable Processing and Concurrency

- Persist source member, single successor, actor, timestamps, and unresolved
  responsibility references. Keep historical audit authors intact.
- Preview returns counts, required permissions, reasons for blocked work, and
  a concurrency token. Recompute eligibility/state on execution; stale previews
  cannot overwrite a decision made since preview.
- Revocation and its durable handover intent are transactional. Retry transfer
  safely without issuing duplicate approvals, emails or calendar changes.
- Background screening and routing must resolve current operational ownership
  separately from immutable `created_by`; late task completion must not recreate
  ownership for a removed user.
- If a successor is later removed, use its latest valid handover destination;
  detect cycles and missing/inactive destinations. Fail visibly rather than
  assigning to an arbitrary administrator.
- Do not transfer queue leases, rewrite past job payload actors, or replay
  exports under another person's authority.

## UI Flow

1. Administrator selects a member and opens the removal drawer.
2. Show access revocation impact, current responsibilities and one successor
   selector. Explain unavailable successors and blocked item categories.
3. Confirm removal and handover, or revoke access first and keep visible pending
   handover work. Errors must explain what was and was not completed.
4. The member list has active/removed/pending-handover filters and an entry to
   resume handover. Completed history remains readable under the original name.

## Verification and Delivery Boundary

- Durable regression tests cover revocation, existing login/reset/invite paths,
  tenant/RBAC/self/last-admin protections, successor eligibility, stale previews,
  retries, historical attribution, late background work and unresolved items.
- PostgreSQL tests must prove the named lock/interleaving and migration/grant
  risks using an isolated disposable database, not production.
- Frontend tests and a browser flow cover preview, selection, confirmation,
  partial/pending state, retry and mobile layout. Existing invitation, reset,
  role/scope configuration and recruiting tests remain regression contracts.
- One independent R2 review covers the combined change and reachable consumers.
- No production mutation, real user removal, commit, push, or deployment is
  authorized by this design work. Those require an explicit delivery request.
