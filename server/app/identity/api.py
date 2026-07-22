from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from server.app.identity.service import (
    AccountTemporarilyLocked,
    AuthenticationFailed,
    CsrfFailed,
    CurrentPasswordInvalid,
    IdentityService,
    InvalidSession,
    InvitationInvalidOrExpired,
    PasswordUnchanged,
)


PRODUCTION_COOKIE_NAME = "__Host-hr_session"
DEVELOPMENT_COOKIE_NAME = "hr_session"
router = APIRouter(prefix="/api/v1")


class LoginRequest(BaseModel):
    organization_slug: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("email")
    @classmethod
    def validate_email_shape(cls, value: str) -> str:
        if value.count("@") != 1 or not all(value.split("@")):
            raise ValueError("invalid email address")
        return value


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    password: str = Field(min_length=12, max_length=128)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


def problem(
    request: Request,
    status: int,
    code: str,
    detail: str,
    *,
    extra: dict | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    content = {"type": "about:blank", "title": "Request denied", "status": status, "detail": detail, "code": code, "trace_id": request.state.trace_id, "errors": []}
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=content,
        headers=headers,
    )


def allowed_origin(request: Request) -> bool:
    return request.headers.get("origin") in request.app.state.settings.cors_origins


def cookie_name(request: Request) -> str:
    return PRODUCTION_COOKIE_NAME if request.app.state.settings.environment == "production" else DEVELOPMENT_COOKIE_NAME


def session_token(request: Request) -> str | None:
    return request.cookies.get(cookie_name(request))


@router.get("/auth/config")
def auth_config(request: Request):
    settings = request.app.state.settings
    default_organization = None
    if settings.default_organization_slug is not None:
        default_organization = {
            "slug": settings.default_organization_slug,
            "name": settings.default_organization_name,
        }
    return {"data": {"default_organization": default_organization}}


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request):
    if not allowed_origin(request):
        return problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
    service: IdentityService = request.app.state.identity_service
    try:
        session_token, csrf = service.login(payload.organization_slug, payload.email, payload.password, trace_id=request.state.trace_id, network=request.headers.get("x-real-ip") or request.client.host if request.client else None)
    except AccountTemporarilyLocked as error:
        retry_after = error.retry_after_seconds
        return problem(
            request,
            429,
            "account_temporarily_locked",
            "Too many failed login attempts.",
            extra={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )
    except AuthenticationFailed:
        return problem(request, 401, "authentication_failed", "Invalid credentials or account unavailable.")
    response = JSONResponse({"data": {"authenticated": True}})
    response.set_cookie(cookie_name(request), session_token, httponly=True, secure=request.app.state.settings.environment == "production", samesite="lax", path="/")
    response.headers["X-CSRF-Token"] = csrf
    return response


@router.post("/auth/invitations/accept")
def accept_invitation(payload: InvitationAcceptRequest, request: Request):
    if not allowed_origin(request):
        return problem(
            request,
            403,
            "csrf_validation_failed",
            "Request origin or CSRF token is invalid.",
        )
    try:
        registration = request.app.state.identity_service.accept_password_invitation(
            payload.token, payload.password, trace_id=request.state.trace_id
        )
    except InvitationInvalidOrExpired:
        return problem(
            request,
            422,
            "invitation_invalid_or_expired",
            "The invitation is invalid or expired.",
        )
    return {"data": registration}


@router.get("/me")
def me(request: Request):
    if request.headers.get("sec-fetch-site") not in {"same-origin", "same-site"}:
        return problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
    if request.headers.get("origin") is not None and not allowed_origin(request):
        return problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
    token = session_token(request)
    if not token:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    try:
        data, csrf = request.app.state.identity_service.me(token)
    except InvalidSession:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    response = JSONResponse({"data": data})
    response.headers["X-CSRF-Token"] = csrf
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout", status_code=204)
def logout(request: Request):
    token = session_token(request)
    csrf = request.headers.get("x-csrf-token")
    if not token or not csrf or not allowed_origin(request):
        return problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
    try:
        request.app.state.identity_service.logout(token, csrf, trace_id=request.state.trace_id, network=request.headers.get("x-real-ip") or request.client.host if request.client else None)
    except CsrfFailed:
        return problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
    except InvalidSession:
        return problem(request, 401, "authentication_required", "Authentication is required.")
    response = Response(status_code=204)
    response.delete_cookie(cookie_name(request), path="/", secure=request.app.state.settings.environment == "production", httponly=True, samesite="lax")
    return response


@router.post("/me/password", status_code=204)
def change_password(payload: PasswordChangeRequest, request: Request):
    token = session_token(request)
    if not token:
        return problem(
            request, 401, "authentication_required", "Authentication is required."
        )
    try:
        request.app.state.identity_service.change_password(
            token,
            payload.current_password,
            payload.new_password,
            trace_id=request.state.trace_id,
            network=request.headers.get("x-real-ip")
            or (request.client.host if request.client else None),
        )
    except InvalidSession:
        return problem(
            request, 401, "authentication_required", "Authentication is required."
        )
    except CurrentPasswordInvalid:
        return problem(
            request,
            422,
            "current_password_invalid",
            "The current password is invalid.",
        )
    except PasswordUnchanged:
        return problem(
            request,
            422,
            "password_unchanged",
            "The new password must be different.",
        )
    return Response(status_code=204)
