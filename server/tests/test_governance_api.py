import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.core.settings import Settings
from server.app.governance.models import RetentionPolicy
from server.app.governance import api as governance_api
from server.app.governance import audit as governance_audit
from server.app.governance import service as governance_service
from server.app.governance.audit import category_for_event
from server.app.governance.service import GovernanceTokenCodec, InvalidGovernanceToken
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole
from server.app.interviews.models import Interview, InterviewFeedback, InterviewParticipant
from server.app.main import create_app
from server.app.recruiting.models import (
    Application,
    Candidate,
    CandidateEvent,
    FileObject,
    IdempotencyRecord,
    Resume,
)
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.tests.test_recruiting_api import login, seed_user


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
ORIGIN = "https://hr.example.test"


class Probe:
    async def check(self) -> None:
        pass


class FixedClock:
    def current_time(self) -> datetime:
        return NOW


class OffsetClock:
    def __init__(self, current: datetime):
        self.current = current

    def current_time(self) -> datetime:
        return self.current


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'governance-api.db'}",
            cors_origins=[ORIGIN],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
        clock=FixedClock(),
    )
    app.state.identity_store.create_schema()
    return app


def add_role(app, user_id, role):
    with app.state.identity_store.sync_session() as db:
        db.add(UserRole(user_id=user_id, role=role))
        db.commit()


def policy_payload(**changes):
    payload = {
        "terminal_days": 365,
        "talent_pool_days": 730,
        "backup_window_days": 90,
    }
    payload.update(changes)
    return payload


def seed_recruiting_audit(app, actor_id, *, granted=True, created_at=NOW, event_type="candidate.created"):
    with app.state.identity_store.sync_session() as db:
        actor = db.get(User, actor_id)
        job = Job(
            organization_id=actor.organization_id,
            title="Visible role" if granted else "Revoked role",
            owner_id=actor.id,
            status="open",
        )
        candidate = Candidate(
            organization_id=actor.organization_id,
            display_name="Visible candidate" if granted else "Private candidate",
            owner_id=actor.id,
        )
        file = FileObject(
            organization_id=actor.organization_id,
            storage_key=f"private/{uuid.uuid4()}",
            original_filename="private.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="a" * 64,
            uploaded_by=actor.id,
        )
        db.add_all([job, candidate, file])
        db.flush()
        resume = Resume(
            organization_id=actor.organization_id,
            candidate_id=candidate.id,
            file_object_id=file.id,
            version_number=1,
            parsed_text="raw resume must never leak",
        )
        db.add(resume)
        db.flush()
        application = Application(
            organization_id=actor.organization_id,
            candidate_id=candidate.id,
            job_id=job.id,
            resume_id=resume.id,
            owner_id=actor.id,
            stage="hired",
            updated_at=created_at,
        )
        db.add(application)
        db.flush()
        if granted:
            db.add(
                JobCollaborator(
                    organization_id=actor.organization_id,
                    job_id=job.id,
                    user_id=actor.id,
                    access_role="job_recruiter",
                )
            )
        audit = AuditLog(
            organization_id=actor.organization_id,
            actor_user_id=actor.id,
            category="recruiting",
            event_type=event_type,
            outcome="success",
            resource_type="candidate",
            resource_id=candidate.id,
            ip_hash="abcdef012345" + "0" * 52,
            trace_id="trace-safe",
            metadata_json={
                "safe_scalar": "allowed",
                "resume_text": "raw resume must never leak",
                "object_key": "private/object",
            },
            created_at=created_at,
        )
        db.add(audit)
        db.commit()
        return {"audit_id": audit.id, "candidate_id": candidate.id, "job_id": job.id}


