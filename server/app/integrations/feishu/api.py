from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from server.app.identity.api import cookie_name, problem, session_token
from server.app.identity.models import AuditLog, Organization, User, UserStatus
from server.app.identity.service import InvalidSession
from server.app.integrations.feishu.models import (
    FeishuIdentityBinding,
    FeishuOAuthState,
    FeishuOrganizationConfig,
)
from server.app.integrations.feishu.provider import FeishuCredentials, FeishuProviderError, chunk_freebusy_requests
from server.app.integrations.feishu.service import hash_oauth_state, public_config
from server.app.integrations.feishu.sync import mark_provider_change


router = APIRouter(prefix="/api/v1")


def _app_redirect(*, status: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode({key: value for key, value in {"feishu_status": status, "feishu_error": error}.items() if value})
    response = RedirectResponse(url=f"/?{query}" if query else "/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeishuConfigWrite(StrictModel):
    app_id: str = Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    app_secret: str | None = Field(default=None, min_length=8, max_length=4096)
    redirect_uri: str = Field(min_length=8, max_length=2048)
    calendar_id: str = Field(default="primary", min_length=1, max_length=512)
    verification_token: str | None = Field(default=None, min_length=1, max_length=4096)
    encrypt_key: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool = False

    @field_validator("redirect_uri")
    @classmethod
    def https_redirect(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
            raise ValueError("redirect URI must be an absolute HTTPS URL without a fragment")
        return value


class FeishuAuthorize(StrictModel):
    organization_slug: str = Field(min_length=1, max_length=100)


class FreeBusyInput(StrictModel):
    open_ids: list[str] = Field(min_length=1, max_length=100)
    time_min: datetime
    time_max: datetime


class FeishuEventInput(StrictModel):
    organization_id: UUID
    verification_token: str = Field(min_length=1, max_length=4096)
    external_event_id: str = Field(min_length=1, max_length=512)
    provider_revision: str | None = Field(default=None, max_length=255)


def _principal(request: Request):
    token = session_token(request)
    if not token:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    try:
        return request.app.state.identity_service.principal(token)
    except InvalidSession:
        return problem(request, 401, "authentication_required", "Authentication is required.")


def _can_manage(principal) -> bool:
    return bool(principal.roles.intersection({"system_admin", "recruiting_admin"}))


def _credentials(request: Request, config: FeishuOrganizationConfig) -> FeishuCredentials:
    if config.encrypted_app_secret is None:
        raise ValueError("Feishu App Secret is not configured")
    return FeishuCredentials(
        config.app_id,
        request.app.state.feishu_secret_cipher.decrypt(config.encrypted_app_secret),
        config.redirect_uri,
        config.calendar_id,
    )


def _config_response(config: FeishuOrganizationConfig | None) -> JSONResponse:
    data = public_config(config) if config else {"configured": False, "enabled": False}
    if config:
        data = {"configured": True, **data}
    response = JSONResponse({"data": data})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/settings/integrations/feishu")
def get_config(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _can_manage(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == principal.organization_id))
        return _config_response(config)


@router.put("/settings/integrations/feishu")
def put_config(payload: FeishuConfigWrite, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _can_manage(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    cipher = request.app.state.feishu_secret_cipher
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == principal.organization_id).with_for_update())
        encrypted_secret = cipher.encrypt(payload.app_secret) if payload.app_secret is not None else (config.encrypted_app_secret if config else None)
        if payload.enabled and encrypted_secret is None:
            return problem(request, 422, "feishu_secret_required", "An App Secret is required before enabling Feishu.")
        encrypted_token = cipher.encrypt(payload.verification_token) if payload.verification_token is not None else (config.encrypted_verification_token if config else None)
        encrypted_key = cipher.encrypt(payload.encrypt_key) if payload.encrypt_key is not None else (config.encrypted_encrypt_key if config else None)
        if config is None:
            config = FeishuOrganizationConfig(
                organization_id=principal.organization_id,
                app_id=payload.app_id,
                encrypted_app_secret=encrypted_secret,
                redirect_uri=payload.redirect_uri,
                calendar_id=payload.calendar_id,
                encrypted_verification_token=encrypted_token,
                encrypted_encrypt_key=encrypted_key,
                enabled=payload.enabled,
                created_by=principal.user_id,
                updated_by=principal.user_id,
            )
            db.add(config)
        else:
            config.app_id = payload.app_id
            config.encrypted_app_secret = encrypted_secret
            config.redirect_uri = payload.redirect_uri
            config.calendar_id = payload.calendar_id
            config.encrypted_verification_token = encrypted_token
            config.encrypted_encrypt_key = encrypted_key
            config.enabled = payload.enabled
            config.updated_by = principal.user_id
            config.version += 1
        config.last_test_status = None
        config.last_tested_at = None
        config.last_test_error_code = None
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="feishu.config_updated", outcome="success", trace_id=request.state.trace_id, metadata_json={"enabled": payload.enabled}))
        db.commit()
        return _config_response(config)


@router.post("/settings/integrations/feishu/test")
def test_config(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if not _can_manage(principal):
        return problem(request, 403, "forbidden", "The operation is not permitted.")
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == principal.organization_id).with_for_update())
        if config is None or config.encrypted_app_secret is None:
            return problem(request, 409, "feishu_not_configured", "Feishu is not configured.")
        credentials = _credentials(request, config)
        result = request.app.state.feishu_provider.test_connection(credentials)
        config.last_test_status = "succeeded" if result.ok else "failed"
        config.last_tested_at = now
        config.last_test_error_code = result.safe_error_code
        db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="feishu.connection_tested", outcome=config.last_test_status, trace_id=request.state.trace_id, metadata_json={}))
        db.commit()
        response = _config_response(config)
        if not result.ok:
            response.status_code = 502
        return response


