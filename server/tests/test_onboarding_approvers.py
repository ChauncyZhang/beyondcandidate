import asyncio
import importlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from server.app.governance.deletion_models import DeletionRequest
from server.app.identity.models import Base, User, UserStatus
from server.app.integrations.feishu.models import (
    FeishuDepartmentMapping, FeishuIdentityBinding, FeishuOnboardingConfig, FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import ApprovalNode, FakeFeishuProvider, FeishuProviderError
from server.app.integrations.feishu.service import FeishuSecretCipher
from server.app.integrations.feishu.worker import FeishuOnboardingOutboxHandler
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import FeishuNodeApprovers
from server.app.onboarding.security import OnboardingPiiCipher
from server.app.onboarding.service import (
    OnboardingNotReady, approval_nodes_snapshot, onboarding_projection, resolve_node_approvers,
    start_onboarding_submission,
)
from server.app.queue.models import OutboxEvent
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.tests.test_feishu_api import configure_feishu, feishu_app, login, seed_user, write_headers
from server.tests.test_onboarding import KEY, SEMANTICS, _definition


URL = "/api/v1/settings/integrations/feishu/onboarding-approval"
DEFINITION = replace(_definition(), nodes=(ApprovalNode("approval_hr", "HR approval", True, False),))
NODES = approval_nodes_snapshot(DEFINITION)
NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)


def payload(user_ids=()):
    return {
        "approval_code": DEFINITION.approval_code, "field_mapping": SEMANTICS,
        "node_approvers": [{"node_id": "approval_hr", "user_ids": [str(item) for item in user_ids]}],
    }


def test_admin_draft_options_members_and_config_validation_lifecycle(feishu_app):
    app, client = feishu_app
    organization_id, csrf = configure_feishu(feishu_app)
    unbound_id = seed_user(app, email="unbound@example.test")
    disabled_id = seed_user(app, email="disabled@example.test", status=UserStatus.DISABLED)
    app.state.feishu_provider.approval_definition = DEFINITION
    with app.state.identity_store.sync_session() as db:
        admin_id = db.scalar(select(FeishuIdentityBinding.user_id).where(
            FeishuIdentityBinding.organization_id == organization_id,
        ))
    response = client.get(URL + "/approver-options", params={"approval_code": DEFINITION.approval_code})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["nodes"] == NODES
    members = {item["user_id"]: item for item in data["members"]}
    assert members[str(admin_id)]["available"] is True
    assert members[str(unbound_id)]["available"] is False
    assert members[str(unbound_id)]["unavailable_reason"] == "feishu_approver_unbound"
    assert str(disabled_id) not in members
    assert "ou_admin" not in response.text and "open_id" not in response.text
    assert client.get(URL).json()["data"]["configured"] is False

    draft = client.put(URL, json=payload(), headers=write_headers(csrf))
    assert draft.status_code == 200
    assert draft.json()["data"]["approval_nodes"] is None
    invalid = client.post(URL + "/validate", headers={**write_headers(csrf), "If-Match": draft.headers["ETag"]})
    assert invalid.status_code == 409 and "feishu_approver_required" in invalid.text
    current = client.get(URL)
    saved = client.put(URL, json=payload([admin_id]), headers={**write_headers(csrf), "If-Match": current.headers["ETag"]})
    assert saved.status_code == 200
    validated = client.post(URL + "/validate", headers={**write_headers(csrf), "If-Match": saved.headers["ETag"]})
    assert validated.status_code == 200
    assert validated.json()["data"]["approval_nodes"] == NODES
    enabled = client.put(URL, json={**payload([admin_id]), "enabled": True}, headers={**write_headers(csrf), "If-Match": validated.headers["ETag"]})
    assert enabled.status_code == 200 and enabled.json()["data"]["enabled"] is True
    stale = client.put(URL, json=payload(), headers={**write_headers(csrf), "If-Match": validated.headers["ETag"]})
    assert stale.status_code == 409
    cleared = client.put(URL, json=payload(), headers={**write_headers(csrf), "If-Match": enabled.headers["ETag"]})
    assert cleared.status_code == 200
    assert cleared.json()["data"]["validation_status"] == "unvalidated"
    assert cleared.json()["data"]["enabled"] is False
    assert cleared.json()["data"]["approval_nodes"] is None


