from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import AuditLog, Job, JobCollaborator, PasswordInvitation, User, UserRole, UserSession, UserStatus
from server.app.identity.security import hash_token
from server.tests.test_identity_management import login, management_app, seed_user


ORIGIN = {"Origin": "https://hr.example.test"}


def preview(client, user_id, successor_id=None):
    return client.get(f"/api/v1/settings/users/{user_id}/offboarding", params={"successor_id": str(successor_id)} if successor_id else {})


def remove(client, user_id, headers, *, successor_id=None, revoke_only=False, version=None):
    if version is None:
        response = preview(client, user_id, successor_id)
        assert response.status_code == 200, response.text
        version = response.json()["data"]["preview_version"]
    return client.post(f"/api/v1/settings/users/{user_id}/offboarding", headers=headers, json={
        "successor_user_id": str(successor_id) if successor_id else None,
        "preview_version": version, "revoke_only": revoke_only,
    })


def test_preview_is_read_only_and_revocation_blocks_sessions_password_reset_and_bootstrap(management_app):
    app, client, clock = management_app
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    with TestClient(app) as target_client:
        login(target_client, "target@example.test")
        with app.state.identity_store.sync_session() as db:
            db.add(PasswordInvitation(organization_id=target.organization_id, user_id=target.user_id,
                token_hash=hash_token("offboard-existing-reset-token"), purpose="password_reset",
                reset_authorization_version=1, expires_at=clock.now + timedelta(hours=1)))
            db.commit()
        headers = login(client, "admin@example.test")
        before = preview(client, target.user_id)
        assert before.status_code == 200
        assert before.json()["data"]["handover_status"] == "not_started"
        assert before.headers["cache-control"] == "no-store"
        with app.state.identity_store.sync_session() as db:
            assert db.get(User, target.user_id).status == UserStatus.ACTIVE
        result = remove(client, target.user_id, headers, revoke_only=True)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["removed_at"]
        assert target_client.get("/api/v1/me", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 401
    assert client.post("/api/v1/auth/login", headers=ORIGIN, json={"organization_slug": "acme",
        "email": "target@example.test", "password": "correct horse battery"}).status_code == 401
    assert client.post("/api/v1/auth/password-reset/accept", headers=ORIGIN,
        json={"token": "offboard-existing-reset-token", "password": "new password secure value"}).status_code == 422
    headers = login(client, "admin@example.test")
    assert client.post(f"/api/v1/settings/users/{target.user_id}/password-reset", headers=headers).status_code == 409
    assert client.post(f"/api/v1/settings/users/{target.user_id}/invitation", headers=headers).status_code == 409
    with app.state.identity_store.sync_session() as db:
        user = db.get(User, target.user_id)
        assert user.authorization_version == 2
        assert user.display_name == "Recruiter"
        assert all(row.revoked_at and row.revocation_reason == "member_offboarded" for row in db.scalars(select(UserSession).where(UserSession.user_id == user.id)))
        assert app.state.identity_service._aware(db.scalar(select(PasswordInvitation).where(PasswordInvitation.user_id == user.id)).expires_at) <= clock.now
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "identity.member_offboarded"))
        assert audit.actor_user_id == actor.user_id and audit.resource_id == target.user_id
    from server.app.identity.bootstrap import bootstrap_system_admin
    with pytest.raises(ValueError, match="offboarded"):
        bootstrap_system_admin(app.state.identity_store, "acme", "Acme", "target@example.test", "Do Not Restore", "bootstrap new secure password")
    users = client.get("/api/v1/settings/users").json()["data"]
    target_row = next(row for row in users if row["id"] == str(target.user_id))
    assert target_row["status"] == "disabled"
    assert target_row["offboarding"]["removed_at"]


