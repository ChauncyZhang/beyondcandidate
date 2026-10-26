import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from server.app.identity.models import User, UserSession, UserStatus
from server.app.integrations.feishu.models import FeishuIdentityBinding
from server.app.integrations.feishu.provider import OAuthIdentity
from server.tests.test_feishu_api import config_payload, feishu_app, login, seed_user, write_headers


@pytest.mark.parametrize("bound", [True, False])
def test_removed_member_cannot_log_in_or_reactivate_through_feishu(feishu_app, bound):
    app, client = feishu_app
    seed_user(app)
    target_id = seed_user(app, email="departed@example.test", role="recruiter")
    csrf = login(client)
    assert client.put("/api/v1/settings/integrations/feishu", json=config_payload(), headers=write_headers(csrf)).status_code == 200
    with app.state.identity_store.sync_session() as db:
        if bound:
            db.add(FeishuIdentityBinding(organization_id=db.get(User, target_id).organization_id,
                user_id=target_id, union_id="on_departed", open_id="ou_departed", tenant_key="tenant"))
        db.commit()
    preview = client.get(f"/api/v1/settings/users/{target_id}/offboarding")
    assert preview.status_code == 200
    removed = client.post(f"/api/v1/settings/users/{target_id}/offboarding", headers=write_headers(csrf),
        json={"preview_version": preview.json()["data"]["preview_version"], "revoke_only": True})
    assert removed.status_code == 200, removed.text
    client.post("/api/v1/auth/logout", headers=write_headers(csrf))
    app.state.feishu_provider.identity = OAuthIdentity("on_departed", "ou_departed", "departed@example.test", "tenant")
    authorized = client.post("/api/v1/auth/feishu/authorize", json={"organization_slug": "acme"}, headers={"Origin": "https://hr.example.test"})
    assert authorized.status_code == 200
    callback = client.get("/api/v1/auth/feishu/callback", params={"code": "departed-code", "state": authorized.json()["data"]["state"]}, follow_redirects=False)
    if bound:
        assert callback.status_code == 403 and callback.json()["code"] == "feishu_account_unavailable"
    else:
        assert callback.status_code == 303 and "feishu_error=" in callback.headers["location"]
    assert "set-cookie" not in callback.headers
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target_id).status == UserStatus.DISABLED
        assert db.scalar(select(UserSession).where(UserSession.user_id == target_id)) is None


def test_removal_between_feishu_callback_and_session_issuance_is_a_safe_denial(feishu_app, monkeypatch):
    app, client = feishu_app
    seed_user(app)
    target_id = seed_user(app, email="departed@example.test", role="recruiter")
    csrf = login(client)
    assert client.put("/api/v1/settings/integrations/feishu", json=config_payload(), headers=write_headers(csrf)).status_code == 200
    client.post("/api/v1/auth/logout", headers=write_headers(csrf))
    app.state.feishu_provider.identity = OAuthIdentity("on_departed", "ou_departed", "departed@example.test", "tenant")
    authorized = client.post("/api/v1/auth/feishu/authorize", json={"organization_slug": "acme"}, headers={"Origin": "https://hr.example.test"})
    original = app.state.identity_service.issue_session
    def remove_before_session(user_id, **kwargs):
        assert user_id == target_id
        with TestClient(app) as administrator:
            headers = write_headers(login(administrator))
            detail = administrator.get(f"/api/v1/settings/users/{target_id}/offboarding").json()["data"]
            response = administrator.post(f"/api/v1/settings/users/{target_id}/offboarding", headers=headers,
                json={"preview_version": detail["preview_version"], "revoke_only": True})
            assert response.status_code == 200
        return original(user_id, **kwargs)
    monkeypatch.setattr(app.state.identity_service, "issue_session", remove_before_session)
    response = client.get("/api/v1/auth/feishu/callback", params={"code": "racing-code", "state": authorized.json()["data"]["state"]}, follow_redirects=False)
    assert response.status_code == 403 and response.json()["code"] == "feishu_account_unavailable"
    assert "set-cookie" not in response.headers
    with app.state.identity_store.sync_session() as db:
        assert db.get(User, target_id).status == UserStatus.DISABLED
        assert db.scalar(select(UserSession).where(UserSession.user_id == target_id)) is None
