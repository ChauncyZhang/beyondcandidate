from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import User
from server.app.integrations.feishu.models import FeishuIdentityBinding, FeishuInterviewSync
from server.app.interviews.models import Interview
from server.tests.test_interview_api import (
    create_interview,
    interview_payload,
    make_app,
    seed_application,
)
from server.tests.test_recruiting_api import login


def test_v2_calendar_event_does_not_guess_a_specific_interview(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        assert client.put(
            "/api/v1/settings/integrations/feishu",
            headers=headers,
            json={
                "app_id": "cli_test",
                "app_secret": "app-secret-value",
                "redirect_uri": "https://hr.example.test/api/v1/auth/feishu/callback",
                "calendar_id": "primary",
                "verification_token": "verified-event-token",
                "enabled": True,
            },
        ).status_code == 200
        created, _ = create_interview(
            client,
            seed,
            key="feishu-v2-provider-change",
            payload=interview_payload(
                seed,
                starts_at=datetime.now(timezone.utc) + timedelta(days=5),
            ),
        )
        interview_id = UUID(created.json()["data"]["id"])
        with app.state.identity_store.sync_session() as db:
            interview = db.get(Interview, interview_id)
            sync = db.scalar(
                select(FeishuInterviewSync).where(
                    FeishuInterviewSync.interview_id == interview_id
                )
            )
            sync.external_event_id = "evt_provider_changed"
            sync.sync_status = "synced"
            user = db.scalar(
                select(User).where(
                    User.organization_id == interview.organization_id,
                    User.normalized_email == "interview-admin@example.test",
                )
            )
            db.add(
                FeishuIdentityBinding(
                    organization_id=user.organization_id,
                    user_id=user.id,
                    union_id="on_interview_admin",
                    open_id="ou_interview_admin",
                    tenant_key="tenant-key",
                )
            )
            db.commit()
            original = (interview.starts_at, interview.ends_at)

        response = client.post(
            "/api/v1/integrations/feishu/events",
            json={
                "schema": "2.0",
                "header": {
                    "event_id": "evt-envelope-1",
                    "event_type": "calendar.calendar.event.changed_v4",
                    "token": "verified-event-token",
                    "app_id": "cli_test",
                    "tenant_key": "tenant-key",
                },
                "event": {
                    "calendar_id": "primary",
                    "user_id_list": [],
                },
                "organization_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        assert response.status_code == 200

    with app.state.identity_store.sync_session() as db:
        interview = db.get(Interview, interview_id)
        sync = db.scalar(
            select(FeishuInterviewSync).where(
                FeishuInterviewSync.interview_id == interview_id
            )
        )
        assert (interview.starts_at, interview.ends_at) == original
        assert sync.sync_status == "synced"
        assert sync.provider_revision is None
