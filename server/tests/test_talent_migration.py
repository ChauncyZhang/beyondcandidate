import os
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from server.tests.test_interview_persistence_postgres import _seed_application


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


def test_0014_downgrade_preserves_cross_job_source_in_a_history_event() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0013_interview_calendar_contacts"],
        check=True,
        env=env,
    )
    target_job_id = uuid.uuid4()
    target_application_id = uuid.uuid4()
    with engine.begin() as connection:
        identifiers = _seed_application(connection)
        connection.execute(
            text("UPDATE applications SET stage = 'rejected' WHERE id = :application"),
            identifiers,
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs(
                  id, organization_id, title, owner_id, status, headcount, priority,
                  version, created_at, updated_at
                ) VALUES (
                  :target_job, :organization, 'Target job', :owner, 'open', 1,
                  'normal', 1, now(), now()
                )
                """
            ),
            {**identifiers, "target_job": target_job_id},
        )

    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0014_talent_pools"],
        check=True,
        env=env,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO applications(
                  id, organization_id, candidate_id, job_id, resume_id,
                  source_application_id, owner_id, stage, source, version,
                  created_at, updated_at
                ) VALUES (
                  :target_application, :organization, :candidate, :target_job, :resume,
                  :application, :owner, 'new', 'talent_pool_reactivation', 1,
                  now(), now()
                )
                """
            ),
            {
                **identifiers,
                "target_job": target_job_id,
                "target_application": target_application_id,
            },
        )

    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "0013_interview_calendar_contacts"],
        check=True,
        env=env,
    )
    with engine.connect() as connection:
        application = connection.execute(
            text(
                """
                SELECT source_application_id, source
                FROM applications WHERE id = :application_id
                """
            ),
            {"application_id": target_application_id},
        ).one()
        history = connection.execute(
            text(
                """
                SELECT payload ->> 'source_application_id'
                FROM application_stage_events
                WHERE application_id = :application_id
                  AND event_type = 'application.source_detached_for_downgrade'
                """
            ),
            {"application_id": target_application_id},
        ).one()
    assert application == (None, "talent_pool_reactivation")
    assert history[0] == str(identifiers["application"])
    engine.dispose()


def test_0021_enforces_one_system_pool_key_per_organization() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=env,
    )
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_application(connection)
        connection.execute(
            text(
                """
                INSERT INTO talent_pools(
                  id, organization_id, name, purpose, owner_id, suitable_roles,
                  retention_days, version, system_key, created_at, updated_at
                ) VALUES (
                  :pool, :organization, 'Deferred', 'AI review', :owner, '[]',
                  730, 1, 'ai_screening_deferred', now(), now()
                )
                """
            ),
            {**identifiers, "pool": uuid.uuid4()},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO talent_pools(
                      id, organization_id, name, purpose, owner_id, suitable_roles,
                      retention_days, version, system_key, created_at, updated_at
                    ) VALUES (
                      :pool, :organization, 'Deferred duplicate', 'AI review', :owner, '[]',
                      730, 1, 'ai_screening_deferred', now(), now()
                    )
                    """
                ),
                {**identifiers, "pool": uuid.uuid4()},
            )
    engine.dispose()


def test_0029_moves_legacy_recruiter_hiring_owner_to_recruiting_owner() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0028_image_resume_support"],
        check=True,
        env=env,
    )
    with engine.begin() as connection:
        identifiers = _seed_application(connection)
        connection.execute(
            text("INSERT INTO user_roles(id, user_id, role, created_at, updated_at) VALUES (:id, :user, 'recruiter', now(), now())"),
            {"id": uuid.uuid4(), "user": identifiers["interviewer"]},
        )
        connection.execute(
            text("UPDATE jobs SET hiring_owner_id = :user WHERE id = :job"),
            {"user": identifiers["interviewer"], "job": identifiers["job"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO job_collaborators(
                  id, organization_id, job_id, user_id, access_role, created_at, updated_at
                ) VALUES
                  (:owner_grant, :organization, :job, :owner, 'job_owner', now(), now()),
                  (:manager_grant, :organization, :job, :interviewer, 'job_manager', now(), now())
                """
            ),
            {
                **identifiers,
                "owner_grant": uuid.uuid4(),
                "manager_grant": uuid.uuid4(),
            },
        )

    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=env,
    )
    with engine.connect() as connection:
        job = connection.execute(
            text("SELECT owner_id, hiring_owner_id, version FROM jobs WHERE id = :job"),
            {"job": identifiers["job"]},
        ).one()
        grants = connection.execute(
            text("SELECT user_id, access_role FROM job_collaborators WHERE job_id = :job ORDER BY access_role"),
            {"job": identifiers["job"]},
        ).all()

    assert job == (identifiers["interviewer"], None, 2)
    assert grants == [(identifiers["interviewer"], "job_owner")]
    engine.dispose()
