from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from server.app.identity.models import User
from server.app.integrations.feishu.models import (
    FeishuIdentityBinding,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.notifications import (
    FEISHU_NOTIFICATION_EVENTS,
    schedule_feishu_notification,
)
from server.app.integrations.feishu.provider import (
    FakeFeishuProvider,
    FeishuCredentials,
    FeishuProviderError,
    HttpFeishuProvider,
)
from server.app.integrations.feishu.worker import (
    FeishuNotificationOutboxHandler,
    _notification_card,
)
from server.app.interviews.models import Interview, InterviewParticipant
from server.app.queue.models import OutboxEvent
from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, UnsafePayload
from server.app.queue.service import RetryableJobError
from server.tests.test_interview_api import make_app, seed_application


def _enable_feishu(app, seed) -> None:
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, seed["admin_id"])
        db.add(
            FeishuOrganizationConfig(
                organization_id=admin.organization_id,
                app_id="cli_test",
                encrypted_app_secret=app.state.feishu_secret_cipher.encrypt("app-secret"),
                redirect_uri="https://hr.example.test/api/v1/auth/feishu/callback",
                calendar_id="primary",
                enabled=True,
                created_by=admin.id,
                updated_by=admin.id,
            )
        )
        db.commit()


def _feedback_event(app, seed, *, bind_recipient: bool = True):
    _enable_feishu(app, seed)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, seed["admin_id"])
        interview = Interview(
            organization_id=admin.organization_id,
            application_id=seed["application_id"],
            round_name="技术一面",
            method="video",
            timezone="Asia/Shanghai",
            starts_at=datetime(2030, 7, 20, 8, tzinfo=timezone.utc),
            ends_at=datetime(2030, 7, 20, 9, tzinfo=timezone.utc),
            status="pending_feedback",
            owner_id=admin.id,
            created_by=admin.id,
        )
        db.add(interview)
        db.flush()
        db.add_all(
            [
                InterviewParticipant(
                    organization_id=admin.organization_id,
                    interview_id=interview.id,
                    user_id=seed["interviewer_id"],
                    required_feedback=True,
                ),
                InterviewParticipant(
                    organization_id=admin.organization_id,
                    interview_id=interview.id,
                    user_id=seed["other_interviewer_id"],
                    required_feedback=False,
                ),
            ]
        )
        if bind_recipient:
            db.add(
                FeishuIdentityBinding(
                    organization_id=admin.organization_id,
                    user_id=seed["interviewer_id"],
                    union_id="on_interviewer",
                    open_id="ou_interviewer",
                    tenant_key="tenant",
                )
            )
        events = schedule_feishu_notification(
            db,
            organization_id=admin.organization_id,
            recipient_user_ids=[
                seed["interviewer_id"],
                seed["interviewer_id"],
                seed["other_interviewer_id"],
                seed["admin_id"],
            ],
            event_type="feedback_requested",
            interview_id=interview.id,
            actor_user_id=seed["admin_id"],
        )
        db.commit()
        event_id = events[0].id
        interview_id = interview.id
    with app.state.identity_store.sync_session() as db:
        event = db.get(OutboxEvent, event_id)
        db.expunge(event)
    return event, interview_id


