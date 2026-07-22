from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import pytest

from server.app.identity.models import Job, Organization
from server.app.notifications import api as notification_api
from server.app.notifications.models import NotificationRead
from server.app.recruiting import api as recruiting_api
from server.app.recruiting.models import ApplicationReviewTask, Candidate
from server.tests.test_workbench_api import make_app, principal, seed_application, seed_user


WRITE_HEADERS = {
    "Origin": "https://hr.example.test",
    "X-CSRF-Token": "test-csrf",
    "Cookie": "hr_session=test-session",
}


def _seed_notification(app, stage="decision"):
    base = datetime(2026, 7, 22, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as db:
        organization = Organization(slug="acme", name="Acme", status="active")
        first = seed_user(db, organization, "recruiting_admin", "first@example.test")
        second = seed_user(db, organization, "recruiting_admin", "second@example.test")
        job = Job(
            organization_id=organization.id,
            title="Backend Engineer",
            owner_id=first.id,
            status="open",
            updated_at=base,
        )
        db.add(job)
        db.flush()
        application = seed_application(db, job, first, 1, stage, base)
        db.commit()
        return principal(first), principal(second), application.id


def _notification(client: TestClient, group="decision"):
    response = client.get("/api/v1/workbench")
    assert response.status_code == 200
    notification_group = response.json()["data"]["notifications"][group]
    assert notification_group["count"] == 1
    return notification_group["items"][0]


def test_notification_read_is_user_scoped_persistent_and_idempotent(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    first, second, application_id = _seed_notification(app)
    current = {"principal": first}
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: current["principal"])
    monkeypatch.setattr(notification_api, "_principal", lambda request: current["principal"])
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)

    with TestClient(app) as client:
        item = _notification(client)
        path = f"/api/v1/notifications/workbench/{application_id}/read"
        first_read = client.put(path, json={"version": item["notification_version"]}, headers=WRITE_HEADERS)
        repeated = client.put(path, json={"version": item["notification_version"]}, headers=WRITE_HEADERS)
        assert first_read.status_code == repeated.status_code == 200
        assert first_read.json()["data"]["read_at"] == repeated.json()["data"]["read_at"]

        refreshed = client.get("/api/v1/workbench").json()["data"]
        assert refreshed["notifications"]["decision"] == {"count": 0, "items": []}
        assert refreshed["tasks"]["decision"]["count"] == 1

        current["principal"] = second
        assert _notification(client)["application_id"] == str(application_id)

    current["principal"] = first
    with TestClient(app) as another_device:
        restored = another_device.get("/api/v1/workbench").json()["data"]
        assert restored["notifications"]["decision"] == {"count": 0, "items": []}
        assert restored["tasks"]["decision"]["count"] == 1

    with app.state.identity_store.sync_session() as db:
        receipts = db.query(NotificationRead).all()
        assert len(receipts) == 1
        assert receipts[0].user_id == first.user_id
        assert receipts[0].application_id == application_id


def test_substantive_notification_update_becomes_unread_and_stale_version_is_rejected(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    actor, _, application_id = _seed_notification(app)
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor)
    monkeypatch.setattr(notification_api, "_principal", lambda request: actor)
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)

    with TestClient(app) as client:
        original = _notification(client)
        path = f"/api/v1/notifications/workbench/{application_id}/read"
        assert client.put(path, json={"version": original["notification_version"]}, headers=WRITE_HEADERS).status_code == 200

        with app.state.identity_store.sync_session() as db:
            application = db.get(recruiting_api.Application, application_id)
            application.version += 1
            application.updated_at = application.updated_at + timedelta(minutes=1)
            db.commit()

        updated = _notification(client)
        assert updated["notification_version"] != original["notification_version"]
        stale = client.put(path, json={"version": original["notification_version"]}, headers=WRITE_HEADERS)
        assert stale.status_code == 409
        assert stale.json()["code"] == "notification_version_conflict"


def test_notification_read_is_non_disclosing_for_unauthorized_application(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    owner, _, application_id = _seed_notification(app)
    with app.state.identity_store.sync_session() as db:
        outsider = seed_user(db, db.get(Organization, owner.organization_id), "hiring_manager", "outsider@example.test")
        db.commit()
        outsider_principal = principal(outsider)

    monkeypatch.setattr(notification_api, "_principal", lambda request: outsider_principal)
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)
    with TestClient(app) as client:
        response = client.put(
            f"/api/v1/notifications/workbench/{application_id}/read",
            json={"version": "0" * 64},
            headers=WRITE_HEADERS,
        )
    assert response.status_code == 404
    assert response.json()["code"] == "resource_not_found"


@pytest.mark.parametrize("stage", ["interview_pending", "decision", "passed"])
def test_mark_read_permission_matches_each_actionable_workbench_group(tmp_path, monkeypatch, stage) -> None:
    app = make_app(tmp_path)
    actor, _, application_id = _seed_notification(app, stage)
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor)
    monkeypatch.setattr(notification_api, "_principal", lambda request: actor)
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)

    with TestClient(app) as client:
        item = _notification(client, stage)
        response = client.put(
            f"/api/v1/notifications/workbench/{application_id}/read",
            json={"version": item["notification_version"]},
            headers=WRITE_HEADERS,
        )
    assert response.status_code == 200


def test_mark_read_permission_matches_assigned_review_task_group(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    actor, _, application_id = _seed_notification(app, "review")
    with app.state.identity_store.sync_session() as db:
        db.add(ApplicationReviewTask(
            organization_id=actor.organization_id,
            application_id=application_id,
            assignee_id=actor.user_id,
            status="open",
            ai_status="succeeded",
        ))
        db.commit()
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor)
    monkeypatch.setattr(notification_api, "_principal", lambda request: actor)
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)

    with TestClient(app) as client:
        item = _notification(client, "review")
        response = client.put(
            f"/api/v1/notifications/workbench/{application_id}/read",
            json={"version": item["notification_version"]},
            headers=WRITE_HEADERS,
        )
    assert response.status_code == 200


def test_candidate_and_job_profile_edits_do_not_reopen_read_notification(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    actor, _, application_id = _seed_notification(app)
    monkeypatch.setattr(recruiting_api, "_principal", lambda request: actor)
    monkeypatch.setattr(notification_api, "_principal", lambda request: actor)
    monkeypatch.setattr(app.state.identity_service, "validate_csrf", lambda *args, **kwargs: True)

    with TestClient(app) as client:
        item = _notification(client)
        path = f"/api/v1/notifications/workbench/{application_id}/read"
        assert client.put(path, json={"version": item["notification_version"]}, headers=WRITE_HEADERS).status_code == 200

        with app.state.identity_store.sync_session() as db:
            application = db.get(recruiting_api.Application, application_id)
            candidate = db.get(Candidate, application.candidate_id)
            job = db.get(Job, application.job_id)
            candidate.display_name = "Profile-only rename"
            candidate.updated_at = candidate.updated_at + timedelta(minutes=1)
            job.title = "Profile-only job rename"
            job.updated_at = job.updated_at + timedelta(minutes=1)
            db.commit()

        refreshed = client.get("/api/v1/workbench").json()["data"]
        assert refreshed["notifications"]["decision"] == {"count": 0, "items": []}
        assert refreshed["tasks"]["decision"]["count"] == 1