@pytest.mark.parametrize("actor_role,target_role,expected", [
    ("system_admin", "recruiter", 200), ("system_admin", "recruiting_admin", 200),
    ("recruiting_admin", "recruiter", 200), ("recruiting_admin", "hiring_manager", 200),
    ("recruiting_admin", "system_admin", 403), ("recruiting_admin", "recruiting_admin", 403),
    ("recruiter", "recruiter", 403), ("interviewer", "recruiter", 403),
])
def test_role_hierarchy_is_enforced_for_revocation(management_app, actor_role, target_role, expected):
    app, client, _ = management_app
    seed_user(app, role=actor_role, email="actor@example.test")
    target = seed_user(app, role=target_role, email="target@example.test")
    headers = login(client, "actor@example.test")
    response = client.post(f"/api/v1/settings/users/{target.user_id}/offboarding", headers=headers,
        json={"successor_user_id": None, "preview_version": "unused", "revoke_only": True}) if expected != 200 else remove(client, target.user_id, headers, revoke_only=True)
    assert response.status_code == expected, response.text


def test_self_and_cross_tenant_removal_are_rejected(management_app):
    app, client, _ = management_app
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    foreign = seed_user(app, role="recruiter", email="foreign@example.test", organization_slug="other")
    headers = login(client, "admin@example.test")
    assert preview(client, foreign.user_id).status_code == 404
    for user_id, status in [(actor.user_id, 409), (foreign.user_id, 404), (uuid4(), 404)]:
        response = client.post(f"/api/v1/settings/users/{user_id}/offboarding", headers=headers,
            json={"successor_user_id": None, "preview_version": "unused", "revoke_only": True})
        assert response.status_code == status


def test_stale_preview_does_not_revoke_and_repeated_removal_does_not_duplicate_audit(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    headers = login(client, "admin@example.test")
    version = preview(client, target.user_id).json()["data"]["preview_version"]
    with app.state.identity_store.sync_session() as db:
        db.get(User, target.user_id).authorization_version += 1
        db.commit()
    stale = remove(client, target.user_id, headers, revoke_only=True, version=version)
    assert stale.status_code == 409 and stale.json()["code"] == "offboarding_preview_changed"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).status == UserStatus.ACTIVE
    assert remove(client, target.user_id, headers, revoke_only=True).status_code == 200
    assert remove(client, target.user_id, headers, revoke_only=True).status_code == 200
    with app.state.identity_store.sync_session() as db:
        assert len(db.scalars(select(AuditLog).where(AuditLog.event_type == "identity.member_offboarded")).all()) == 1