def test_schedule_feedback_notification_is_private_deduplicated_and_required_only(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, interview_id = _feedback_event(app, seed)

    assert event.topic == "feishu.notification.send"
    assert event.aggregate_type == "user"
    assert event.aggregate_id == seed["interviewer_id"]
    assert event.payload == {
        "organization_id": str(event.organization_id),
        "recipient_user_id": str(seed["interviewer_id"]),
        "event_type": "feedback_requested",
        "interview_id": str(interview_id),
    }
    with app.state.identity_store.sync_session() as db:
        assert list(
            db.scalars(
                select(OutboxEvent.id).where(
                    OutboxEvent.topic == "feishu.notification.send"
                )
            )
        ) == [event.id]


def test_disabled_config_does_not_schedule_notification_and_payload_rejects_pii(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, seed["admin_id"])
        assert schedule_feishu_notification(
            db,
            organization_id=admin.organization_id,
            recipient_user_ids=[seed["interviewer_id"]],
            event_type="review_requested",
            application_id=seed["application_id"],
        ) == []
        with pytest.raises(ValueError, match="unsupported"):
            schedule_feishu_notification(
                db,
                organization_id=admin.organization_id,
                recipient_user_ids=[seed["interviewer_id"]],
                event_type="arbitrary_event",
                application_id=seed["application_id"],
            )

    with pytest.raises(UnsafePayload):
        DEFAULT_PAYLOAD_POLICIES.validate_topic(
            "feishu.notification.send",
            {
                "organization_id": str(uuid4()),
                "recipient_user_id": str(uuid4()),
                "event_type": "review_requested",
                "application_id": str(uuid4()),
                "candidate_name": "不应进入 Outbox",
            },
        )


def test_feedback_handler_sends_open_id_card_with_origin_and_outbox_idempotency(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, interview_id = _feedback_event(app, seed)
    provider = FakeFeishuProvider()
    handler = FeishuNotificationOutboxHandler(
        app.state.identity_store.sync_session,
        provider,
        app.state.feishu_secret_cipher,
    )

    asyncio.run(handler(event, event.id))
    asyncio.run(handler(event, event.id))

    assert len(provider.cards) == 1
    assert provider.cards[0][0] == "ou_interviewer"
    assert provider.cards[0][2] == str(event.id)
    card = provider.cards[0][1]
    rendered = json.dumps(card, ensure_ascii=False)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "面试评价待提交"
    assert "李嘉明" in rendered and "AI Engineer" in rendered and "技术一面" in rendered
    button = card["body"]["elements"][-1]
    assert button["text"]["content"] == "提交评价"
    assert button["behaviors"] == [{
        "type": "open_url",
        "default_url": f"https://hr.example.test/interviews/{interview_id}/feedback",
    }]
    assert "Python RAG Agent" not in rendered
    assert "@example.test" not in rendered


def test_unbound_recipient_is_a_successful_noop(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, _ = _feedback_event(app, seed, bind_recipient=False)
    provider = FakeFeishuProvider()

    asyncio.run(
        FeishuNotificationOutboxHandler(
            app.state.identity_store.sync_session,
            provider,
            app.state.feishu_secret_cipher,
        )(event, event.id)
    )

    assert provider.cards == []


def test_provider_failure_preserves_retryability(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, _ = _feedback_event(app, seed)
    provider = FakeFeishuProvider()
    provider.failure = FeishuProviderError("feishu_unavailable", retryable=True)

    with pytest.raises(RetryableJobError, match="feishu_unavailable"):
        asyncio.run(
            FeishuNotificationOutboxHandler(
                app.state.identity_store.sync_session,
                provider,
                app.state.feishu_secret_cipher,
            )(event, event.id)
        )


def test_all_controlled_events_build_chinese_platform_cards() -> None:
    application = SimpleNamespace(id=uuid4())
    candidate = SimpleNamespace(id=uuid4(), display_name="候选人甲")
    job = SimpleNamespace(id=uuid4(), title="后端工程师")
    interview = SimpleNamespace(
        id=uuid4(),
        round_name="技术面",
        starts_at=datetime(2030, 7, 20, 8, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
    )

    assert len(FEISHU_NOTIFICATION_EVENTS) == 13
    for event_type in FEISHU_NOTIFICATION_EVENTS:
        card = _notification_card(
            event_type,
            origin="https://hr.example.test",
            application=application,
            candidate=candidate,
            job=job,
            interview=interview,
        )
        rendered = json.dumps(card, ensure_ascii=False)
        assert card["schema"] == "2.0"
        assert card["config"]["width_mode"] == "default"
        assert len(card["body"]["elements"]) == 3
        button = card["body"]["elements"][-1]
        assert button["type"] == "primary_filled" and button["width"] == "fill"
        assert button["behaviors"][0]["type"] == "open_url"
        if event_type != "interview_assignment_removed":
            assert "候选人甲" in rendered
            assert "后端工程师" in rendered
        assert "https://hr.example.test/" in rendered
        assert "电话" not in rendered and "邮箱" not in rendered and "简历正文" not in rendered
    feedback = _notification_card(
        "feedback_requested",
        origin="https://hr.example.test",
        application=application,
        candidate=candidate,
        job=job,
        interview=interview,
    )
    assert feedback["body"]["elements"][-1]["behaviors"][0]["default_url"].endswith(
        f"/interviews/{interview.id}/feedback"
    )


def test_card_escapes_dynamic_lark_markdown() -> None:
    application = SimpleNamespace(id=uuid4())
    candidate = SimpleNamespace(id=uuid4(), display_name="陈*曦_[测试]")
    job = SimpleNamespace(id=uuid4(), title="AI <工程师>")

    card = _notification_card(
        "review_requested",
        origin="https://hr.example.test",
        application=application,
        candidate=candidate,
        job=job,
        interview=None,
    )

    rendered = json.dumps(card, ensure_ascii=False)
    assert "陈&#42;曦&#95;&#91;测试&#93;" in rendered
    assert "AI &#60;工程师&#62;" in rendered
    assert "陈*曦_[测试]" not in rendered


def test_actor_is_kept_when_they_are_also_the_next_responsible_person(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    _enable_feishu(app, seed)
    with app.state.identity_store.sync_session() as db:
        admin = db.get(User, seed["admin_id"])
        kept = schedule_feishu_notification(
            db,
            organization_id=admin.organization_id,
            recipient_user_ids=[admin.id],
            event_type="interview_arrangement_requested",
            application_id=seed["application_id"],
            actor_user_id=admin.id,
        )
        excluded = schedule_feishu_notification(
            db,
            organization_id=admin.organization_id,
            recipient_user_ids=[admin.id],
            event_type="interview_scheduled",
            interview_id=uuid4(),
            actor_user_id=admin.id,
            exclude_actor=True,
        )
        assert len(kept) == 1
        assert excluded == []


def test_stale_feedback_notification_is_skipped_after_state_or_assignment_changes(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    event, interview_id = _feedback_event(app, seed)
    provider = FakeFeishuProvider()
    handler = FeishuNotificationOutboxHandler(
        app.state.identity_store.sync_session,
        provider,
        app.state.feishu_secret_cipher,
    )
    with app.state.identity_store.sync_session() as db:
        db.get(Interview, interview_id).status = "feedback_completed"
        db.commit()
    asyncio.run(handler(event, event.id))
    assert provider.cards == []

    with app.state.identity_store.sync_session() as db:
        db.get(Interview, interview_id).status = "pending_feedback"
        participant = db.scalar(
            select(InterviewParticipant).where(
                InterviewParticipant.interview_id == interview_id,
                InterviewParticipant.user_id == seed["interviewer_id"],
            )
        )
        db.delete(participant)
        db.commit()
    asyncio.run(handler(event, event.id))
    assert provider.cards == []


def test_http_provider_uses_message_api_open_id_and_uuid() -> None:
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path.endswith("/im/v1/messages"):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(transport)))
    provider.send_message(
        FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
        "ou_recipient",
        "请处理招聘任务。",
        idempotency_key="00000000-0000-4000-8000-000000000001",
    )

    assert len(requests) == 1
    assert requests[0].url.params["receive_id_type"] == "open_id"
    body = json.loads(requests[0].content)
    assert body == {
        "receive_id": "ou_recipient",
        "msg_type": "text",
        "content": json.dumps({"text": "请处理招聘任务。"}, ensure_ascii=False),
        "uuid": "00000000-0000-4000-8000-000000000001",
    }


def test_http_provider_sends_interactive_card_as_json_content() -> None:
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path.endswith("/im/v1/messages"):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_card"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "待处理"}},
        "body": {"elements": []},
    }
    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(transport)))
    provider.send_card(
        FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
        "ou_recipient",
        card,
        idempotency_key="00000000-0000-4000-8000-000000000004",
    )

    body = json.loads(requests[0].content)
    assert body == {
        "receive_id": "ou_recipient",
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        "uuid": "00000000-0000-4000-8000-000000000004",
    }


def test_http_provider_exposes_network_failure_as_retryable() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection unavailable", request=request)

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(transport)))
    with pytest.raises(FeishuProviderError) as raised:
        provider.send_message(
            FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
            "ou_recipient",
            "请处理招聘任务。",
            idempotency_key="00000000-0000-4000-8000-000000000002",
        )
    assert raised.value.retryable is True
    assert raised.value.safe_code == "feishu_unavailable"


@pytest.mark.parametrize(("status_code", "retryable"), [(429, True), (503, True), (403, False)])
def test_http_provider_classifies_http_failures_without_exposing_response(
    status_code: int, retryable: bool
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(status_code, text="provider private response")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(transport)))
    with pytest.raises(FeishuProviderError) as raised:
        provider.send_message(
            FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
            "ou_recipient",
            "请处理招聘任务。",
            idempotency_key="00000000-0000-4000-8000-000000000003",
        )
    assert raised.value.retryable is retryable
    assert raised.value.safe_code == "feishu_request_failed"