def _start_authorization(request: Request, organization_id: UUID, purpose: str, user_id: UUID | None):
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == organization_id))
        if config is None or not config.enabled or config.encrypted_app_secret is None:
            return problem(request, 409, "feishu_disabled", "Feishu login is not enabled for this organization.")
        state = secrets.token_urlsafe(32)
        db.add(FeishuOAuthState(state_hash=hash_oauth_state(state), organization_id=organization_id, initiating_user_id=user_id, purpose=purpose, expires_at=now + timedelta(minutes=10)))
        db.commit()
        authorization_url = request.app.state.feishu_provider.authorization_url(_credentials(request, config), state)
        response = JSONResponse({"data": {"authorization_url": authorization_url, "state": state}})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.post("/auth/feishu/authorize")
def authorize_login(payload: FeishuAuthorize, request: Request):
    with request.app.state.identity_store.sync_session() as db:
        organization = db.scalar(select(Organization).where(Organization.slug == payload.organization_slug, Organization.status == "active"))
        if organization is None:
            return problem(request, 409, "feishu_disabled", "Feishu login is not enabled for this organization.")
        organization_id = organization.id
    return _start_authorization(request, organization_id, "login", None)


@router.post("/me/integrations/feishu/authorize")
def authorize_binding(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    return _start_authorization(request, principal.organization_id, "bind", principal.user_id)


def _find_binding(db, organization_id: UUID, identity):
    clauses = []
    if identity.union_id:
        clauses.append(FeishuIdentityBinding.union_id == identity.union_id)
    if identity.open_id:
        clauses.append(FeishuIdentityBinding.open_id == identity.open_id)
    if not clauses:
        return None
    return db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.organization_id == organization_id, or_(*clauses)))


@router.get("/auth/feishu/callback")
def oauth_callback(request: Request, code: str = Query(min_length=1, max_length=1024), state: str = Query(min_length=20, max_length=512)):
    now = request.app.state.identity_service.clock.current_time()
    with request.app.state.identity_store.sync_session() as db:
        oauth_state = db.scalar(select(FeishuOAuthState).where(FeishuOAuthState.state_hash == hash_oauth_state(state)).with_for_update())
        if oauth_state is None or oauth_state.consumed_at is not None or oauth_state.expires_at.replace(tzinfo=oauth_state.expires_at.tzinfo or timezone.utc) <= now:
            return problem(request, 422, "oauth_state_invalid", "The OAuth state is invalid or expired.")
        oauth_state.consumed_at = now
        organization_id, purpose, initiating_user_id = oauth_state.organization_id, oauth_state.purpose, oauth_state.initiating_user_id
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == organization_id))
        if config is None or not config.enabled:
            db.commit()
            return problem(request, 409, "feishu_disabled", "Feishu login is not enabled for this organization.")
        credentials = _credentials(request, config)
        db.commit()
    try:
        identity = request.app.state.feishu_provider.exchange_code(credentials, code)
        if not identity.open_id:
            raise FeishuProviderError("feishu_identity_invalid", retryable=False)
    except FeishuProviderError as error:
        return problem(request, 502 if error.retryable else 422, error.safe_code, "Feishu authentication could not be completed.")

    if purpose == "bind":
        principal = _principal(request)
        if isinstance(principal, JSONResponse) or principal.organization_id != organization_id or principal.user_id != initiating_user_id:
            return problem(request, 403, "feishu_binding_session_mismatch", "The binding session no longer matches.")
        user_id = principal.user_id
    else:
        user_id = None

    try:
        with request.app.state.identity_store.sync_session() as db:
            existing = _find_binding(db, organization_id, identity)
            if purpose == "login":
                if existing is not None:
                    user = db.get(User, existing.user_id)
                    if user is None or user.status != UserStatus.ACTIVE:
                        return problem(request, 403, "feishu_account_unavailable", "The linked account is unavailable.")
                    user_id = user.id
                else:
                    normalized_email = identity.email.strip().casefold() if identity.email else None
                    eligible = db.scalar(
                        select(User)
                        .where(
                            User.organization_id == organization_id,
                            User.normalized_email == normalized_email,
                            User.status.in_([UserStatus.INVITED, UserStatus.ACTIVE]),
                        )
                        .with_for_update()
                    ) if normalized_email else None
                    if eligible is None:
                        return _app_redirect(error="feishu_account_not_invited_or_bound")
                    eligible.status = UserStatus.ACTIVE
                    user_id = eligible.id
            if existing is not None and existing.user_id != user_id:
                return problem(request, 409, "feishu_identity_already_bound", "The Feishu identity is already linked.")
            own_binding = db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.organization_id == organization_id, FeishuIdentityBinding.user_id == user_id).with_for_update())
            if own_binding is None:
                own_binding = FeishuIdentityBinding(organization_id=organization_id, user_id=user_id, union_id=identity.union_id, open_id=identity.open_id, tenant_key=identity.tenant_key)
                db.add(own_binding)
            else:
                own_binding.union_id = identity.union_id
                own_binding.open_id = identity.open_id
                own_binding.tenant_key = identity.tenant_key
            db.add(AuditLog(organization_id=organization_id, actor_user_id=user_id, event_type="feishu.account_bound", outcome="success", trace_id=request.state.trace_id, metadata_json={}))
            db.commit()
    except IntegrityError:
        return problem(request, 409, "feishu_identity_already_bound", "The Feishu identity is already linked.")

    if purpose == "bind":
        response = _app_redirect(status="bound")
    else:
        token, _csrf = request.app.state.identity_service.issue_session(user_id, trace_id=request.state.trace_id, network=request.client.host if request.client else None, event="authentication.feishu_login")
        response = _app_redirect(status="connected")
        response.set_cookie(cookie_name(request), token, httponly=True, secure=request.app.state.settings.environment == "production", samesite="lax", path="/")
    return response


