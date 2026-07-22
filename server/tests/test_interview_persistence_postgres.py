import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


@pytest.fixture()
def database():
    async_url = os.environ["POSTGRES_SMOKE_URL"]
    environment = {**os.environ, "DATABASE_URL": async_url}
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"],
        check=True,
        env=environment,
    )
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=environment,
    )
    engine = create_engine(async_url.replace("+asyncpg", "+psycopg"))
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_application(connection):
    identifiers = {
        name: uuid.uuid4()
        for name in ("organization", "other_organization", "owner", "interviewer", "outsider", "job", "candidate", "file", "resume", "application")
    }
    connection.execute(
        text(
            """
            INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
            VALUES
              (:organization, 'interview-test', 'Interview Test', 'active', now(), now()),
              (:other_organization, 'interview-other', 'Interview Other', 'active', now(), now())
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO users(
              id, organization_id, email, normalized_email, display_name, password_hash,
              status, authorization_version, created_at, updated_at
            ) VALUES
              (:owner, :organization, 'owner@test', 'owner@test', 'Owner', 'x', 'active', 1, now(), now()),
              (:interviewer, :organization, 'interviewer@test', 'interviewer@test', 'Interviewer', 'x', 'active', 1, now(), now()),
              (:outsider, :other_organization, 'outsider@test', 'outsider@test', 'Outsider', 'x', 'active', 1, now(), now())
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO jobs(
              id, organization_id, title, owner_id, status, headcount, priority,
              version, created_at, updated_at
            ) VALUES (:job, :organization, 'AI Engineer', :owner, 'open', 1, 'normal', 1, now(), now())
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO candidates(
              id, organization_id, display_name, owner_id, version, created_at, updated_at
            ) VALUES (:candidate, :organization, 'Candidate', :owner, 1, now(), now())
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO file_objects(
              id, organization_id, storage_key, original_filename, mime_type, size_bytes,
              sha256, uploaded_by, storage_state, scan_status, created_at
            ) VALUES (
              :file, :organization, 'interviews/resume.pdf', 'resume.pdf', 'application/pdf',
              10, repeat('a', 64), :owner, 'clean', 'clean', now()
            )
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO resumes(
              id, organization_id, candidate_id, file_object_id, version_number, created_at
            ) VALUES (:resume, :organization, :candidate, :file, 1, now())
            """
        ),
        identifiers,
    )
    connection.execute(
        text(
            """
            INSERT INTO applications(
              id, organization_id, candidate_id, job_id, resume_id, owner_id, stage,
              source, version, created_at, updated_at
            ) VALUES (
              :application, :organization, :candidate, :job, :resume, :owner,
              'interview_pending', 'manual', 1, now(), now()
            )
            """
        ),
        identifiers,
    )
    return identifiers


def _insert_interview(connection, identifiers, *, status="draft"):
    interview_id = uuid.uuid4()
    starts_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    connection.execute(
        text(
            """
            INSERT INTO interviews(
              id, organization_id, application_id, round_name, method, timezone,
              starts_at, ends_at, status, notification_status, invitation_status,
              owner_id, created_by, version, calendar_sequence,
              calendar_organizer, calendar_attendees, created_at, updated_at
            ) VALUES (
              :id, :organization, :application, 'First round', 'video', 'Asia/Shanghai',
              :starts_at, :ends_at, :status, 'not_sent', 'artifact_ready',
              :owner, :owner, 1, 0,
              jsonb_build_object('name', 'Owner', 'email', 'owner@test'),
              jsonb_build_array(jsonb_build_object('name', 'Interviewer', 'email', 'interviewer@test')),
              now(), now()
            )
            """
        ),
        {
            **identifiers,
            "id": interview_id,
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(minutes=45),
            "status": status,
        },
    )
    return interview_id


