import re
import uuid
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, text

from server.app.communications.models import EmailDelivery, EmailProviderConfig, EmailTemplate
from server.app.communications.schemas import EmailConfigUpdate, EmailTemplateUpdate, EmailTestSend
from server.app.communications.service import DeliveryCommand, DeliveryIdempotencyConflict, SenderPolicy, enqueue_delivery, validate_template
from server.app.identity.api import problem
from server.app.identity.models import AuditLog
from server.app.identity.policy import Permission, require_permission
from server.app.recruiting.api import _idempotency, _principal
from server.app.recruiting.service import IdempotencyConflict, persisted_idempotent


router = APIRouter()


def _response(data, status=200, *, meta=None):
    body = {"data": data}
    if meta is not None: body["meta"] = meta
    response = JSONResponse(body, status_code=status); response.headers["Cache-Control"] = "no-store"
    return response


def _error(request, status, code):
    response = problem(request, status, code, "The request could not be completed."); response.headers["Cache-Control"] = "no-store"
    return response


def _system(principal): return require_permission(principal, Permission.MANAGE_SYSTEM)
def _recruiting_admin(principal): return principal.active and "recruiting_admin" in principal.roles


def _version(request, value):
    if value is None: return _error(request, 428, "precondition_required")
    match = re.fullmatch(r'^"(0|[1-9][0-9]*)"$', value)
    return int(match.group(1)) if match else _error(request, 422, "validation_failed")


def _config_view(config):
    if config is None:
        return {"configured": False, "host": None, "port": None, "tls_mode": None, "username": None, "password_masked": None, "enabled": False, "version": 0}
    return {"configured": True, "host": config.host, "port": config.port, "tls_mode": config.tls_mode, "username": config.username, "password_masked": "********", "enabled": config.enabled, "version": config.version}


def _delivery_view(row):
    return {"id": str(row.id), "recipient": row.recipient_masked, "subject": row.rendered_subject, "resource_type": row.resource_type, "resource_id": str(row.resource_id), "status": row.status, "attempts": row.attempts, "version": row.version, "safe_error_code": row.safe_error_code, "created_at": row.created_at.isoformat(), "sent_at": row.sent_at.isoformat() if row.sent_at else None, "failed_at": row.failed_at.isoformat() if row.failed_at else None}


def _sender_policy(request: Request) -> SenderPolicy:
    return SenderPolicy(request.app.state.settings.email_from_address, request.app.state.settings.email_from_name)


def _email_idempotency_body(request: Request, purpose: str, payload: object) -> dict[str, str]:
    return {"request_fingerprint": request.app.state.email_secret_cipher.fingerprint(purpose, payload)}


