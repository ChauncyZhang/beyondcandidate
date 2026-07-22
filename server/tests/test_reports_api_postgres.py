import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from server.app.identity.models import Job, JobCollaborator, Organization, User, UserRole
from server.app.identity.policy import Principal
from server.app.queue.models import BackgroundJob
from server.app.recruiting.models import Application, Candidate, FileObject, IdempotencyRecord, Resume
from server.app.recruiting.service import persisted_idempotent
from server.app.reports.models import ExportRecord
from server.app.reports.service import (
    authorized_job_ids,
    consume_export_ticket,
    create_export_record,
    issue_export_ticket,
    recruiting_funnel,
)


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


@pytest.fixture
def reports_db():
    async_url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": async_url}
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=env,
    )
    engine = create_engine(async_url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        connection.execute(text("UPDATE queue_claim_cursors SET last_organization_id=NULL, updated_at=now()"))
    yield engine
    engine.dispose()


def _seed_scope(engine):
    with Session(engine) as db:
        organization = Organization(slug="reports-pg", name="Reports PG", status="active")
        other_organization = Organization(slug="reports-other", name="Reports Other", status="active")
        user = User(
            organization=organization,
            email="reports-pg@test",
            normalized_email="reports-pg@test",
            display_name="Reports recruiter",
            password_hash="not-used",
        )
        user.roles.append(UserRole(role="recruiter"))
        other_user = User(
            organization=other_organization,
            email="reports-other@test",
            normalized_email="reports-other@test",
            display_name="Other recruiter",
            password_hash="not-used",
        )
        other_user.roles.append(UserRole(role="recruiter"))
        db.add_all([organization, other_organization, user, other_user])
        db.flush()
        allowed = Job(organization_id=organization.id, title="Allowed", owner_id=user.id, status="open")
        denied = Job(organization_id=organization.id, title="Denied", owner_id=user.id, status="open")
        other = Job(
            organization_id=other_organization.id, title="Other tenant", owner_id=other_user.id, status="open"
        )
        db.add_all([allowed, denied, other])
        db.flush()
        db.add(
            JobCollaborator(
                organization_id=organization.id,
                job_id=allowed.id,
                user_id=user.id,
                access_role="job_recruiter",
            )
        )
        for index, (owner, job, org) in enumerate(
            ((user, allowed, organization), (user, denied, organization), (other_user, other, other_organization)),
            start=1,
        ):
            candidate = Candidate(organization_id=org.id, display_name=f"Candidate {index}", owner_id=owner.id)
            file = FileObject(
                organization_id=org.id,
                storage_key=f"private/{index}",
                original_filename=f"resume-{index}.pdf",
                mime_type="application/pdf",
                size_bytes=1,
                sha256=str(index) * 64,
                uploaded_by=owner.id,
            )
            db.add_all([candidate, file])
            db.flush()
            resume = Resume(
                organization_id=org.id,
                candidate_id=candidate.id,
                file_object_id=file.id,
                version_number=1,
            )
            db.add(resume)
            db.flush()
            db.add(
                Application(
                    organization_id=org.id,
                    candidate_id=candidate.id,
                    job_id=job.id,
                    resume_id=resume.id,
                    owner_id=owner.id,
                    stage="new",
                )
            )
        db.commit()
        return Principal(user.id, organization.id, frozenset({"recruiter"}), True), allowed.id


def test_postgres_report_scope_and_concurrent_idempotent_export_creation(reports_db) -> None:
    principal, allowed_job_id = _seed_scope(reports_db)
    with Session(reports_db) as db:
        job_ids = authorized_job_ids(db, principal)
        result = recruiting_funnel(db, principal, job_ids, None, None, datetime.now(timezone.utc))
        assert job_ids == [allowed_job_id]
        assert result["total_applications"] == 1

    barrier = threading.Barrier(2)
    results: list[dict] = []

    def create_once() -> None:
        with Session(reports_db) as db:
            barrier.wait()

            def action():
                export = create_export_record(
                    db,
                    principal,
                    [allowed_job_id],
                    None,
                    None,
                    "reports-pg-trace",
                    "same-export-key",
                )
                return 201, {"data": {"id": str(export.id)}}

            _, response = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "report_export.create",
                "same-export-key",
                {"job_id": str(allowed_job_id)},
                action,
            )
            db.commit()
            results.append(response)

    threads = [threading.Thread(target=create_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0] == results[1]
    with Session(reports_db) as db:
        assert db.scalar(select(func.count()).select_from(ExportRecord)) == 1
        assert db.scalar(select(func.count()).select_from(BackgroundJob).where(BackgroundJob.type == "reports.export")) == 1
        assert db.scalar(
            select(func.count()).select_from(IdempotencyRecord).where(
                IdempotencyRecord.operation == "report_export.create"
            )
        ) == 1


def test_postgres_concurrent_ticket_consumption_allows_one_storage_read(reports_db) -> None:
    principal, allowed_job_id = _seed_scope(reports_db)
    now = datetime.now(timezone.utc)

    class Clock:
        def current_time(self):
            return now

    class Tokens:
        def new_token(self):
            return "concurrent-report-download-token"

    with Session(reports_db) as db:
        export = create_export_record(
            db,
            principal,
            [allowed_job_id],
            None,
            None,
            "ticket-race-trace",
            "ticket-race-key",
        )
        export.status = "succeeded"
        export.object_key = f"exports/{principal.organization_id}/{export.id}.csv"
        export.completed_at = now
        raw = issue_export_ticket(db, export, principal, Clock(), Tokens())
        db.commit()
        export_id = export.id

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    reads = 0
    lock = threading.Lock()

    def consume_once() -> None:
        nonlocal reads
        with Session(reports_db) as db:
            export = db.get(ExportRecord, export_id)
            barrier.wait()
            try:
                consume_export_ticket(db, raw, principal, export, Clock())
                with lock:
                    reads += 1
                    outcomes.append("read")
                db.commit()
            except LookupError:
                db.rollback()
                with lock:
                    outcomes.append("denied")

    threads = [threading.Thread(target=consume_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert reads == 1
    assert sorted(outcomes) == ["denied", "read"]
