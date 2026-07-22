import hashlib,json,re,uuid
from datetime import datetime,timezone
from urllib.parse import unquote
from pathlib import PurePath
from tempfile import SpooledTemporaryFile
from uuid import UUID
from fastapi import APIRouter,File,Header,Query,Request,UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import and_,case,exists,func,or_,select
from server.app.identity.models import AuditLog,Job,User
from server.app.recruiting.api import AUTH,_denied,_idempotency,_job_scope,_load_job,_principal,_problem_for,_validated_terminal_routes
from server.app.recruiting.authorization import RecruitingAction
from server.app.recruiting.models import Application,Candidate,FileObject,IdempotencyRecord,JobJdVersion,Resume,ScreeningRuleVersion
from server.app.recruiting.tasks import normalize_llm_terminal_safe_error_code
from server.app.recruiting.service import IdempotencyConflict,persisted_idempotent
from server.app.screening.models import CandidateDuplicateHint,ScreeningItem,ScreeningResult,ScreeningRun
from server.app.llm.models import LlmProviderConfig,LlmScreeningEvaluation,PromptVersion
from server.app.screening.parsers import ParserError,ParserLimits,validate_upload_preflight
from server.app.screening.schemas import BulkAction,BulkResource,ItemCollection,ItemResource,RetryResource,RunCollection,RunCreate,RunResource
from server.app.screening.storage import StorageWriteFailed
from server.app.queue.repository import QueueRepository
from server.app.screening.actions import CandidateTombstoned,ScreeningBulkConflict,ScreeningItemNotRetryable,ScreeningRetryActive,apply_bulk_action,is_llm_retryable,retry_screening_item,RECOVERABLE_CODES

router=APIRouter(prefix="/api/v1"); _SAFE_STATUS={"queued","parsing","parsed","scoring","scored","failed","cancelled"}
class RunNotQueued(Exception): pass
class ScreeningItemLimit(Exception): pass
class ScreeningRunEmpty(Exception): pass
class ScreeningRunAlreadyStarted(Exception): pass
class ScreeningRunNotCancellable(Exception): pass
def _response(data,status=200):
    response=JSONResponse({"data":data},status_code=status); response.headers["Cache-Control"]="no-store"; return response
def _problem(request,status,code):
    from server.app.identity.api import problem
    response=problem(request,status,code,"The request could not be completed."); response.headers["Cache-Control"]="no-store"; return response
def _not_found(request): return _problem(request,404,"resource_not_found")
_REVIEW_APPROVED_STAGES=("contact","interview_pending","interviewing","decision","passed","hired")
_REVIEW_REJECTED_STAGES=("rejected","withdrawn")
def _empty_review_progress(): return {"review_total_count":0,"reviewed_count":0,"review_pending_count":0,"review_approved_count":0,"review_rejected_count":0,"review_status":"not_applicable"}
def _empty_route_summary(): return {"manager_review_count":0,"deferred_count":0,"ai_unavailable_count":0,"file_failed_count":0}
def _route_summaries(db,organization_id,run_ids):
    run_ids=list(run_ids)
    if not run_ids: return {}
    rows=db.execute(select(ScreeningItem.run_id,ScreeningItem.status,Application.id).outerjoin(Application,and_(Application.organization_id==ScreeningItem.organization_id,Application.id==ScreeningItem.application_id)).where(ScreeningItem.organization_id==organization_id,ScreeningItem.run_id.in_(run_ids))).all()
    routes=_validated_terminal_routes(db,organization_id,[application_id for _,_,application_id in rows if application_id is not None])
    summaries={run_id:_empty_route_summary() for run_id in run_ids}
    for run_id,item_status,application_id in rows:
        summary=summaries[run_id]
        route=routes.get(application_id)
        if route and route["route_result"]=="review": summary["manager_review_count"]+=1
        if route and route["route_result"]=="deferred": summary["deferred_count"]+=1
        if route and route["ai_status"]=="failed": summary["ai_unavailable_count"]+=1
        if item_status=="failed": summary["file_failed_count"]+=1
    return summaries
