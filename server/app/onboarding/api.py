from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog, Department, Job
from server.app.integrations.feishu.models import (
    FeishuDepartmentMapping,
    FeishuOnboardingConfig,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import FeishuCredentials, FeishuProviderError
from server.app.onboarding.models import OnboardingRecord
from server.app.onboarding.schemas import FeishuOnboardingConfigWrite, OnboardingUpdateCommand
from server.app.onboarding.service import (
    OnboardingNotReady,
    OnboardingVersionConflict,
    create_onboarding_from_accepted_offer,
    onboarding_projection,
    start_onboarding_submission,
    update_onboarding,
    validate_definition,
)
from server.app.offers.models import Offer, OfferResponse
from server.app.recruiting.api import _principal
from server.app.recruiting.authorization import RecruitingAction, RecruitingAuthorizationService
from server.app.recruiting.models import Application


router = APIRouter(prefix="/api/v1")
AUTH = RecruitingAuthorizationService()


def _response(data, status=200, *, etag: int | None = None):
    response = JSONResponse({"data": data}, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    if etag is not None:
        response.headers["ETag"] = f'"{etag}"'
    return response


def _expected_version(request: Request, value: str | None):
    if value is None:
        return problem(request, 428, "precondition_required", "The current onboarding version is required.")
    if len(value) < 3 or not value.startswith('"') or not value.endswith('"') or not value[1:-1].isdigit():
        return problem(request, 422, "validation_failed", "The onboarding version is invalid.")
    return int(value[1:-1])


def _authorized_application(db, principal, application_id, *, lock=False):
    if not principal.active or not principal.roles.intersection({"recruiting_admin", "recruiter"}):
        return None
    query = (
        select(Application)
        .join(Job, (Job.organization_id == Application.organization_id) & (Job.id == Application.job_id))
        .where(
            Application.organization_id == principal.organization_id,
            Application.id == application_id,
            AUTH.job_predicate(principal, RecruitingAction.MANAGE_CANDIDATE, Job),
        )
    )
    return db.scalar(query.with_for_update() if lock else query)


def _authorized_record(db, principal, onboarding_id, *, lock=False):
    if not principal.active or not principal.roles.intersection({"recruiting_admin", "recruiter"}):
        return None
    query = (
        select(OnboardingRecord)
        .join(Application, (Application.organization_id == OnboardingRecord.organization_id) & (Application.id == OnboardingRecord.application_id))
        .join(Job, (Job.organization_id == Application.organization_id) & (Job.id == Application.job_id))
        .where(
            OnboardingRecord.organization_id == principal.organization_id,
            OnboardingRecord.id == onboarding_id,
            AUTH.job_predicate(principal, RecruitingAction.MANAGE_CANDIDATE, Job),
        )
    )
    return db.scalar(query.with_for_update() if lock else query)


@router.get("/applications/{application_id}/onboarding")
def get_application_onboarding(application_id: uuid.UUID, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        application = _authorized_application(db, principal, application_id)
        if application is None:
            return problem(request, 404, "resource_not_found", "The resource was not found.")
        record = db.scalar(select(OnboardingRecord).where(
            OnboardingRecord.organization_id == principal.organization_id,
            OnboardingRecord.application_id == application.id,
        ))
        job = department = response = None
        if record is None:
            job = db.scalar(select(Job).where(
                Job.organization_id == principal.organization_id,
                Job.id == application.job_id,
            ))
            department = db.scalar(select(Department).where(
                Department.organization_id == principal.organization_id,
                Department.id == job.department_id,
            )) if job is not None and job.department_id is not None else None
            response = db.scalar(
                select(OfferResponse)
                .join(Offer, (Offer.organization_id == OfferResponse.organization_id) & (Offer.id == OfferResponse.offer_id))
                .where(
                    OfferResponse.organization_id == principal.organization_id,
                    Offer.application_id == application.id,
                    OfferResponse.status == "accepted",
                )
                .order_by(OfferResponse.responded_at.desc())
            )
        data = onboarding_projection(
            record,
            cipher=request.app.state.onboarding_pii_cipher,
            now=now,
            application=application,
            job=job,
            department=department,
            expected_start_date=response.expected_start_date if response else None,
        )
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="onboarding.data_viewed",
            outcome="success",
            resource_type="onboarding",
            resource_id=record.id if record else application.id,
            trace_id=request.state.trace_id,
            metadata_json={"status": data["status"]},
        ))
        db.commit()
        return _response(data, etag=record.version if record else 0)


@router.put("/onboardings/{onboarding_id}")
def put_onboarding(onboarding_id: uuid.UUID, payload: OnboardingUpdateCommand, request: Request, if_match: str | None = Header(default=None)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return expected
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        try:
            record = _authorized_record(db, principal, onboarding_id, lock=True)
            if record is None:
                application = _authorized_application(db, principal, onboarding_id, lock=True)
                if application is None:
                    return problem(request, 404, "resource_not_found", "The resource was not found.")
                record = create_onboarding_from_accepted_offer(
                    db,
                    application,
                    payload,
                    cipher=request.app.state.onboarding_pii_cipher,
                    expected_version=expected,
                    actor_user_id=principal.user_id,
                    trace_id=request.state.trace_id,
                )
            else:
                update_onboarding(
                    db,
                    record,
                    payload,
                    cipher=request.app.state.onboarding_pii_cipher,
                    expected_version=expected,
                    actor_user_id=principal.user_id,
                    trace_id=request.state.trace_id,
                )
        except OnboardingVersionConflict:
            return problem(request, 409, "version_conflict", "The onboarding record changed.")
        except OnboardingNotReady as error:
            return problem(request, 409, error.code, "The onboarding record cannot be changed.")
        db.commit()
        return _response(onboarding_projection(record, cipher=request.app.state.onboarding_pii_cipher, now=now), etag=record.version)


@router.post("/onboardings/{onboarding_id}/submissions")
def submit_onboarding(
    onboarding_id: uuid.UUID,
    request: Request,
    if_match: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    expected = _expected_version(request, if_match)
    if isinstance(expected, JSONResponse):
        return expected
    try:
        generation = uuid.UUID(idempotency_key) if idempotency_key else None
    except ValueError:
        generation = None
    if generation is None:
        return problem(request, 422, "idempotency_key_invalid", "Idempotency-Key must be a UUID.")
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        record = _authorized_record(db, principal, onboarding_id, lock=True)
        if record is None:
            return problem(request, 404, "resource_not_found", "The resource was not found.")
        try:
            record, replayed = start_onboarding_submission(
                db,
                record,
                expected_version=expected,
                generation=generation,
                actor_user_id=principal.user_id,
                now=now,
                trace_id=request.state.trace_id,
            )
        except OnboardingVersionConflict:
            return problem(request, 409, "version_conflict", "The onboarding record changed.")
        except OnboardingNotReady as error:
            return problem(request, 409, error.code, "Onboarding cannot be started.")
        db.commit()
        data = onboarding_projection(record, cipher=request.app.state.onboarding_pii_cipher, now=now)
        data["replayed"] = replayed
        return _response(data, status=202, etag=record.version)


def _config_view(config, mappings):
    if config is None:
        return {"configured": False, "enabled": False, "validation_status": "unvalidated", "department_mappings": []}
    field_mapping = {
        semantic: {
            "control_id": value["control_id"],
            "type": value.get("control_type", value.get("type")),
            **({"options": value["options"]} if value.get("options") is not None else {}),
        }
        for semantic, value in config.field_mapping.items()
    }
    return {
        "configured": True,
        "approval_code": config.approval_code,
        "field_mapping": field_mapping,
        "enabled": config.enabled,
        "validation_status": config.validation_status,
        "validated_at": config.validated_at.isoformat() if config.validated_at else None,
        "validation_safe_error_code": config.validation_safe_error_code,
        "definition_fingerprint": config.definition_fingerprint,
        "version": config.version,
        "department_mappings": [
            {"department_id": str(item.department_id), "feishu_department_id": item.feishu_department_id}
            for item in mappings
        ],
    }


def _config_admin(principal):
    return principal.active and bool(principal.roles.intersection({"system_admin", "recruiting_admin"}))


@router.get("/settings/integrations/feishu/onboarding-approval")
def get_onboarding_config(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _config_admin(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOnboardingConfig).where(FeishuOnboardingConfig.organization_id == principal.organization_id))
        mappings = list(db.scalars(select(FeishuDepartmentMapping).where(FeishuDepartmentMapping.organization_id == principal.organization_id)))
        return _response(_config_view(config, mappings), etag=config.version if config else None)


@router.put("/settings/integrations/feishu/onboarding-approval")
def put_onboarding_config(
    payload: FeishuOnboardingConfigWrite,
    request: Request,
    if_match: str | None = Header(default=None),
):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _config_admin(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    mapping_value = {key: value.model_dump(mode="json", exclude_none=True) for key, value in payload.field_mapping.items()}
    with request.app.state.identity_store.sync_session() as db:
        department_ids = set(db.scalars(select(Department.id).where(
            Department.organization_id == principal.organization_id,
            Department.id.in_([item.department_id for item in payload.department_mappings]),
        ))) if payload.department_mappings else set()
        if department_ids != {item.department_id for item in payload.department_mappings}:
            return problem(request, 422, "feishu_department_invalid", "A department mapping is invalid.")
        config = db.scalar(select(FeishuOnboardingConfig).where(
            FeishuOnboardingConfig.organization_id == principal.organization_id,
        ).with_for_update())
        if config is not None:
            expected = _expected_version(request, if_match)
            if isinstance(expected, JSONResponse):
                return expected
            if config.version != expected:
                return problem(request, 409, "resource_version_conflict", "The Feishu onboarding configuration changed.")
        definition_unchanged = bool(config and config.approval_code == payload.approval_code and config.field_mapping == mapping_value)
        if payload.enabled and (config is None or not definition_unchanged or config.validation_status != "valid"):
            return problem(request, 409, "feishu_onboarding_validation_required", "Validate the approval definition before enabling it.")
        if config is None:
            config = FeishuOnboardingConfig(
                organization_id=principal.organization_id,
                approval_code=payload.approval_code,
                field_mapping=mapping_value,
                enabled=False,
                created_by=principal.user_id,
                updated_by=principal.user_id,
            )
            db.add(config)
        else:
            config.approval_code = payload.approval_code
            config.field_mapping = mapping_value
            config.enabled = payload.enabled
            config.updated_by = principal.user_id
            config.version += 1
            if not definition_unchanged:
                config.validation_status = "unvalidated"
                config.validated_at = None
                config.validation_safe_error_code = None
                config.definition_fingerprint = None
                config.enabled = False
        db.execute(delete(FeishuDepartmentMapping).where(FeishuDepartmentMapping.organization_id == principal.organization_id))
        for item in payload.department_mappings:
            db.add(FeishuDepartmentMapping(
                organization_id=principal.organization_id,
                department_id=item.department_id,
                feishu_department_id=item.feishu_department_id,
            ))
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="feishu.onboarding_config_updated",
            outcome="success",
            trace_id=request.state.trace_id,
            metadata_json={"enabled": config.enabled},
        ))
        db.commit()
        mappings = list(db.scalars(select(FeishuDepartmentMapping).where(FeishuDepartmentMapping.organization_id == principal.organization_id)))
        return _response(_config_view(config, mappings), etag=config.version)


@router.post("/settings/integrations/feishu/onboarding-approval/validate")
def validate_onboarding_config(request: Request, if_match: str | None = Header(default=None)):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _config_admin(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOnboardingConfig).where(FeishuOnboardingConfig.organization_id == principal.organization_id))
        base = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == principal.organization_id))
        if config is None or base is None or base.encrypted_app_secret is None or not base.enabled:
            return problem(request, 409, "feishu_onboarding_not_configured", "Feishu and onboarding approval must be configured first.")
        expected = _expected_version(request, if_match)
        if isinstance(expected, JSONResponse):
            return expected
        if config.version != expected:
            return problem(request, 409, "resource_version_conflict", "The Feishu onboarding configuration changed.")
        credentials = FeishuCredentials(
            base.app_id,
            request.app.state.feishu_secret_cipher.decrypt(base.encrypted_app_secret),
            base.redirect_uri,
            base.calendar_id,
        )
        config_id, config_version = config.id, config.version
        approval_code, field_mapping = config.approval_code, config.field_mapping

    definition = None
    safe_error_code = None
    try:
        definition = request.app.state.feishu_provider.get_approval_definition(credentials, approval_code)
        safe_error_code = validate_definition(field_mapping, definition)
    except FeishuProviderError as error:
        safe_error_code = error.safe_code

    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOnboardingConfig).where(
            FeishuOnboardingConfig.organization_id == principal.organization_id,
            FeishuOnboardingConfig.id == config_id,
            FeishuOnboardingConfig.version == config_version,
        ).with_for_update())
        if config is None:
            return problem(request, 409, "feishu_config_changed", "Feishu onboarding configuration changed during validation.")
        config.validation_status = "invalid" if safe_error_code else "valid"
        config.validated_at = now
        config.validation_safe_error_code = safe_error_code
        config.definition_fingerprint = definition.fingerprint if definition and not safe_error_code else None
        config.updated_by = principal.user_id
        if safe_error_code:
            config.enabled = False
        config.version += 1
        db.add(AuditLog(
            organization_id=principal.organization_id,
            actor_user_id=principal.user_id,
            event_type="feishu.onboarding_config_validated",
            outcome="failed" if safe_error_code else "success",
            trace_id=request.state.trace_id,
            metadata_json={},
        ))
        db.commit()
        mappings = list(db.scalars(select(FeishuDepartmentMapping).where(FeishuDepartmentMapping.organization_id == principal.organization_id)))
        if safe_error_code:
            return problem(request, 409, safe_error_code, "The Feishu onboarding approval definition is invalid.")
        data = _config_view(config, mappings)
        data["controls"] = [
            {"control_id": control.control_id, "custom_id": control.custom_id, "control_type": control.control_type}
            for control in definition.controls
        ]
        return _response(data, etag=config.version)