def test_openapi_registers_only_task_a_governance_families(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        document = client.get("/openapi.json").json()

    paths = document["paths"]
    assert set(paths["/api/v1/audit-logs"]) == {"get"}
    assert set(paths["/api/v1/settings/retention-policy"]) == {"get", "patch"}
    assert set(paths["/api/v1/settings/retention-policy/previews"]) == {"post"}
    assert "metadata_json" not in str(document)


def test_llm_audit_events_are_explicitly_system_and_governance_keys_are_domain_separated() -> None:
    assert "llm." in governance_audit.CATEGORY_PREFIXES["system"]
    assert category_for_event("llm.config_updated") == "system"
    assert category_for_event("llm.connection_tested") == "system"
    root = b"governance-root-secret"
    audit_key = governance_service.derive_governance_key(root, "audit-cursor")
    preview_key = governance_service.derive_governance_key(root, "retention-preview")
    assert audit_key != preview_key
    audit_codec = GovernanceTokenCodec(audit_key)
    preview_codec = GovernanceTokenCodec(preview_key)
    token = audit_codec.encode({"kind": "audit_cursor"})
    try:
        preview_codec.decode(token)
    except InvalidGovernanceToken:
        pass
    else:
        raise AssertionError("an audit cursor must not verify as a retention preview token")


def test_retention_read_roles_defaults_and_fail_closed_denials(tmp_path) -> None:
    app = make_app(tmp_path)
    allowed = {}
    for role in ("system_admin", "recruiting_admin", "recruiter"):
        user_id = seed_user(app, role, f"{role}@governance.test")
        allowed[role] = user_id
    denied = [
        seed_user(app, role, f"{role}@governance.test")
        for role in ("hiring_manager", "interviewer")
    ]

    with TestClient(app) as client:
        for role in allowed:
            headers = login(client, f"{role}@governance.test")
            response = client.get("/api/v1/settings/retention-policy", headers=headers)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.json()["data"] | {} == response.json()["data"]
            assert response.json()["data"]["terminal_days"] == 365
            assert response.json()["data"]["updated_by"] == {
                "id": None,
                "display_name": "System migration",
            }
        for user_id in denied:
            with app.state.identity_store.sync_session() as db:
                email = db.get(User, user_id).email
            headers = login(client, email)
            response = client.get("/api/v1/settings/retention-policy", headers=headers)
            assert response.status_code == 404
            assert response.json()["code"] == "resource_not_found"
            assert response.headers["Cache-Control"] == "no-store"


def test_audit_role_union_tenant_isolation_redaction_and_safe_projection(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "recruiter@governance.test")
    add_role(app, recruiter_id, "system_admin")
    visible = seed_recruiting_audit(app, recruiter_id, granted=True)
    hidden = seed_recruiting_audit(app, recruiter_id, granted=False, created_at=NOW - timedelta(seconds=1))
    with app.state.identity_store.sync_session() as db:
        actor = db.get(User, recruiter_id)
        db.add(
            AuditLog(
                organization_id=actor.organization_id,
                actor_user_id=actor.id,
                category="system",
                event_type="authentication.login",
                outcome="success",
                trace_id="trace-system",
                metadata_json={"email": "must-not-leak@example.test"},
                created_at=NOW - timedelta(seconds=2),
            )
        )
        other = Organization(slug="other-governance", name="Other", status="active")
        db.add(other)
        db.flush()
        db.add(
            AuditLog(
                organization_id=other.id,
                category="system",
                event_type="authentication.login",
                outcome="success",
                metadata_json={},
                created_at=NOW,
            )
        )
        db.commit()

    with TestClient(app) as client:
        headers = login(client, "recruiter@governance.test")
        response = client.get("/api/v1/audit-logs", headers=headers)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    items = response.json()["data"]
    assert {item["id"] for item in items} >= {str(visible["audit_id"]), str(hidden["audit_id"])}
    assert all(item["network_ref"] in {None, "abcdef012345"} for item in items)
    assert "raw resume" not in response.text
    assert "private/object" not in response.text
    by_id = {item["id"]: item for item in items}
    assert by_id[str(visible["audit_id"])]["resource"]["label"] == "Visible candidate"
    assert by_id[str(hidden["audit_id"])]["resource"] is None


def test_real_candidate_writer_is_recruiting_visible_for_role_union_only(tmp_path) -> None:
    app = make_app(tmp_path)
    recruiter_id = seed_user(app, "recruiter", "producer@governance.test")
    seed_user(app, "recruiting_admin", "producer-admin@governance.test")
    seed_user(app, "system_admin", "producer-system@governance.test")
    seed_user(app, "hiring_manager", "producer-manager@governance.test")
    seed_user(app, "interviewer", "producer-interviewer@governance.test")
    with TestClient(app) as client:
        recruiter_headers = login(client, "producer@governance.test")
        created = client.post(
            "/api/v1/candidates",
            json={"display_name": "Real producer", "contacts": []},
            headers=recruiter_headers,
        )
        assert created.status_code == 201
        with app.state.identity_store.sync_session() as db:
            produced_audit = db.scalar(
                select(AuditLog).where(
                    AuditLog.actor_user_id == recruiter_id,
                    AuditLog.event_type == "candidate.created",
                )
            )
            assert produced_audit is not None
            produced_audit.created_at = NOW
            db.commit()
        recruiter_headers = login(client, "producer@governance.test")
        recruiter_rows = client.get(
            "/api/v1/audit-logs?event_type=candidate.created", headers=recruiter_headers
        )
        admin_headers = login(client, "producer-admin@governance.test")
        admin_rows = client.get(
            "/api/v1/audit-logs?event_type=candidate.created", headers=admin_headers
        )
        system_headers = login(client, "producer-system@governance.test")
        system_rows = client.get(
            "/api/v1/audit-logs?event_type=candidate.created", headers=system_headers
        )
        for email in ("producer-manager@governance.test", "producer-interviewer@governance.test"):
            denied_headers = login(client, email)
            denied = client.get("/api/v1/audit-logs", headers=denied_headers)
            assert denied.status_code == 404

    assert [item["event_type"] for item in recruiter_rows.json()["data"]] == ["candidate.created"]
    assert [item["event_type"] for item in admin_rows.json()["data"]] == ["candidate.created"]
    assert system_rows.json()["data"] == []
    add_role(app, recruiter_id, "system_admin")
    with TestClient(app) as client:
        dual_headers = login(client, "producer@governance.test")
        with app.state.identity_store.sync_session() as db:
            produced_audits = db.scalars(
                select(AuditLog).where(AuditLog.actor_user_id == recruiter_id)
            ).all()
            assert produced_audits
            for produced_audit in produced_audits:
                produced_audit.created_at = NOW
            db.commit()
        dual = client.get("/api/v1/audit-logs", headers=dual_headers)
    assert {item["category"] for item in dual.json()["data"]} >= {"system", "recruiting"}


def test_audit_filters_range_cursor_binding_and_equal_timestamp_pagination(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "recruiting_admin", "admin@governance.test")
    ids = [
        seed_recruiting_audit(app, admin_id, created_at=NOW)["audit_id"]
        for _ in range(3)
    ]
    with TestClient(app) as client:
        headers = login(client, "admin@governance.test")
        first = client.get("/api/v1/audit-logs?limit=2&outcome=success", headers=headers)
        cursor = first.json()["meta"]["next_cursor"]
        second = client.get(
            f"/api/v1/audit-logs?limit=2&outcome=success&cursor={cursor}", headers=headers
        )
        tampered = client.get(
            f"/api/v1/audit-logs?limit=2&outcome=success&cursor={cursor[:-1]}x", headers=headers
        )
        rebound = client.get(
            f"/api/v1/audit-logs?limit=2&outcome=denied&cursor={cursor}", headers=headers
        )
        too_wide = client.get(
            "/api/v1/audit-logs?from=2026-01-01T00:00:00Z&to=2026-07-14T00:00:00Z",
            headers=headers,
        )

    returned = [item["id"] for item in first.json()["data"] + second.json()["data"]]
    assert set(map(str, ids)) <= set(returned)
    assert len(returned) == len(set(returned))
    assert tampered.status_code == rebound.status_code == 422
    assert too_wide.status_code == 422


def test_policy_preview_patch_preconditions_idempotency_and_expiry(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "system@governance.test")
    recruiter_id = seed_user(app, "recruiter", "reader@governance.test")
    candidate = seed_recruiting_audit(app, admin_id)["candidate_id"]

    with TestClient(app) as client:
        admin_headers = login(client, "system@governance.test")
        reader_headers = login(client, "reader@governance.test")
        denied = client.post(
            "/api/v1/settings/retention-policy/previews",
            json=policy_payload(terminal_days=300),
            headers=reader_headers,
        )
        admin_headers = login(client, "system@governance.test")
        invalid = client.post(
            "/api/v1/settings/retention-policy/previews",
            json={**policy_payload(), "unexpected": True},
            headers=admin_headers,
        )
        preview = client.post(
            "/api/v1/settings/retention-policy/previews",
            json=policy_payload(terminal_days=300),
            headers=admin_headers,
        )
        token = preview.json()["data"]["impact_token"]
        body = {**policy_payload(terminal_days=300), "impact_token": token}
        missing_version = client.patch(
            "/api/v1/settings/retention-policy",
            json=body,
            headers={**admin_headers, "Idempotency-Key": "policy-1"},
        )
        first = client.patch(
            "/api/v1/settings/retention-policy",
            json=body,
            headers={**admin_headers, "Idempotency-Key": "policy-1", "If-Match": '"1"'},
        )
        replay = client.patch(
            "/api/v1/settings/retention-policy",
            json=body,
            headers={**admin_headers, "Idempotency-Key": "policy-1", "If-Match": '"1"'},
        )
        conflict = client.patch(
            "/api/v1/settings/retention-policy",
            json={**body, "backup_window_days": 100},
            headers={**admin_headers, "Idempotency-Key": "policy-1", "If-Match": '"1"'},
        )

    assert denied.status_code == 404
    assert invalid.status_code == 422
    assert preview.status_code == 200 and preview.json()["data"]["shortening"] is True
    assert missing_version.status_code == 428
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["data"]["version"] == 2
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    with app.state.identity_store.sync_session() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "policy-1"))
        assert record.expires_at is not None
        record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

    with TestClient(app) as client:
        headers = login(client, "system@governance.test")
        extension = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=400),
            headers={**headers, "Idempotency-Key": "policy-1", "If-Match": '"2"'},
        )
    assert extension.status_code == 200
    assert extension.json()["data"]["version"] == 3
    with app.state.identity_store.sync_session() as db:
        policy = db.scalar(select(RetentionPolicy))
        audit_count = len(
            db.scalars(select(AuditLog).where(AuditLog.event_type == "retention_policy.updated")).all()
        )
        assert policy.updated_by == admin_id
        assert audit_count == 2
        assert db.get(Candidate, candidate).retention_due_at is not None


