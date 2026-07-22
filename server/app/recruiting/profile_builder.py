import uuid
from dataclasses import dataclass

from sqlalchemy import select

from server.app.llm.gateway import GatewayError
from server.app.llm.models import LlmInvocation, LlmProviderConfig
from server.app.llm.resume_profile import ResumeProfileRequest
from server.app.recruiting.resume_profile import extract_resume_profile


PROFILE_VERSION = "resume-profile-v1"


@dataclass(frozen=True)
class ProfileBuild:
    data: dict[str, object]
    status: str
    source: str
    safe_error_code: str | None = None


class ResumeProfileBuilder:
    def __init__(self, sessions, gateway, cipher):
        self.sessions = sessions
        self.gateway = gateway
        self.cipher = cipher

    @staticmethod
    def _rule_fallback(text: str, *, used_ocr: bool, safe_error_code: str | None = None) -> ProfileBuild:
        data = extract_resume_profile(text, include_metadata=True)
        source = "ocr_rules" if used_ocr else "rules"
        data["source"] = source
        return ProfileBuild(data, str(data["status"]), source, safe_error_code)

    async def build(
        self,
        organization_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None,
        resume_text: str,
        candidate_name: str | None,
        used_ocr: bool,
        trace_id: str | None = None,
    ) -> ProfileBuild:
        fallback = self._rule_fallback(resume_text, used_ocr=used_ocr)
        with self.sessions() as db:
            config = db.scalar(select(LlmProviderConfig).where(LlmProviderConfig.organization_id == organization_id))
            if config is None or not config.enabled or config.encrypted_api_key is None:
                return fallback
            if config.allowed_job_ids and (job_id is None or str(job_id) not in config.allowed_job_ids):
                return fallback
            config_id, provider_id, model = config.id, config.provider_id, config.model
            try:
                api_key = self.cipher.decrypt(config.encrypted_api_key)
            except ValueError:
                return self._rule_fallback(resume_text, used_ocr=used_ocr, safe_error_code="llm_key_unavailable")

        safe_error_code = None
        usage: dict[str, int] = {}
        latency_ms = None
        try:
            evaluation = await self.gateway.extract_resume_profile(
                provider_id,
                model,
                api_key,
                ResumeProfileRequest(resume_text=resume_text, candidate_name=candidate_name),
                organization_id=organization_id,
            )
            usage = evaluation.usage
            latency_ms = evaluation.latency_ms
            source = "ocr_llm" if used_ocr else "llm"
            data = evaluation.result.display_profile(source=source)
            data["evidence"] = evaluation.result.evidence.model_dump()
            result = ProfileBuild(data, str(data["status"]), source)
            invocation_status = "succeeded"
        except (GatewayError, ValueError) as error:
            safe_error_code = error.safe_code if isinstance(error, GatewayError) else "resume_text_unavailable"
            result = self._rule_fallback(resume_text, used_ocr=used_ocr, safe_error_code=safe_error_code)
            invocation_status = "failed"
        except Exception:
            safe_error_code = "profile_provider_unavailable"
            result = self._rule_fallback(resume_text, used_ocr=used_ocr, safe_error_code=safe_error_code)
            invocation_status = "failed"

        try:
            with self.sessions() as db:
                db.add(LlmInvocation(
                    organization_id=organization_id,
                    config_id=config_id,
                    provider_id=provider_id,
                    model=model,
                    request_field_manifest=["resume_text"],
                    status=invocation_status,
                    latency_ms=latency_ms,
                    usage=usage,
                    safe_error_code=safe_error_code,
                    trace_id=trace_id,
                ))
                db.commit()
        except Exception:
            # Profile extraction must remain best-effort; recruiting can continue with the result.
            pass
        return result
