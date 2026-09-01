from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from server.app.identity.models import AuditLog, Department, Job
from server.app.integrations.feishu.models import FeishuDepartmentMapping, FeishuOnboardingConfig
from server.app.integrations.feishu.provider import ApprovalDefinition
from server.app.onboarding.models import OnboardingRecord
from server.app.offers.models import Offer, OfferResponse
from server.app.queue.repository import QueueRepository
from server.app.recruiting.models import Application, Candidate, CandidateContact


BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUIRED_SEMANTICS = {
    "candidate_name",
    "gender",
    "department",
    "job_title",
    "phone",
    "email",
    "home_address",
}
UNSUPPORTED_CONTROL_TYPES = {"apaascorehrOnboardingGroup"}
VALID_GENDERS = {"male", "female"}


class OnboardingNotFound(Exception):
    pass


class OnboardingVersionConflict(Exception):
    pass


class OnboardingNotReady(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def business_date(now: datetime):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(BUSINESS_TIMEZONE).date()


def public_onboarding_prefill(db, organization_id, application: Application, *, contact_cipher) -> dict:
    candidate = db.scalar(select(Candidate).where(
        Candidate.organization_id == organization_id,
        Candidate.id == application.candidate_id,
        Candidate.deleted_at.is_(None),
    ))
    if candidate is None:
        raise OnboardingNotFound
    contacts = list(db.scalars(select(CandidateContact).where(
        CandidateContact.organization_id == organization_id,
        CandidateContact.candidate_id == candidate.id,
        CandidateContact.kind.in_(("email", "phone")),
    )))
    contacts.sort(key=lambda item: (item.confirmation_status != "confirmed", item.created_at))
    values: dict[str, str | None] = {"email": None, "phone": None}
    for contact in contacts:
        if values[contact.kind] is None:
            values[contact.kind] = contact_cipher.decrypt(contact.ciphertext)
    job = db.scalar(select(Job).where(
        Job.organization_id == organization_id,
        Job.id == application.job_id,
    ))
    department = db.scalar(select(Department).where(
        Department.organization_id == organization_id,
        Department.id == job.department_id,
    )) if job is not None and job.department_id is not None else None
    return {
        "candidate_name": candidate.display_name,
        "email": values["email"],
        "phone": values["phone"],
        "department_name": department.name if department else None,
        "job_title": job.title if job else None,
    }


def _mask_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    visible = local[:1]
    return f"{visible}***@{domain}"


def _mask_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) <= 4:
        return "***"
    return f"***{digits[-4:]}"


def normalize_stored_field_mapping(field_mapping: dict) -> dict:
    """Read configurations saved under the previous eight-field contract safely."""
    normalized = {
        semantic: dict(value)
        for semantic, value in field_mapping.items()
        if semantic in REQUIRED_SEMANTICS
    }
    gender = normalized.get("gender")
    if gender is not None and gender.get("options") is not None:
        gender["options"] = {
            key: value
            for key, value in gender["options"].items()
            if key in VALID_GENDERS
        }
    return normalized


def onboarding_data_blocking_reason(pii: dict) -> str | None:
    if pii.get("gender") not in VALID_GENDERS:
        return "onboarding_gender_invalid"
    if not all(pii.get(field) for field in ("name", "phone", "email", "home_address")):
        return "onboarding_data_incomplete"
    return None


def can_update_onboarding(record: OnboardingRecord, data_blocking_reason: str | None) -> bool:
    if record.status == "ready":
        return True
    return (
        record.status == "failed"
        and data_blocking_reason == "onboarding_gender_invalid"
        and record.safe_error_code != "feishu_approval_reconciliation_required"
    )


