import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from server.app.identity.models import Base
from server.app.integrations.feishu.models import (
    FeishuDepartmentMapping,
    FeishuIdentityBinding,
    FeishuOnboardingConfig,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import (
    ApprovalControl,
    ApprovalDefinition,
    ApprovalInstance,
    ApprovalInstanceRequest,
    FakeFeishuProvider,
    FeishuCredentials,
    FeishuProviderError,
    HttpFeishuProvider,
)
from server.app.integrations.feishu.service import FeishuSecretCipher
from server.app.integrations.feishu.worker import FeishuOnboardingOutboxHandler, _approval_form_value
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import FeishuOnboardingConfigWrite, OnboardingData, OnboardingUpdate, OnboardingUpdateCommand
from server.app.onboarding.security import OnboardingPiiCipher
from server.app.onboarding.service import (
    OnboardingNotReady,
    normalize_stored_field_mapping,
    onboarding_projection,
    start_onboarding_submission,
    update_onboarding,
    validate_definition,
)
from server.app.queue.models import OutboxEvent


KEY = b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
SEMANTICS = {
    "candidate_name": {"control_id": "name", "control_type": "input"},
    "gender": {"control_id": "gender", "control_type": "radio", "options": {"male": "M", "female": "F"}},
    "department": {"control_id": "department", "control_type": "department"},
    "job_title": {"control_id": "job", "control_type": "input"},
    "phone": {"control_id": "phone", "control_type": "telephone"},
    "email": {"control_id": "email", "control_type": "input"},
    "home_address": {"control_id": "address", "control_type": "textarea"},
}


def _definition() -> ApprovalDefinition:
    controls = tuple(
        ApprovalControl(
            value["control_id"],
            value["control_id"],
            value["control_type"],
            tuple(value.get("options", {}).values()),
        )
        for value in SEMANTICS.values()
    )
    return ApprovalDefinition("approval_onboarding", "Onboarding", "ACTIVE", controls, "f" * 64)


def test_onboarding_data_is_normalized_and_encrypted_with_a_purpose_separated_key():
    data = OnboardingData(
        gender="female",
        phone="+86 138-0013-8000",
        email="Candidate@Example.com",
        home_address="  Shenzhen  ",
    )
    cipher = OnboardingPiiCipher(KEY)
    ciphertext = cipher.encrypt({"name": "Candidate", **data.model_dump()})

    assert data.phone == "+8613800138000"
    assert data.email == "candidate@example.com"
    assert data.home_address == "Shenzhen"
    assert b"Candidate" not in ciphertext and b"candidate@example.com" not in ciphertext
    assert cipher.decrypt(ciphertext)["gender"] == "female"

    with pytest.raises(ValueError, match="gender must be male or female"):
        OnboardingData(
            gender="other",
            phone="13800138000",
            email="candidate@example.com",
            home_address="Shenzhen",
        )


def test_hr_onboarding_update_accepts_nested_partial_data_and_expected_date():
    command = OnboardingUpdateCommand.model_validate({
        "onboarding_data": {"name": "  New Name  ", "phone": "+86 138-0013-8000"},
        "expected_start_date": "2026-09-01",
    })

    assert command.onboarding_data.name == "New Name"
    assert command.onboarding_data.phone == "+8613800138000"
    assert command.expected_start_date == date(2026, 9, 1)


def test_hr_onboarding_update_rejects_an_empty_command():
    with pytest.raises(ValueError, match="onboarding_data or expected_start_date"):
        OnboardingUpdateCommand.model_validate({})


def test_definition_validation_requires_exact_ids_and_rejects_corehr_onboarding_control():
    definition = _definition()
    assert validate_definition(SEMANTICS, definition) is None
    unsupported = ApprovalDefinition(
        definition.approval_code,
        definition.name,
        definition.status,
        definition.controls + (ApprovalControl("corehr", None, "apaascorehrOnboardingGroup"),),
        definition.fingerprint,
    )
    assert validate_definition(SEMANTICS, unsupported) == "feishu_approval_control_unsupported"
    changed = {**SEMANTICS, "email": {"control_id": "missing", "control_type": "input"}}
    assert validate_definition(changed, definition) == "feishu_approval_control_missing"
    invalid_option = {**SEMANTICS, "gender": {**SEMANTICS["gender"], "options": {"male": "invalid", "female": "F"}}}
    assert validate_definition(invalid_option, definition) == "feishu_approval_option_invalid"
    duplicate_option = {**SEMANTICS, "gender": {**SEMANTICS["gender"], "options": {"male": "M", "female": "M"}}}
    assert validate_definition(duplicate_option, definition) == "feishu_approval_option_duplicate"


def test_definition_validation_accepts_native_template_without_start_date_mapping():
    mapping = {
        **SEMANTICS,
        "gender": {"control_id": "gender", "control_type": "radioV2", "options": {"male": "male-option", "female": "female-option"}},
        "phone": {"control_id": "phone", "control_type": "input"},
        "home_address": {"control_id": "address", "control_type": "input"},
    }
    definition = ApprovalDefinition(
        "approval_onboarding",
        "Onboarding",
        "ACTIVE",
        (
            ApprovalControl("name", None, "input"),
            ApprovalControl("gender", None, "radioV2", ("female-option", "male-option")),
            ApprovalControl("department", None, "department"),
            ApprovalControl("job", None, "input"),
            ApprovalControl("phone", None, "input"),
            ApprovalControl("email", None, "input"),
            ApprovalControl("address", None, "input"),
            ApprovalControl("remarks", None, "textarea"),
        ),
        "f" * 64,
    )

    assert validate_definition(mapping, definition) is None
    assert "expected_start_date" not in mapping


def test_legacy_eight_field_mapping_is_normalized_without_changing_the_saved_value():
    legacy = {
        **SEMANTICS,
        "gender": {**SEMANTICS["gender"], "options": {"male": "M", "female": "F", "other": "O"}},
        "expected_start_date": {"control_id": "start", "control_type": "date"},
    }

    normalized = normalize_stored_field_mapping(legacy)

    assert validate_definition(normalized, _definition()) is None
    assert set(normalized) == set(SEMANTICS)
    assert normalized["gender"]["options"] == {"male": "M", "female": "F"}
    assert "expected_start_date" in legacy and "other" in legacy["gender"]["options"]


def test_historical_other_gender_is_incomplete_editable_and_cannot_be_submitted():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = OnboardingPiiCipher(KEY)
    record = OnboardingRecord(
        organization_id=uuid4(),
        offer_response_id=uuid4(),
        offer_id=uuid4(),
        application_id=uuid4(),
        candidate_id=uuid4(),
        job_id=uuid4(),
        department_id=uuid4(),
        job_title="Role",
        department_name="Department",
        expected_start_date=date(2026, 8, 18),
        pii_ciphertext=cipher.encrypt({
            "name": "Candidate",
            "gender": "other",
            "phone": "+8613800138000",
            "email": "candidate@example.test",
            "home_address": "Shenzhen",
        }),
        status="ready",
    )
    with Session(engine) as db:
        db.add(record)
        db.flush()
        projection = onboarding_projection(
            record,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        )
        assert projection["complete"] is False
        assert projection["can_submit"] is False
        assert projection["blocking_reason"] == "onboarding_gender_invalid"
        assert projection["allowed_actions"]["update"] is True
        with pytest.raises(OnboardingNotReady, match="onboarding_gender_invalid"):
            start_onboarding_submission(
                db,
                record,
                expected_version=1,
                generation=uuid4(),
                actor_user_id=uuid4(),
                cipher=cipher,
                now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
                trace_id="historical-other",
            )
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == record.id)) is None


