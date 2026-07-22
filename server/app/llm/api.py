import hashlib,json,re
from datetime import datetime,timezone
from fastapi import APIRouter,Header,Request
from fastapi.responses import JSONResponse
from sqlalchemy import func,select
from server.app.identity.api import problem
from server.app.identity.models import AuditLog,Job
from server.app.identity.policy import Permission,require_permission
from server.app.llm.gateway import GatewayError
from server.app.llm.models import LlmInvocation,LlmProvider,LlmProviderConfig
from server.app.llm.policy import ProviderAllowlist,ProviderPolicyError
from server.app.llm.schemas import LlmConfigResource,LlmConfigUpdate,LlmProviderCollection,LlmProviderCreate,LlmProviderResource,LlmTestResource
from server.app.recruiting.api import _idempotency,_principal
from server.app.recruiting.models import IdempotencyRecord
from server.app.recruiting.service import IdempotencyConflict,persisted_idempotent

router=APIRouter(prefix="/api/v1/settings/llm")
def _response(data,status=200):
    response=JSONResponse({"data":data},status_code=status); response.headers["Cache-Control"]="no-store"; return response
def _error(request,status,code):
    response=problem(request,status,code,"The request could not be completed."); response.headers["Cache-Control"]="no-store"; return response
def _system(principal): return require_permission(principal,Permission.MANAGE_SYSTEM)
def _reader(principal): return _system(principal) or (principal.active and "recruiting_admin" in principal.roles)
def _view(config,*,system,available=None,options=None):
    if config is None: return {"configured":False,"enabled":False,"provider_id":None,"model":None,"version":0,"last_test_status":None,"last_test_error_code":None,"last_test_latency_ms":None,"last_tested_at":None,**({"key_configured":False,"allowed_job_ids":[],"available_providers":available or {},"provider_options":options or []} if system else {})}
    data={"configured":True,"enabled":config.enabled,"provider_id":config.provider_id,"model":config.model,"version":config.version,"last_test_status":config.last_test_status,"last_test_error_code":config.last_test_error_code,"last_test_latency_ms":config.last_test_latency_ms,"last_tested_at":config.last_tested_at.isoformat() if config.last_tested_at else None}
    if system: data.update({"key_configured":config.encrypted_api_key is not None,"allowed_job_ids":config.allowed_job_ids,"available_providers":available or {},"provider_options":options or []})
    return data
def _catalog(request,organization_id):
    allowlist=request.app.state.llm_allowlist; available=allowlist.public_view(organization_id)
    options=allowlist.options(organization_id) if hasattr(allowlist,"options") else [{"provider_id":provider_id,"display_name":provider_id,"base_url":None,"models":models,"source":"deployment"} for provider_id,models in available.items()]
    return available,options
def _request_hash(body): return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
def _config_version(request,value):
    if value is None: return _error(request,428,"precondition_required")
    match=re.fullmatch(r'^"(0|[1-9][0-9]*)"$',value)
    return int(match.group(1)) if match else _error(request,422,"validation_failed")

@router.get("",response_model=LlmConfigResource)
def get_config(request:Request):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    if not _reader(principal): return _error(request,404,"resource_not_found")
    with request.app.state.identity_store.sync_session() as db: config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==principal.organization_id))
    available,options=_catalog(request,principal.organization_id)
    return _response(_view(config,system=_system(principal),available=available,options=options))

@router.get("/providers",response_model=LlmProviderCollection)
def get_providers(request:Request):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    if not _system(principal): return _error(request,404,"resource_not_found")
    return _response(request.app.state.llm_allowlist.options(principal.organization_id))

