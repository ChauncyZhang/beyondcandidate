import asyncio
import json

import pytest
from pydantic import ValidationError

from server.app.llm.gateway import OpenAiCompatibleGateway, TransportResponse
from server.app.llm.policy import ProviderAllowlist
from server.app.llm.resume_profile import ResumeProfileRequest, ResumeProfileResult


def resolver(_host, port, type):
    return [(2, 1, 6, "", ("8.8.8.8", port))]


class Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def post(self, *args):
        self.calls.append(args)
        body = json.dumps({"choices": [{"message": {"content": json.dumps(self.result)}}]}).encode()
        return TransportResponse(200, body)


def valid_result(**changes):
    value = {
        "summary": "负责企业级智能体平台研发，具备端到端交付经验。",
        "summary_origin": "generated",
        "skills": ["Python", "RAG"],
        "experience": "2022.01-至今 某科技公司 AI 工程师，负责智能体平台研发。",
        "education": "2017.09-2021.06 某大学 计算机科学 本科",
        "evidence": {
            "summary": ["负责智能体平台研发和上线交付"],
            "skills": ["Python、RAG"],
            "experience": ["2022.01-至今 某科技公司 AI 工程师"],
            "education": ["2017.09-2021.06 某大学 计算机科学 本科"],
        },
    }
    value.update(changes)
    return value


def test_resume_profile_result_requires_evidence_for_every_populated_field():
    result = ResumeProfileResult.model_validate(valid_result())
    assert result.summary_origin == "generated"
    assert result.skills == ["Python", "RAG"]

    missing = valid_result()
    missing["evidence"]["education"] = []
    with pytest.raises(ValidationError):
        ResumeProfileResult.model_validate(missing)


def test_resume_profile_result_allows_genuinely_missing_sections_without_invention():
    result = ResumeProfileResult.model_validate(valid_result(
        summary=None,
        summary_origin=None,
        education=None,
        evidence={"summary": [], "skills": ["Python、RAG"], "experience": ["AI 工程师"], "education": []},
    ))
    assert result.summary is None
    assert result.education is None


def test_gateway_structures_resume_with_thinking_disabled_and_bounded_output():
    policy = ProviderAllowlist(
        {"provider": {"base_url": "https://provider.example/v1", "models": ["vision-model"]}},
        resolver=resolver,
    )
    transport = Transport(valid_result())
    gateway = OpenAiCompatibleGateway(policy, transport)

    evaluation = asyncio.run(gateway.extract_resume_profile(
        "provider",
        "vision-model",
        "secret",
        ResumeProfileRequest(resume_text="张三 13800138000\nPython、RAG\n负责智能体平台研发"),
        organization_id=None,
    ))

    payload = json.loads(transport.calls[0][4])
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 8192
    assert "13800138000" not in json.dumps(payload, ensure_ascii=False)
    assert evaluation.result.summary_origin == "generated"