def test_successor_must_be_active_distinct_and_same_tenant_but_revoke_only_needs_none(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    foreign = seed_user(app, role="recruiter", email="foreign@example.test", organization_slug="other")
    inactive = seed_user(app, role="recruiter", email="inactive@example.test")
    with app.state.identity_store.sync_session() as db:
        db.get(User, inactive.user_id).status = UserStatus.DISABLED
        db.commit()
    headers = login(client, "admin@example.test")
    for successor in (target.user_id, foreign.user_id, inactive.user_id):
        assert preview(client, target.user_id, successor).status_code == 422
    assert remove(client, target.user_id, headers, revoke_only=True).status_code == 200


def test_mutation_rechecks_actor_authority_after_initial_principal(management_app, monkeypatch):
    app, client, _ = management_app
    actor = seed_user(app, role="recruiting_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    headers = login(client, "admin@example.test")
    version = preview(client, target.user_id).json()["data"]["preview_version"]
    original = app.state.identity_service.principal
    def demote(token):
        principal = original(token)
        with app.state.identity_store.sync_session() as db:
            db.scalar(select(UserRole).where(UserRole.user_id == actor.user_id)).role = "recruiter"
            db.commit()
        return principal
    monkeypatch.setattr(app.state.identity_service, "principal", demote)
    assert remove(client, target.user_id, headers, revoke_only=True, version=version).status_code == 403
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).status == UserStatus.ACTIVE


def test_api_handover_transfers_eligible_work_preserves_history_and_exposes_role_blockers(management_app):
    app, client, clock = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    successor = seed_user(app, role="recruiter", email="successor@example.test")
    from server.app.recruiting.models import Candidate
    with app.state.identity_store.sync_session() as db:
        db.add(UserRole(user_id=target.user_id, role="hiring_manager"))
        job = Job(organization_id=target.organization_id, owner_id=target.user_id,
            hiring_owner_id=target.user_id, title="Operational Job", status="open")
        candidate = Candidate(organization_id=target.organization_id, display_name="Live Candidate", owner_id=target.user_id)
        db.add_all([job, candidate])
        db.flush()
        historical = AuditLog(organization_id=target.organization_id, actor_user_id=target.user_id,
            event_type="historical.action", outcome="success", metadata_json={"original": True})
        db.add(historical)
        db.commit()
        job_id, candidate_id, audit_id = job.id, candidate.id, historical.id
    headers = login(client, "admin@example.test")
    result = remove(client, target.user_id, headers, successor_id=successor.user_id)
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["transferred_count"] >= 2
    assert data["handover_status"] == "pending"
    assert any(item["reason_code"] == "successor_role_required" for item in data["items"])
    with app.state.identity_store.sync_session() as db:
        assert db.get(Job, job_id).owner_id == successor.user_id
        assert db.get(Job, job_id).hiring_owner_id == target.user_id
        assert db.get(Candidate, candidate_id).owner_id == successor.user_id
        assert set(db.scalars(select(UserRole.role).where(UserRole.user_id == successor.user_id))) == {"recruiter"}
        assert db.get(AuditLog, audit_id).actor_user_id == target.user_id
        assert db.scalar(select(JobCollaborator).where(JobCollaborator.user_id == successor.user_id, JobCollaborator.job_id == job_id))
        # An administrator can repair the missing role through its existing
        # configuration; handover itself never creates a role.
        db.add(UserRole(user_id=successor.user_id, role="hiring_manager"))
        db.get(User, successor.user_id).authorization_version += 1
        db.commit()
    retry = remove(client, target.user_id, headers, successor_id=successor.user_id)
    assert retry.status_code == 200, retry.text
    assert retry.json()["data"]["handover_status"] == "completed"
    with app.state.identity_store.sync_session() as db:
        assert db.get(Job, job_id).hiring_owner_id == successor.user_id
        assert len(db.scalars(select(AuditLog).where(AuditLog.event_type == "identity.member_offboarded")).all()) == 1
        assert db.get(AuditLog, audit_id).metadata_json == {"original": True}


def test_durable_pending_snapshot_contains_routing_references_not_candidate_content(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    from server.app.recruiting.models import Candidate
    from server.app.identity.offboarding_models import MemberOffboarding
    with app.state.identity_store.sync_session() as db:
        db.add(Candidate(organization_id=target.organization_id, display_name="Private Candidate Name", owner_id=target.user_id))
        db.commit()
    response = remove(client, target.user_id, login(client, "admin@example.test"), revoke_only=True)
    assert response.status_code == 200
    assert response.json()["data"]["handover_status"] == "pending"
    with app.state.identity_store.sync_session() as db:
        record = db.scalar(select(MemberOffboarding))
        assert record.unresolved
        assert all(set(item) <= {"category", "resource_type", "resource_id", "reason_code"} for item in record.unresolved)
        assert "Private Candidate Name" not in str(record.unresolved)


def test_member_pending_filter_reconciles_work_resolved_outside_handover_without_writing_on_get(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    from server.app.recruiting.models import Candidate
    from server.app.identity.offboarding_models import MemberOffboarding
    with app.state.identity_store.sync_session() as db:
        candidate = Candidate(organization_id=target.organization_id, display_name="Resolved elsewhere", owner_id=target.user_id)
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    headers = login(client, "admin@example.test")
    assert remove(client, target.user_id, headers, revoke_only=True).status_code == 200
    with app.state.identity_store.sync_session() as db:
        db.get(Candidate, candidate_id).deleted_at = app.state.identity_service.clock.current_time()
        db.commit()
    listed = client.get("/api/v1/settings/users").json()["data"]
    summary = next(row["offboarding"] for row in listed if row["id"] == str(target.user_id))
    assert summary["pending_count"] == 0 and summary["handover_status"] == "completed"
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(MemberOffboarding)).status == "pending", "GET reconciles the view without mutating durable state"


def test_revoke_only_does_not_activate_preselected_successor_or_transfer_future_work(management_app):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    successor = seed_user(app, role="recruiter", email="successor@example.test")
    headers = login(client, "admin@example.test")
    result = remove(client, target.user_id, headers, successor_id=successor.user_id, revoke_only=True)
    assert result.status_code == 200
    assert result.json()["data"]["successor_user_id"] is None
    from server.app.identity.offboarding_models import MemberOffboarding
    from server.app.identity.offboarding_resolver import OperationalOwnerUnavailable, resolve_operational_user_id
    with app.state.identity_store.sync_session() as db:
        assert db.scalar(select(MemberOffboarding)).successor_user_id is None
        with pytest.raises(OperationalOwnerUnavailable):
            resolve_operational_user_id(db, target.organization_id, target.user_id)


def test_unaccepted_member_can_be_removed_and_activation_cannot_restore_access(management_app):
    app, client, clock = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="pending@example.test")
    token = "pending-member-activation-offboarding"
    with app.state.identity_store.sync_session() as db:
        db.get(User, target.user_id).status = UserStatus.INVITED
        db.add(PasswordInvitation(organization_id=target.organization_id, user_id=target.user_id,
            token_hash=hash_token(token), expires_at=clock.now + timedelta(hours=48)))
        db.commit()
    assert remove(client, target.user_id, login(client, "admin@example.test"), revoke_only=True).status_code == 200
    activation = client.post("/api/v1/auth/invitations/accept", headers=ORIGIN,
        json={"token": token, "password": "new password secure value"})
    assert activation.status_code == 422 and activation.json()["code"] == "invitation_invalid_or_expired"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).status == UserStatus.DISABLED