def onboarding_projection(
    record: OnboardingRecord | None,
    *,
    cipher,
    now: datetime,
    application: Application | None = None,
    job: Job | None = None,
    department: Department | None = None,
    expected_start_date=None,
) -> dict:
    if record is None:
        return {
            "id": str(application.id) if application else None,
            "status": "incomplete",
            "version": 0 if application else None,
            "application_id": str(application.id) if application else None,
            "candidate_id": str(application.candidate_id) if application else None,
            "job_id": str(job.id) if job else None,
            "department_id": str(department.id) if department else None,
            "job_title": job.title if job else None,
            "department_name": department.name if department else None,
            "expected_start_date": expected_start_date.isoformat() if expected_start_date else None,
            "complete": False,
            "can_submit": False,
            "masked_phone": None,
            "masked_email": None,
            "instance_code": None,
            "allowed_actions": {"update": application is not None, "start_onboarding": False},
            "blocking_reason": "onboarding_data_missing",
        }
    pii = cipher.decrypt(record.pii_ciphertext)
    data_blocking_reason = onboarding_data_blocking_reason(pii)
    complete = data_blocking_reason is None
    date_reached = business_date(now) >= record.expected_start_date
    can_start = complete and date_reached and record.status in {"ready", "failed"}
    can_update = can_update_onboarding(record, data_blocking_reason)
    protected_failure_reason = (
        record.safe_error_code
        if record.status == "failed" and not can_update and data_blocking_reason is not None
        else None
    )
    return {
        "id": str(record.id),
        "status": record.status,
        "version": record.version,
        "application_id": str(record.application_id),
        "candidate_id": str(record.candidate_id),
        "job_id": str(record.job_id),
        "department_id": str(record.department_id),
        "job_title": record.job_title,
        "department_name": record.department_name,
        "expected_start_date": record.expected_start_date.isoformat(),
        "complete": complete,
        "can_submit": can_start,
        "masked_phone": _mask_phone(pii["phone"]) if pii.get("phone") else None,
        "masked_email": _mask_email(pii["email"]) if pii.get("email") else None,
        "safe_error_code": record.safe_error_code,
        "instance_code": record.feishu_instance_code,
        "allowed_actions": {
            "update": can_update,
            "start_onboarding": can_start,
        },
        "blocking_reason": None if can_start else (
            protected_failure_reason
            or data_blocking_reason
            or ("expected_start_date_not_reached" if not date_reached else None)
            or ("onboarding_submission_in_progress" if record.status == "submitting" else None)
            or ("onboarding_already_submitted" if record.status == "submitted" else None)
            or record.safe_error_code
        ),
    }


def update_onboarding(db, record: OnboardingRecord, payload, *, cipher, expected_version: int, actor_user_id, trace_id: str):
    if record.version != expected_version:
        raise OnboardingVersionConflict
    current = cipher.decrypt(record.pii_ciphertext)
    if not can_update_onboarding(record, onboarding_data_blocking_reason(current)):
        raise OnboardingNotReady("onboarding_update_not_allowed")
    preserve_failed_submission = record.status == "failed"
    if preserve_failed_submission and (
        payload.expected_start_date is not None
        or payload.onboarding_data is None
        or payload.onboarding_data.model_fields_set != {"gender"}
    ):
        raise OnboardingNotReady("onboarding_update_not_allowed")
    if payload.onboarding_data is not None:
        current.update(payload.onboarding_data.model_dump(mode="json", exclude_unset=True, exclude_none=True))
    if payload.expected_start_date is not None:
        record.expected_start_date = payload.expected_start_date
    record.pii_ciphertext = cipher.encrypt(current)
    if not preserve_failed_submission:
        record.status = "ready"
        record.generation = None
        record.started_by = None
        record.started_at = None
        record.failed_at = None
        record.safe_error_code = None
    record.version += 1
    db.add(AuditLog(
        organization_id=record.organization_id,
        actor_user_id=actor_user_id,
        event_type="onboarding.data_updated",
        outcome="success",
        resource_type="onboarding",
        resource_id=record.id,
        trace_id=trace_id,
        metadata_json={},
    ))
    db.flush()
    return record


def create_onboarding_from_accepted_offer(
    db,
    application: Application,
    payload,
    *,
    cipher,
    expected_version: int,
    actor_user_id,
    trace_id: str,
):
    if expected_version != 0:
        raise OnboardingVersionConflict
    values = (
        payload.onboarding_data.model_dump(mode="json", exclude_unset=True, exclude_none=True)
        if payload.onboarding_data is not None
        else {}
    )
    required = {"gender", "phone", "email", "home_address"}
    if not required.issubset(values):
        raise OnboardingNotReady("onboarding_data_incomplete")
    existing = db.scalar(select(OnboardingRecord).where(
        OnboardingRecord.organization_id == application.organization_id,
        OnboardingRecord.application_id == application.id,
    ).with_for_update())
    if existing is not None:
        raise OnboardingVersionConflict
    offer = db.scalar(select(Offer).where(
        Offer.organization_id == application.organization_id,
        Offer.application_id == application.id,
        Offer.status == "accepted",
    ).with_for_update())
    response = db.scalar(select(OfferResponse).where(
        OfferResponse.organization_id == application.organization_id,
        OfferResponse.offer_id == offer.id,
        OfferResponse.status == "accepted",
    ).order_by(OfferResponse.responded_at.desc())) if offer is not None else None
    candidate = db.scalar(select(Candidate).where(
        Candidate.organization_id == application.organization_id,
        Candidate.id == application.candidate_id,
        Candidate.deleted_at.is_(None),
    ))
    job = db.scalar(select(Job).where(
        Job.organization_id == application.organization_id,
        Job.id == application.job_id,
    ))
    department = db.scalar(select(Department).where(
        Department.organization_id == application.organization_id,
        Department.id == job.department_id,
    )) if job is not None and job.department_id is not None else None
    if offer is None or response is None or candidate is None or job is None or department is None:
        raise OnboardingNotReady("accepted_offer_context_incomplete")
    expected_start_date = payload.expected_start_date or response.expected_start_date
    if expected_start_date is None:
        raise OnboardingNotReady("onboarding_start_date_required")
    name = values.pop("name", None) or candidate.display_name.strip()
    record = OnboardingRecord(
        organization_id=application.organization_id,
        offer_response_id=response.id,
        offer_id=offer.id,
        application_id=application.id,
        candidate_id=candidate.id,
        job_id=job.id,
        department_id=department.id,
        job_title=job.title,
        department_name=department.name,
        expected_start_date=expected_start_date,
        pii_ciphertext=cipher.encrypt({"name": name, **values}),
        status="ready",
    )
    db.add(record)
    db.add(AuditLog(
        organization_id=record.organization_id,
        actor_user_id=actor_user_id,
        event_type="onboarding.data_created",
        outcome="success",
        resource_type="onboarding",
        resource_id=record.id,
        trace_id=trace_id,
        metadata_json={},
    ))
    db.flush()
    return record


