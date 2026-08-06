"""Separate recruiting owners from hiring managers on legacy jobs."""

from alembic import op


revision = "0029_separate_job_owners"
down_revision = "0028_image_resume_support"
branch_labels = None
depends_on = None


LEGACY_RECRUITER_PREDICATE = """
    j.hiring_owner_id IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM user_roles recruiter_role
        WHERE recruiter_role.user_id = j.hiring_owner_id
          AND recruiter_role.role = 'recruiter'
    )
    AND NOT EXISTS (
        SELECT 1 FROM user_roles manager_role
        WHERE manager_role.user_id = j.hiring_owner_id
          AND manager_role.role IN ('hiring_manager', 'recruiting_admin')
    )
"""


def upgrade() -> None:
    op.execute(
        f"""
        DELETE FROM job_collaborators collaborator
        USING jobs j
        WHERE collaborator.organization_id = j.organization_id
          AND collaborator.job_id = j.id
          AND collaborator.access_role = 'job_owner'
          AND {LEGACY_RECRUITER_PREDICATE}
        """
    )
    op.execute(
        f"""
        INSERT INTO job_collaborators (
            id, organization_id, job_id, user_id, access_role, created_at, updated_at
        )
        SELECT gen_random_uuid(), j.organization_id, j.id, j.hiring_owner_id,
               'job_owner', now(), now()
        FROM jobs j
        WHERE {LEGACY_RECRUITER_PREDICATE}
        ON CONFLICT (job_id, user_id, access_role) DO NOTHING
        """
    )
    op.execute(
        f"""
        DELETE FROM job_collaborators collaborator
        USING jobs j
        WHERE collaborator.organization_id = j.organization_id
          AND collaborator.job_id = j.id
          AND collaborator.user_id = j.hiring_owner_id
          AND collaborator.access_role = 'job_manager'
          AND {LEGACY_RECRUITER_PREDICATE}
        """
    )
    op.execute(
        f"""
        UPDATE jobs j
        SET owner_id = j.hiring_owner_id,
            hiring_owner_id = NULL,
            version = j.version + 1,
            updated_at = now()
        WHERE {LEGACY_RECRUITER_PREDICATE}
        """
    )


def downgrade() -> None:
    # The previous owner cannot be reconstructed safely after reassignment.
    pass