def test_gender_form_value_never_falls_back_to_an_unmapped_value():
    with pytest.raises(FeishuProviderError, match="onboarding_gender_invalid"):
        _approval_form_value(
            "gender",
            SEMANTICS["gender"],
            "other",
            feishu_department_id="od_department",
        )


def test_reconciliation_failure_cannot_be_edited_or_lose_its_generation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = OnboardingPiiCipher(KEY)
    generation = uuid4()
    record = OnboardingRecord(
        organization_id=uuid4(),
        offer_response_id=uuid4(),
        offer_id=uuid4(),
        application_id=uuid4(),
        candidate_id=uuid4(),
        job_id=uuid4(),
        department_id=uuid4(),
        job_title="Role",
        department_name="Department",
        expected_start_date=date(2026, 8, 18),
        pii_ciphertext=cipher.encrypt({
            "name": "Candidate",
            "gender": "female",
            "phone": "+8613800138000",
            "email": "candidate@example.test",
            "home_address": "Shenzhen",
        }),
        status="failed",
        generation=generation,
        started_by=uuid4(),
        started_at=datetime(2026, 8, 18, 0, tzinfo=timezone.utc),
        failed_at=datetime(2026, 8, 18, 0, 1, tzinfo=timezone.utc),
        safe_error_code="feishu_approval_reconciliation_required",
    )
    with Session(engine) as db:
        db.add(record)
        db.flush()
        projection = onboarding_projection(
            record,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        )
        assert projection["allowed_actions"]["update"] is False
        with pytest.raises(OnboardingNotReady, match="onboarding_update_not_allowed"):
            update_onboarding(
                db,
                record,
                OnboardingUpdateCommand(onboarding_data=OnboardingUpdate(gender="male")),
                cipher=cipher,
                expected_version=1,
                actor_user_id=uuid4(),
                trace_id="reconciliation-edit",
            )
        assert record.generation == generation
        assert record.status == "failed"

        record.pii_ciphertext = cipher.encrypt({
            "name": "Candidate",
            "gender": "other",
            "phone": "+8613800138000",
            "email": "candidate@example.test",
            "home_address": "Shenzhen",
        })
        protected_projection = onboarding_projection(
            record,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        )
        assert protected_projection["blocking_reason"] == "feishu_approval_reconciliation_required"
        assert protected_projection["allowed_actions"]["update"] is False


