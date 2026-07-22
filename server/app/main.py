import asyncio
import base64
import logging
import re
import secrets
from contextlib import asynccontextmanager
from time import perf_counter
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from server.app.core.logging import configure_logging
from server.app.core.probes import ReadinessProbe, check_readiness
from server.app.core.settings import Settings
from server.app.observability.http_metrics import (
    HttpMetrics,
    InstrumentedReadinessProbe,
    method_label,
    route_template,
)
from server.app.identity.api import allowed_origin, problem, router as identity_router, session_token
from server.app.identity.admin_api import router as identity_admin_router
from server.app.identity.service import Clock, IdentityService, TokenSource
from server.app.identity.store import IdentityStore
from server.app.recruiting.api import router as recruiting_router
from server.app.recruiting.cursor import CursorCodec
from server.app.recruiting.security import ContactCipher
from server.app.recruiting.service import SystemClock, SystemTokens
from server.app.recruiting.storage import MinioResumeStorage
from server.app.recruiting.http import derive_cursor_key
from server.app.talent.api import router as talent_router
from server.app.reports.api import router as reports_router
from server.app.governance.api import router as governance_router
from server.app.governance.service import GovernanceTokenCodec, derive_governance_key


TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
logger = logging.getLogger(__name__)


def _is_governance_path(path: str) -> bool:
    return path == "/api/v1/audit-logs" or path.startswith("/api/v1/audit-logs/") or path == "/api/v1/settings/retention-policy" or path.startswith("/api/v1/settings/retention-policy/")


def _requires_no_store(path: str) -> bool:
    return _is_governance_path(path) or path.startswith("/api/v1/notifications/") or path.startswith("/api/v1/auth/feishu") or path == "/api/v1/settings" or path.startswith(
        "/api/v1/settings/"
    )


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _contact_lookup_key(value: str) -> bytes:
    raw = value.encode()
    if len(raw) == 32:
        return raw
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except (ValueError, base64.binascii.Error):
        raise ValueError("invalid contact lookup secret") from None
    if len(decoded) != 32:
        raise ValueError("contact lookup secret must decode to 32 bytes")
    return decoded


