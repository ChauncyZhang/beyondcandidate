import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.app.llm.redaction import redact_screening_text


MAX_RESUME_CHARS = 30_000
MAX_SUMMARY_CHARS = 800
MAX_SECTION_CHARS = 4_000
MAX_ITEM_CHARS = 500
MAX_SKILLS = 30
MAX_EVIDENCE_ITEMS = 12

RESUME_PROFILE_SYSTEM_PROMPT = """Extract a job-independent candidate profile from the supplied redacted resume text. Return one JSON object with exactly these fields: summary, summary_origin, skills, experience, education, evidence. summary_origin must be resume when the resume contains an explicit self-summary, generated when you create a concise candidate overview only from supported resume facts, or null when no safe summary can be produced. skills must be an array of strings. experience and education must preserve important employers, schools, roles and dates without inventing details. evidence must contain exactly summary, skills, experience and education arrays with short verbatim facts from the supplied text. Every populated field must have supporting evidence. Return null or an empty array when a field is genuinely unavailable. Do not infer identity, protected traits, compensation, intent or facts absent from the resume."""


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


ProfileItem = Annotated[str, Field(min_length=1, max_length=MAX_ITEM_CHARS)]


class ResumeProfileRequest(ContractModel):
    resume_text: str = Field(min_length=1, max_length=MAX_RESUME_CHARS)
    candidate_name: str | None = Field(default=None, min_length=1, max_length=200, exclude=True, repr=False)

    @model_validator(mode="after")
    def redact_input(self):
        self.resume_text = redact_screening_text(self.resume_text, candidate_name=self.candidate_name)
        if not self.resume_text.strip() or len(self.resume_text) > MAX_RESUME_CHARS:
            raise ValueError("redacted resume profile input is invalid")
        return self

    def provider_content(self) -> str:
        return json.dumps({"resume_text": self.resume_text}, ensure_ascii=False, separators=(",", ":"))


class ResumeProfileEvidence(ContractModel):
    summary: list[ProfileItem] = Field(default_factory=list, max_length=MAX_EVIDENCE_ITEMS)
    skills: list[ProfileItem] = Field(default_factory=list, max_length=MAX_EVIDENCE_ITEMS)
    experience: list[ProfileItem] = Field(default_factory=list, max_length=MAX_EVIDENCE_ITEMS)
    education: list[ProfileItem] = Field(default_factory=list, max_length=MAX_EVIDENCE_ITEMS)


class ResumeProfileResult(ContractModel):
    summary: str | None = Field(default=None, min_length=1, max_length=MAX_SUMMARY_CHARS)
    summary_origin: Literal["resume", "generated"] | None = None
    skills: list[ProfileItem] = Field(default_factory=list, max_length=MAX_SKILLS)
    experience: str | None = Field(default=None, min_length=1, max_length=MAX_SECTION_CHARS)
    education: str | None = Field(default=None, min_length=1, max_length=MAX_SECTION_CHARS)
    evidence: ResumeProfileEvidence

    @model_validator(mode="after")
    def validate_evidence_and_redact(self):
        if (self.summary is None) != (self.summary_origin is None):
            raise ValueError("summary and summary_origin must be present together")
        populated = {
            "summary": self.summary is not None,
            "skills": bool(self.skills),
            "experience": self.experience is not None,
            "education": self.education is not None,
        }
        for field, present in populated.items():
            if present and not getattr(self.evidence, field):
                raise ValueError(f"{field} requires evidence")
        if self.summary is not None:
            self.summary = redact_screening_text(self.summary)
        if self.experience is not None:
            self.experience = redact_screening_text(self.experience)
        if self.education is not None:
            self.education = redact_screening_text(self.education)
        self.skills = [redact_screening_text(value) for value in self.skills]
        for field in ("summary", "skills", "experience", "education"):
            setattr(self.evidence, field, [redact_screening_text(value) for value in getattr(self.evidence, field)])
        return self

    def display_profile(self, *, source: str) -> dict[str, object]:
        populated = sum((bool(self.summary), bool(self.skills), bool(self.experience), bool(self.education)))
        return {
            "summary": self.summary,
            "summary_origin": self.summary_origin,
            "skills": self.skills,
            "experience": self.experience,
            "education": self.education,
            "status": "ready" if populated == 4 else "partial" if populated else "unavailable",
            "source": source,
        }


def validate_resume_profile_content(content: str) -> ResumeProfileResult:
    document = json.loads(content)
    if not isinstance(document, dict):
        raise ValueError
    if isinstance(document.get("skills"), str):
        document["skills"] = [document["skills"]] if document["skills"].strip() else []
    evidence = document.get("evidence")
    if isinstance(evidence, dict):
        for field in ("summary", "skills", "experience", "education"):
            value = evidence.get(field)
            if isinstance(value, str):
                evidence[field] = [value] if value.strip() else []
    return ResumeProfileResult.model_validate(document)


@dataclass(frozen=True)
class ResumeProfileEvaluation:
    result: ResumeProfileResult
    latency_ms: int
    usage: dict[str, int]