def test_interview_schema_accepts_the_specified_lifecycle_and_rejects_cross_tenant_participants(database) -> None:
    with database.begin() as connection:
        identifiers = _seed_application(connection)
        interview_id = _insert_interview(connection, identifiers)
        for status in (
            "scheduled",
            "confirmed",
            "completed",
            "pending_feedback",
            "feedback_completed",
            "rescheduled",
            "cancelled",
            "no_show",
        ):
            connection.execute(
                text("UPDATE interviews SET status=:status WHERE id=:id"),
                {"status": status, "id": interview_id},
            )

        connection.execute(
            text(
                """
                INSERT INTO interview_participants(
                  id, organization_id, interview_id, user_id, role, required_feedback,
                  attendance_status, task_status, created_at, updated_at
                ) VALUES (
                  :id, :organization, :interview, :interviewer, 'interviewer', true,
                  'invited', 'ready', now(), now()
                )
                """
            ),
            {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
        )

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        INSERT INTO interview_participants(
                          id, organization_id, interview_id, user_id, role, required_feedback,
                          attendance_status, task_status, created_at, updated_at
                        ) VALUES (
                          :id, :organization, :interview, :outsider, 'interviewer', true,
                          'invited', 'ready', now(), now()
                        )
                        """
                    ),
                    {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
                )


def test_submitted_feedback_requires_revision_context_and_preserves_both_payloads(database) -> None:
    with database.begin() as connection:
        identifiers = _seed_application(connection)
        interview_id = _insert_interview(connection, identifiers, status="pending_feedback")
        connection.execute(
            text(
                """
                INSERT INTO interview_participants(
                  id, organization_id, interview_id, user_id, role, required_feedback,
                  attendance_status, task_status, created_at, updated_at
                ) VALUES (
                  :id, :organization, :interview, :interviewer, 'interviewer', true,
                  'attended', 'ready', now(), now()
                )
                """
            ),
            {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
        )
        feedback_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO interview_feedbacks(
                  id, organization_id, interview_id, author_id, status, ratings,
                  strengths, risks, conclusion, notes, version, submitted_at, created_at, updated_at
                ) VALUES (
                  :id, :organization, :interview, :interviewer, 'submitted', '{}',
                  'Strong Python', 'Needs system design evidence', 'recommend', 'Original note',
                  1, now(), now(), now()
                )
                """
            ),
            {**identifiers, "id": feedback_id, "interview": interview_id},
        )

        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE interview_feedbacks SET notes='silent overwrite' WHERE id=:id"),
                    {"id": feedback_id},
                )

        connection.execute(text("SELECT set_config('app.actor_user_id', :actor, true)"), {"actor": str(identifiers["interviewer"])})
        connection.execute(text("SELECT set_config('app.feedback_revision_reason', 'Clarified evidence', true)"))
        connection.execute(
            text("UPDATE interview_feedbacks SET notes='Clarified note' WHERE id=:id"),
            {"id": feedback_id},
        )
        feedback = connection.execute(
            text("SELECT status, version, notes FROM interview_feedbacks WHERE id=:id"),
            {"id": feedback_id},
        ).one()
        revision = connection.execute(
            text(
                """
                SELECT revision_number, previous_payload->>'notes', new_payload->>'notes', reason, actor_id
                FROM interview_feedback_revisions WHERE feedback_id=:id
                """
            ),
            {"id": feedback_id},
        ).one()
        assert feedback == ("amended", 2, "Clarified note")
        assert revision == (
            1,
            "Original note",
            "Clarified note",
            "Clarified evidence",
            identifiers["interviewer"],
        )

        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        UPDATE interview_feedbacks
                        SET created_at = created_at + interval '1 day',
                            submitted_at = submitted_at + interval '1 day'
                        WHERE id=:id
                        """
                    ),
                    {"id": feedback_id},
                )

        with pytest.raises(DBAPIError):
            with connection.begin_nested():
                connection.execute(text("DELETE FROM interview_feedbacks WHERE id=:id"), {"id": feedback_id})


def test_feedback_status_and_submitted_timestamp_cannot_disagree(database) -> None:
    with database.begin() as connection:
        identifiers = _seed_application(connection)
        interview_id = _insert_interview(connection, identifiers, status="pending_feedback")
        connection.execute(
            text(
                """
                INSERT INTO interview_participants(
                  id, organization_id, interview_id, user_id, role, required_feedback,
                  attendance_status, task_status, created_at, updated_at
                ) VALUES (
                  :id, :organization, :interview, :interviewer, 'interviewer', true,
                  'attended', 'ready', now(), now()
                )
                """
            ),
            {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        """
                        INSERT INTO interview_feedbacks(
                          id, organization_id, interview_id, author_id, status, ratings,
                          version, submitted_at, created_at, updated_at
                        ) VALUES (
                          :id, :organization, :interview, :interviewer, 'submitted', '{}',
                          1, null, now(), now()
                        )
                        """
                    ),
                    {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
                )
