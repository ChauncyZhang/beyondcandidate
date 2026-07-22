"""Persist stable interview calendar contact snapshots."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_interview_calendar_contacts"
down_revision = "0012_interviews_feedback"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "interviews",
        sa.Column("calendar_organizer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "interviews",
        sa.Column("calendar_attendees", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        """
        UPDATE interviews AS interview
        SET calendar_organizer = jsonb_build_object(
                'name', organizer.display_name,
                'email', organizer.email
            ),
            calendar_attendees = COALESCE(
                (
                    SELECT jsonb_agg(
                        jsonb_build_object('name', attendee.display_name, 'email', attendee.email)
                        ORDER BY attendee.id
                    )
                    FROM interview_participants AS participant
                    JOIN users AS attendee
                      ON attendee.organization_id = participant.organization_id
                     AND attendee.id = participant.user_id
                    WHERE participant.organization_id = interview.organization_id
                      AND participant.interview_id = interview.id
                ),
                '[]'::jsonb
            )
        FROM users AS organizer
        WHERE organizer.organization_id = interview.organization_id
          AND organizer.id = interview.created_by
        """
    )
    op.alter_column("interviews", "calendar_organizer", nullable=False)
    op.alter_column("interviews", "calendar_attendees", nullable=False)


def downgrade():
    op.drop_column("interviews", "calendar_attendees")
    op.drop_column("interviews", "calendar_organizer")
