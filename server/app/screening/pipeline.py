import hashlib,json,uuid
from datetime import datetime,timezone
from pathlib import PurePath
from sqlalchemy import and_,func,select
from server.app.queue.repository import QueueRepository
from server.app.queue.service import PermanentJobError,RetryableJobError
from server.app.governance.retention import recalculate_candidate_retention
from server.app.recruiting.models import Application,Candidate,FileObject,JobJdVersion,Resume,ResumeProfile,ScreeningRuleVersion
from server.app.recruiting.profile_builder import PROFILE_VERSION,ProfileBuild,ResumeProfileBuilder
from server.app.recruiting.resume_profile import extract_resume_profile
from server.app.screening.actions import CandidateTombstoned,lock_candidate_screening_item
from server.app.screening.models import ScreeningItem,ScreeningResult,ScreeningRun
from server.app.screening.progress import aggregate_run
from server.app.screening.parsers import ParserLimits
from server.app.screening.isolated_parser import IsolatedParser,IsolatedParserError
from server.app.screening.rules import ENGINE_VERSION,RuleSnapshot,RuleSnapshotError,score_resume
from server.app.screening.scanner import ScanResult
from server.app.screening.storage import StorageWriteFailed
from server.app.llm.models import LlmProviderConfig,PromptVersion
from server.app.llm.screening import SCREENING_SYSTEM_PROMPT
from server.app.screening.routing import route_llm_screening_terminal
from server.app.screening.document_quality import assess_text_quality
from server.app.screening.resume_enrichment import EnrichedResumeText