def test_options_and_config_are_admin_only_and_reject_unbound_selection(feishu_app):
    app, client = feishu_app
    _, csrf = configure_feishu(feishu_app)
    user_id = seed_user(app, email="recruiter@example.test", role="recruiter")
    denied = client.put(URL, json=payload([user_id]), headers=write_headers(csrf))
    assert denied.status_code == 422 and "feishu_approver_unavailable" in denied.text
    assert client.get(URL).json()["data"]["configured"] is False
    csrf = login(client, "recruiter@example.test")
    assert client.get(URL + "/approver-options", params={"approval_code": "draft"}).status_code == 403
    assert client.get(URL).status_code == 403
    assert client.put(URL, json=payload(), headers=write_headers(csrf)).status_code == 403
    assert client.post(URL + "/validate", headers=write_headers(csrf)).status_code == 403


@pytest.mark.parametrize("value", [
    {"node_id": " ", "user_ids": []},
    {"node_id": "n", "user_ids": ["ou_unverified"]},
    {"node_id": "n", "user_ids": [], "open_ids": ["ou_unverified"]},
])
def test_selection_schema_rejects_unverified_identity_payloads(value):
    with pytest.raises(ValueError):
        FeishuNodeApprovers.model_validate(value)


@pytest.fixture
def context():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    pii = OnboardingPiiCipher(KEY)
    secret = FeishuSecretCipher(KEY)
    org_id, actor_id, record_id, department_id = uuid4(), uuid4(), uuid4(), uuid4()
    with sessions.begin() as db:
        db.add_all([
            User(id=actor_id, organization_id=org_id, email="a@example.test", normalized_email="a@example.test",
                 display_name="Approver", password_hash="unused", status=UserStatus.ACTIVE),
            FeishuIdentityBinding(organization_id=org_id, user_id=actor_id, open_id="ou_fixed"),
            FeishuOrganizationConfig(organization_id=org_id, app_id="cli", encrypted_app_secret=secret.encrypt("secret"),
                redirect_uri="https://hr.example.test/callback", enabled=True, created_by=actor_id, updated_by=actor_id),
            FeishuOnboardingConfig(organization_id=org_id, approval_code=DEFINITION.approval_code, field_mapping=SEMANTICS,
                node_approvers=payload([actor_id])["node_approvers"], approval_nodes=NODES,
                enabled=True, validation_status="valid", definition_fingerprint=DEFINITION.fingerprint,
                created_by=actor_id, updated_by=actor_id),
            FeishuDepartmentMapping(organization_id=org_id, department_id=department_id, feishu_department_id="od_department"),
            OnboardingRecord(id=record_id, organization_id=org_id, offer_response_id=uuid4(), offer_id=uuid4(),
                application_id=uuid4(), candidate_id=uuid4(), job_id=uuid4(), department_id=department_id,
                job_title="Role", department_name="Department", expected_start_date=NOW.date(),
                pii_ciphertext=pii.encrypt({"name": "Candidate", "gender": "female", "phone": "+8613800138000",
                    "email": "c@example.test", "home_address": "Address"}), status="ready"),
        ])
    provider = FakeFeishuProvider()
    provider.approval_definition = DEFINITION
    value = SimpleNamespace(sessions=sessions, pii=pii, secret=secret, org_id=org_id, actor_id=actor_id,
                            record_id=record_id, provider=provider)
    yield value
    engine.dispose()


def start(ctx):
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        start_onboarding_submission(db, record, expected_version=record.version, generation=uuid4(),
            actor_user_id=ctx.actor_id, cipher=ctx.pii, now=NOW, trace_id="approver-test")
        return db.scalar(select(OutboxEvent).order_by(OutboxEvent.created_at.desc()))


def worker(ctx, event):
    handler = FeishuOnboardingOutboxHandler(ctx.sessions, ctx.provider, ctx.secret, ctx.pii)
    return asyncio.run(handler(event, event.id))