def test_shortening_preview_rejects_tamper_expiry_and_stale_impact(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "preview@governance.test")
    seed_recruiting_audit(app, admin_id)
    with TestClient(app) as client:
        headers = login(client, "preview@governance.test")
        preview = client.post(
            "/api/v1/settings/retention-policy/previews",
            json=policy_payload(terminal_days=300),
            headers=headers,
        ).json()["data"]
        tampered = client.patch(
            "/api/v1/settings/retention-policy",
            json={**policy_payload(terminal_days=300), "impact_token": preview["impact_token"] + "x"},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "tampered"},
        )
        missing = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=300),
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "missing"},
        )
    assert tampered.status_code == 409
    assert tampered.json()["code"] == "retention_preview_invalid"
    assert missing.status_code == 409
    assert missing.json()["code"] == "retention_preview_required"

    app.state.recruiting_clock = OffsetClock(NOW + timedelta(minutes=11))
    with TestClient(app) as client:
        headers = login(client, "preview@governance.test")
        expired = client.patch(
            "/api/v1/settings/retention-policy",
            json={**policy_payload(terminal_days=300), "impact_token": preview["impact_token"]},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "expired"},
        )
    assert expired.status_code == 409
    assert expired.json()["code"] == "retention_preview_expired"

    app.state.recruiting_clock = OffsetClock(NOW)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        db.add(Candidate(organization_id=admin.organization_id, display_name="Late candidate", owner_id=admin.id))
        db.commit()
    with TestClient(app) as client:
        headers = login(client, "preview@governance.test")
        stale = client.patch(
            "/api/v1/settings/retention-policy",
            json={**policy_payload(terminal_days=300), "impact_token": preview["impact_token"]},
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "stale-impact"},
        )
    assert stale.status_code == 409
    assert stale.json()["code"] == "retention_preview_stale_impact"