@router.get("/api/v1/settings/email")
def get_email_config(request: Request):
    principal = _principal(request)
    if isinstance(principal, JSONResponse): return principal
    if not _system(principal): return _error(request, 404, "resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        config = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.organization_id == principal.organization_id).order_by(EmailProviderConfig.version.desc()).limit(1))
        return _response(_config_view(config))


@router.put("/api/v1/settings/email")
def put_email_config(payload: EmailConfigUpdate, request: Request, if_match: str | None = Header(None), idempotency_key: str | None = Header(None)):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse): return principal
    if not _system(principal): return _error(request, 404, "resource_not_found")
    if isinstance(expected, JSONResponse): return expected
    if isinstance(key, JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                if db.bind.dialect.name == "postgresql":
                    db.execute(text("select pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": f"email-provider-config:{principal.organization_id}"})
                current = db.scalar(select(EmailProviderConfig).where(EmailProviderConfig.organization_id == principal.organization_id).order_by(EmailProviderConfig.version.desc()).limit(1).with_for_update())
                if (current.version if current else 0) != expected: raise RuntimeError("version")
                endpoint_changed = current is not None and (current.host, current.port, current.tls_mode, current.username) != (payload.host, payload.port, payload.tls_mode, payload.username)
                if payload.password is None and endpoint_changed:
                    raise ValueError("password_reentry_required")
                if payload.password is None and current is None:
                    raise ValueError("password_required")
                encrypted = request.app.state.email_secret_cipher.encrypt_smtp_password(payload.password) if payload.password is not None else current.encrypted_password
                config = EmailProviderConfig(
                    organization_id=principal.organization_id, host=payload.host, port=payload.port,
                    tls_mode=payload.tls_mode, username=payload.username, encrypted_password=encrypted,
                    enabled=payload.enabled, version=expected + 1, created_by=principal.user_id,
                    updated_by=principal.user_id,
                )
                db.add(config)
                db.flush(); db.add(AuditLog(organization_id=principal.organization_id, actor_user_id=principal.user_id, category="system", event_type="email.config_updated", outcome="success", resource_type="email_provider_config", resource_id=config.id, trace_id=request.state.trace_id, metadata_json={"enabled": config.enabled, "tls_mode": config.tls_mode}))
                return 200, {"data": _config_view(config)}
            semantic = {"expected_version": expected, **payload.model_dump()}
            status, body = persisted_idempotent(db, principal.organization_id, principal.user_id, "email.config.put", key, _email_idempotency_body(request, "email.config.put", semantic), action); db.commit()
        except IdempotencyConflict: db.rollback(); return _error(request, 409, "idempotency_conflict")
        except RuntimeError: db.rollback(); return _error(request, 409, "resource_version_conflict")
        except ValueError as error: db.rollback(); return _error(request, 422, str(error))
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; response.headers["ETag"]=f'"{body["data"]["version"]}"'; return response


@router.put("/api/v1/email-templates/{template_key}")
def put_template(template_key: str, payload: EmailTemplateUpdate, request: Request, if_match: str | None = Header(None), idempotency_key: str | None = Header(None)):
    principal, expected, key = _principal(request), _version(request, if_match), _idempotency(request, idempotency_key)
    if isinstance(principal, JSONResponse): return principal
    if not _recruiting_admin(principal): return _error(request, 404, "resource_not_found")
    if isinstance(expected, JSONResponse): return expected
    if isinstance(key, JSONResponse): return key
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,99}", template_key): return _error(request, 422, "validation_failed")
    try: validate_template(payload.subject_template, payload.body_template, payload.allowed_variables)
    except ValueError as error: return _error(request, 422, str(error))
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                template=db.scalar(select(EmailTemplate).where(EmailTemplate.organization_id==principal.organization_id,EmailTemplate.key==template_key).with_for_update())
                if (template.version if template else 0)!=expected: raise RuntimeError
                if template is None:
                    template=EmailTemplate(organization_id=principal.organization_id,key=template_key,subject_template=payload.subject_template,body_template=payload.body_template,variable_allowlist=payload.allowed_variables,enabled=payload.enabled,version=1,created_by=principal.user_id,updated_by=principal.user_id); db.add(template)
                else:
                    template.subject_template=payload.subject_template; template.body_template=payload.body_template; template.variable_allowlist=payload.allowed_variables; template.enabled=payload.enabled; template.updated_by=principal.user_id; template.version+=1
                db.flush(); return 200,{"data":{"id":str(template.id),"key":template.key,"subject_template":template.subject_template,"body_template":template.body_template,"allowed_variables":template.variable_allowlist,"enabled":template.enabled,"version":template.version}}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,f"email.template.put:{template_key}",key,payload.model_dump(),action); db.commit()
        except IdempotencyConflict: db.rollback(); return _error(request,409,"idempotency_conflict")
        except RuntimeError: db.rollback(); return _error(request,409,"resource_version_conflict")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response


@router.get("/api/v1/email-templates")
def list_templates(request: Request):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    if not _recruiting_admin(principal): return _error(request,404,"resource_not_found")
    with request.app.state.identity_store.sync_session() as db:
        rows=db.scalars(select(EmailTemplate).where(EmailTemplate.organization_id==principal.organization_id).order_by(EmailTemplate.key)).all()
        return _response([{"id":str(row.id),"key":row.key,"subject_template":row.subject_template,"body_template":row.body_template,"allowed_variables":row.variable_allowlist,"enabled":row.enabled,"version":row.version} for row in rows])


