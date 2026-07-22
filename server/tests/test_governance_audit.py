from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from server.app.governance.audit import AuditValidationError, append_audit
from server.app.governance.models import RetentionPolicy  # noqa: F401
from server.app.identity.models import AuditLog, Base
from server.app.recruiting.models import Candidate  # noqa: F401


def test_append_audit_persists_only_normalized_resource_and_safe_metadata() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    actor = SimpleNamespace(user_id=uuid4(), organization_id=uuid4())
    resource_id = uuid4()

    with Session(engine) as db:
        log = append_audit(
            db,
            actor=actor,
            category="governance",
            event_type="retention_policy.updated",
            outcome="success",
            trace_id="trace-governance-1",
            resource_type="retention_policy",
            resource_id=resource_id,
            ip_hash="a" * 64,
            metadata={"previous_version": 1, "new_version": 2, "shortened": False},
        )
        db.commit()

        stored = db.scalar(select(AuditLog))
        assert stored is log
        assert stored.organization_id == actor.organization_id
        assert stored.actor_user_id == actor.user_id
        assert stored.category == "governance"
        assert stored.resource_type == "retention_policy"
        assert stored.resource_id == resource_id
        assert stored.ip_hash == "a" * 64
        assert stored.metadata_json == {
            "previous_version": 1,
            "new_version": 2,
            "shortened": False,
        }


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"category": "billing"}, "category"),
        ({"outcome": "maybe"}, "outcome"),
        ({"event_type": "not stable"}, "event_type"),
        ({"metadata": {"candidate_email": "person@example.test"}}, "PII or secret"),
        ({"metadata": {"access_token": "opaque"}}, "PII or secret"),
        ({"metadata": {"unknown": "value"}}, "allowlisted"),
        ({"metadata": {"previous_version": {"nested": 1}}}, "scalar"),
        ({"trace_id": "x" * 65}, "trace_id"),
        ({"ip_hash": "not-a-sha256"}, "ip_hash"),
    ],
)
def test_append_audit_rejects_invalid_or_sensitive_event_data(overrides, message) -> None:
    values = {
        "actor": SimpleNamespace(user_id=uuid4(), organization_id=uuid4()),
        "category": "governance",
        "event_type": "retention_policy.updated",
        "outcome": "success",
        "trace_id": "trace-governance-2",
        "resource_type": "retention_policy",
        "resource_id": uuid4(),
        "metadata": {},
    }
    values.update(overrides)

    with pytest.raises(AuditValidationError, match=message):
        append_audit(Session(create_engine("sqlite://")), **values)


def test_append_audit_rejects_metadata_string_over_event_limit() -> None:
    actor = SimpleNamespace(user_id=uuid4(), organization_id=uuid4())
    with pytest.raises(AuditValidationError, match="safe_error_code"):
        append_audit(
            Session(create_engine("sqlite://")),
            actor=actor,
            category="governance",
            event_type="retention_policy.recalculation_failed",
            outcome="failure",
            trace_id="trace-governance-3",
            metadata={"safe_error_code": "x" * 65},
        )


def test_append_audit_accepts_screening_terminal_routed_safe_metadata() -> None:
    actor = SimpleNamespace(user_id=uuid4(), organization_id=uuid4())
    application_id = uuid4()
    item_id = uuid4()
    with Session(create_engine("sqlite://")) as db:
        log = append_audit(
            db,
            actor=actor,
            category="recruiting",
            event_type="screening.terminal_routed",
            outcome="success",
            trace_id="trace-screening-route",
            resource_type="application",
            resource_id=application_id,
            metadata={
                "application_id": str(application_id),
                "item_id": str(item_id),
                "from_stage": "new",
                "to_stage": "review",
                "ai_status": "failed",
                "recommendation": "AI评分不可用",
                "safe_error_code": "provider_unavailable",
            },
        )
        assert log.metadata_json["safe_error_code"] == "provider_unavailable"


def test_append_audit_accepts_terminal_screening_evidence_ids() -> None:
    actor = SimpleNamespace(user_id=uuid4(), organization_id=uuid4())
    application_id = uuid4()
    item_id = uuid4()
    evaluation_id = uuid4()
    invocation_id = uuid4()
    with Session(create_engine("sqlite://")) as db:
        log = append_audit(
            db,
            actor=actor,
            category="recruiting",
            event_type="screening.terminal_routed",
            outcome="success",
            trace_id="trace-screening-evidence",
            resource_type="application",
            resource_id=application_id,
            metadata={
                "application_id": str(application_id),
                "item_id": str(item_id),
                "evaluation_id": str(evaluation_id),
                "invocation_id": str(invocation_id),
                "from_stage": "new",
                "to_stage": "deferred",
                "ai_status": "succeeded",
                "recommendation": "暂缓",
                "score": 48,
            },
        )

    assert log.metadata_json["evaluation_id"] == str(evaluation_id)
    assert log.metadata_json["invocation_id"] == str(invocation_id)


def test_append_audit_rejects_unknown_screening_route_metadata_key() -> None:
    actor = SimpleNamespace(user_id=uuid4(), organization_id=uuid4())
    with pytest.raises(AuditValidationError, match="metadata key 'provider_body'"):
        append_audit(
            Session(create_engine("sqlite://")),
            actor=actor,
            category="recruiting",
            event_type="screening.terminal_routed",
            outcome="success",
            trace_id="trace-screening-route",
            metadata={"provider_body": "not allowed"},
        )