def test_retention_due_active_null_and_terminal_latest_fact(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "due@governance.test")
    terminal_id = seed_recruiting_audit(app, admin_id, created_at=NOW - timedelta(days=10))["candidate_id"]
    active_id = seed_recruiting_audit(app, admin_id, created_at=NOW - timedelta(days=10))["candidate_id"]
    with app.state.identity_store.sync_session() as db:
        active_application = db.scalar(select(Application).where(Application.candidate_id == active_id))
        active_application.stage = "review"
        terminal = db.get(Candidate, terminal_id)
        terminal.updated_at = NOW - timedelta(days=20)
        source_updated_at = terminal.updated_at
        terminal_application = db.scalar(select(Application).where(Application.candidate_id == terminal_id))
        interview = Interview(
            organization_id=terminal.organization_id,
            application_id=terminal_application.id,
            round_name="Final",
            method="video",
            timezone="UTC",
            starts_at=NOW - timedelta(days=4),
            ends_at=NOW - timedelta(days=4, hours=-1),
            owner_id=admin_id,
            created_by=admin_id,
            status="feedback_completed",
            updated_at=NOW - timedelta(days=3),
        )
        db.add(interview)
        db.flush()
        db.add(
            InterviewParticipant(
                organization_id=terminal.organization_id,
                interview_id=interview.id,
                user_id=admin_id,
            )
        )
        db.flush()
        db.add(
            InterviewFeedback(
                organization_id=terminal.organization_id,
                interview_id=interview.id,
                author_id=admin_id,
                status="amended",
                ratings={},
                submitted_at=NOW - timedelta(days=10),
                updated_at=NOW - timedelta(days=2),
            )
        )
        db.add(
            CandidateEvent(
                organization_id=terminal.organization_id,
                candidate_id=terminal.id,
                actor_user_id=admin_id,
                event_type="candidate.note",
                payload={},
                created_at=NOW - timedelta(days=5),
            )
        )
        db.commit()
    with TestClient(app) as client:
        headers = login(client, "due@governance.test")
        response = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=400),
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "due-update"},
        )
    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        assert db.get(Candidate, active_id).retention_due_at is None
        terminal = db.get(Candidate, terminal_id)
        due = terminal.retention_due_at
        assert terminal.updated_at.replace(tzinfo=terminal.updated_at.tzinfo or timezone.utc) == source_updated_at
        assert due.replace(tzinfo=due.tzinfo or timezone.utc) == NOW - timedelta(days=2) + timedelta(days=400)


