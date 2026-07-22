"""Interview scheduling and feedback persistence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012_interviews_feedback"
down_revision = "0011_queue_claim_cursor"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "interviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("application_id", sa.Uuid(), nullable=False),
        sa.Column("round_name", sa.String(100), nullable=False),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(1000)),
        sa.Column("meeting_url", sa.String(2000)),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("notification_status", sa.String(24), nullable=False, server_default="not_sent"),
        sa.Column("invitation_status", sa.String(24), nullable=False, server_default="artifact_ready"),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("calendar_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.CheckConstraint("method in ('video','onsite','phone')", name="ck_interviews_method"),
        sa.CheckConstraint(
            "status in ('draft','scheduled','confirmed','completed','pending_feedback','feedback_completed','rescheduled','cancelled','no_show')",
            name="ck_interviews_status",
        ),
        sa.CheckConstraint(
            "notification_status in ('not_sent','queued','sent','failed')",
            name="ck_interviews_notification_status",
        ),
        sa.CheckConstraint("invitation_status in ('not_generated','artifact_ready','generation_failed')", name="ck_interviews_invitation_status"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_interviews_time_range"),
        sa.CheckConstraint("version >= 1", name="ck_interviews_version"),
        sa.CheckConstraint("calendar_sequence >= 0", name="ck_interviews_calendar_sequence"),
        sa.ForeignKeyConstraint(["organization_id", "application_id"], ["applications.organization_id", "applications.id"]),
        sa.ForeignKeyConstraint(["organization_id", "owner_id"], ["users.organization_id", "users.id"]),
        sa.ForeignKeyConstraint(["organization_id", "created_by"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_interviews_tenant_start_status", "interviews", ["organization_id", "starts_at", "status"])
    op.create_index("ix_interviews_tenant_application_start", "interviews", ["organization_id", "application_id", "starts_at"])

    op.create_table(
        "interview_participants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="interviewer"),
        sa.Column("required_feedback", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("attendance_status", sa.String(24), nullable=False, server_default="invited"),
        sa.Column("task_status", sa.String(24), nullable=False, server_default="ready"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "interview_id", "user_id", name="uq_interview_participant_user"),
        sa.CheckConstraint("role in ('interviewer','observer')", name="ck_interview_participants_role"),
        sa.CheckConstraint("attendance_status in ('invited','accepted','declined','attended','no_show')", name="ck_interview_participants_attendance"),
        sa.CheckConstraint("task_status in ('ready','completed','cancelled')", name="ck_interview_participants_task_status"),
        sa.ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_interview_participants_tenant_user", "interview_participants", ["organization_id", "user_id", "interview_id"])

    op.create_table(
        "interview_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id", "interview_id"], ["interviews.organization_id", "interviews.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "actor_user_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index("ix_interview_events_tenant_interview", "interview_events", ["organization_id", "interview_id", "created_at"])

    op.create_table(
        "interview_feedbacks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("interview_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("ratings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("strengths", sa.Text()),
        sa.Column("risks", sa.Text()),
        sa.Column("conclusion", sa.String(32)),
        sa.Column("notes", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id"),
        sa.UniqueConstraint("organization_id", "interview_id", "author_id", name="uq_interview_feedback_author"),
        sa.CheckConstraint("status in ('draft','submitted','amended')", name="ck_interview_feedbacks_status"),
        sa.CheckConstraint("conclusion is null or conclusion in ('strong_recommend','recommend','hold','no_hire')", name="ck_interview_feedbacks_conclusion"),
        sa.CheckConstraint("version >= 1", name="ck_interview_feedbacks_version"),
        sa.CheckConstraint(
            "(status = 'draft' and submitted_at is null) or (status in ('submitted','amended') and submitted_at is not null)",
            name="ck_interview_feedbacks_submission",
        ),
        sa.ForeignKeyConstraint(["organization_id", "interview_id", "author_id"], ["interview_participants.organization_id", "interview_participants.interview_id", "interview_participants.user_id"]),
    )
    op.create_index("ix_interview_feedbacks_tenant_interview_status", "interview_feedbacks", ["organization_id", "interview_id", "status"])

    op.create_table(
        "interview_feedback_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("previous_payload", postgresql.JSONB(), nullable=False),
        sa.Column("new_payload", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "feedback_id", "revision_number", name="uq_interview_feedback_revision"),
        sa.CheckConstraint("revision_number >= 1", name="ck_interview_feedback_revisions_number"),
        sa.ForeignKeyConstraint(["organization_id", "feedback_id"], ["interview_feedbacks.organization_id", "interview_feedbacks.id"]),
        sa.ForeignKeyConstraint(["organization_id", "actor_id"], ["users.organization_id", "users.id"]),
    )
    op.create_index(
        "ix_interview_feedback_revisions_tenant_feedback",
        "interview_feedback_revisions",
        ["organization_id", "feedback_id", "revision_number"],
    )
    op.execute("""CREATE FUNCTION protect_interview_history() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'interview history is append-only' USING ERRCODE='55000'; END $$""")
    op.execute("CREATE TRIGGER interview_events_append_only BEFORE UPDATE OR DELETE ON interview_events FOR EACH ROW EXECUTE FUNCTION protect_interview_history()")
    op.execute("CREATE TRIGGER interview_feedback_revisions_append_only BEFORE UPDATE OR DELETE ON interview_feedback_revisions FOR EACH ROW EXECUTE FUNCTION protect_interview_history()")
    op.execute(
        """
        CREATE FUNCTION preserve_submitted_feedback() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            actor_text text;
            revision_reason text;
            next_revision integer;
            previous_document jsonb;
            new_document jsonb;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status IN ('submitted', 'amended') THEN
                    RAISE EXCEPTION 'submitted feedback cannot be deleted' USING ERRCODE='55000';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.status IN ('submitted', 'amended') AND NEW IS DISTINCT FROM OLD THEN
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
                   OR NEW.interview_id IS DISTINCT FROM OLD.interview_id
                   OR NEW.author_id IS DISTINCT FROM OLD.author_id
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at
                   OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at THEN
                    RAISE EXCEPTION 'submitted feedback identity and original timestamps are immutable' USING ERRCODE='55000';
                END IF;
                actor_text := nullif(current_setting('app.actor_user_id', true), '');
                revision_reason := nullif(current_setting('app.feedback_revision_reason', true), '');
                IF actor_text IS NULL OR revision_reason IS NULL THEN
                    RAISE EXCEPTION 'submitted feedback amendment requires actor and reason' USING ERRCODE='55000';
                END IF;

                NEW.status := 'amended';
                NEW.version := OLD.version + 1;
                NEW.updated_at := now();
                previous_document := jsonb_build_object(
                    'ratings', OLD.ratings,
                    'strengths', OLD.strengths,
                    'risks', OLD.risks,
                    'conclusion', OLD.conclusion,
                    'notes', OLD.notes,
                    'status', OLD.status,
                    'submitted_at', OLD.submitted_at
                );
                new_document := jsonb_build_object(
                    'ratings', NEW.ratings,
                    'strengths', NEW.strengths,
                    'risks', NEW.risks,
                    'conclusion', NEW.conclusion,
                    'notes', NEW.notes,
                    'status', NEW.status,
                    'submitted_at', NEW.submitted_at
                );
                SELECT coalesce(max(revision_number), 0) + 1 INTO next_revision
                FROM interview_feedback_revisions
                WHERE organization_id = OLD.organization_id AND feedback_id = OLD.id;
                INSERT INTO interview_feedback_revisions(
                    id, organization_id, feedback_id, revision_number,
                    previous_payload, new_payload, reason, actor_id, created_at
                ) VALUES (
                    gen_random_uuid(), OLD.organization_id, OLD.id, next_revision,
                    previous_document, new_document, revision_reason, actor_text::uuid, now()
                );
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER interview_feedback_preserve_history "
        "BEFORE UPDATE OR DELETE ON interview_feedbacks "
        "FOR EACH ROW EXECUTE FUNCTION preserve_submitted_feedback()"
    )


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS preserve_submitted_feedback() CASCADE")
    op.drop_table("interview_feedback_revisions")
    op.drop_table("interview_feedbacks")
    op.drop_table("interview_events")
    op.drop_table("interview_participants")
    op.drop_table("interviews")
    op.execute("DROP FUNCTION protect_interview_history()")