@router.post("/providers",response_model=LlmProviderResource,status_code=201)
def create_provider(payload:LlmProviderCreate,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if not _system(principal): return _error(request,404,"resource_not_found")
    if isinstance(key,JSONResponse): return key
    try:
        ProviderAllowlist({payload.provider_id:{"base_url":payload.base_url,"models":payload.models}},allow_http=request.app.state.settings.environment!="production")
        deployed=request.app.state.llm_allowlist.deployed
        if payload.provider_id in deployed.public_view(): return _error(request,409,"provider_already_exists")
    except ProviderPolicyError as error:
        if str(error)!="provider_or_model_not_allowed": return _error(request,422,str(error))
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                if db.scalar(select(LlmProvider.id).where(LlmProvider.organization_id==principal.organization_id,LlmProvider.provider_id==payload.provider_id)) is not None: raise ValueError("provider_already_exists")
                row=LlmProvider(organization_id=principal.organization_id,provider_id=payload.provider_id,display_name=payload.display_name,base_url=payload.base_url,models=payload.models,created_by=principal.user_id); db.add(row); db.flush()
                db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="llm.provider_created",outcome="success",trace_id=request.state.trace_id,metadata_json={"provider_id":row.provider_id}))
                return 201,{"data":{"provider_id":row.provider_id,"display_name":row.display_name,"base_url":row.base_url,"models":row.models,"source":"organization"}}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"llm.provider.post",key,payload.model_dump(),action); db.commit()
        except IdempotencyConflict: db.rollback(); return _error(request,409,"idempotency_conflict")
        except ValueError: db.rollback(); return _error(request,409,"provider_already_exists")
        except Exception: db.rollback(); return _error(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.put("",response_model=LlmConfigResource)
def put_config(payload:LlmConfigUpdate,request:Request,if_match:str|None=Header(None),idempotency_key:str|None=Header(None)):
    principal=_principal(request); expected=_config_version(request,if_match); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if not _system(principal): return _error(request,404,"resource_not_found")
    if isinstance(expected,JSONResponse): return expected
    if isinstance(key,JSONResponse): return key
    try: request.app.state.llm_allowlist.require(payload.provider_id,payload.model,organization_id=principal.organization_id)
    except ProviderPolicyError: return _error(request,422,"provider_or_model_not_allowed")
    with request.app.state.identity_store.sync_session() as db:
        if payload.allowed_job_ids:
            count=db.scalar(select(func.count(Job.id)).where(Job.organization_id==principal.organization_id,Job.id.in_(payload.allowed_job_ids)))
            if count!=len(payload.allowed_job_ids): return _error(request,422,"validation_failed")
        try:
            def action():
                config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==principal.organization_id).with_for_update()); current=config.version if config else 0
                if current!=expected: raise RuntimeError("version")
                encrypted=request.app.state.llm_key_cipher.encrypt(payload.api_key) if payload.api_key is not None else (config.encrypted_api_key if config else None)
                if payload.enabled and encrypted is None: raise ValueError("key")
                if config is None: config=LlmProviderConfig(organization_id=principal.organization_id,provider_id=payload.provider_id,model=payload.model,encrypted_api_key=encrypted,enabled=payload.enabled,allowed_job_ids=[str(value) for value in payload.allowed_job_ids],version=1,created_by=principal.user_id,updated_by=principal.user_id); db.add(config)
                else: config.provider_id=payload.provider_id; config.model=payload.model; config.encrypted_api_key=encrypted; config.enabled=payload.enabled; config.allowed_job_ids=[str(value) for value in payload.allowed_job_ids]; config.updated_by=principal.user_id; config.version+=1; config.updated_at=datetime.now(timezone.utc)
                db.flush(); available,options=_catalog(request,principal.organization_id); db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="llm.config_updated",outcome="success",trace_id=request.state.trace_id,metadata_json={"config_id":str(config.id),"enabled":config.enabled})); return 200,{"data":_view(config,system=True,available=available,options=options)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"llm.config.put",key,payload.model_dump(),action); db.commit()
        except IdempotencyConflict: db.rollback(); return _error(request,409,"idempotency_conflict")
        except RuntimeError: db.rollback(); return _error(request,409,"resource_version_conflict")
        except ValueError: db.rollback(); return _error(request,422,"api_key_required")
        except Exception: db.rollback(); return _error(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.post("/test",response_model=LlmTestResource)
async def test_config(request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if not _system(principal): return _error(request,404,"resource_not_found")
    if isinstance(key,JSONResponse): return key
    operation="llm.config.test"; fingerprint={"fixed_probe_version":"v1"}
    with request.app.state.identity_store.sync_session() as db:
        previous=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.organization_id==principal.organization_id,IdempotencyRecord.user_id==principal.user_id,IdempotencyRecord.operation==operation,IdempotencyRecord.idempotency_key==key))
        if previous:
            if previous.request_hash!=_request_hash(fingerprint): return _error(request,409,"idempotency_conflict")
            response=JSONResponse(previous.response_json,status_code=previous.status_code); response.headers["Cache-Control"]="no-store"; return response
        config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==principal.organization_id))
        if config is None or config.encrypted_api_key is None: return _error(request,409,"llm_not_configured")
        config_id,provider_id,model=config.id,config.provider_id,config.model
        try: api_key=request.app.state.llm_key_cipher.decrypt(config.encrypted_api_key)
        except ValueError: return _error(request,503,"llm_key_unavailable")
    safe_code=None; latency=None
    try: latency=await request.app.state.llm_gateway.test_connection(provider_id,model,api_key,organization_id=principal.organization_id); status_code=200; data={"status":"succeeded","safe_error_code":None,"latency_ms":latency}
    except GatewayError as error: safe_code=error.safe_code; status_code=422; data={"status":"failed","safe_error_code":safe_code,"latency_ms":None}
    with request.app.state.identity_store.sync_session() as db:
        try:
            def action():
                config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==principal.organization_id,LlmProviderConfig.id==config_id).with_for_update())
                if config is None: raise RuntimeError
                config.last_test_status=data["status"]; config.last_test_error_code=safe_code; config.last_test_latency_ms=latency; config.last_tested_at=datetime.now(timezone.utc)
                db.add(LlmInvocation(organization_id=principal.organization_id,config_id=config.id,provider_id=provider_id,model=model,request_field_manifest=["fixed_probe"],status=data["status"],latency_ms=latency,usage={},safe_error_code=safe_code,trace_id=request.state.trace_id)); db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="llm.connection_tested",outcome=data["status"],trace_id=request.state.trace_id,metadata_json={"config_id":str(config.id),"safe_error_code":safe_code})); return status_code,{"data":data}
            stored_status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,operation,key,fingerprint,action); db.commit()
        except IdempotencyConflict: db.rollback(); return _error(request,409,"idempotency_conflict")
        except Exception: db.rollback(); return _error(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=stored_status); response.headers["Cache-Control"]="no-store"; return response
