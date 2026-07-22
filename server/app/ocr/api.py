import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from server.app.identity.api import problem
from server.app.identity.models import AuditLog
from server.app.identity.policy import Permission, require_permission
from server.app.llm.policy import ProviderPolicyError
from server.app.ocr.gateway import OcrGateway, OcrGatewayError
from server.app.ocr.models import OcrProviderConfig
from server.app.ocr.schemas import OcrConfigResource, OcrConfigUpdate, OcrTestResource
from server.app.recruiting.api import _idempotency, _principal
from server.app.recruiting.models import IdempotencyRecord
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent


router = APIRouter(prefix="/api/v1/settings/ocr")


def _response(data, status: int = 200):
    response = JSONResponse({"data": data}, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(request: Request, status: int, code: str):
    response = problem(request, status, code, "The request could not be completed.")
    response.headers["Cache-Control"] = "no-store"
    return response


def _system(principal) -> bool:
    return require_permission(principal, Permission.MANAGE_SYSTEM)


def _reader(principal) -> bool:
    return _system(principal) or (principal.active and "recruiting_admin" in principal.roles)


def _iso(value):
    return value.isoformat() if value else None


def _view(config: OcrProviderConfig | None, *, system: bool):
    if config is None:
        data = {
            "configured": False,
            "provider_id": None,
            "base_url": None,
            "model": None,
            "enabled": False,
            "version": 0,
            "last_test_status": None,
            "last_test_error_code": None,
            "last_test_latency_ms": None,
            "last_tested_at": None,
            "created_by": None,
            "updated_by": None,
            "created_at": None,
            "updated_at": None,
        }
    else:
        data = {
            "configured": True,
            "provider_id": config.provider_id,
            "base_url": config.base_url,
            "model": config.model,
            "enabled": config.enabled,
            "version": config.version,
            "last_test_status": config.last_test_status,
            "last_test_error_code": config.last_test_error_code,
            "last_test_latency_ms": config.last_test_latency_ms,
            "last_tested_at": _iso(config.last_tested_at),
            "created_by": str(config.created_by),
            "updated_by": str(config.updated_by),
            "created_at": _iso(config.created_at),
            "updated_at": _iso(config.updated_at),
        }
    if system:
        data["key_configured"] = config is not None and config.encrypted_api_key is not None
    return data


def _config_version(request: Request, value: str | None):
    if value is None:
        return _error(request, 428, "precondition_required")
    match = re.fullmatch(r'^"(0|[1-9][0-9]*)"$', value)
    return int(match.group(1)) if match else _error(request, 422, "validation_failed")


def _request_hash(body) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _gateway(request: Request) -> OcrGateway:
    gateway = getattr(request.app.state, "ocr_gateway", None)
    if gateway is None:
        gateway = OcrGateway()
        request.app.state.ocr_gateway = gateway
    return gateway


def _cipher(request: Request):
    return getattr(request.app.state, "ocr_key_cipher", request.app.state.llm_key_cipher)


@router.get("", response_model=OcrConfigResource)
def get_config(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _reader(principal):
        return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(
            select(OcrProviderConfig).where(OcrProviderConfig.organization_id == principal.organization_id)
        )
    return _response(_view(config, system=_system(principal)))


@router.put("", response_model=OcrConfigResource)
def put_config(
    payload: OcrConfigUpdate,
    request: Request,
    if_match: str | None = Header(None),
    idempotency_key: str | None = Header(None),
):
    principal = _principal(request)
    expected = _config_version(request, if_match)
    key = _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse):
        return principal
    if not _system(principal):
        return _error(request, 404, "resource_not_found")
    if isinstance(expected, JSONResponse):
        return expected
    if isinstance(key, JSONResponse):
        return key
    try:
        _gateway(request).validate_provider(payload.provider_id, payload.base_url, payload.model)
    except ProviderPolicyError as error:
        return _error(request, 422, str(error))
    except OcrGatewayError as error:
        return _error(request, 422, error.safe_code)

    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                config = db.scalar(
                    select(OcrProviderConfig)
                    .where(OcrProviderConfig.organization_id == principal.organization_id)
                    .with_for_update()
                )
                current = config.version if config else 0
                if current != expected:
                    raise RuntimeError("version")
                encrypted = (
                    _cipher(request).encrypt(payload.api_key)
                    if payload.api_key is not None
                    else (config.encrypted_api_key if config else None)
                )
                if payload.enabled and encrypted is None:
                    raise ValueError("key")
                if config is None:
                    config = OcrProviderConfig(
                        organization_id=principal.organization_id,
                        provider_id=payload.provider_id,
                        base_url=payload.base_url,
                        model=payload.model,
                        encrypted_api_key=encrypted,
                        enabled=payload.enabled,
                        version=1,
                        created_by=principal.user_id,
                        updated_by=principal.user_id,
                    )
                    db.add(config)
                else:
                    config.provider_id = payload.provider_id
                    config.base_url = payload.base_url
                    config.model = payload.model
                    config.encrypted_api_key = encrypted
                    config.enabled = payload.enabled
                    config.updated_by = principal.user_id
                    config.version += 1
                    config.updated_at = datetime.now(timezone.utc)
                config.last_test_status = None
                config.last_test_error_code = None
                config.last_test_latency_ms = None
                config.last_tested_at = None
                db.flush()
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type="ocr.config_updated",
                        outcome="success",
                        trace_id=request.state.trace_id,
                        metadata_json={"config_id": str(config.id), "enabled": config.enabled},
                    )
                )
                return 200, {"data": _view(config, system=True)}

            status, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                "ocr.config.put",
                key,
                payload.model_dump(),
                action,
            )
            db.commit()
        except IdempotencyConflict:
            db.rollback()
            return _error(request, 409, "idempotency_conflict")
        except RuntimeError:
            db.rollback()
            return _error(request, 409, "resource_version_conflict")
        except ValueError:
            db.rollback()
            return _error(request, 422, "api_key_required")
        except Exception:
            db.rollback()
            return _error(request, 503, "persistence_failed")
    response = JSONResponse(body, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/test", response_model=OcrTestResource)
async def test_config(request: Request, idempotency_key: str | None = Header(None)):
    principal = _principal(request)
    key = _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse):
        return principal
    if not _system(principal):
        return _error(request, 404, "resource_not_found")
    if isinstance(key, JSONResponse):
        return key
    operation = "ocr.config.test"
    fingerprint = {"fixed_probe_version": "v1"}
    with request.app.state.identity_store.sync_session() as db:
        previous = db.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.organization_id == principal.organization_id,
                IdempotencyRecord.user_id == principal.user_id,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.idempotency_key == key,
            )
        )
        if previous:
            if previous.request_hash != _request_hash(fingerprint):
                return _error(request, 409, "idempotency_conflict")
            response = JSONResponse(previous.response_json, status_code=previous.status_code)
            response.headers["Cache-Control"] = "no-store"
            return response
        config = db.scalar(
            select(OcrProviderConfig).where(OcrProviderConfig.organization_id == principal.organization_id)
        )
        if config is None or config.encrypted_api_key is None:
            return _error(request, 409, "ocr_not_configured")
        config_id, config_version = config.id, config.version
        provider_id, base_url, model = config.provider_id, config.base_url, config.model
        try:
            api_key = _cipher(request).decrypt(config.encrypted_api_key)
        except ValueError:
            return _error(request, 503, "ocr_key_unavailable")

    safe_code = None
    latency = None
    try:
        latency = await _gateway(request).test_connection(provider_id, base_url, model, api_key)
        status_code = 200
        data = {"status": "succeeded", "safe_error_code": None, "latency_ms": latency}
    except OcrGatewayError as error:
        safe_code = error.safe_code
        status_code = 422
        data = {"status": "failed", "safe_error_code": safe_code, "latency_ms": None}

    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                config = db.scalar(
                    select(OcrProviderConfig)
                    .where(
                        OcrProviderConfig.organization_id == principal.organization_id,
                        OcrProviderConfig.id == config_id,
                    )
                    .with_for_update()
                )
                if config is None:
                    raise RuntimeError
                if config.version != config_version:
                    raise RuntimeError("version")
                config.last_test_status = data["status"]
                config.last_test_error_code = safe_code
                config.last_test_latency_ms = latency
                config.last_tested_at = datetime.now(timezone.utc)
                db.add(
                    AuditLog(
                        organization_id=principal.organization_id,
                        actor_user_id=principal.user_id,
                        event_type="ocr.connection_tested",
                        outcome=data["status"],
                        trace_id=request.state.trace_id,
                        metadata_json={"config_id": str(config.id), "safe_error_code": safe_code},
                    )
                )
                return status_code, {"data": data}

            stored_status, body = persisted_idempotent(
                db,
                principal.organization_id,
                principal.user_id,
                operation,
                key,
                fingerprint,
                action,
            )
            db.commit()
        except IdempotencyConflict:
            db.rollback()
            return _error(request, 409, "idempotency_conflict")
        except RuntimeError:
            db.rollback()
            return _error(request, 409, "resource_version_conflict")
        except Exception:
            db.rollback()
            return _error(request, 503, "persistence_failed")
    response = JSONResponse(body, status_code=stored_status)
    response.headers["Cache-Control"] = "no-store"
    return response