@pytest.mark.parametrize("case,code", [
    ("required", "feishu_approver_required"), ("unknown", "feishu_approver_node_invalid"),
    ("multiple", "feishu_approver_multiple_not_allowed"), ("unbound", "feishu_approver_unavailable"),
    ("inactive", "feishu_approver_unavailable"), ("foreign", "feishu_approver_unavailable"),
    ("legacy", "feishu_onboarding_validation_required"),
])
def test_submission_and_worker_fail_closed(context, case, code):
    ctx = context
    event = start(ctx)
    with ctx.sessions.begin() as db:
        config = db.scalar(select(FeishuOnboardingConfig))
        if case == "required":
            config.node_approvers = []
        elif case == "unknown":
            config.node_approvers = [{"node_id": "stale", "user_ids": [str(ctx.actor_id)]}]
        elif case == "multiple":
            config.node_approvers = payload([ctx.actor_id, uuid4()])["node_approvers"]
        elif case == "unbound":
            unbound_id = uuid4()
            db.add(User(id=unbound_id, organization_id=ctx.org_id, email="unbound@example.test",
                normalized_email="unbound@example.test", display_name="Unbound", password_hash="unused"))
            config.node_approvers = payload([unbound_id])["node_approvers"]
        elif case == "inactive":
            db.get(User, ctx.actor_id).status = UserStatus.DISABLED
        elif case == "foreign":
            db.get(User, ctx.actor_id).organization_id = uuid4()
        else:
            config.approval_nodes = None
        record = db.get(OnboardingRecord, ctx.record_id)
        record.status = "failed"
    with pytest.raises(OnboardingNotReady, match=code):
        start(ctx)
    with pytest.raises(PermanentJobError, match=code):
        worker(ctx, event)
    assert ctx.provider.approval_instances == []


def test_resolves_verified_members_and_worker_sends_fixed_approvers(context):
    ctx = context
    event = start(ctx)
    assert set(event.payload) == {"organization_id", "onboarding_id", "generation"}
    worker(ctx, event)
    assert ctx.provider.approval_instances[0][0].node_approvers == ({"key": "approval_hr", "value": ["ou_fixed"]},)
    with ctx.sessions() as db:
        stored = db.get(OutboxEvent, event.id)
        assert len(stored.payload["submission_fingerprint"]) == 64
        assert "ou_fixed" not in json.dumps(stored.payload)
        assert db.get(OnboardingRecord, ctx.record_id).status == "submitted"


def test_worker_rechecks_nodes_even_if_provider_fingerprint_is_unchanged(context):
    ctx = context
    event = start(ctx)
    ctx.provider.approval_definition = replace(DEFINITION, nodes=(ApprovalNode("new_required", "Changed", True, False),))
    with pytest.raises(PermanentJobError, match="feishu_approval_definition_changed"):
        worker(ctx, event)
    assert ctx.provider.approval_instances == []


def test_preflight_only_failure_can_retry_after_configuration_is_fixed(context):
    ctx = context
    event = start(ctx)
    with ctx.sessions.begin() as db:
        db.get(OutboxEvent, event.id).attempts = 1
        db.scalar(select(FeishuOnboardingConfig)).node_approvers = []
    with pytest.raises(PermanentJobError, match="feishu_approver_required"):
        worker(ctx, event)
    with ctx.sessions.begin() as db:
        prior = db.get(OutboxEvent, event.id)
        assert prior.payload["submission_state"] == "preflight"
        assert "submission_fingerprint" not in prior.payload
        db.scalar(select(FeishuOnboardingConfig)).node_approvers = payload([ctx.actor_id])["node_approvers"]
    retry = start(ctx)
    assert retry.payload["generation"] == event.payload["generation"]
    worker(ctx, retry)
    assert len(ctx.provider.approval_instances) == 1


