"""Bounded, fail-closed checks against an initially empty disposable database.

Run with TERMINATION_POSTGRES_URL (asyncpg URL). Never truncate/downgrade.
"""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from server.app.core.settings import Settings
from server.app.identity.models import Department, Job, User
from server.app.interviews.models import Interview, InterviewParticipant
from server.app.main import create_app
from server.app.offers.models import OfferAccessToken, OfferApproval, OfferResponse, OfferVersion
from server.app.offers.schemas import OfferCommand, PublicOfferResponse
from server.app.offers.service import OfferNotFound, OfferTokenCodec, create_offer, issue_offer_access_token, record_public_offer_response
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.service import OnboardingNotReady, start_onboarding_submission
from server.app.recruiting.models import Application, Candidate
from server.app.recruiting.termination import dependent_resources, lock_application, preview_version, terminate_application
from server.app.recruiting.termination_models import ApplicationTermination
from server.tests.test_application_workflow_actions import seed_workflow_application
from server.tests.test_recruiting_api import FakeStorage, Probe, login, seed_user


pytestmark = pytest.mark.skipif(not os.getenv("TERMINATION_POSTGRES_URL"), reason="Isolated TERMINATION_POSTGRES_URL not configured")


def test_migration_grants_redaction_and_real_forward_lock_races():
    url = os.environ["TERMINATION_POSTGRES_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"), connect_args={"options": "-c statement_timeout=15000"})
    assert inspect(engine).get_table_names() == [], "Refusing nonempty database: use a fresh disposable database"
    environment = {**os.environ, "DATABASE_URL": url}
    def upgrade(revision):
        subprocess.run([sys.executable, "-m", "alembic", "-c", "server/alembic.ini", "upgrade", revision],
            env=environment, check=True, timeout=90, capture_output=True, text=True)
    upgrade("0045_member_offboarding")
    role = "termination_smoke_" + uuid4().hex[:12]
    with engine.begin() as connection:
        connection.execute(text(f"CREATE ROLE {role} NOLOGIN"))
        connection.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON applications TO {role}"))
    upgrade("head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0046_application_termination"
        assert connection.scalar(text("SELECT has_table_privilege(:role,'application_terminations','SELECT,INSERT,UPDATE,DELETE')"), {"role": role})
        width = connection.scalar(text("SELECT character_maximum_length FROM information_schema.columns WHERE table_name='alembic_version' AND column_name='version_num'"))
        assert len("0046_application_termination") <= width
    app = create_app(settings=Settings(environment="test", database_url=url, cors_origins=["https://hr.example.test"]),
        database_probe=Probe(), storage_probe=Probe(), resume_storage=FakeStorage())
    admin = seed_user(app, "recruiting_admin", "termination-pg@example.test")
    codec = OfferTokenCodec(b"t" * 32)
    timestamp = datetime.now(timezone.utc)
    with TestClient(app) as client:
        headers = login(client, "termination-pg@example.test")
        for scenario, stage in [("offer", "passed"), ("feedback", "interviewing"), ("reschedule", "interview_pending"), ("onboarding", "hired")]:
            application_id = UUID(seed_workflow_application(app, admin, admin, stage))
            with Session(engine) as db:
                application = db.get(Application, application_id)
                org = application.organization_id
                if scenario in {"offer", "onboarding"}:
                    application.stage = "passed"
                    db.flush()
                    offer = create_offer(db, org, admin, OfferCommand(application_id=application_id,
                        candidate_response_deadline=timestamp + timedelta(days=2), content={"body": "Offer"}), trace_id="pg-seed")
                    offer.status = "sent"
                    offer_id = offer.id
                    token, raw = issue_offer_access_token(db, org, offer, db.get(OfferVersion, offer.current_version_id), codec=codec, now=timestamp)
                    token.delivered_at = timestamp
                    approval = OfferApproval(organization_id=org, offer_id=offer.id, offer_version_id=offer.current_version_id,
                        version_number=1, round_number=1, sequence=1, assignee_id=admin, status="pending")
                    db.add(approval)
                    if scenario == "onboarding":
                        application.stage = "hired"
                        response = OfferResponse(organization_id=org, offer_id=offer.id, status="accepted", responded_at=timestamp)
                        department = Department(organization_id=org, name="Test team")
                        db.add_all([response, department])
                        db.flush()
                        record = OnboardingRecord(organization_id=org, offer_response_id=response.id, offer_id=offer.id,
                            application_id=application_id, candidate_id=application.candidate_id, job_id=application.job_id,
                            department_id=department.id, job_title="Test", department_name="Test team",
                            expected_start_date=timestamp.date(), pii_ciphertext=b"not-decrypted-after-stop")
                        db.add(record)
                        db.flush()
                        record_id = record.id
                else:
                    interview = Interview(organization_id=org, application_id=application_id, round_name="Technical",
                        method="phone", timezone="UTC", starts_at=timestamp, ends_at=timestamp + timedelta(hours=1),
                        status="scheduled", owner_id=admin, created_by=admin)
                    db.add(interview)
                    db.flush()
                    db.add(InterviewParticipant(organization_id=org, interview_id=interview.id, user_id=admin))
                    interview_id = interview.id
                db.commit()

            # The local stop is complete but not yet committed. Every forward
            # producer must wait on the same candidate lock, then see that stop.
            with Session(engine) as stop_db:
                application = lock_application(stop_db, org, application_id)
                blocker = stop_db.scalar(text("SELECT pg_backend_pid()"))
                terminate_application(stop_db, application, expected_version=application.version,
                    expected_preview=preview_version(application, dependent_resources(stop_db, application)),
                    reason_text="Candidate withdrew", confirm_not_onboarded=True, actor_user_id=admin, trace_id="pg-stop")
                def forward():
                    if scenario == "offer":
                        with Session(engine) as db:
                            with pytest.raises(OfferNotFound):
                                record_public_offer_response(db, raw, PublicOfferResponse(decision="accepted", expected_start_date=timestamp.date(),
                                    onboarding_data={"gender": "female", "phone": "+8613800138000", "email": "candidate@example.com", "home_address": "Test"}),
                                    codec=codec, now=timestamp, trace_id="pg-offer")
                        return
                    if scenario == "onboarding":
                        with Session(engine) as db:
                            record = db.get(OnboardingRecord, record_id)
                            with pytest.raises(OnboardingNotReady, match="application_terminated"):
                                start_onboarding_submission(db, record, expected_version=record.version, generation=uuid4(),
                                    actor_user_id=admin, cipher=None, now=timestamp, trace_id="pg-onboarding")
                        return
                    if scenario == "feedback":
                        result = client.put(f"/api/v1/interviews/{interview_id}/my-feedback", json={"strengths": "Late feedback"}, headers={**headers, "If-Match": '"0"'})
                    else:
                        result = client.patch(f"/api/v1/interviews/{interview_id}", json={"starts_at": (timestamp + timedelta(days=1)).isoformat(), "ends_at": (timestamp + timedelta(days=1, hours=1)).isoformat()},
                            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "late-reschedule"})
                    assert result.status_code == 409, result.text
                    assert result.json()["code"] == "application_terminated", result.text
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(forward)
                    try:
                        deadline = time.monotonic() + 8
                        blocked = False
                        while time.monotonic() < deadline and not future.done():
                            with engine.connect() as observer:
                                blocked = observer.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :blocker = ANY(pg_blocking_pids(pid)))"), {"blocker": blocker})
                            if blocked:
                                break
                            time.sleep(0.03)
                        if future.done():
                            future.result()
                        assert blocked, f"{scenario} did not wait on candidate/application termination lock"
                    finally:
                        stop_db.commit()
                    future.result(timeout=15)
            with Session(engine) as db:
                application = db.get(Application, application_id)
                assert application.stage == "withdrawn"
                assert db.scalar(select(ApplicationTermination.id).where(ApplicationTermination.application_id == application_id)) is not None
                if scenario in {"offer", "onboarding"}:
                    approval = db.scalar(select(OfferApproval).where(OfferApproval.offer_id == offer_id))
                    assert approval.status == "cancelled" and approval.decided_at is None
                    assert db.scalar(select(OfferAccessToken).where(OfferAccessToken.offer_id == offer_id)).revoked_at is not None
                if scenario == "onboarding":
                    assert db.get(OnboardingRecord, record_id).status == "ready"
                candidate_id = application.candidate_id
                # Mirrors the owning governance trigger's approved tombstone
                # boundary; no direct deletion of termination history.
                db.execute(text("UPDATE candidates SET deleted_at=now() WHERE id=:candidate AND organization_id=:org"), {"candidate": candidate_id, "org": org})
                db.commit()
                assert db.scalar(select(ApplicationTermination.id).where(ApplicationTermination.application_id == application_id)) is None
    engine.dispose()