def _review_progress(db,organization_id,run_ids):
    run_ids=list(run_ids)
    if not run_ids: return {}
    reviewed=or_(Application.human_conclusion.is_not(None),Application.stage.not_in(("new","review")))
    rows=db.execute(select(ScreeningItem.run_id,func.count(Application.id),func.sum(case((reviewed,1),else_=0)),func.sum(case((Application.stage.in_(_REVIEW_APPROVED_STAGES),1),else_=0)),func.sum(case((Application.stage.in_(_REVIEW_REJECTED_STAGES),1),else_=0))).join(Application,and_(Application.organization_id==ScreeningItem.organization_id,Application.id==ScreeningItem.application_id)).where(ScreeningItem.organization_id==organization_id,ScreeningItem.run_id.in_(run_ids),ScreeningItem.status=="scored").group_by(ScreeningItem.run_id)).all()
    result={}
    for run_id,total,done,approved,rejected in rows:
        total=int(total or 0); done=int(done or 0); approved=int(approved or 0); rejected=int(rejected or 0)
        status="completed" if total and done>=total else "in_progress" if done else "pending" if total else "not_applicable"
        result[run_id]={"review_total_count":total,"reviewed_count":done,"review_pending_count":max(0,total-done),"review_approved_count":approved,"review_rejected_count":rejected,"review_status":status}
    return result
def _run_data(run,error_summary=None,job_title=None,created_by_name=None,review_progress=None,route_summary=None):
    data={"id":str(run.id),"job_id":str(run.job_id),"jd_version_id":str(run.jd_version_id),"rule_version_id":str(run.rule_version_id),"source":run.source,"status":run.status,"total_count":run.total_count,"processed_count":run.processed_count,"succeeded_count":run.succeeded_count,"failed_count":run.failed_count,"version":run.version,"created_at":run.created_at.isoformat(),"error_summary":error_summary or {}}
    if job_title is not None: data["job_title"]=job_title
    if created_by_name is not None: data["created_by_name"]=created_by_name
    data.update(review_progress or _empty_review_progress())
    data.update(route_summary or _empty_route_summary())
    return data
def _item_data(item,file,application=None,evaluation=None,candidate=None,rule_result=None,llm_retryable=False,terminal_route=None):
    if terminal_route is not None and terminal_route["ai_status"]=="failed": evaluation=None
    llm_evaluation=None if evaluation is None else {"score":evaluation.score,"recommendation":evaluation.recommendation,"dimensions":list(evaluation.dimensions or []),"summary":evaluation.summary,"strengths":evaluation.strengths,"gaps":evaluation.gaps,"risks":evaluation.risks,"questions":evaluation.interview_questions}
    rule_data=None if rule_result is None or item.status!="scored" else {"score":rule_result.rule_score,"recommendation":rule_result.recommendation,"required_hits":rule_result.required_hits,"required_missing":rule_result.required_missing,"bonus_hits":rule_result.bonus_hits,"risks":rule_result.risks}
    human_reviewed=application is not None and (application.human_conclusion is not None or application.stage not in ("new","review"))
    route_result=terminal_route["route_result"] if terminal_route else None
    ai_unavailable=evaluation is None and terminal_route is not None and terminal_route["ai_status"]=="failed"
    llm_error_code=normalize_llm_terminal_safe_error_code(terminal_route["safe_error_code"]) if ai_unavailable else None
    return {"id":str(item.id),"run_id":str(item.run_id),"filename":file.original_filename,"mime_type":file.mime_type,"size_bytes":file.size_bytes,"status":item.status,"parser_version":item.parser_version,"parse_quality":item.parse_quality,"error_code":item.safe_error_code,"attempts":item.attempts,"created_at":item.created_at.isoformat(),"retryable":item.status=="failed" and item.safe_error_code in RECOVERABLE_CODES,"llm_retryable":llm_retryable,"candidate_id":str(candidate.id) if candidate else None,"candidate_name":candidate.display_name if candidate else None,"rule_result":rule_data,"application_stage":application.stage if application else None,"application_version":application.version if application else None,"human_reviewed":human_reviewed,"route_result":route_result,"ai_score":evaluation.score if evaluation is not None else None,"ai_recommendation":evaluation.recommendation if evaluation is not None else "AI评分不可用" if ai_unavailable else None,"llm_status":terminal_route["ai_status"] if terminal_route else item.llm_status,"llm_error_code":llm_error_code,"llm_attempts":item.llm_attempts,"llm_evaluation":llm_evaluation}
def _load_run(db,principal,run_id,action=RecruitingAction.READ,lock=False):
    query=select(ScreeningRun).join(Job,and_(Job.organization_id==ScreeningRun.organization_id,Job.id==ScreeningRun.job_id)).where(ScreeningRun.organization_id==principal.organization_id,ScreeningRun.id==run_id,_job_scope(principal,action))
    return db.scalar(query.with_for_update() if lock else query)
