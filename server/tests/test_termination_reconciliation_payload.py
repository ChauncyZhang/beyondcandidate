from uuid import uuid4

import pytest

from server.app.queue.payloads import DEFAULT_PAYLOAD_POLICIES, UnsafePayload


def test_reconciliation_outbox_accepts_only_opaque_original_request_ids():
    topic = "feishu.approval.onboarding.reconcile"
    payload = {name: str(uuid4()) for name in ("organization_id", "onboarding_id", "generation")}
    assert DEFAULT_PAYLOAD_POLICIES.validate_topic(topic, payload) == payload
    for extra in ({"email": "candidate@example.com"}, {"form": "personal data"}, {"approval_code": "template"}):
        with pytest.raises(UnsafePayload):
            DEFAULT_PAYLOAD_POLICIES.validate_topic(topic, {**payload, **extra})
    with pytest.raises(UnsafePayload):
        DEFAULT_PAYLOAD_POLICIES.validate_topic(topic, {**payload, "generation": "not-a-uuid"})