def test_historical_other_can_be_corrected_after_a_retryable_failure_without_changing_generation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cipher = OnboardingPiiCipher(KEY)
    generation = uuid4()
    actor_id = uuid4()
    record = OnboardingRecord(
        organization_id=uuid4(),
        offer_response_id=uuid4(),
        offer_id=uuid4(),
        application_id=uuid4(),
        candidate_id=uuid4(),
        job_id=uuid4(),
        department_id=uuid4(),
        job_title="Role",
        department_name="Department",
        expected_start_date=date(2026, 8, 18),
        pii_ciphertext=cipher.encrypt({
            "name": "Candidate",
            "gender": "other",
            "phone": "+8613800138000",
            "email": "candidate@example.test",
            "home_address": "Shenzhen",
        }),
        status="failed",
        generation=generation,
        started_by=actor_id,
        started_at=datetime(2026, 8, 18, 0, tzinfo=timezone.utc),
        failed_at=datetime(2026, 8, 18, 0, 1, tzinfo=timezone.utc),
        safe_error_code="feishu_timeout",
    )
    with Session(engine) as db:
        db.add(record)
        db.add_all([
            FeishuOnboardingConfig(
                organization_id=record.organization_id,
                approval_code="approval",
                field_mapping=SEMANTICS,
                enabled=True,
                validation_status="valid",
                definition_fingerprint="f" * 64,
                created_by=actor_id,
                updated_by=actor_id,
            ),
            FeishuDepartmentMapping(
                organization_id=record.organization_id,
                department_id=record.department_id,
                feishu_department_id="od_department",
            ),
        ])
        db.flush()
        before = onboarding_projection(
            record,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        )
        assert before["allowed_actions"]["update"] is True

        with pytest.raises(OnboardingNotReady, match="onboarding_update_not_allowed"):
            update_onboarding(
                db,
                record,
                OnboardingUpdateCommand(onboarding_data=OnboardingUpdate(phone="13800138001")),
                cipher=cipher,
                expected_version=1,
                actor_user_id=actor_id,
                trace_id="reject-unrelated-failed-edit",
            )

        update_onboarding(
            db,
            record,
            OnboardingUpdateCommand(onboarding_data=OnboardingUpdate(gender="male")),
            cipher=cipher,
            expected_version=1,
            actor_user_id=actor_id,
            trace_id="correct-historical-other",
        )

        assert record.status == "failed"
        assert record.generation == generation
        assert record.started_by == actor_id
        assert record.safe_error_code == "feishu_timeout"
        corrected = onboarding_projection(
            record,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        )
        assert corrected["complete"] is True and corrected["can_submit"] is True

        start_onboarding_submission(
            db,
            record,
            expected_version=2,
            generation=uuid4(),
            actor_user_id=actor_id,
            cipher=cipher,
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            trace_id="retry-corrected-historical-other",
        )
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == record.id))
        assert event.payload["generation"] == str(generation)


def test_onboarding_config_rejects_duplicate_gender_option_ids():
    duplicate = {**SEMANTICS, "gender": {**SEMANTICS["gender"], "options": {"male": "M", "female": "M"}}}
    with pytest.raises(ValueError, match="must be unique"):
        FeishuOnboardingConfigWrite.model_validate({
            "approval_code": "approval",
            "field_mapping": {
                semantic: {"control_id": value["control_id"], "type": value["control_type"], **({"options": value["options"]} if "options" in value else {})}
                for semantic, value in duplicate.items()
            },
        })


