"""Shared read predicates for candidate onboarding eligibility."""

from sqlalchemy import select
from sqlalchemy.sql.selectable import Exists

from server.app.governance.deletion_models import DeletionRequest


ACTIVE_CANDIDATE_DELETION_STATUSES = ("requested", "approved", "executing", "failed")


def pending_candidate_deletion(organization_id, candidate_id) -> Exists:
    """Accept UUID values or correlated Candidate/Application column expressions."""
    return (
        select(DeletionRequest.id)
        .where(
            DeletionRequest.organization_id == organization_id,
            DeletionRequest.candidate_id == candidate_id,
            DeletionRequest.status.in_(ACTIVE_CANDIDATE_DELETION_STATUSES),
        )
        .correlate_except(DeletionRequest)
        .exists()
    )


def candidate_deletion_pending(db, organization_id, candidate_id) -> bool:
    """Read only; mutation callers must first acquire candidate/governance locks."""
    return bool(db.scalar(select(pending_candidate_deletion(organization_id, candidate_id))))