def create_app(
    settings: Settings | None = None,
    *,
    database_probe: ReadinessProbe | None = None,
    storage_probe: ReadinessProbe | None = None,
    clock: Clock | None = None,
    token_source: TokenSource | None = None,
    initialize_identity_schema: bool = False,
    resume_storage=None,
    quarantine_storage=None,
    export_storage=None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    from server.app.governance.orm import register_governance_orm

    register_governance_orm()

    if database_probe is None or storage_probe is None:
        from server.app.core.storage import ObjectStorageProbe, create_storage_client
        from server.app.db.session import DatabaseProbe, create_engine

        database_probe = database_probe or DatabaseProbe(create_engine(settings.database_url))
        storage_client = create_storage_client(
                settings.object_storage_endpoint,
                settings.object_storage_access_key,
                settings.object_storage_secret_key,
                secure=settings.object_storage_secure,
                connect_timeout_seconds=settings.object_storage_connect_timeout_seconds,
                read_timeout_seconds=settings.object_storage_read_timeout_seconds,
                total_timeout_seconds=settings.object_storage_total_timeout_seconds,
            )
        storage_probe = storage_probe or ObjectStorageProbe(storage_client, settings.object_storage_bucket)
        resume_storage = resume_storage or MinioResumeStorage(storage_client, settings.object_storage_bucket)
        if export_storage is None:
            from server.app.reports.storage import MinioExportStorage
            export_storage = MinioExportStorage(storage_client, settings.object_storage_bucket)
        if quarantine_storage is None:
            from server.app.screening.storage import QuarantineStorage
            quarantine_storage = QuarantineStorage(storage_client, settings.object_storage_bucket)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        if initialize_identity_schema:
            app.state.identity_store.create_schema()
        yield

    app = FastAPI(title="UX-09 Recruiting API", lifespan=lifespan)
    http_metrics = HttpMetrics()
    app.state.http_metrics = http_metrics
    app.state.settings = settings
    app.state.identity_store = IdentityStore(settings.database_url)
    app.state.identity_service = IdentityService(
        app.state.identity_store, clock or Clock(), token_source or TokenSource()
    )
    app.state.recruiting_clock = clock or SystemClock()
    app.state.recruiting_tokens = SystemTokens()
    cursor_secret = settings.contact_lookup_secret.get_secret_value()
    cursor_source = cursor_secret.encode() if cursor_secret != "change-me" else b"test-only-cursor-signing-boundary"
    app.state.recruiting_cursor = CursorCodec(derive_cursor_key(cursor_source))
    app.state.governance_audit_cursor = GovernanceTokenCodec(
        derive_governance_key(cursor_source, "audit-cursor")
    )
    app.state.governance_retention_preview = GovernanceTokenCodec(
        derive_governance_key(cursor_source, "retention-preview")
    )
    app.state.contact_cipher = ContactCipher(
        settings.contact_encryption_key.get_secret_value().encode(),
        _contact_lookup_key(settings.contact_lookup_secret.get_secret_value()),
    ) if settings.contact_encryption_key.get_secret_value() != "change-me" else ContactCipher(
        b"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=", b"fedcba9876543210fedcba9876543210"
    )
    app.state.resume_storage = resume_storage
    app.state.quarantine_storage = quarantine_storage
    app.state.export_storage = export_storage
    from server.app.llm.gateway import OpenAiCompatibleGateway
    from server.app.llm.policy import ProviderAllowlist
    from server.app.llm.security import ApiKeyCipher
    llm_key=settings.llm_config_encryption_key.get_secret_value()
    if llm_key=="change-me": llm_key="QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl8="
    app.state.llm_key_cipher=ApiKeyCipher(llm_key.encode())
    app.state.ocr_key_cipher=app.state.llm_key_cipher
    from server.app.llm.registry import DatabaseProviderCatalog
    deployed_llm_allowlist=ProviderAllowlist(settings.llm_provider_allowlist,allow_http=settings.environment!="production")
    app.state.llm_allowlist=DatabaseProviderCatalog(app.state.identity_store.sync_session,deployed_llm_allowlist,allow_http=settings.environment!="production")
    app.state.llm_gateway=OpenAiCompatibleGateway(app.state.llm_allowlist)
    from server.app.ocr.gateway import OcrGateway
    app.state.ocr_gateway=OcrGateway()
    from server.app.integrations.feishu.provider import HttpFeishuProvider
    from server.app.integrations.feishu.service import FeishuSecretCipher
    feishu_key = settings.feishu_config_encryption_key.get_secret_value()
    if feishu_key == "change-me":
        feishu_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    app.state.feishu_secret_cipher = FeishuSecretCipher(feishu_key.encode())
    app.state.feishu_provider = HttpFeishuProvider()
    from server.app.integrations.feishu.availability import FeishuAwareAvailabilityProvider
    from server.app.interviews.availability import INTERNAL_AVAILABILITY_PROVIDER
    app.state.interview_availability_provider = FeishuAwareAvailabilityProvider(
        INTERNAL_AVAILABILITY_PROVIDER,
        app.state.feishu_provider,
        app.state.feishu_secret_cipher,
    )
    app.include_router(identity_router)
    app.include_router(identity_admin_router)
    app.include_router(recruiting_router)
    from server.app.screening.api import router as screening_router
    app.include_router(screening_router)
    from server.app.llm.api import router as llm_router
    app.include_router(llm_router)
    from server.app.ocr.api import router as ocr_router
    app.include_router(ocr_router)
    from server.app.interviews.api import router as interview_router
    app.include_router(interview_router)
    from server.app.notifications.api import router as notification_router
    app.include_router(notification_router)
    app.include_router(talent_router)
    app.include_router(reports_router)
    app.include_router(governance_router)
    from server.app.integrations.feishu.api import router as feishu_router
    app.include_router(feishu_router)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, _: RequestValidationError):
        response = problem(request, 422, "validation_failed", "The request is invalid.")
        if _requires_no_store(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = perf_counter()
        supplied = request.headers.get("x-trace-id", "")
        trace_id = supplied if TRACE_ID_PATTERN.fullmatch(supplied) else _new_trace_id()
        request.state.trace_id = trace_id
        response = None
        if request.url.path.startswith("/api/v1") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            service: IdentityService = request.app.state.identity_service
            network = request.headers.get("x-real-ip") or (request.client.host if request.client else None)
            provider_authenticated = request.url.path == "/api/v1/integrations/feishu/events"
            if not provider_authenticated and not allowed_origin(request):
                event = "authentication.logout" if request.url.path == "/api/v1/auth/logout" else "csrf.denied"
                audited = service.audit_denial(event, token=session_token(request), trace_id=trace_id, network=network)
                if not audited:
                    logger.info("anonymous_csrf_denied", extra={"context": {"trace_id": trace_id}})
                response = problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
            if response is None and not provider_authenticated and request.url.path not in {
                "/api/v1/auth/login",
                "/api/v1/auth/invitations/accept",
                "/api/v1/auth/feishu/authorize",
            }:
                token = session_token(request)
                csrf = request.headers.get("x-csrf-token")
                if not token or not csrf or not service.validate_csrf(token, csrf, trace_id=trace_id, network=network):
                    event = "authentication.logout" if request.url.path == "/api/v1/auth/logout" else "csrf.denied"
                    audited = service.audit_denial(event, token=token, trace_id=trace_id, network=network)
                    if not audited:
                        logger.info("anonymous_csrf_denied", extra={"context": {"trace_id": trace_id}})
                    response = problem(request, 403, "csrf_validation_failed", "Request origin or CSRF token is invalid.")
        if response is None:
            try:
                response = await call_next(request)
            except Exception as error:
                route = route_template(request)
                http_metrics.observe_request(
                    method=request.method,
                    route=route,
                    status_code=500,
                    duration_seconds=perf_counter() - started,
                )
                logger.exception(
                    "request_failed",
                    extra={
                        "context": {
                            "trace_id": trace_id,
                            "method": method_label(request.method),
                            "route": route,
                            "error_type": type(error).__name__,
                        }
                    },
                )
                raise
        route = route_template(request)
        http_metrics.observe_request(
            method=request.method,
            route=route,
            status_code=response.status_code,
            duration_seconds=perf_counter() - started,
        )
        if _requires_no_store(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Trace-ID"] = trace_id
        logger.info(
            "request_complete",
            extra={
                "context": {
                    "trace_id": trace_id,
                    "method": method_label(request.method),
                    "route": route,
                    "status_code": response.status_code,
                }
            },
        )
        return response

    @app.middleware("http")
    async def governance_no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _is_governance_path(request.url.path):
            return await call_next(request)
        try:
            response = await call_next(request)
        except Exception as error:
            trace_id = getattr(request.state, "trace_id", _new_trace_id())
            logger.exception(
                "governance_request_failed",
                extra={"context": {"trace_id": trace_id, "error_type": type(error).__name__}},
            )
            request.state.trace_id = trace_id
            response = problem(
                request,
                500,
                "internal_error",
                "The request could not be completed.",
            )
            response.headers["X-Trace-ID"] = trace_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=generate_latest(http_metrics.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    @app.get("/health/ready")
    async def ready(request: Request):  # type: ignore[no-untyped-def]
        try:
            await asyncio.wait_for(
                check_readiness(
                    InstrumentedReadinessProbe("database", database_probe, http_metrics),
                    InstrumentedReadinessProbe("storage", storage_probe, http_metrics),
                ),
                timeout=settings.readiness_timeout_seconds,
            )
        except Exception as error:
            logger.warning(
                "dependency_readiness_failed",
                extra={
                    "context": {
                        "trace_id": request.state.trace_id,
                        "error_type": type(error).__name__,
                    }
                },
            )
            return JSONResponse(
                status_code=503,
                media_type="application/problem+json",
                content={
                    "type": "about:blank",
                    "title": "Service unavailable",
                    "status": 503,
                    "detail": "Required dependencies are unavailable.",
                    "code": "dependencies_unavailable",
                    "trace_id": request.state.trace_id,
                    "errors": [],
                },
            )
        return {"status": "ready"}

    return app