@router.get("/me/integrations/feishu")
def binding_status(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        binding = db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.organization_id == principal.organization_id, FeishuIdentityBinding.user_id == principal.user_id))
        data = {"bound": False} if binding is None else {"bound": True, "union_id": binding.union_id, "open_id": binding.open_id}
        response = JSONResponse({"data": data})
        response.headers["Cache-Control"] = "no-store"
        return response


@router.delete("/me/integrations/feishu", status_code=204)
def unbind(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    with request.app.state.identity_store.sync_session() as db:
        binding = db.scalar(select(FeishuIdentityBinding).where(FeishuIdentityBinding.organization_id == principal.organization_id, FeishuIdentityBinding.user_id == principal.user_id).with_for_update())
        if binding is not None:
            db.delete(binding)
            db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, event_type="feishu.account_unbound", outcome="success", trace_id=request.state.trace_id, metadata_json={}))
        db.commit()
    return Response(status_code=204)


@router.post("/integrations/feishu/freebusy")
def freebusy(payload: FreeBusyInput, request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse):
        return principal
    if payload.time_min.tzinfo is None or payload.time_max.tzinfo is None or payload.time_min >= payload.time_max:
        return problem(request, 422, "validation_failed", "The freebusy time range is invalid.")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == principal.organization_id))
        if config is None or not config.enabled:
            return JSONResponse({"data": [], "meta": {"degraded": True, "reason": "feishu_disabled"}})
        credentials = _credentials(request, config)
    windows = []
    try:
        for provider_request in chunk_freebusy_requests(payload.open_ids, payload.time_min, payload.time_max):
            windows.extend(request.app.state.feishu_provider.batch_freebusy(credentials, provider_request))
    except FeishuProviderError:
        return JSONResponse({"data": [], "meta": {"degraded": True, "reason": "feishu_unavailable"}})
    return {"data": [{"open_id": item.user_id, "starts_at": item.starts_at.isoformat(), "ends_at": item.ends_at.isoformat()} for item in windows], "meta": {"degraded": False}}


@router.post("/integrations/feishu/events", status_code=202)
def provider_event(payload: FeishuEventInput, request: Request):
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(FeishuOrganizationConfig).where(FeishuOrganizationConfig.organization_id == payload.organization_id))
        if config is None or not config.enabled or config.encrypted_verification_token is None:
            return problem(request, 403, "feishu_event_verification_failed", "The event could not be verified.")
        expected = request.app.state.feishu_secret_cipher.decrypt(config.encrypted_verification_token)
        if not secrets.compare_digest(expected, payload.verification_token):
            return problem(request, 403, "feishu_event_verification_failed", "The event could not be verified.")
        found = mark_provider_change(db, payload.organization_id, payload.external_event_id, provider_revision=payload.provider_revision)
        db.commit()
        if not found:
            return Response(status_code=202)
    return Response(status_code=202)