def test_domain_write_failure_rolls_back_handover_but_commits_access_revocation(management_app, monkeypatch):
    app, client, _ = management_app
    seed_user(app, role="system_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    successor = seed_user(app, role="recruiter", email="successor@example.test")
    from server.app.identity import offboarding_handover
    from server.app.recruiting.models import Candidate
    from sqlalchemy.exc import IntegrityError
    with app.state.identity_store.sync_session() as db:
        candidate = Candidate(organization_id=target.organization_id, display_name="Do not half-transfer", owner_id=target.user_id)
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    def fail_after_partial_write(db, plan, **kwargs):
        db.get(Candidate, candidate_id).owner_id = successor.user_id
        db.flush()
        raise IntegrityError("synthetic domain failure", {}, None)
    monkeypatch.setattr(offboarding_handover, "apply_handover", fail_after_partial_write)
    result = remove(client, target.user_id, login(client, "admin@example.test"), successor_id=successor.user_id)
    assert result.status_code == 200
    assert result.json()["data"]["handover_error"]
    assert result.json()["data"]["transferred_count"] == 0
    assert result.json()["data"]["handover_status"] == "pending"
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).status == UserStatus.DISABLED
        assert db.get(Candidate, candidate_id).owner_id == target.user_id


def test_target_promotion_is_rechecked_under_lock_before_preview_comparison(management_app):
    app, client, _ = management_app
    seed_user(app, role="recruiting_admin", email="admin@example.test")
    target = seed_user(app, role="recruiter", email="target@example.test")
    headers = login(client, "admin@example.test")
    version = preview(client, target.user_id).json()["data"]["preview_version"]
    with app.state.identity_store.sync_session() as db:
        db.scalar(select(UserRole).where(UserRole.user_id == target.user_id)).role = "system_admin"
        db.commit()
    response = remove(client, target.user_id, headers, version=version, revoke_only=True)
    assert response.status_code == 403
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target.user_id).status == UserStatus.ACTIVE