@router.post("/api/v1/settings/email/test")
def test_email_config(payload: EmailTestSend, request: Request, idempotency_key: str | None = Header(None)):
    principal,key=_principal(request),_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if not _system(principal): return _error(request,404,"resource_not_found")
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                delivery=enqueue_delivery(db,DeliveryCommand(organization_id=principal.organization_id,recipient=str(payload.recipient),reply_to_email=str(payload.reply_to_email),reply_to_name=payload.reply_to_name,subject="Transactional email test",body="This is a transactional email configuration test.",resource_type="email_test",resource_id=uuid.uuid4(),idempotency_key=key,operation="email.config.test",created_by=principal.user_id,trace_id=request.state.trace_id),cipher=request.app.state.email_secret_cipher,sender_policy=_sender_policy(request))
                return 202,{"data":_delivery_view(delivery)}
            semantic={"recipient":str(payload.recipient),"reply_to_email":str(payload.reply_to_email),"reply_to_name":payload.reply_to_name}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"email.config.test",key,_email_idempotency_body(request,"email.config.test",semantic),action); db.commit()
        except (IdempotencyConflict,DeliveryIdempotencyConflict): db.rollback(); return _error(request,409,"idempotency_conflict")
        except ValueError as error: db.rollback(); return _error(request,409,str(error))
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response


@router.get("/api/v1/email-deliveries")
def delivery_history(request: Request, limit: int=Query(50,ge=1,le=100), status: str|None=Query(None), cursor: uuid.UUID|None=Query(None)):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    if not _recruiting_admin(principal): return _error(request,404,"resource_not_found")
    if status not in {None,"queued","sent","failed"}: return _error(request,422,"validation_failed")
    with request.app.state.identity_store.sync_session() as db:
        query=select(EmailDelivery).where(EmailDelivery.organization_id==principal.organization_id)
        if status: query=query.where(EmailDelivery.status==status)
        if cursor is not None:
            boundary=db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id==principal.organization_id,EmailDelivery.id==cursor))
            if boundary is None: return _error(request,422,"validation_failed")
            query=query.where(or_(EmailDelivery.created_at<boundary.created_at,(EmailDelivery.created_at==boundary.created_at)&(EmailDelivery.id<boundary.id)))
        rows=db.scalars(query.order_by(EmailDelivery.created_at.desc(),EmailDelivery.id.desc()).limit(limit+1)).all()
        page=rows[:limit]
        return _response([_delivery_view(row) for row in page],meta={"limit":limit,"next_cursor":str(page[-1].id) if len(rows)>limit else None})


@router.post("/api/v1/email-deliveries/{delivery_id}/resend")
def resend_delivery(delivery_id: uuid.UUID,request:Request,if_match:str|None=Header(None),idempotency_key:str|None=Header(None)):
    principal,key,expected=_principal(request),_idempotency(request,idempotency_key),_version(request,if_match)
    if isinstance(principal,JSONResponse): return principal
    if not _recruiting_admin(principal): return _error(request,404,"resource_not_found")
    if isinstance(key,JSONResponse): return key
    if isinstance(expected,JSONResponse): return expected
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                original=db.scalar(select(EmailDelivery).where(EmailDelivery.organization_id==principal.organization_id,EmailDelivery.id==delivery_id).with_for_update())
                if original is None: raise LookupError
                if original.version != expected: raise RuntimeError("version")
                recipient=request.app.state.email_secret_cipher.decrypt_recipient(original.recipient_ciphertext)
                delivery=enqueue_delivery(db,DeliveryCommand(organization_id=principal.organization_id,recipient=recipient,reply_to_email=original.reply_to_email,reply_to_name=original.reply_to_name,subject=original.rendered_subject,body=original.rendered_body,resource_type=original.resource_type,resource_id=original.resource_id,idempotency_key=key,operation=f"email.delivery.resend:{delivery_id}",created_by=principal.user_id,template_id=original.template_id,template_version=original.template_version,parent_delivery_id=original.id,trace_id=request.state.trace_id),cipher=request.app.state.email_secret_cipher,sender_policy=_sender_policy(request))
                original.version += 1
                return 202,{"data":_delivery_view(delivery)}
            semantic={"delivery_id":str(delivery_id),"expected_version":expected}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,f"email.delivery.resend:{delivery_id}",key,_email_idempotency_body(request,"email.delivery.resend",semantic),action); db.commit()
        except (IdempotencyConflict,DeliveryIdempotencyConflict): db.rollback(); return _error(request,409,"idempotency_conflict")
        except RuntimeError: db.rollback(); return _error(request,409,"resource_version_conflict")
        except (LookupError,ValueError): db.rollback(); return _error(request,404,"resource_not_found")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response
