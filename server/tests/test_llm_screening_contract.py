import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from server.app.llm.gateway import EVALUATION_MAX_TOKENS, GatewayError, OpenAiCompatibleGateway, TransportResponse
from server.app.llm.policy import ProviderAllowlist
from server.app.llm.redaction import redact_screening_text
from server.app.llm.screening import (
    SCREENING_SYSTEM_PROMPT,
    MAX_JD_CHARS,
    MAX_RESUME_CHARS,
    ScreeningRequest,
    ScreeningResult,
)


def test_screening_prompt_requires_simplified_chinese_text_fields() -> None:
    assert "Simplified Chinese" in SCREENING_SYSTEM_PROMPT
    assert "summary, strengths, gaps" in SCREENING_SYSTEM_PROMPT


def resolver(host, port, type):
    return [(2, 1, 6, "", ("8.8.8.8", port))]


class Transport:
    def __init__(self, body, status=200):
        self.body = body; self.status=status
        self.calls = []

    def post(self, spec, address, path, headers, body, max_response_bytes):
        self.calls.append((spec, address, path, headers, body, max_response_bytes))
        return TransportResponse(self.status, self.body)


class SlowTransport(Transport):
    def __init__(self, body, delay):
        super().__init__(body)
        self.delay = delay

    def post(self, *args, **kwargs):
        time.sleep(self.delay)
        return super().post(*args, **kwargs)


def request(**changes):
    values = {
        "job_description": "Python backend role",
        "resume_text": "Built Python services",
    }
    values.update(changes)
    return ScreeningRequest(**values)


def provider_body(content, usage=None):
    document = {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document, ensure_ascii=False).encode()


def valid_result(**changes):
    value = {
        "score": 88,
        "dimensions": [
            {"key": "core_capability", "score": 35, "evidence": ["Python"], "gaps": []},
            {"key": "experience_depth", "score": 25, "evidence": ["Services"], "gaps": []},
            {"key": "role_seniority", "score": 18, "evidence": [], "gaps": []},
            {"key": "transferability", "score": 5, "evidence": [], "gaps": []},
            {"key": "explicit_constraints", "score": 5, "evidence": [], "gaps": []},
        ],
        "summary": "经验与职位较匹配",
        "strengths": ["Python"],
        "gaps": [],
        "risks": ["需确认到岗时间"],
        "questions": ["请说明高并发项目经验"],
    }
    value.update(changes)
    return value


def gateway(transport, **changes):
    policy = ProviderAllowlist(
        {"provider": {"base_url": "https://provider.example/v1", "models": ["model"]}},
        resolver=resolver,
    )
    return OpenAiCompatibleGateway(policy, transport, **changes)


def test_redaction_is_deterministic_and_removes_direct_identifiers():
    source = """姓名: 张三
Name: Jane Doe
邮箱: jane@example.com
Phone: +86 138-0013-8000
地址：上海市浦东新区
经历: Jane Doe 在 ACME 工作，备用电话 021-61234567。"""

    first = redact_screening_text(source, candidate_name="Jane Doe")

    assert first == redact_screening_text(source, candidate_name="Jane Doe")
    assert all(value not in first for value in ("张三", "Jane Doe", "jane@example.com", "138-0013-8000", "021-61234567", "上海市浦东新区"))
    assert "经历:" in first and "ACME" in first


def test_screening_request_bounds_all_provider_inputs():
    assert len(request(job_description="j" * MAX_JD_CHARS).job_description) == MAX_JD_CHARS
    assert len(request(resume_text="r" * MAX_RESUME_CHARS).resume_text) == MAX_RESUME_CHARS

    for changes in (
        {"job_description": "j" * (MAX_JD_CHARS + 1)},
        {"resume_text": "r" * (MAX_RESUME_CHARS + 1)},
    ):
        with pytest.raises(ValidationError):
            request(**changes)


def test_redaction_cannot_expand_input_or_output_past_contract_bounds():
    with pytest.raises(ValidationError):
        request(resume_text="r"*(MAX_RESUME_CHARS-len(" a@b.co"))+" a@b.co")
    with pytest.raises(ValidationError):
        ScreeningResult.model_validate(valid_result(summary="x"*(1000-len(" a@b.co"))+" a@b.co"))


def test_screening_request_redacts_before_provider_serialization():
    screening_request = request(
        job_description="Contact: hiring@example.test",
        resume_text="姓名: 张三\n张三 13800138000\nPython services",
        candidate_name="张三",
    )

    rendered = screening_request.provider_content()

    assert all(value not in rendered for value in ("hiring@example.test", "张三", "13800138000", "candidate_name"))
    assert "Python services" in rendered
    assert set(json.loads(rendered)) == {"job_description", "resume_text"}


def test_screening_request_rejects_rule_facts():
    with pytest.raises(ValidationError):
        request(rule_facts=["Python required: hit"])


def test_screening_result_is_strict_and_bounded():
    assert ScreeningResult.model_validate(valid_result()).score == 88

    invalid = (
        valid_result(score=101),
        valid_result(summary="x" * 1001),
        valid_result(strengths=["x"] * 11),
        valid_result(questions=["x" * 501]),
        {**valid_result(), "recommendation": "优先沟通"},
        {**valid_result(), "reasoning": "private chain of thought"},
    )
    for value in invalid:
        with pytest.raises(ValidationError):
            ScreeningResult.model_validate(value)