def test_talent_maximum_wins_and_explicit_membership_date_is_unchanged(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "talent-due@governance.test")
    candidate_id = seed_recruiting_audit(app, admin_id, created_at=NOW - timedelta(days=30))["candidate_id"]
    explicit_until = NOW + timedelta(days=1000)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, admin_id)
        pool = TalentPool(
            organization_id=admin.organization_id,
            name="Governance pool",
            purpose="Retention test",
            owner_id=admin.id,
        )
        db.add(pool)
        db.flush()
        membership = TalentPoolMembership(
            organization_id=admin.organization_id,
            pool_id=pool.id,
            candidate_id=candidate_id,
            owner_id=admin.id,
            suitable_roles=[],
            tags=[],
            reason="Keep",
            retention_until=explicit_until,
        )
        db.add(membership)
        db.commit()
        membership_id = membership.id
    with TestClient(app) as client:
        headers = login(client, "talent-due@governance.test")
        response = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=400, talent_pool_days=800),
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "talent-due"},
        )
    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        stored_until = db.get(TalentPoolMembership, membership_id).retention_until
        due = db.get(Candidate, candidate_id).retention_due_at
        assert stored_until.replace(tzinfo=stored_until.tzinfo or timezone.utc) == explicit_until
        assert due.replace(tzinfo=due.tzinfo or timezone.utc) == explicit_until