def test_failed_onboarding_retry_reuses_the_original_generation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    original_generation = uuid4()
    record = OnboardingRecord(
        organization_id=uuid4(),
        offer_response_id=uuid4(),
        offer_id=uuid4(),
        application_id=uuid4(),
        candidate_id=uuid4(),
        job_id=uuid4(),
        department_id=uuid4(),
        job_title="Role",
        department_name="Department",
        expected_start_date=date(2026, 8, 18),
        pii_ciphertext=OnboardingPiiCipher(KEY).encrypt({
            "name": "Candidate",
            "gender": "female",
            "phone": "+8613800138000",
            "email": "candidate@example.test",
            "home_address": "Shenzhen",
        }),
        status="failed",
        generation=original_generation,
        started_by=uuid4(),
        started_at=datetime(2026, 8, 18, 0, tzinfo=timezone.utc),
        failed_at=datetime(2026, 8, 18, 0, 1, tzinfo=timezone.utc),
        safe_error_code="feishu_timeout",
    )
    with Session(engine) as db:
        original_started_by = record.started_by
        original_started_at = record.started_at
        db.add(record)
        db.add_all([
            FeishuOnboardingConfig(
                organization_id=record.organization_id,
                approval_code="approval",
                field_mapping=SEMANTICS,
                enabled=True,
                validation_status="valid",
                definition_fingerprint="f" * 64,
                created_by=record.started_by,
                updated_by=record.started_by,
            ),
            FeishuDepartmentMapping(
                organization_id=record.organization_id,
                department_id=record.department_id,
                feishu_department_id="od_department",
            ),
        ])
        db.flush()
        start_onboarding_submission(
            db,
            record,
            expected_version=1,
            generation=uuid4(),
            actor_user_id=record.started_by,
            cipher=OnboardingPiiCipher(KEY),
            now=datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            trace_id="retry-same-generation",
        )
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == record.id))

        assert record.status == "submitting"
        assert record.generation == original_generation
        assert record.started_by == original_started_by
        assert record.started_at == original_started_at
        assert event.payload["generation"] == str(original_generation)


def test_http_provider_reads_approval_option_ids_from_legacy_and_v2_shapes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant"})
        if request.url.path.endswith("/approval/v4/approvals/approval"):
            return httpx.Response(200, json={"code": 0, "data": {"form": json.dumps([
                {"id": "legacy", "type": "radio", "option": [{"value": "M"}, {"value": "F"}]},
                {"id": "v2", "type": "radioV2", "value": [{"key": "O"}]},
            ])}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    definition = provider.get_approval_definition(FeishuCredentials("cli", "secret", "https://hr.example.test/callback"), "approval")

    assert {control.control_id: control.option_values for control in definition.controls} == {
        "legacy": ("M", "F"),
        "v2": ("O",),
    }


def test_hr_submission_queues_only_ids_and_worker_marks_submitted_after_provider_receipt():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    organization_id, onboarding_id, actor_id, department_id = uuid4(), uuid4(), uuid4(), uuid4()
    generation = uuid4()
    pii_cipher = OnboardingPiiCipher(KEY)
    feishu_cipher = FeishuSecretCipher(KEY)
    definition = _definition()
    with Session(engine) as db:
        db.add_all([
            FeishuOrganizationConfig(
                organization_id=organization_id,
                app_id="cli_test",
                encrypted_app_secret=feishu_cipher.encrypt("secret-value"),
                redirect_uri="https://hr.example.test/api/v1/auth/feishu/callback",
                enabled=True,
                created_by=actor_id,
                updated_by=actor_id,
            ),
            FeishuOnboardingConfig(
                organization_id=organization_id,
                approval_code=definition.approval_code,
                field_mapping=SEMANTICS,
                enabled=True,
                validation_status="valid",
                definition_fingerprint=definition.fingerprint,
                created_by=actor_id,
                updated_by=actor_id,
            ),
            FeishuDepartmentMapping(
                organization_id=organization_id,
                department_id=department_id,
                feishu_department_id="od_engineering",
            ),
            FeishuIdentityBinding(
                organization_id=organization_id,
                user_id=actor_id,
                open_id="ou_hr",
            ),
            OnboardingRecord(
                id=onboarding_id,
                organization_id=organization_id,
                offer_response_id=uuid4(),
                offer_id=uuid4(),
                application_id=uuid4(),
                candidate_id=uuid4(),
                job_id=uuid4(),
                department_id=department_id,
                job_title="AI Engineer",
                department_name="Engineering",
                expected_start_date=date(2026, 8, 18),
                pii_ciphertext=pii_cipher.encrypt({
                    "name": "Candidate",
                    "gender": "female",
                    "phone": "+8613800138000",
                    "email": "candidate@example.test",
                    "home_address": "Shenzhen",
                }),
                status="ready",
            ),
        ])
        db.flush()
        record = db.get(OnboardingRecord, onboarding_id)
        start_onboarding_submission(
            db,
            record,
            expected_version=1,
            generation=generation,
            actor_user_id=actor_id,
            cipher=pii_cipher,
            now=datetime(2026, 8, 18, 0, tzinfo=timezone.utc),
            trace_id="trace-onboarding",
        )
        db.commit()
        event = db.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == onboarding_id))
        assert event.payload == {
            "organization_id": str(organization_id),
            "onboarding_id": str(onboarding_id),
            "generation": str(generation),
        }
        assert "Candidate" not in json.dumps(event.payload)

    class RecoveringProvider(FakeFeishuProvider):
        def create_approval_instance(self, credentials, request, *, idempotency_key):
            instance = ApprovalInstance("approval_existing")
            self.approval_instances.append((request, idempotency_key, instance))
            self._idempotency[idempotency_key] = instance
            raise FeishuProviderError("feishu_conflict", retryable=False, provider_code=60012)

    provider = RecoveringProvider()
    provider.approval_definition = definition
    handler = FeishuOnboardingOutboxHandler(sessions, provider, feishu_cipher, pii_cipher)
    asyncio.run(handler(SimpleNamespace(payload=event.payload), event.id))

    with Session(engine) as db:
        record = db.get(OnboardingRecord, onboarding_id)
        assert record.status == "submitted"
        assert record.feishu_instance_code.startswith("approval_")
        request, idempotency_key, _ = provider.approval_instances[0]
        assert isinstance(request, ApprovalInstanceRequest)
        assert request.initiator_open_id == "ou_hr"
        assert request.department_id is None
        assert idempotency_key == str(generation)
        assert {item["id"] for item in request.form} == {value["control_id"] for value in SEMANTICS.values()}


