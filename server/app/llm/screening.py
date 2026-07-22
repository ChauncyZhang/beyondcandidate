import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.app.llm.redaction import redact_screening_text


MAX_JD_CHARS = 12_000
MAX_RESUME_CHARS = 30_000
MAX_SUMMARY_CHARS = 1_000
MAX_LIST_ITEMS = 10
MAX_ITEM_CHARS = 500

DIMENSION_LIMITS = {
    "core_capability": 35,
    "experience_depth": 25,
    "role_seniority": 20,
    "transferability": 10,
    "explicit_constraints": 10,
}

SCREENING_SYSTEM_PROMPT = """You evaluate a candidate for recruiter screening using only the supplied redacted job description and redacted resume. Return one JSON object with exactly these fields: score, dimensions, summary, strengths, gaps, risks, questions. You must not return recommendation or any other field. dimensions must contain each key exactly once: core_capability (0-35), experience_depth (0-25), role_seniority (0-20), transferability (0-10), explicit_constraints (0-10). Every dimension must contain key, integer score, evidence, and gaps. The total score must equal the sum of all five dimension scores. Evidence must use only facts present in the supplied inputs. Evaluate explicit_constraints only from non-sensitive constraints explicitly stated in the job description. Do not infer identity or protected traits. All text fields (summary, strengths, gaps, dimension evidence, dimension gaps, risks, questions) must be written in Simplified Chinese (简体中文)."""


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


ResultItem = Annotated[str, Field(min_length=1, max_length=MAX_ITEM_CHARS)]


class ScreeningRequest(ContractModel):
    job_description: str = Field(min_length=1, max_length=MAX_JD_CHARS)
    resume_text: str = Field(min_length=1, max_length=MAX_RESUME_CHARS)
    candidate_name: str | None = Field(default=None, min_length=1, max_length=200, exclude=True, repr=False)

    @model_validator(mode="after")
    def redact_inputs(self):
        self.job_description = redact_screening_text(self.job_description)
        self.resume_text = redact_screening_text(self.resume_text, candidate_name=self.candidate_name)
        if len(self.job_description)>MAX_JD_CHARS or len(self.resume_text)>MAX_RESUME_CHARS: raise ValueError("redacted screening input exceeds bounds")
        return self

    def provider_content(self) -> str:
        return json.dumps(
            {"job_description": self.job_description, "resume_text": self.resume_text},
            ensure_ascii=False,
            separators=(",", ":"),
        )


class DimensionScore(ContractModel):
    key: Literal[
        "core_capability",
        "experience_depth",
        "role_seniority",
        "transferability",
        "explicit_constraints",
    ]
    score: int = Field(ge=0)
    evidence: list[ResultItem] = Field(max_length=8)
    gaps: list[ResultItem] = Field(max_length=8)

    @model_validator(mode="after")
    def redact_items(self):
        self.evidence = [redact_screening_text(value) for value in self.evidence]
        self.gaps = [redact_screening_text(value) for value in self.gaps]
        if any(len(value)>MAX_ITEM_CHARS for value in (*self.evidence, *self.gaps)):
            raise ValueError("redacted dimension output exceeds bounds")
        return self


class ScreeningResult(ContractModel):
    score: int = Field(ge=0, le=100)
    dimensions: list[DimensionScore] = Field(min_length=5, max_length=5)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    strengths: list[ResultItem] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    gaps: list[ResultItem] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    risks: list[ResultItem] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)
    questions: list[ResultItem] = Field(default_factory=list, max_length=MAX_LIST_ITEMS)

    @model_validator(mode="after")
    def validate_and_redact_output(self):
        keys = [dimension.key for dimension in self.dimensions]
        if len(set(keys)) != len(DIMENSION_LIMITS) or set(keys) != set(DIMENSION_LIMITS):
            raise ValueError("dimensions must contain each required key exactly once")
        for dimension in self.dimensions:
            if dimension.score > DIMENSION_LIMITS[dimension.key]:
                raise ValueError(f"dimension score exceeds limit for {dimension.key}")
        if sum(dimension.score for dimension in self.dimensions) != self.score:
            raise ValueError("score must equal the sum of dimension scores")
        self.summary = redact_screening_text(self.summary)
        for field in ("strengths", "gaps", "risks", "questions"):
            setattr(self, field, [redact_screening_text(value) for value in getattr(self, field)])
        if len(self.summary)>MAX_SUMMARY_CHARS or any(len(value)>MAX_ITEM_CHARS for field in ("strengths","gaps","risks","questions") for value in getattr(self,field)): raise ValueError("redacted screening output exceeds bounds")
        return self


@dataclass(frozen=True)
class ScreeningEvaluation:
    result: ScreeningResult
    latency_ms: int
    usage: dict[str, int]