def start_onboarding_submission(
    db,
    record: OnboardingRecord,
    *,
    expected_version: int,
    generation: uuid.UUID,
    actor_user_id,
    cipher,
    now: datetime,
    trace_id: str,
):
    if record.generation == generation and record.status in {"submitting", "submitted"}:
        return record, True
    if record.version != expected_version:
        raise OnboardingVersionConflict
    if record.status not in {"ready", "failed"}:
        raise OnboardingNotReady("onboarding_submission_not_allowed")
    if business_date(now) < record.expected_start_date:
        raise OnboardingNotReady("expected_start_date_not_reached")
    data_blocking_reason = onboarding_data_blocking_reason(cipher.decrypt(record.pii_ciphertext))
    if data_blocking_reason:
        raise OnboardingNotReady(data_blocking_reason)
    config = db.scalar(select(FeishuOnboardingConfig).where(
        FeishuOnboardingConfig.organization_id == record.organization_id,
        FeishuOnboardingConfig.enabled.is_(True),
        FeishuOnboardingConfig.validation_status == "valid",
    ))
    if config is None:
        raise OnboardingNotReady("feishu_onboarding_not_configured")
    mapping = db.scalar(select(FeishuDepartmentMapping).where(
        FeishuDepartmentMapping.organization_id == record.organization_id,
        FeishuDepartmentMapping.department_id == record.department_id,
    ))
    if mapping is None:
        raise OnboardingNotReady("feishu_department_unmapped")
    retry_existing_generation = record.status == "failed" and record.generation is not None
    submission_generation = record.generation if retry_existing_generation else generation
    record.status = "submitting"
    record.generation = submission_generation
    if not retry_existing_generation:
        record.started_by = actor_user_id
        record.started_at = now
    record.failed_at = None
    record.safe_error_code = None
    record.version += 1
    QueueRepository(db).append_outbox(
        record.organization_id,
        "feishu.approval.onboarding.create",
        "feishu_onboarding",
        record.id,
        {
            "organization_id": str(record.organization_id),
            "onboarding_id": str(record.id),
            "generation": str(submission_generation),
        },
    )
    db.add(AuditLog(
        organization_id=record.organization_id,
        actor_user_id=actor_user_id,
        event_type="onboarding.submission_started",
        outcome="success",
        resource_type="onboarding",
        resource_id=record.id,
        trace_id=trace_id,
        metadata_json={},
    ))
    db.flush()
    return record, False


def validate_definition(field_mapping: dict, definition: ApprovalDefinition) -> str | None:
    if definition.status is not None and definition.status.upper() not in {"ACTIVE", "ENABLED"}:
        return "feishu_approval_definition_inactive"
    if any(control.control_type in UNSUPPORTED_CONTROL_TYPES for control in definition.controls):
        return "feishu_approval_control_unsupported"
    if set(field_mapping) != REQUIRED_SEMANTICS:
        return "feishu_approval_mapping_incomplete"
    controls: dict[str, object] = {}
    for control in definition.controls:
        controls[control.control_id] = control
        if control.custom_id:
            controls[control.custom_id] = control
    seen: set[str] = set()
    for semantic, value in field_mapping.items():
        control_id = value["control_id"]
        if control_id in seen:
            return "feishu_approval_mapping_duplicate"
        seen.add(control_id)
        control = controls.get(control_id)
        if control is None:
            return "feishu_approval_control_missing"
        mapped_type = value.get("control_type", value.get("type"))
        if control.control_type != mapped_type:
            return "feishu_approval_control_type_mismatch"
        if semantic == "gender":
            configured_option_values = tuple((value.get("options") or {}).values())
            if len(set(configured_option_values)) != len(configured_option_values):
                return "feishu_approval_option_duplicate"
            configured_options = set(configured_option_values)
            if not control.option_values:
                return "feishu_approval_option_metadata_missing"
            if not configured_options.issubset(set(control.option_values)):
                return "feishu_approval_option_invalid"
    return None