def seed_unparsed_batch_handover(app):
    from server.app.recruiting.models import JobJdVersion, ScreeningRuleVersion
    from server.app.screening.models import ScreeningRun
    actor = seed_user(app, role="system_admin", email="admin@example.test")
    source = seed_user(app, role="recruiter", email="source@example.test")
    successor = seed_user(app, role="recruiter", email="successor@example.test")
    final = seed_user(app, role="recruiter", email="final@example.test")
    owner = seed_user(app, role="recruiting_admin", email="job-owner@example.test")
    with app.state.identity_store.sync_session() as db:
        for member in (source, successor):
            db.get(User, member.user_id).recruiting_scope_type = "organization"
        job = Job(organization_id=actor.organization_id, owner_id=owner.user_id, title="Upstream batch", status="open")
        db.add(job)
        db.flush()
        jd = JobJdVersion(organization_id=actor.organization_id, job_id=job.id, version_number=1, content={}, created_by=source.user_id)
        rule = ScreeningRuleVersion(organization_id=actor.organization_id, job_id=job.id, version_number=1, content={}, created_by=source.user_id)
        db.add_all([jd, rule])
        db.flush()
        run = ScreeningRun(organization_id=actor.organization_id, job_id=job.id, jd_version_id=jd.id,
            rule_version_id=rule.id, source="upload", status="queued", created_by=source.user_id, total_count=1)
        db.add(run)
        db.commit()
        return source, successor, final, job.id, run.id


def test_completed_handover_reappears_in_pending_filter_after_successor_scope_change(management_app):
    app, client, _ = management_app
    source, successor, _, _, _ = seed_unparsed_batch_handover(app)
    headers = login(client, "admin@example.test")
    result = remove(client, source.user_id, headers, successor_id=successor.user_id)
    assert result.status_code == 200 and result.json()["data"]["handover_status"] == "completed"
    changed = client.patch(f"/api/v1/settings/users/{successor.user_id}/recruiting-scope", headers=headers,
        json={"role": "recruiter", "recruiting_scope_type": "jobs", "recruiting_department_ids": []})
    assert changed.status_code == 200
    detail = preview(client, source.user_id).json()["data"]
    assert detail["handover_status"] == "pending"
    assert any(row["reason_code"] == "successor_scope_required" for row in detail["items"])
    users = client.get("/api/v1/settings/users").json()["data"]
    summary = next(row["offboarding"] for row in users if row["id"] == str(source.user_id))
    assert summary["handover_status"] == "pending" and summary["pending_count"] == len(detail["items"])


@pytest.mark.parametrize("final_has_scope", [False, True])
def test_inherited_batches_remain_visible_and_effective_chain_is_consistent(management_app, final_has_scope):
    app, client, _ = management_app
    source, successor, final, job_id, run_id = seed_unparsed_batch_handover(app)
    if final_has_scope:
        with app.state.identity_store.sync_session() as db:
            db.get(User, final.user_id).recruiting_scope_type = "organization"
            db.commit()
    headers = login(client, "admin@example.test")
    assert remove(client, source.user_id, headers, successor_id=successor.user_id).json()["data"]["handover_status"] == "completed"
    before = preview(client, successor.user_id, final.user_id).json()["data"]
    assert any(row["resource_type"] == "screening_run" for row in before["items"]), "Unfinished inherited batches must be inventoried"
    result = remove(client, successor.user_id, headers, successor_id=final.user_id)
    assert result.status_code == 200
    expected = "completed" if final_has_scope else "pending"
    assert result.json()["data"]["handover_status"] == expected
    detail = preview(client, source.user_id).json()["data"]
    assert detail["successor_user_id"] == str(final.user_id)
    assert detail["handover_status"] == expected
    users = client.get("/api/v1/settings/users").json()["data"]
    assert all(next(row["offboarding"] for row in users if row["id"] == str(member.user_id))["handover_status"] == expected for member in (source, successor))
    from server.app.screening.models import ScreeningRun
    from server.app.identity.offboarding_resolver import OperationalOwnerUnavailable, resolve_operational_user_id
    with app.state.identity_store.sync_session() as db:
        assert db.get(ScreeningRun, run_id).created_by == source.user_id
        if final_has_scope:
            assert resolve_operational_user_id(db, source.organization_id, source.user_id, job_id=job_id) == final.user_id
        else:
            with pytest.raises(OperationalOwnerUnavailable):
                resolve_operational_user_id(db, source.organization_id, source.user_id, job_id=job_id)