@pytest.mark.parametrize("change", ["binding", "selection"])
def test_ambiguous_send_retry_cannot_change_approver_identity(context, change):
    ctx = context
    event = start(ctx)
    calls = []

    def ambiguous(credentials, request, *, idempotency_key):
        calls.append(request)
        raise FeishuProviderError("feishu_timeout", retryable=True)

    ctx.provider.create_approval_instance = ambiguous
    with pytest.raises(RetryableJobError):
        worker(ctx, event)
    with ctx.sessions.begin() as db:
        if change == "binding":
            db.scalar(select(FeishuIdentityBinding)).open_id = "ou_rebound"
        else:
            other_id = uuid4()
            db.add(User(id=other_id, organization_id=ctx.org_id, email="other@example.test", normalized_email="other@example.test",
                display_name="Other", password_hash="unused", status=UserStatus.ACTIVE))
            db.add(FeishuIdentityBinding(organization_id=ctx.org_id, user_id=other_id, open_id="ou_other"))
            db.scalar(select(FeishuOnboardingConfig)).node_approvers = payload([other_id])["node_approvers"]
        db.get(OnboardingRecord, ctx.record_id).status = "failed"
    retried_event = start(ctx)
    assert retried_event.payload["generation"] == event.payload["generation"]
    with pytest.raises(PermanentJobError, match="feishu_approval_reconciliation_required"):
        worker(ctx, retried_event)
    assert len(calls) == 1


def test_ambiguous_send_can_retry_unchanged_payload_with_original_uuid(context):
    ctx = context
    event = start(ctx)
    original_create = ctx.provider.create_approval_instance
    calls = []

    def lost_response(credentials, request, *, idempotency_key):
        calls.append(idempotency_key)
        result = original_create(credentials, request, idempotency_key=idempotency_key)
        if len(calls) == 1:
            raise FeishuProviderError("feishu_timeout", retryable=True)
        return result

    ctx.provider.create_approval_instance = lost_response
    with pytest.raises(RetryableJobError):
        worker(ctx, event)
    worker(ctx, event)
    assert calls == [event.payload["generation"], event.payload["generation"]]
    with ctx.sessions() as db:
        assert db.get(OnboardingRecord, ctx.record_id).status == "submitted"


def test_legacy_attempt_without_payload_identity_requires_reconciliation(context):
    ctx = context
    event = start(ctx)
    with ctx.sessions.begin() as db:
        db.get(OutboxEvent, event.id).attempts = 2
    with pytest.raises(PermanentJobError, match="feishu_approval_reconciliation_required"):
        worker(ctx, event)
    assert ctx.provider.approval_instances == []


def test_worker_rechecks_membership_after_remote_template_read(context):
    ctx = context
    event = start(ctx)

    def deactivate(credentials, approval_code):
        with ctx.sessions.begin() as db:
            db.get(User, ctx.actor_id).status = UserStatus.DISABLED
        return DEFINITION

    ctx.provider.get_approval_definition = deactivate
    with pytest.raises(PermanentJobError, match="feishu_approver_unavailable"):
        worker(ctx, event)
    assert ctx.provider.approval_instances == []


@pytest.mark.parametrize("status", ["requested", "approved", "executing", "failed"])
def test_pending_deletion_blocks_projection_start_and_worker(context, status):
    ctx = context
    event = start(ctx)
    with ctx.sessions.begin() as db:
        record = db.get(OnboardingRecord, ctx.record_id)
        record.status = "failed"
        db.add(DeletionRequest(organization_id=ctx.org_id, candidate_id=record.candidate_id,
            status=status, requested_by=ctx.actor_id, reason_code="candidate_request",
            impact_manifest={}, manifest_hash="f" * 64, policy_version=1, candidate_version=1))
        db.flush()
        projection = onboarding_projection(record, cipher=ctx.pii, now=NOW)
        assert projection["blocking_reason"] == "candidate_deletion_pending"
        assert projection["can_submit"] is False
        assert projection["allowed_actions"] == {"update": False, "start_onboarding": False}
    with pytest.raises(OnboardingNotReady, match="candidate_deletion_pending"):
        start(ctx)
    with pytest.raises(PermanentJobError, match="candidate_deletion_pending"):
        worker(ctx, event)
    assert ctx.provider.approval_instances == []


def test_migration_preserves_legacy_unknown_metadata_and_is_reversible():
    migration = importlib.import_module("server.migrations.versions.0041_onboarding_approvers")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE feishu_onboarding_configs (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO feishu_onboarding_configs (id) VALUES (1)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            row = connection.execute(text("SELECT node_approvers, approval_nodes FROM feishu_onboarding_configs")).one()
            assert json.loads(row[0]) == [] and row[1] is None
            migration.downgrade()
        assert [column["name"] for column in inspect(connection).get_columns("feishu_onboarding_configs")] == ["id"]
    engine.dispose()