_RETRYABLE={"scanner_unavailable","scanner_error","storage_unavailable"}
def _uuid(item_id,name): return uuid.uuid5(uuid.UUID(str(item_id)),name)
_PROMPT_CONTENT={"system":SCREENING_SYSTEM_PROMPT,"schema_version":"screening-evaluation-v2"}
_PROMPT_VERSION=3
def _ensure_screening_prompt(db,organization_id,created_by,*,content=None,version_number=_PROMPT_VERSION):
    prompt_content=dict(content or _PROMPT_CONTENT); prompt_hash=hashlib.sha256(json.dumps(prompt_content,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    prompt_id=uuid.uuid5(uuid.UUID(str(organization_id)),f"screening-evaluation:{version_number}")
    prompt=db.scalar(select(PromptVersion).where(PromptVersion.organization_id==organization_id,PromptVersion.name=="screening-evaluation",PromptVersion.version_number==version_number))
    if prompt is None:
        prompt=PromptVersion(id=prompt_id,organization_id=organization_id,name="screening-evaluation",version_number=version_number,content=prompt_content,content_hash=prompt_hash,created_by=created_by); db.add(prompt); db.flush()
    elif prompt.content_hash!=prompt_hash:
        raise PermanentJobError("llm_prompt_version_conflict")
    return prompt

class ScreeningPipeline:
    def __init__(self,sessions,storage,scanner,settings,parser=None,text_enhancer=None,profile_builder:ResumeProfileBuilder|None=None):
        self.sessions,self.storage,self.scanner,self.settings=sessions,storage,scanner,settings
        self.parser=parser or IsolatedParser(settings.parser_hard_timeout_seconds)
        self.text_enhancer=text_enhancer
        self.profile_builder=profile_builder
    def _limits(self):
        s=self.settings; return ParserLimits(s.parser_max_source_bytes,s.parser_max_text_chars,s.parser_pdf_max_pages,s.parser_docx_max_entries,s.parser_docx_max_uncompressed_bytes,s.parser_docx_max_compression_ratio)
    async def parse_item(self,job):
        organization_id=uuid.UUID(str(job.payload["organization_id"])); item_id=uuid.UUID(str(job.payload["screening_item_id"])); limits=self._limits()
        candidate_id=_uuid(item_id,"candidate")
        with self.sessions() as db:
            try: _,item,blocked=lock_candidate_screening_item(db,organization_id,item_id,candidate_id,allow_unassociated=True,allow_missing_candidate=True,allow_blocked=True)
            except CandidateTombstoned: raise PermanentJobError("screening_item_missing") from None
            if item.status in {"parsed","scored","cancelled"} or (item.status=="failed" and item.safe_error_code not in _RETRYABLE): db.commit(); return
            if blocked: raise PermanentJobError("screening_item_missing")
            item.attempts=max(item.attempts,getattr(job,"attempts",1)); item.started_at=item.started_at or datetime.now(timezone.utc)
            item.status="parsing"; db.commit()
        with self.sessions() as db:
            row=db.execute(select(ScreeningItem,FileObject).join(FileObject,and_(FileObject.organization_id==ScreeningItem.organization_id,FileObject.id==ScreeningItem.file_object_id)).where(ScreeningItem.organization_id==organization_id,ScreeningItem.id==item_id)).one_or_none()
            if not row: raise PermanentJobError("screening_item_missing")
            item,stored=row
            if item.status in {"parsed","scored","cancelled"} or (item.status=="failed" and item.safe_error_code not in _RETRYABLE): return
            key,mime,filename,scan_status=stored.storage_key,stored.mime_type,stored.original_filename,stored.scan_status
        if scan_status!="clean":
            source=None
            try: source=await self.storage.open(key,limits.max_source_bytes); scan=await self.scanner.scan(source,limits.max_source_bytes)
            except StorageWriteFailed as error:
                if error.safe_code=="file_too_large": self._fail_terminal(organization_id,item_id,error.safe_code); return
                return await self._retry_or_finish(job,organization_id,item_id,"storage_unavailable","parse")
            except Exception: return await self._retry_or_finish(job,organization_id,item_id,"storage_unavailable","parse")
            finally:
                if source is not None: source.close()
            if scan==ScanResult.INFECTED:
                self._fail_terminal(organization_id,item_id,"malware_detected",file_state=("rejected","rejected")); await self.storage.delete(key); return
            if scan!=ScanResult.CLEAN: return await self._retry_or_finish(job,organization_id,item_id,"scanner_unavailable" if scan==ScanResult.UNAVAILABLE else "scanner_error","parse")
            clean_key=f"clean/{organization_id}/{stored.id}"
            try: await self.storage.copy(key,clean_key,limits.max_source_bytes)
            except Exception: return await self._retry_or_finish(job,organization_id,item_id,"storage_unavailable","parse")
            with self.sessions() as db:
                current=db.scalar(select(FileObject).where(FileObject.organization_id==organization_id,FileObject.id==stored.id).with_for_update()); current.storage_key=clean_key; current.storage_state="clean"; current.scan_status="clean"; current.quarantine_cleanup_key=key; db.commit()
            deleted=await self.storage.delete(key)
            if deleted:
                with self.sessions() as db:
                    current=db.scalar(select(FileObject).where(FileObject.organization_id==organization_id,FileObject.id==stored.id).with_for_update()); current.quarantine_cleanup_key=None; db.commit()
            key=clean_key
        stream=None
        try:
            stream=await self.storage.open(key,limits.max_source_bytes)
            parsed=await self.parser.parse(stream,extension=PurePath(filename).suffix,mime_type=mime,limits=limits)
        except StorageWriteFailed as error:
            if error.safe_code=="file_too_large": self._fail_terminal(organization_id,item_id,error.safe_code); return
            return await self._retry_or_finish(job,organization_id,item_id,"storage_unavailable","parse")
        except IsolatedParserError as error: self._fail_terminal(organization_id,item_id,error.safe_code); return
        except Exception: return await self._retry_or_finish(job,organization_id,item_id,"storage_unavailable","parse")
        finally:
            if stream is not None: stream.close()
        if self.text_enhancer is not None:
            enriched=await self.text_enhancer.enhance(
                organization_id,
                storage_key=key,
                filename=filename,
                mime_type=mime,
                native_text=parsed.text,
            )
        else:
            assessment=assess_text_quality(parsed.text)
            enriched=EnrichedResumeText(parsed.text,assessment,False)
        with self.sessions() as db:
            run_context=db.scalar(select(ScreeningRun).where(ScreeningRun.organization_id==organization_id,ScreeningRun.id==item.run_id))
            if run_context is None: raise PermanentJobError("screening_item_missing")
            job_id=run_context.job_id
        candidate_name=(PurePath(filename).stem[:200] or "Candidate")
        if self.profile_builder is not None:
            profile=await self.profile_builder.build(
                organization_id,
                job_id=job_id,
                resume_text=enriched.text,
                candidate_name=candidate_name,
                used_ocr=enriched.used_ocr,
                trace_id=getattr(job,"trace_id",None),
            )
        else:
            profile_data=extract_resume_profile(enriched.text,include_metadata=True)
            profile_source="ocr_rules" if enriched.used_ocr else "rules"
            profile_data["source"]=profile_source
            profile=ProfileBuild(profile_data,str(profile_data["status"]),profile_source)
        with self.sessions() as db:
            try: candidate,item,blocked=lock_candidate_screening_item(db,organization_id,item_id,candidate_id,allow_unassociated=True,allow_missing_candidate=True,allow_blocked=True)
            except CandidateTombstoned: raise PermanentJobError("screening_item_missing") from None
            if item.status in {"parsed","scored","cancelled"}: return
            run=db.scalar(select(ScreeningRun).where(ScreeningRun.organization_id==organization_id,ScreeningRun.id==item.run_id).with_for_update())
            if blocked:
                item.status="cancelled"; item.safe_error_code="candidate_unavailable"; item.finished_at=item.finished_at or datetime.now(timezone.utc)
                self._aggregate(db,run); db.commit(); return
            resume_id,application_id=_uuid(item.id,"resume"),_uuid(item.id,"application")
            candidate=candidate or Candidate(id=candidate_id,organization_id=organization_id,display_name=candidate_name,owner_id=run.created_by)
            if candidate not in db: db.add(candidate)
            resume=db.get(Resume,resume_id) or Resume(id=resume_id,organization_id=organization_id,candidate_id=candidate_id,file_object_id=item.file_object_id,version_number=1,parsed_text=enriched.text)
            if resume not in db: db.add(resume)
            resume_profile=db.scalar(select(ResumeProfile).where(ResumeProfile.organization_id==organization_id,ResumeProfile.resume_id==resume_id))
            if resume_profile is None:
                db.add(ResumeProfile(
                    id=_uuid(item.id,"resume-profile"),
                    organization_id=organization_id,
                    resume_id=resume_id,
                    data=profile.data,
                    status=profile.status,
                    source=profile.source,
                    profile_version=PROFILE_VERSION,
                    safe_error_code=profile.safe_error_code or enriched.safe_error_code,
                ))
            application=db.get(Application,application_id) or Application(id=application_id,organization_id=organization_id,candidate_id=candidate_id,job_id=run.job_id,resume_id=resume_id,owner_id=run.created_by,stage="new",source="screening")
            if application not in db:
                db.add(application); db.flush(); recalculate_candidate_retention(db,organization_id,candidate_id)
            item.candidate_id=candidate_id; item.resume_id=resume_id; item.application_id=application_id; item.status="parsed"; item.parser_version=parsed.parser_version; item.parse_quality=enriched.assessment.quality; item.safe_error_code=None
            QueueRepository(db).enqueue(organization_id,"screening.score_item",{"organization_id":str(organization_id),"screening_item_id":str(item.id),"jd_version_id":str(run.jd_version_id),"rule_version_id":str(run.rule_version_id),"rule_engine_version":ENGINE_VERSION},dedupe_key=f"score:{item.id}",trace_id=getattr(job,"trace_id",None),max_attempts=3); run.status="rule_scoring"; db.commit()
    async def score_item(self,job):
        organization_id=uuid.UUID(str(job.payload["organization_id"])); item_id=uuid.UUID(str(job.payload["screening_item_id"]))
        with self.sessions() as db:
            candidate_id=db.scalar(select(ScreeningItem.candidate_id).where(ScreeningItem.organization_id==organization_id,ScreeningItem.id==item_id))
        if candidate_id is None: raise PermanentJobError("screening_item_missing")
        with self.sessions() as db:
            try: _,item,_=lock_candidate_screening_item(db,organization_id,item_id,candidate_id)
            except CandidateTombstoned: raise PermanentJobError("screening_item_missing") from None
            if item.status!="parsed": return
            item.status="scoring"; db.commit()
        with self.sessions() as db:
            row=db.execute(select(ScreeningItem,ScreeningRun,Resume,JobJdVersion,ScreeningRuleVersion).join(ScreeningRun,and_(ScreeningRun.organization_id==ScreeningItem.organization_id,ScreeningRun.id==ScreeningItem.run_id)).join(Resume,and_(Resume.organization_id==ScreeningItem.organization_id,Resume.id==ScreeningItem.resume_id)).join(JobJdVersion,and_(JobJdVersion.organization_id==ScreeningRun.organization_id,JobJdVersion.id==ScreeningRun.jd_version_id)).join(ScreeningRuleVersion,and_(ScreeningRuleVersion.organization_id==ScreeningRun.organization_id,ScreeningRuleVersion.id==ScreeningRun.rule_version_id)).where(ScreeningItem.organization_id==organization_id,ScreeningItem.id==item_id)).one_or_none()
            if not row: raise PermanentJobError("screening_item_missing")
            item,run,resume,jd,rule=row
            text=resume.parsed_text or ""
            try:
                snapshot=RuleSnapshot.from_storage(jd.content,rule.content)
            except RuleSnapshotError:
                self._fail_terminal(organization_id,item_id,"rule_snapshot_invalid"); raise PermanentJobError("rule_snapshot_invalid") from None
        try: result=score_resume(text,snapshot)
        except Exception: return await self._retry_or_finish(job,organization_id,item_id,"scoring_failed","score")
        with self.sessions() as db:
            _,item,blocked=lock_candidate_screening_item(db,organization_id,item_id,candidate_id,allow_blocked=True)
            run=db.scalar(select(ScreeningRun).where(ScreeningRun.organization_id==organization_id,ScreeningRun.id==item.run_id).with_for_update())
            if blocked:
                item.status="cancelled"; item.safe_error_code="candidate_unavailable"; item.finished_at=item.finished_at or datetime.now(timezone.utc); self._aggregate(db,run); db.commit(); raise PermanentJobError("screening_item_missing")
            existing=db.scalar(select(ScreeningResult).where(ScreeningResult.organization_id==organization_id,ScreeningResult.item_id==item.id,ScreeningResult.rule_engine_version==result.engine_version))
            if not existing:
                existing=ScreeningResult(organization_id=organization_id,item_id=item.id,application_id=item.application_id,resume_id=item.resume_id,rule_engine_version=result.engine_version,rule_score=result.score,recommendation=result.recommendation,required_hits=result.required_hits,required_missing=result.required_missing,bonus_hits=result.bonus_hits,estimated_years=result.estimated_years,risks=result.risks,questions=result.questions); db.add(existing); db.flush()
            config=db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id==organization_id))
            allowed=config and (not config.allowed_job_ids or str(run.job_id) in config.allowed_job_ids)
            item.status="scored"; item.safe_error_code=None
            if config and config.enabled and config.encrypted_api_key is not None and allowed:
                prompt=_ensure_screening_prompt(db,organization_id,config.updated_by)
                QueueRepository(db).enqueue(organization_id,"screening.llm_score_item",{"organization_id":str(organization_id),"screening_item_id":str(item.id),"screening_result_id":str(existing.id),"application_id":str(item.application_id),"config_id":str(config.id),"config_version":config.version,"prompt_version_id":str(prompt.id)},dedupe_key=f"llm:{item.id}:{config.version}:{prompt.id}",trace_id=getattr(job,"trace_id",None),max_attempts=3)
                item.llm_status="queued"; item.llm_safe_error_code=None; item.llm_finished_at=None; item.finished_at=None
            else:
                skip_code="llm_job_not_allowed" if config and not allowed else "llm_config_disabled"
                item.llm_status="skipped"; item.llm_safe_error_code=skip_code; item.llm_finished_at=item.llm_finished_at or datetime.now(timezone.utc); item.finished_at=item.finished_at or datetime.now(timezone.utc)
                route_llm_screening_terminal(db,organization_id=organization_id,item_id=item.id,actor_user_id=run.created_by,score=None,ai_status="failed",safe_error_code=skip_code,trace_id=getattr(job,"trace_id",None) or f"screening:{item.id}")
            self._aggregate(db,run); db.commit()
    async def _retry_or_finish(self,job,organization_id,item_id,code,stage):
        final=getattr(job,"attempts",1)>=getattr(job,"max_attempts",3)
        candidate_id=_uuid(item_id,"candidate")
        with self.sessions() as db:
            _candidate,item,blocked=lock_candidate_screening_item(db,organization_id,item_id,candidate_id,allow_unassociated=True,allow_missing_candidate=True,allow_blocked=True)
            run=db.get(ScreeningRun,item.run_id)
            if blocked:
                item.status="cancelled"; item.safe_error_code="candidate_unavailable"; item.finished_at=item.finished_at or datetime.now(timezone.utc); self._aggregate(db,run); db.commit(); raise PermanentJobError("screening_item_missing")
            item.status="failed" if final else "queued" if stage=="parse" else "parsed"; item.safe_error_code=code
            if stage=="parse": item.attempts=max(item.attempts,getattr(job,"attempts",1))
            if final: item.finished_at=item.finished_at or datetime.now(timezone.utc); self._aggregate(db,run)
            db.commit()
        if final: raise PermanentJobError(code)
        raise RetryableJobError(code)
    def _fail_terminal(self,organization_id,item_id,code,file_state=None):
        candidate_id=_uuid(item_id,"candidate")
        with self.sessions() as db:
            _candidate,item,blocked=lock_candidate_screening_item(db,organization_id,item_id,candidate_id,allow_unassociated=True,allow_missing_candidate=True,allow_blocked=True)
            run=db.get(ScreeningRun,item.run_id)
            item.status="cancelled" if blocked else "failed"; item.safe_error_code="candidate_unavailable" if blocked else code; item.finished_at=item.finished_at or datetime.now(timezone.utc)
            if file_state:
                stored=db.get(FileObject,item.file_object_id); stored.storage_state,stored.scan_status=file_state
            self._aggregate(db,run); db.commit()
    @staticmethod
    def _aggregate(db,run):
        aggregate_run(db,run)