def test_active_application_overrides_talent_membership_due_date(tmp_path) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "active-talent@governance.test")
    candidate_id = seed_recruiting_audit(app, admin_id)["candidate_id"]
    with app.state.identity_store.sync_session() as db:
        application = db.scalar(select(Application).where(Application.candidate_id == candidate_id))
        application.stage = "review"
        admin = db.get(User, admin_id)
        pool = TalentPool(
            organization_id=admin.organization_id,
            name="Active candidate pool",
            purpose="Retention test",
            owner_id=admin.id,
        )
        db.add(pool)
        db.flush()
        db.add(
            TalentPoolMembership(
                organization_id=admin.organization_id,
                pool_id=pool.id,
                candidate_id=candidate_id,
                owner_id=admin.id,
                suitable_roles=[],
                tags=[],
                reason="Keep",
                retention_until=NOW + timedelta(days=1000),
            )
        )
        db.commit()
    with TestClient(app) as client:
        headers = login(client, "active-talent@governance.test")
        response = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=400),
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "active-talent"},
        )
    assert response.status_code == 200
    with app.state.identity_store.sync_session() as db:
        assert db.get(Candidate, candidate_id).retention_due_at is None


def test_governance_redirect_and_unexpected_error_are_no_store(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    seed_user(app, "system_admin", "errors@governance.test")
    with TestClient(app, follow_redirects=False) as client:
        headers = login(client, "errors@governance.test")
        redirect = client.get("/api/v1/audit-logs/", headers=headers)
    assert redirect.status_code in {307, 308}
    assert redirect.headers["Cache-Control"] == "no-store"

    def explode(*_args, **_kwargs):
        raise RuntimeError("private SQL and credential detail")

    monkeypatch.setattr(governance_api, "policy_projection", explode)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = login(client, "errors@governance.test")
        failed = client.get("/api/v1/settings/retention-policy", headers=headers)
    assert failed.status_code == 500
    assert failed.headers["Cache-Control"] == "no-store"
    assert "private SQL" not in failed.text


def test_retention_failure_rolls_back_policy_due_date_idempotency_and_audit(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    admin_id = seed_user(app, "system_admin", "rollback@governance.test")
    candidate_id = seed_recruiting_audit(app, admin_id)["candidate_id"]
    with app.state.identity_store.sync_session() as db:
        candidate = db.get(Candidate, candidate_id)
        candidate.retention_due_at = NOW + timedelta(days=365)
        db.commit()
        original_due = candidate.retention_due_at

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit storage failed")

    monkeypatch.setattr(governance_service, "append_audit", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = login(client, "rollback@governance.test")
        response = client.patch(
            "/api/v1/settings/retention-policy",
            json=policy_payload(terminal_days=400),
            headers={**headers, "If-Match": '"1"', "Idempotency-Key": "rollback"},
        )
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(RetentionPolicy.version)) == 1
        rolled_back_due = db.get(Candidate, candidate_id).retention_due_at
        assert rolled_back_due.replace(tzinfo=timezone.utc) == original_due
        assert db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.idempotency_key == "rollback")) is None
        assert db.scalar(select(AuditLog).where(AuditLog.event_type == "retention_policy.updated")) is None