def test_hr_submission_is_blocked_before_shanghai_start_date():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    record = OnboardingRecord(
        organization_id=uuid4(),
        offer_response_id=uuid4(),
        offer_id=uuid4(),
        application_id=uuid4(),
        candidate_id=uuid4(),
        job_id=uuid4(),
        department_id=uuid4(),
        job_title="Role",
        department_name="Department",
        expected_start_date=date(2026, 8, 19),
        pii_ciphertext=b"encrypted",
        status="ready",
    )
    with Session(engine) as db:
        db.add(record)
        db.flush()
        with pytest.raises(OnboardingNotReady, match="expected_start_date_not_reached"):
            start_onboarding_submission(
                db,
                record,
                expected_version=1,
                generation=uuid4(),
                actor_user_id=uuid4(),
                cipher=OnboardingPiiCipher(KEY),
                now=datetime(2026, 8, 18, 15, 59, tzinfo=timezone.utc),
                trace_id="trace",
            )


def test_http_provider_serializes_feishu_approval_form_as_json_string():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant"})
        if request.url.path.endswith("/approval/v4/instances"):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"instance_code": "approval_1"}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.create_approval_instance(
        FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
        ApprovalInstanceRequest("approval", "ou_hr", None, ({"id": "name", "type": "input", "value": "Candidate"},)),
        idempotency_key="8d32f17d-d176-4945-8c8f-0785307ab34a",
    )

    body = json.loads(requests[0].content)
    assert result.instance_code == "approval_1"
    assert body["open_id"] == "ou_hr" and "department_id" not in body
    assert isinstance(body["form"], str) and json.loads(body["form"])[0]["id"] == "name"
    assert body["uuid"] == "8d32f17d-d176-4945-8c8f-0785307ab34a"


def test_http_provider_reconciles_an_existing_approval_by_uuid():
    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant"})
        if request.url.path.endswith("/approval/v4/instances"):
            return httpx.Response(200, json={"code": 0, "data": {"instance_code_list": ["instance_1"], "has_more": False}})
        if request.url.path.endswith("/approval/v4/instances/instance_1"):
            return httpx.Response(200, json={"code": 0, "data": {"uuid": "8D32F17D-D176-4945-8C8F-0785307AB34A"}})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    provider = HttpFeishuProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.find_approval_instance_by_uuid(
        FeishuCredentials("cli", "secret", "https://hr.example.test/callback"),
        "approval",
        "8d32f17d-d176-4945-8c8f-0785307ab34a",
        started_at=datetime(2026, 8, 18, 0, tzinfo=timezone.utc),
    )

    assert result == ApprovalInstance("instance_1")
    assert requested_paths[-1].endswith("/approval/v4/instances/instance_1")