def test_screening_result_requires_exactly_one_of_each_dimension():
    missing = valid_result()
    missing["dimensions"] = missing["dimensions"][:-1]
    duplicate = valid_result()
    duplicate["dimensions"][-1] = {
        "key": "transferability", "score": 5, "evidence": [], "gaps": []
    }

    for value in (missing, duplicate):
        with pytest.raises(ValidationError):
            ScreeningResult.model_validate(value)


def test_screening_result_enforces_dimension_limits():
    value = valid_result(score=89)
    value["dimensions"][0]["score"] = 36

    with pytest.raises(ValidationError):
        ScreeningResult.model_validate(value)


def test_dimension_evidence_and_gaps_are_bounded():
    evidence = valid_result()
    evidence["dimensions"][0]["evidence"] = ["fact"] * 9
    gaps = valid_result()
    gaps["dimensions"][0]["gaps"] = ["gap"] * 9

    for value in (evidence, gaps):
        with pytest.raises(ValidationError):
            ScreeningResult.model_validate(value)


def test_screening_result_requires_dimension_total_to_equal_score():
    value = valid_result(score=89)

    with pytest.raises(ValidationError):
        ScreeningResult.model_validate(value)


def test_gateway_evaluate_reuses_pinned_transport_and_returns_bounded_facts():
    transport = Transport(provider_body(valid_result(), {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}))

    evaluation = asyncio.run(gateway(transport).evaluate(
        "provider", "model", "sk-secret", request(), system_prompt="persisted prompt v2"
    ))

    assert evaluation.result.score == 88 and evaluation.latency_ms >= 0
    assert evaluation.usage == {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200}
    _, address, path, headers, body, _ = transport.calls[0]
    assert address == "8.8.8.8" and path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-secret"
    payload = json.loads(body)
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["content"] == "persisted prompt v2"
    assert "Python backend role" in payload["messages"][1]["content"]
    assert set(json.loads(payload["messages"][1]["content"])) == {"job_description", "resume_text"}
    assert payload["max_tokens"] == EVALUATION_MAX_TOKENS == 8192


def test_gateway_evaluate_accepts_provider_specific_usage_details():
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 80,
        "total_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 100},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }

    evaluation = asyncio.run(
        gateway(Transport(provider_body(valid_result(), usage))).evaluate(
            "provider", "model", "sk-secret", request(), system_prompt="persisted prompt v2"
        )
    )

    assert evaluation.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 80,
        "total_tokens": 200,
    }


def test_gateway_evaluation_has_a_longer_budget_than_the_connection_probe():
    transport = SlowTransport(provider_body(valid_result()), delay=0.03)

    evaluation = asyncio.run(gateway(
        transport,
        total_timeout=0.01,
        evaluation_total_timeout=0.1,
    ).evaluate("provider", "model", "sk-secret", request(), system_prompt="persisted prompt v2"))

    assert evaluation.result.score == 88


def test_gateway_disables_default_thinking_for_official_glm_models():
    transport = Transport(provider_body(valid_result()))
    policy = ProviderAllowlist(
        {"zai": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-5.2"]}},
        resolver=resolver,
    )

    asyncio.run(OpenAiCompatibleGateway(policy, transport).evaluate(
        "zai", "glm-5.2", "sk-secret", request(), system_prompt="persisted prompt v2"
    ))

    payload = json.loads(transport.calls[0][4])
    assert payload["thinking"] == {"type": "disabled"}


def test_gateway_evaluate_redacts_identifiers_echoed_by_provider():
    echoed=valid_result(summary="联系 jane@example.test 或 13800138000",questions=["地址: 上海市浦东新区"])
    evaluation=asyncio.run(gateway(Transport(provider_body(echoed))).evaluate(
        "provider","model","secret",request(),system_prompt="persisted prompt v2"
    ))
    rendered=evaluation.result.model_dump_json()
    assert all(value not in rendered for value in ("jane@example.test","13800138000","上海市浦东新区"))


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not-json", "provider_response_invalid"),
        (provider_body({**valid_result(), "unknown": True}), "provider_response_invalid"),
        (provider_body(valid_result(), {"total_tokens": "many"}), "provider_response_invalid"),
    ],
)
def test_gateway_evaluate_maps_malformed_output_to_safe_error(body, code):
    with pytest.raises(GatewayError) as raised:
        asyncio.run(gateway(Transport(body)).evaluate(
            "provider", "model", "secret", request(), system_prompt="persisted prompt v2"
        ))
    assert raised.value.safe_code == code and str(raised.value) == code


def test_gateway_evaluate_maps_oversize_response_to_safe_error():
    with pytest.raises(GatewayError) as raised:
        asyncio.run(gateway(Transport(b"x" * 101), max_response_bytes=100).evaluate(
            "provider", "model", "secret", request(), system_prompt="persisted prompt v2"
        ))
    assert raised.value.safe_code == "provider_response_too_large"


@pytest.mark.parametrize("status",[400,422])
def test_gateway_evaluate_treats_rejected_requests_as_permanent(status):
    with pytest.raises(GatewayError,match="provider_request_rejected") as raised:
        asyncio.run(gateway(Transport(b"private provider body",status)).evaluate(
            "provider","model","secret",request(),system_prompt="persisted prompt v2"
        ))
    assert raised.value.safe_code=="provider_request_rejected" and "private provider body" not in str(raised.value)
