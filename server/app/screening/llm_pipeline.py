import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, select

from server.app.llm.gateway import GatewayError
from server.app.llm.models import LlmInvocation, LlmProviderConfig, LlmScreeningEvaluation, PromptVersion
from server.app.llm.screening import MAX_JD_CHARS, MAX_RESUME_CHARS, ScreeningRequest
from server.app.llm.redaction import redact_screening_text
from server.app.queue.models import BackgroundJob
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Candidate, JobJdVersion, Resume
from server.app.recruiting.tasks import normalize_llm_terminal_safe_error_code
from server.app.screening.models import ScreeningItem, ScreeningResult, ScreeningRun
from server.app.screening.progress import aggregate_run
from server.app.screening.actions import CandidateTombstoned, lock_candidate_screening_item
from server.app.screening.routing import derive_screening_outcome, route_llm_screening_terminal


_TRANSIENT_ERRORS = {"provider_unavailable", "provider_quota_or_rate_limited", "provider_response_invalid"}
_REQUEST_FIELDS = ["job_description", "resume_text"]


def _payload_uuid(payload, name):
    try:
        return uuid.UUID(str(payload[name]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise PermanentJobError("llm_job_payload_invalid") from None


def _invocation_id(queue_job_id, attempt_no):
    return uuid.uuid5(queue_job_id, f"llm-invocation:{attempt_no}")


class LlmScreeningPipeline:
    def __init__(self, sessions, gateway, cipher):
        self.sessions = sessions
        self.gateway = gateway
        self.cipher = cipher

    async def evaluate_item(self, job):
        ids = self._ids(job.payload)
        attempt_no = max(1, int(getattr(job, "attempts", 1)))
        max_attempts = max(1, int(getattr(job, "max_attempts", 3)))
        queue_job_id = uuid.UUID(str(job.id))

        with self.sessions() as db:
            completed=db.scalar(select(LlmInvocation.id).where(LlmInvocation.organization_id==ids["organization_id"],LlmInvocation.queue_job_id==queue_job_id,LlmInvocation.status=="succeeded"))
            item_state=db.scalar(select(ScreeningItem.llm_status).where(ScreeningItem.organization_id==ids["organization_id"],ScreeningItem.id==ids["screening_item_id"]))
            if completed or item_state in {"succeeded","failed","skipped"}: return
            existing = db.scalar(
                select(LlmInvocation).where(
                    LlmInvocation.organization_id == ids["organization_id"],
                    LlmInvocation.queue_job_id == queue_job_id,
                    LlmInvocation.attempt_no == attempt_no,
                )
            )
            if existing:
                return
            loaded = self._load(db, ids, queue_job_id)
            if loaded is None:
                raise PermanentJobError("screening_item_missing")
            item, run, result, resume, jd, candidate, config, prompt = loaded
            request = self._request(jd, resume, candidate)
            system_prompt = self._system_prompt(prompt)
            input_sha256 = hashlib.sha256(request.provider_content().encode()).hexdigest()
            skip_code = self._skip_code(config, run, ids["config_version"])
            try:
                _candidate,item,_blocked=lock_candidate_screening_item(db,ids["organization_id"],ids["screening_item_id"],candidate.id)
            except CandidateTombstoned:
                db.rollback()
                return
            if item.llm_status != "queued":
                db.rollback()
                return
            if skip_code:
                if config is not None:
                    self._append_invocation(db,ids,queue_job_id,attempt_no,config,input_sha256,status="failed",safe_error_code=skip_code,trace_id=getattr(job,"trace_id",None))
                item.llm_status = "skipped"
                item.llm_safe_error_code = skip_code
                item.llm_attempts = max(item.llm_attempts, attempt_no)
                finished_at=datetime.now(timezone.utc); item.llm_finished_at = item.llm_finished_at or finished_at; item.finished_at=item.finished_at or finished_at
                route_llm_screening_terminal(
                    db,
                    organization_id=ids["organization_id"],
                    item_id=ids["screening_item_id"],
                    actor_user_id=run.created_by,
                    score=None,
                    ai_status="failed",
                    safe_error_code=skip_code,
                    trace_id=getattr(job, "trace_id", None) or f"llm:{queue_job_id}",
                )
                aggregate_run(db, run)
                db.commit()
                return
            item.llm_status = "running"
            item.llm_safe_error_code = None
            item.llm_attempts = max(item.llm_attempts, attempt_no)
            item.llm_started_at = item.llm_started_at or datetime.now(timezone.utc)
            db.commit()
            provider_id, model, encrypted_api_key = config.provider_id, config.model, config.encrypted_api_key

        try:
            api_key = self.cipher.decrypt(encrypted_api_key)
        except Exception:
            return self._finish_failure(job, ids, queue_job_id, attempt_no, input_sha256, "llm_key_decryption_failed", False)

        try:
            evaluation = await self.gateway.evaluate(
                provider_id,
                model,
                api_key,
                request,
                organization_id=ids["organization_id"],
                system_prompt=system_prompt,
            )
        except GatewayError as error:
            code = error.safe_code
            return self._finish_failure(job, ids, queue_job_id, attempt_no, input_sha256, code, code in _TRANSIENT_ERRORS and attempt_no < max_attempts)
        except Exception:
            return self._finish_failure(job, ids, queue_job_id, attempt_no, input_sha256, "provider_unavailable", attempt_no < max_attempts)

        with self.sessions() as db:
            _candidate,item,blocked=lock_candidate_screening_item(db,ids["organization_id"],ids["screening_item_id"],candidate.id,allow_blocked=True)
            run=db.get(ScreeningRun,item.run_id)
            if blocked:
                self._discard_for_deletion(db,item,run)
                db.commit()
                return
            existing = db.scalar(
                select(LlmInvocation).where(
                    LlmInvocation.organization_id == ids["organization_id"],
                    LlmInvocation.queue_job_id == queue_job_id,
                    LlmInvocation.attempt_no == attempt_no,
                )
            )
            if existing:
                return
            loaded = self._load(db, ids, queue_job_id)
            if loaded is None:
                raise PermanentJobError("screening_item_missing")
            item, run, _result, _resume, _jd, _candidate, config, _prompt = loaded
            invocation = self._append_invocation(
                db, ids, queue_job_id, attempt_no, config, input_sha256,
                status="succeeded", latency_ms=evaluation.latency_ms, usage=evaluation.usage,
                trace_id=getattr(job, "trace_id", None),
            )
            facts = evaluation.result
            screening_evaluation = LlmScreeningEvaluation(
                id=uuid.uuid5(invocation.id, "screening-evaluation"),
                organization_id=ids["organization_id"],
                screening_result_id=ids["screening_result_id"],
                invocation_id=invocation.id,
                prompt_version_id=ids["prompt_version_id"],
                score=facts.score,
                recommendation=derive_screening_outcome(facts.score).recommendation,
                summary=facts.summary,
                strengths=facts.strengths,
                gaps=facts.gaps,
                risks=facts.risks,
                interview_questions=facts.questions,
                dimensions=[dimension.model_dump() for dimension in facts.dimensions],
            )
            db.add(screening_evaluation)
            item.llm_status = "succeeded"
            item.llm_safe_error_code = None
            finished_at=datetime.now(timezone.utc); item.llm_finished_at = item.llm_finished_at or finished_at; item.finished_at=item.finished_at or finished_at
            db.flush()
            route_llm_screening_terminal(
                db,
                organization_id=ids["organization_id"],
                item_id=ids["screening_item_id"],
                actor_user_id=run.created_by,
                score=facts.score,
                ai_status="succeeded",
                safe_error_code=None,
                trace_id=getattr(job, "trace_id", None) or f"llm:{queue_job_id}",
                evaluation_id=screening_evaluation.id,
                invocation_id=invocation.id,
            )
            aggregate_run(db, run)
            db.commit()

    def _finish_failure(self, job, ids, queue_job_id, attempt_no, input_sha256, code, retryable):
        code = normalize_llm_terminal_safe_error_code(code)
        with self.sessions() as db:
            candidate_id=db.scalar(select(ScreeningItem.candidate_id).where(ScreeningItem.organization_id==ids["organization_id"],ScreeningItem.id==ids["screening_item_id"]))
            if candidate_id is None:
                raise PermanentJobError("screening_item_missing")
            _candidate,item,blocked=lock_candidate_screening_item(db,ids["organization_id"],ids["screening_item_id"],candidate_id,allow_blocked=True)
            run=db.get(ScreeningRun,item.run_id)
            if blocked:
                self._discard_for_deletion(db,item,run)
                db.commit()
                return
            existing = db.scalar(
                select(LlmInvocation).where(
                    LlmInvocation.organization_id == ids["organization_id"],
                    LlmInvocation.queue_job_id == queue_job_id,
                    LlmInvocation.attempt_no == attempt_no,
                )
            )
            if not existing:
                loaded = self._load(db, ids, queue_job_id)
                if loaded is None:
                    raise PermanentJobError("screening_item_missing")
                item, run, _result, _resume, _jd, _candidate, config, _prompt = loaded
                self._append_invocation(
                    db, ids, queue_job_id, attempt_no, config, input_sha256,
                    status="failed", safe_error_code=code, trace_id=getattr(job, "trace_id", None),
                )
                item.llm_status = "queued" if retryable else "failed"
                item.llm_safe_error_code = code
                if retryable:
                    item.llm_finished_at=None; item.finished_at=None
                else:
                    finished_at=datetime.now(timezone.utc); item.llm_finished_at=item.llm_finished_at or finished_at; item.finished_at=item.finished_at or finished_at
                    route_llm_screening_terminal(
                        db,
                        organization_id=ids["organization_id"],
                        item_id=ids["screening_item_id"],
                        actor_user_id=run.created_by,
                        score=None,
                        ai_status="failed",
                        safe_error_code=code,
                        trace_id=getattr(job, "trace_id", None) or f"llm:{queue_job_id}",
                    )
                aggregate_run(db, run)
                db.commit()
        if retryable:
            raise RetryableJobError(code)
        raise PermanentJobError(code)

    @staticmethod
    def _discard_for_deletion(db,item,run):
        if item.llm_status=="running":
            finished_at=datetime.now(timezone.utc)
            item.llm_status="skipped"; item.llm_safe_error_code="candidate_unavailable"; item.llm_finished_at=item.llm_finished_at or finished_at; item.finished_at=item.finished_at or finished_at
            aggregate_run(db,run)

    @staticmethod
    def _ids(payload):
        values = {name: _payload_uuid(payload, name) for name in (
            "organization_id", "screening_item_id", "screening_result_id", "config_id", "prompt_version_id"
        )}
        values["application_id"] = _payload_uuid(payload, "application_id") if "application_id" in payload else None
        try:
            values["config_version"] = int(payload["config_version"])
        except (KeyError, TypeError, ValueError):
            raise PermanentJobError("llm_job_payload_invalid") from None
        if values["config_version"] < 1:
            raise PermanentJobError("llm_job_payload_invalid")
        return values

    @staticmethod
    def _load(db, ids, queue_job_id, lock_item=False):
        statement = (
            select(ScreeningItem, ScreeningRun, ScreeningResult, Resume, JobJdVersion, Candidate, LlmProviderConfig, PromptVersion)
            .join(ScreeningRun, and_(ScreeningRun.organization_id == ScreeningItem.organization_id, ScreeningRun.id == ScreeningItem.run_id))
            .join(ScreeningResult, and_(ScreeningResult.organization_id == ScreeningItem.organization_id, ScreeningResult.id == ids["screening_result_id"], ScreeningResult.item_id == ScreeningItem.id))
            .join(Resume, and_(Resume.organization_id == ScreeningItem.organization_id, Resume.id == ScreeningItem.resume_id))
            .join(JobJdVersion, and_(JobJdVersion.organization_id == ScreeningRun.organization_id, JobJdVersion.id == ScreeningRun.jd_version_id, JobJdVersion.job_id == ScreeningRun.job_id))
            .join(Candidate, and_(Candidate.organization_id == ScreeningItem.organization_id, Candidate.id == ScreeningItem.candidate_id))
            .outerjoin(LlmProviderConfig, and_(LlmProviderConfig.organization_id == ScreeningItem.organization_id, LlmProviderConfig.id == ids["config_id"]))
            .join(PromptVersion, and_(PromptVersion.organization_id == ScreeningItem.organization_id, PromptVersion.id == ids["prompt_version_id"]))
            .join(BackgroundJob, and_(BackgroundJob.organization_id == ScreeningItem.organization_id, BackgroundJob.id == queue_job_id))
            .where(ScreeningItem.organization_id == ids["organization_id"], ScreeningItem.id == ids["screening_item_id"], ScreeningItem.status == "scored", Candidate.deleted_at.is_(None))
        )
        if ids["application_id"] is not None:
            statement = statement.where(ScreeningItem.application_id == ids["application_id"])
        if lock_item:
            statement = statement.with_for_update()
        return db.execute(statement).one_or_none()

    @staticmethod
    def _request(jd, resume, candidate):
        content = jd.content if isinstance(jd.content, dict) else {}
        jd_text = content.get("description") or content.get("text") or content.get("jd_text") or ""
        if not isinstance(jd_text, str) or not jd_text:
            raise PermanentJobError("llm_input_invalid")
        resume_text = resume.parsed_text or ""
        if not resume_text:
            raise PermanentJobError("llm_input_invalid")
        redacted_jd=redact_screening_text(jd_text)[:MAX_JD_CHARS]
        redacted_resume=redact_screening_text(resume_text,candidate_name=candidate.display_name)[:MAX_RESUME_CHARS]
        return ScreeningRequest(job_description=redacted_jd,resume_text=redacted_resume)

    @staticmethod
    def _system_prompt(prompt):
        content = prompt.content if isinstance(prompt.content, dict) else {}
        system_prompt = content.get("system")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise PermanentJobError("llm_prompt_invalid")
        return system_prompt

    @staticmethod
    def _skip_code(config, run, expected_version):
        if config is None:
            return "llm_config_deleted"
        if config.version != expected_version:
            return "llm_config_changed"
        if not config.enabled:
            return "llm_config_disabled"
        allowed = {str(value) for value in (config.allowed_job_ids or [])}
        if allowed and str(run.job_id) not in allowed:
            return "llm_job_not_allowed"
        return None

    @staticmethod
    def _append_invocation(db, ids, queue_job_id, attempt_no, config, input_sha256, *, status, safe_error_code=None, latency_ms=None, usage=None, trace_id=None):
        invocation = LlmInvocation(
            id=_invocation_id(queue_job_id, attempt_no),
            organization_id=ids["organization_id"],
            config_id=ids["config_id"],
            prompt_version_id=ids["prompt_version_id"],
            screening_result_id=ids["screening_result_id"],
            queue_job_id=queue_job_id,
            attempt_no=attempt_no,
            config_version=ids["config_version"],
            input_sha256=input_sha256,
            provider_id=config.provider_id,
            model=config.model,
            request_field_manifest=list(_REQUEST_FIELDS),
            status=status,
            latency_ms=latency_ms,
            usage=dict(usage or {}),
            safe_error_code=safe_error_code,
            trace_id=trace_id,
        )
        db.add(invocation)
        return invocation