def _load_item_scoped(db,principal,item_id,action=RecruitingAction.READ):
    active_candidate=exists().where(Candidate.organization_id==ScreeningItem.organization_id,Candidate.id==ScreeningItem.candidate_id,Candidate.deleted_at.is_(None))
    return db.scalar(select(ScreeningItem).join(ScreeningRun,and_(ScreeningRun.organization_id==ScreeningItem.organization_id,ScreeningRun.id==ScreeningItem.run_id)).join(Job,and_(Job.organization_id==ScreeningRun.organization_id,Job.id==ScreeningRun.job_id)).where(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.id==item_id,or_(ScreeningItem.candidate_id.is_(None),active_candidate),_job_scope(principal,action)))
def _filename(value,extension):
    name=PurePath(unquote(value or "").replace("\\","/")).name; name=re.sub(r"[\x00-\x1f\x7f]","",name).strip().replace("..",".")
    if not name: name=f"resume{extension}"
    if len(name)>200: name=name[:200-len(extension)].rstrip(".")+extension
    return name
def _limits(settings): return ParserLimits(max_source_bytes=settings.parser_max_source_bytes,max_text_chars=settings.parser_max_text_chars,pdf_max_pages=settings.parser_pdf_max_pages,docx_max_entries=settings.parser_docx_max_entries,docx_max_uncompressed_bytes=settings.parser_docx_max_uncompressed_bytes,docx_max_compression_ratio=settings.parser_docx_max_compression_ratio)
def _audit_rejection(request,principal,run_id,code):
    with request.app.state.identity_store.sync_session() as audit_db:
        run=_load_run(audit_db,principal,run_id,RecruitingAction.MANAGE_JOB)
        if run:
            audit_db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="screening.item_rejected",outcome="failure",trace_id=request.state.trace_id,metadata_json={"run_id":str(run_id),"safe_error_code":code})); audit_db.commit()
def _idempotency_precheck(db,organization_id,user_id,operation,key,body):
    request_hash=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(",",":"),default=str).encode()).hexdigest()
    record=db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.organization_id==organization_id,IdempotencyRecord.user_id==user_id,IdempotencyRecord.operation==operation,IdempotencyRecord.idempotency_key==key))
    if record and record.request_hash!=request_hash: raise IdempotencyConflict
    return (record.status_code,record.response_json) if record else None

@router.post("/jobs/{job_id}/screening-runs",response_model=RunResource,status_code=201)
def create_run(job_id:UUID,payload:RunCreate,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_job(db,principal,job_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
        try:
            def action():
                jd=db.scalar(select(JobJdVersion).where(JobJdVersion.organization_id==principal.organization_id,JobJdVersion.job_id==job_id,*( [JobJdVersion.id==payload.jd_version_id] if payload.jd_version_id else [])).order_by(JobJdVersion.version_number.desc()))
                rule=db.scalar(select(ScreeningRuleVersion).where(ScreeningRuleVersion.organization_id==principal.organization_id,ScreeningRuleVersion.job_id==job_id,*( [ScreeningRuleVersion.id==payload.rule_version_id] if payload.rule_version_id else [])).order_by(ScreeningRuleVersion.version_number.desc()))
                if not jd or not rule: raise ValueError("version_mismatch")
                run=ScreeningRun(organization_id=principal.organization_id,job_id=job_id,jd_version_id=jd.id,rule_version_id=rule.id,source=payload.source,status="queued",total_count=0,processed_count=0,succeeded_count=0,failed_count=0,created_by=principal.user_id); db.add(run); db.flush(); db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="screening.run_created",outcome="success",trace_id=request.state.trace_id,metadata_json={"run_id":str(run.id),"job_id":str(job_id)})); return 201,{"data":_run_data(run)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"screening.run.create",key,{"job_id":str(job_id),**payload.model_dump()},action); db.commit(); response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response
        except IdempotencyConflict: db.rollback(); return _problem(request,409,"idempotency_conflict")
        except Exception: db.rollback(); return _problem(request,422,"version_mismatch")

@router.get("/screening-runs",response_model=RunCollection)
def list_runs(request:Request,cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    cursor_sort="screening-runs:-created_at"
    with request.app.state.identity_store.sync_session() as db:
        query=select(ScreeningRun,Job.title,User.display_name).join(Job,and_(Job.organization_id==ScreeningRun.organization_id,Job.id==ScreeningRun.job_id)).join(User,and_(User.organization_id==ScreeningRun.organization_id,User.id==ScreeningRun.created_by)).where(ScreeningRun.organization_id==principal.organization_id,_job_scope(principal))
        if cursor:
            try:
                decoded=request.app.state.recruiting_cursor.decode(cursor,str(principal.organization_id),cursor_sort); created_at=datetime.fromisoformat(decoded["value"]); run_id=UUID(decoded["id"]); query=query.where(or_(ScreeningRun.created_at<created_at,and_(ScreeningRun.created_at==created_at,ScreeningRun.id<run_id)))
            except Exception: return _problem(request,422,"validation_failed")
        rows=db.execute(query.order_by(ScreeningRun.created_at.desc(),ScreeningRun.id.desc()).limit(limit+1)).all(); next_cursor=None
        if len(rows)>limit:
            last=rows[limit-1][0]; next_cursor=request.app.state.recruiting_cursor.encode(str(principal.organization_id),cursor_sort,last.created_at.isoformat(),str(last.id)); rows=rows[:limit]
        run_ids=[run.id for run,_,_ in rows]; progress=_review_progress(db,principal.organization_id,run_ids); route_summaries=_route_summaries(db,principal.organization_id,run_ids)
        response=JSONResponse({"data":[_run_data(run,job_title=job_title,created_by_name=creator_name,review_progress=progress.get(run.id),route_summary=route_summaries.get(run.id)) for run,job_title,creator_name in rows],"meta":{"limit":limit,"next_cursor":next_cursor}}); response.headers["Cache-Control"]="no-store"; return response

@router.post("/screening-runs/{run_id}/items",response_model=ItemResource,status_code=201)
def upload_item(run_id:UUID,request:Request,file:UploadFile=File(...),idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as auth_db:
        if _load_run(auth_db,principal,run_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
    extension=PurePath(file.filename or "").suffix.lower(); limits=_limits(request.app.state.settings); spool=SpooledTemporaryFile(max_size=min(limits.max_source_bytes,1024*1024),mode="w+b"); digest=hashlib.sha256(); size=0
    try:
        while chunk:=file.file.read(64*1024):
            size+=len(chunk)
            if size>limits.max_source_bytes: raise ParserError("file_too_large")
            digest.update(chunk); spool.write(chunk)
        spool.seek(0); detected=validate_upload_preflight(spool,extension=extension,mime_type=file.content_type or "",limits=limits); display=_filename(file.filename,extension); sha=digest.hexdigest()
    except ParserError as error: spool.close(); _audit_rejection(request,principal,run_id,error.safe_code); return _problem(request,422,error.safe_code)
    fingerprint={"run_id":str(run_id),"filename":display,"mime":file.content_type,"size":size,"sha256":sha}; operation="screening.item.upload"
    try:
        with request.app.state.identity_store.sync_session() as precheck_db:
            previous=_idempotency_precheck(precheck_db,principal.organization_id,principal.user_id,operation,key,fingerprint)
        if previous:
            spool.close(); return _response(previous[1]["data"],previous[0])
    except IdempotencyConflict:
        spool.close(); return _problem(request,409,"idempotency_conflict")
    object_id=uuid.uuid4(); storage_key=f"quarantine/{principal.organization_id}/{run_id}/{object_id}"
    try:
        spool.seek(0); request.app.state.quarantine_storage.write(spool,storage_key,file.content_type or "",limits.max_source_bytes)
    except StorageWriteFailed:
        request.app.state.quarantine_storage.delete(storage_key); spool.close(); _audit_rejection(request,principal,run_id,"storage_write_failed"); return _problem(request,503,"storage_write_failed")
    finally:
        spool.close()
    executed=False
    try:
        with request.app.state.identity_store.sync_session() as db:
            run=_load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB,lock=True)
            if run is None: request.app.state.quarantine_storage.delete(storage_key); return _not_found(request)
            def action():
                nonlocal executed
                if run.status!="queued": raise RunNotQueued
                if run.total_count>=100: raise ScreeningItemLimit
                executed=True
                duplicate=db.scalar(select(FileObject.id).where(FileObject.organization_id==principal.organization_id,FileObject.sha256==sha).limit(1))
                stored=FileObject(id=object_id,organization_id=principal.organization_id,storage_key=storage_key,original_filename=display,mime_type=file.content_type,size_bytes=size,sha256=sha,uploaded_by=principal.user_id,storage_state="quarantine",detected_type=detected,scan_status="pending"); db.add(stored); db.flush(); item=ScreeningItem(organization_id=principal.organization_id,run_id=run.id,file_object_id=stored.id,status="queued",attempts=0); db.add(item); run.total_count+=1; run.version+=1; db.flush()
                if duplicate: db.add(CandidateDuplicateHint(organization_id=principal.organization_id,file_object_id=stored.id,signals={"same_sha":True},status="pending"))
                db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="screening.item_accepted",outcome="success",trace_id=request.state.trace_id,metadata_json={"run_id":str(run.id),"item_id":str(item.id)})); return 201,{"data":_item_data(item,stored)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,operation,key,fingerprint,action); db.commit()
        if not executed: request.app.state.quarantine_storage.delete(storage_key)
    except IdempotencyConflict:
        request.app.state.quarantine_storage.delete(storage_key); return _problem(request,409,"idempotency_conflict")
    except RunNotQueued:
        request.app.state.quarantine_storage.delete(storage_key); return _problem(request,409,"screening_run_not_queued")
    except ScreeningItemLimit:
        request.app.state.quarantine_storage.delete(storage_key); return _problem(request,409,"screening_item_limit")
    except Exception:
        request.app.state.quarantine_storage.delete(storage_key); return _problem(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.get("/screening-runs/{run_id}",response_model=RunResource)
def get_run(run_id:UUID,request:Request):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    with request.app.state.identity_store.sync_session() as db:
        run=_load_run(db,principal,run_id)
        if run is None: return _not_found(request)
        errors={}
        for field in (ScreeningItem.safe_error_code,ScreeningItem.llm_safe_error_code):
            rows=db.execute(select(field,func.count()).where(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.run_id==run.id,field.is_not(None)).group_by(field).order_by(func.count().desc(),field).limit(20))
            for code,count in rows: errors[code]=errors.get(code,0)+count
        summary=dict(sorted(errors.items(),key=lambda item:(-item[1],item[0]))[:20])
        context=db.execute(select(Job.title,User.display_name).join(User,and_(User.organization_id==run.organization_id,User.id==run.created_by)).where(Job.organization_id==run.organization_id,Job.id==run.job_id)).one()
        progress=_review_progress(db,principal.organization_id,[run.id]).get(run.id)
        route_summary=_route_summaries(db,principal.organization_id,[run.id]).get(run.id)
        return _response(_run_data(run,summary,context[0],context[1],progress,route_summary))

@router.post("/screening-items/{item_id}/retry",response_model=RetryResource)
def retry_item(item_id:UUID,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_item_scoped(db,principal,item_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
        try:
            def action():
                item,run,_=retry_screening_item(db,principal.organization_id,item_id,request.state.trace_id,principal.user_id); stored=db.get(FileObject,item.file_object_id); application=db.get(Application,item.application_id) if item.application_id else None; return 200,{"data":{"item":_item_data(item,stored,application),"run":_run_data(run)}}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"screening.item.retry",key,{"item_id":str(item_id)},action); db.commit()
        except IdempotencyConflict: db.rollback(); return _problem(request,409,"idempotency_conflict")
        except CandidateTombstoned: db.rollback(); return _not_found(request)
        except ScreeningItemNotRetryable: db.rollback(); return _problem(request,409,"screening_item_not_retryable")
        except ScreeningRetryActive: db.rollback(); return _problem(request,409,"screening_retry_active")
        except Exception: db.rollback(); return _problem(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.post("/screening-runs/{run_id}/bulk-actions",response_model=BulkResource)
def bulk_actions(run_id:UUID,payload:BulkAction,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
        try:
            def action(): return 200,{"data":apply_bulk_action(db,principal.organization_id,run_id,payload,principal.user_id,request.state.trace_id)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"screening.run.bulk_action",key,{"run_id":str(run_id),**payload.model_dump()},action); db.commit()
        except IdempotencyConflict: db.rollback(); return _problem(request,409,"idempotency_conflict")
        except CandidateTombstoned: db.rollback(); return _not_found(request)
        except ScreeningBulkConflict: db.rollback(); return _problem(request,409,"screening_bulk_conflict")
        except Exception: db.rollback(); return _problem(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.post("/screening-runs/{run_id}/start",response_model=RunResource)
def start_run(run_id:UUID,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
        try:
            def action():
                run=_load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB,lock=True)
                if run is None: raise LookupError
                if run.status!="queued": raise ScreeningRunAlreadyStarted
                items=list(db.scalars(select(ScreeningItem).where(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.run_id==run.id,ScreeningItem.status=="queued").order_by(ScreeningItem.created_at,ScreeningItem.id)))
                if not items: raise ScreeningRunEmpty
                run.status="parsing"; run.started_at=datetime.now(timezone.utc); run.version+=1
                queue=QueueRepository(db)
                for item in items:
                    queue.enqueue(principal.organization_id,"screening.parse_item",{"organization_id":str(principal.organization_id),"screening_item_id":str(item.id),"parser_version":"parser-v1"},dedupe_key=f"parse:{item.id}",trace_id=request.state.trace_id,max_attempts=3)
                db.flush(); return 200,{"data":_run_data(run)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"screening.run.start",key,{"run_id":str(run_id)},action); db.commit()
        except IdempotencyConflict: db.rollback(); return _problem(request,409,"idempotency_conflict")
        except ScreeningRunEmpty: db.rollback(); return _problem(request,409,"screening_run_empty")
        except ScreeningRunAlreadyStarted: db.rollback(); return _problem(request,409,"screening_run_already_started")
        except LookupError: db.rollback(); return _not_found(request)
        except Exception: db.rollback(); return _problem(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.post("/screening-runs/{run_id}/cancel",response_model=RunResource)
def cancel_run(run_id:UUID,request:Request,idempotency_key:str|None=Header(None)):
    principal=_principal(request); key=_idempotency(request,idempotency_key)
    if isinstance(principal,JSONResponse): return principal
    if isinstance(key,JSONResponse): return key
    with request.app.state.identity_store.sync_session() as db:
        if _load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB) is None: return _not_found(request)
        try:
            def action():
                run=_load_run(db,principal,run_id,RecruitingAction.MANAGE_JOB,lock=True)
                if run is None: raise LookupError
                item_count=db.scalar(select(func.count(ScreeningItem.id)).where(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.run_id==run.id)) or 0
                if run.status!="queued" or run.started_at is not None or run.total_count!=0 or item_count!=0: raise ScreeningRunNotCancellable
                run.status="cancelled"; run.finished_at=datetime.now(timezone.utc); run.version+=1
                db.add(AuditLog(organization_id=principal.organization_id,actor_user_id=principal.user_id,event_type="screening.run_cancelled",outcome="success",trace_id=request.state.trace_id,metadata_json={"run_id":str(run.id),"reason":"empty_upload_interrupted"}))
                db.flush(); return 200,{"data":_run_data(run)}
            status,body=persisted_idempotent(db,principal.organization_id,principal.user_id,"screening.run.cancel",key,{"run_id":str(run_id)},action); db.commit()
        except IdempotencyConflict: db.rollback(); return _problem(request,409,"idempotency_conflict")
        except ScreeningRunNotCancellable: db.rollback(); return _problem(request,409,"screening_run_not_cancellable")
        except LookupError: db.rollback(); return _not_found(request)
        except Exception: db.rollback(); return _problem(request,503,"persistence_failed")
    response=JSONResponse(body,status_code=status); response.headers["Cache-Control"]="no-store"; return response

@router.get("/screening-runs/{run_id}/items",response_model=ItemCollection)
def list_items(run_id:UUID,request:Request,status:str|None=None,cursor:str|None=None,limit:int=Query(50,ge=1,le=100)):
    principal=_principal(request)
    if isinstance(principal,JSONResponse): return principal
    if status and status not in _SAFE_STATUS: return _problem(request,422,"validation_failed")
    with request.app.state.identity_store.sync_session() as db:
        run=_load_run(db,principal,run_id)
        if run is None: return _not_found(request)
        active_candidate=exists().where(Candidate.organization_id==ScreeningItem.organization_id,Candidate.id==ScreeningItem.candidate_id,Candidate.deleted_at.is_(None))
        query=select(ScreeningItem,FileObject).join(FileObject,and_(FileObject.organization_id==ScreeningItem.organization_id,FileObject.id==ScreeningItem.file_object_id)).where(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.run_id==run.id,or_(ScreeningItem.candidate_id.is_(None),active_candidate))
        if status: query=query.where(ScreeningItem.status==status)
        if cursor:
            try:
                decoded=request.app.state.recruiting_cursor.decode(cursor,str(principal.organization_id),f"screening-items:{run_id}"); created_at=datetime.fromisoformat(decoded["value"]); item_id=UUID(decoded["id"]); query=query.where(or_(ScreeningItem.created_at>created_at,and_(ScreeningItem.created_at==created_at,ScreeningItem.id>item_id)))
            except Exception: return _problem(request,422,"validation_failed")
        rows=db.execute(query.order_by(ScreeningItem.created_at,ScreeningItem.id).limit(limit+1)).all(); next_cursor=None
        if len(rows)>limit: next_cursor=request.app.state.recruiting_cursor.encode(str(principal.organization_id),f"screening-items:{run_id}",rows[limit-1][0].created_at.isoformat(),str(rows[limit-1][0].id)); rows=rows[:limit]
        item_ids=[item.id for item,_ in rows]; applications={}; candidates={}; rule_results={}; evaluations={}; resumes={}; terminal_routes={}; llm_config=None; llm_prompt=None; jd_version=None
        if item_ids:
            enrichment_scope=(ScreeningItem.organization_id==principal.organization_id,ScreeningItem.run_id==run.id,ScreeningItem.id.in_(item_ids))
            application_rows=db.execute(select(ScreeningItem.id,Application).join(Application,and_(Application.organization_id==ScreeningItem.organization_id,Application.id==ScreeningItem.application_id)).where(*enrichment_scope)).all()
            applications={item_id:application for item_id,application in application_rows}
            terminal_routes=_validated_terminal_routes(db,principal.organization_id,[application.id for _,application in application_rows])
            candidate_rows=db.execute(select(ScreeningItem.id,Candidate).join(Candidate,and_(Candidate.organization_id==ScreeningItem.organization_id,Candidate.id==ScreeningItem.candidate_id)).where(*enrichment_scope,Candidate.deleted_at.is_(None))).all()
            candidates={item_id:candidate for item_id,candidate in candidate_rows}
            result_rows=db.execute(select(ScreeningResult.item_id,ScreeningResult).join(ScreeningItem,and_(ScreeningItem.organization_id==ScreeningResult.organization_id,ScreeningItem.id==ScreeningResult.item_id)).where(*enrichment_scope).order_by(ScreeningResult.created_at.desc(),ScreeningResult.id.desc())).all()
            for item_id,result in result_rows: rule_results.setdefault(item_id,result)
            evaluation_rows=db.execute(select(ScreeningResult.item_id,LlmScreeningEvaluation).join(ScreeningItem,and_(ScreeningItem.organization_id==ScreeningResult.organization_id,ScreeningItem.id==ScreeningResult.item_id)).join(LlmScreeningEvaluation,and_(LlmScreeningEvaluation.organization_id==ScreeningResult.organization_id,LlmScreeningEvaluation.screening_result_id==ScreeningResult.id)).where(*enrichment_scope).order_by(LlmScreeningEvaluation.created_at.desc(),LlmScreeningEvaluation.id.desc())).all()
            for item_id,evaluation in evaluation_rows: evaluations.setdefault(item_id,evaluation)
            resume_ids={item.resume_id for item,_ in rows if item.resume_id is not None}
            if resume_ids: resumes={resume.id:resume for resume in db.scalars(select(Resume).where(Resume.organization_id==principal.organization_id,Resume.id.in_(resume_ids)))}
            jd_version=db.scalar(select(JobJdVersion).where(JobJdVersion.organization_id==principal.organization_id,JobJdVersion.id==run.jd_version_id))
            llm_config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==principal.organization_id))
            llm_prompt=db.scalar(select(PromptVersion).where(PromptVersion.organization_id==principal.organization_id,PromptVersion.name=="screening-evaluation").order_by(PromptVersion.version_number.desc()).limit(1))
        response=JSONResponse({"data":[_item_data(item,stored,applications.get(item.id),evaluations.get(item.id),candidates.get(item.id),rule_results.get(item.id),is_llm_retryable(item,run,rule_results.get(item.id),llm_config,llm_prompt,jd_version,resumes.get(item.resume_id)),terminal_routes.get(item.application_id)) for item,stored in rows],"meta":{"limit":limit,"next_cursor":next_cursor}}); response.headers["Cache-Control"]="no-store"; return response
