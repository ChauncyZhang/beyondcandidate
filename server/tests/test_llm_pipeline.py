import asyncio
import uuid
from datetime import datetime,timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from server.app.llm.gateway import GatewayError
from server.app.llm.models import LlmInvocation, LlmProviderConfig, LlmScreeningEvaluation, PromptVersion
from server.app.llm.screening import MAX_RESUME_CHARS,ScreeningEvaluation, ScreeningResult
from server.app.screening.schemas import LlmEvaluationOut
from server.app.queue.models import BackgroundJob
from server.app.queue.service import PermanentJobError, RetryableJobError, normalize_safe_code
from server.app.identity.models import AuditLog
from server.app.recruiting.models import Application, ApplicationReviewTask, ApplicationStageEvent, Candidate, JobJdVersion, Resume
from server.app.screening.llm_pipeline import LlmScreeningPipeline
from server.app.screening.routing import route_llm_screening_terminal
from server.app.screening.terminal import LlmTerminalFinalizer, finalize_llm_dead_letter
from server.app.screening.models import ScreeningItem, ScreeningResult as RuleResult, ScreeningRun
from server.app.screening.progress import aggregate_run
from server.app.llm.security import ApiKeyCipher
from server.app.talent.models import TalentPoolMembership
from server.tests.test_screening_pipeline import seeded_pipeline
from server.tests.test_screening_api import login


class Gateway:
    def __init__(self, outcome=None, inspect=None):
        self.outcome = outcome or ScreeningEvaluation(
            ScreeningResult(
                score=91,
                dimensions=[
                    {"key":"core_capability","score":35,"evidence":["Python"],"gaps":[]},
                    {"key":"experience_depth","score":25,"evidence":["Services"],"gaps":[]},
                    {"key":"role_seniority","score":20,"evidence":[],"gaps":[]},
                    {"key":"transferability","score":6,"evidence":[],"gaps":[]},
                    {"key":"explicit_constraints","score":5,"evidence":[],"gaps":[]},
                ],
                summary="Strong Python match",
                strengths=["Python services"],
                gaps=[],
                risks=["Confirm availability"],
                questions=["Describe a scaling incident"],
            ),
            17,
            {"total_tokens": 42},
        )
        self.inspect = inspect
        self.calls = []

    async def evaluate(self, provider_id, model, api_key, request, **kwargs):
        if self.inspect:
            self.inspect()
        self.calls.append((provider_id, model, api_key, request, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def screening_evaluation(score):
    remaining = score
    dimensions = []
    for key, limit in (
        ("core_capability", 35),
        ("experience_depth", 25),
        ("role_seniority", 20),
        ("transferability", 10),
        ("explicit_constraints", 10),
    ):
        dimension_score = min(remaining, limit)
        remaining -= dimension_score
        dimensions.append({"key": key, "score": dimension_score, "evidence": [], "gaps": []})
    return ScreeningEvaluation(
        ScreeningResult(
            score=score,
            dimensions=dimensions,
            summary="Safe summary",
            strengths=[],
            gaps=[],
            risks=[],
            questions=[],
        ),
        1,
        {},
    )


def prepared(tmp_path, *, allowed_job_ids=None):
    app, rule_pipeline, _storage, _scanner, parse_job, run, item = seeded_pipeline(tmp_path)
    asyncio.run(rule_pipeline.parse_item(parse_job))
    cipher = ApiKeyCipher(b"ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=")
    with app.state.identity_store.sync_session() as db:
        stored = db.get(ScreeningItem, uuid.UUID(item["id"]))
        aggregate = db.get(ScreeningRun, uuid.UUID(run["id"]))
        config = LlmProviderConfig(
            organization_id=stored.organization_id,
            provider_id="approved",
            model="model",
            encrypted_api_key=cipher.encrypt("sk-private"),
            enabled=True,
            allowed_job_ids=allowed_job_ids or [],
            version=3,
            created_by=aggregate.created_by,
            updated_by=aggregate.created_by,
        )
        db.add(config)
        db.commit()
        score_job = SimpleNamespace(
            payload={
                "organization_id": str(stored.organization_id),
                "screening_item_id": str(stored.id),
                "jd_version_id": str(aggregate.jd_version_id),
                "rule_version_id": str(aggregate.rule_version_id),
                "rule_engine_version": "rule-v1",
            },
            attempts=1,
            max_attempts=3,
            trace_id="rule-trace",
        )
    asyncio.run(rule_pipeline.score_item(score_job))
    with app.state.identity_store.sync_session() as db:
        queue_job = db.scalar(select(BackgroundJob).where(BackgroundJob.type == "screening.llm_score_item"))
        queue_job_id = queue_job.id
        payload = dict(queue_job.payload)
        organization_id = queue_job.organization_id
    job = SimpleNamespace(
        id=queue_job_id,
        organization_id=organization_id,
        payload=payload,
        attempts=1,
        max_attempts=3,
        trace_id="llm-trace",
    )
    return app, cipher, job


def terminal_llm_failure(app, job, code="provider_unavailable"):
    with app.state.identity_store.sync_session() as db:
        queue_job = db.get(BackgroundJob, job.id)
        queue_job.status = "dead_letter"
        queue_job.attempts = queue_job.max_attempts
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        item.llm_status = "failed"
        item.llm_safe_error_code = code
        item.llm_started_at = item.created_at
        item.llm_finished_at = item.created_at
        item.finished_at = item.created_at
        run = db.get(ScreeningRun, item.run_id)
        route_llm_screening_terminal(
            db,
            organization_id=item.organization_id,
            item_id=item.id,
            actor_user_id=run.created_by,
            score=None,
            ai_status="failed",
            safe_error_code=code,
            trace_id="terminal-fixture",
        )
        aggregate_run(db, run)
        db.commit()
        return item.id, run.id, item.application_id


def run_llm_terminal_finalizer(app, job, callback_code, *, repeat_callback=False):
    with app.state.identity_store.sync_session() as db:
        source = db.get(BackgroundJob, job.id)
        source.payload = dict(job.payload)
        source.status = "dead_letter"
        source.attempts = source.max_attempts
        source.last_error_code = normalize_safe_code(callback_code)
        finalize_llm_dead_letter(db, source, callback_code, datetime.now(timezone.utc))
        if repeat_callback:
            finalize_llm_dead_letter(db, source, callback_code, datetime.now(timezone.utc))
        db.commit()
    with app.state.identity_store.sync_session() as db:
        finalizer = db.scalar(
            select(BackgroundJob).where(
                BackgroundJob.type == "screening.llm_finalize_terminal",
                BackgroundJob.dedupe_key == f"llm-terminal:{job.id}",
            )
        )
        detached = SimpleNamespace(
            id=finalizer.id,
            organization_id=finalizer.organization_id,
            payload=dict(finalizer.payload),
            trace_id=finalizer.trace_id,
        )
    asyncio.run(LlmTerminalFinalizer(app.state.identity_store.sync_session)(detached))
    return detached


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("provider_unavailable", True),
        ("llm_provider_unavailable", True),
        ("provider_quota_or_rate_limited", True),
        ("llm_provider_quota_or_rate_limited", True),
        ("provider_response_invalid", True),
        ("llm_provider_response_invalid", True),
        ("provider_auth_failed", False),
        ("llm_provider_auth_failed", False),
    ],
)
def test_screening_item_exposes_independent_llm_retryability(tmp_path, code, expected):
    app, _cipher, job = prepared(tmp_path)
    item_id, run_id, _application_id = terminal_llm_failure(app, job, code)

    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        response = client.get(f"/api/v1/screening-runs/{run_id}/items", headers=headers)

    assert response.status_code == 200
    item = next(value for value in response.json()["data"] if value["id"] == str(item_id))
    assert item["retryable"] is False
    assert item["llm_retryable"] is expected


def test_manual_llm_retry_requeues_current_dependencies_and_is_idempotent(tmp_path):
    app, _cipher, old_job = prepared(tmp_path)
    item_id, run_id, application_id = terminal_llm_failure(app, old_job, "llm_provider_unavailable")
    with app.state.identity_store.sync_session() as db:
        config = db.get(LlmProviderConfig, uuid.UUID(old_job.payload["config_id"]))
        config.version += 1
        result_id = db.scalar(select(RuleResult.id).where(RuleResult.item_id == item_id))
        prompt_id = db.scalar(select(PromptVersion.id).where(PromptVersion.organization_id == config.organization_id))
        rule_count = db.scalar(select(func.count(RuleResult.id)))
        db.commit()
        current_config_version = config.version

    with TestClient(app) as client:
        headers = {**login(client, "admin@example.test"), "Idempotency-Key": "llm-retry"}
        response = client.post(f"/api/v1/screening-items/{item_id}/retry", headers=headers)
        replay = client.post(f"/api/v1/screening-items/{item_id}/retry", headers=headers)
        active = client.post(
            f"/api/v1/screening-items/{item_id}/retry",
            headers={**headers, "Idempotency-Key": "llm-retry-new-request"},
        )

    assert response.status_code == 200
    assert replay.status_code == 200 and replay.json() == response.json()
    assert active.status_code == 409 and active.json()["code"] == "screening_retry_active"
    body = response.json()["data"]
    assert body["item"]["status"] == "scored"
    assert body["item"]["retryable"] is False
    assert body["item"]["llm_status"] == "queued"
    assert body["item"]["llm_retryable"] is False
    assert body["run"]["status"] == "llm_scoring"
    assert body["run"]["processed_count"] == 0

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, item_id)
        jobs = list(db.scalars(select(BackgroundJob).where(BackgroundJob.type == "screening.llm_score_item").order_by(BackgroundJob.created_at)))
        assert len(jobs) == 2
        retry_job = next(value for value in jobs if value.id != old_job.id)
        assert retry_job.status == "queued"
        assert retry_job.dedupe_key != jobs[0].dedupe_key
        assert retry_job.payload == {
            "organization_id": str(item.organization_id),
            "screening_item_id": str(item.id),
            "screening_result_id": str(result_id),
            "application_id": str(application_id),
            "config_id": str(config.id),
            "config_version": current_config_version,
            "prompt_version_id": str(prompt_id),
        }
        assert item.llm_started_at is None and item.llm_finished_at is None and item.finished_at is None
        assert db.get(Application, application_id).stage == "review"
        assert db.scalar(select(func.count(RuleResult.id))) == rule_count
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "screening.item_retried"))
        assert audit.outcome == "success"
        assert audit.metadata_json == {"run_id": str(run_id), "item_id": str(item_id), "retry_stage": "llm"}
        assert all(secret not in repr(audit.metadata_json) for secret in ("sk-private", "prompt", "resume"))


@pytest.mark.parametrize("change", ["disabled", "missing_key", "job_not_allowed"])
def test_manual_llm_retry_rejects_invalid_current_config(tmp_path, change):
    app, _cipher, job = prepared(tmp_path)
    item_id, run_id, _application_id = terminal_llm_failure(app, job)
    with app.state.identity_store.sync_session() as db:
        config = db.get(LlmProviderConfig, uuid.UUID(job.payload["config_id"]))
        if change == "disabled":
            config.enabled = False
        elif change == "missing_key":
            config.enabled = False
            config.encrypted_api_key = None
        else:
            item = db.get(ScreeningItem, item_id)
            run = db.get(ScreeningRun, item.run_id)
            config.allowed_job_ids = [str(uuid.uuid4())]
            assert str(run.job_id) not in config.allowed_job_ids
        db.commit()

    with TestClient(app) as client:
        headers = {**login(client, "admin@example.test"), "Idempotency-Key": change}
        listing = client.get(f"/api/v1/screening-runs/{run_id}/items", headers=headers)
        response = client.post(f"/api/v1/screening-items/{item_id}/retry", headers=headers)

    advertised = next(value for value in listing.json()["data"] if value["id"] == str(item_id))
    assert advertised["llm_retryable"] is False
    assert response.status_code == 409
    assert response.json()["code"] == "screening_item_not_retryable"
    with app.state.identity_store.sync_session() as db:
        assert db.get(ScreeningItem, item_id).llm_status == "failed"
        assert db.scalar(select(func.count(BackgroundJob.id)).where(BackgroundJob.type == "screening.llm_score_item")) == 1


def test_manual_llm_retry_rejects_permanent_failure_and_active_job(tmp_path):
    permanent_root = tmp_path / "permanent"
    permanent_root.mkdir()
    permanent_app, _cipher, permanent_job = prepared(permanent_root)
    permanent_id, _run_id, _application_id = terminal_llm_failure(permanent_app, permanent_job, "provider_auth_failed")
    active_root = tmp_path / "active"
    active_root.mkdir()
    active_app, _cipher, active_job = prepared(active_root)
    active_id, _run_id, _application_id = terminal_llm_failure(active_app, active_job)
    with active_app.state.identity_store.sync_session() as db:
        db.get(BackgroundJob, active_job.id).status = "queued"
        db.commit()

    for app, item_id, key, code in (
        (permanent_app, permanent_id, "permanent", "screening_item_not_retryable"),
        (active_app, active_id, "active", "screening_retry_active"),
    ):
        with TestClient(app) as client:
            headers = {**login(client, "admin@example.test"), "Idempotency-Key": key}
            response = client.post(f"/api/v1/screening-items/{item_id}/retry", headers=headers)
        assert response.status_code == 409 and response.json()["code"] == code


def test_success_commits_running_before_provider_and_is_replay_safe(tmp_path):
    app, cipher, job = prepared(tmp_path)
    observed = []

    def inspect():
        with app.state.identity_store.sync_session() as db:
            item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
            observed.append((item.llm_status, item.llm_attempts))

    gateway = Gateway(inspect=inspect)
    pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher)

    asyncio.run(pipeline.evaluate_item(job))
    job.attempts=2
    asyncio.run(pipeline.evaluate_item(job))

    assert observed == [("running", 1)]
    assert len(gateway.calls) == 1
    request = gateway.calls[0][3]
    assert request.candidate_name is None
    assert "@" not in request.provider_content()
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        run = db.get(ScreeningRun, item.run_id)
        invocation = db.scalar(select(LlmInvocation))
        evaluation = db.scalar(select(LlmScreeningEvaluation))
        assert item.status == "scored" and item.llm_status == "succeeded" and item.llm_finished_at and item.finished_at
        assert run.status == "completed" and run.processed_count == 1 and run.finished_at and run.version>=2
        assert db.scalar(select(func.count(LlmInvocation.id))) == 1
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id))) == 1
        assert invocation.status == "succeeded" and invocation.attempt_no == 1
        assert len(invocation.input_sha256) == 64
        assert invocation.request_field_manifest == ["job_description", "resume_text"]
        assert evaluation.score == 91 and evaluation.interview_questions == ["Describe a scaling incident"]
        assert evaluation.dimensions == [dimension.model_dump() for dimension in gateway.outcome.result.dimensions]
        prompt = db.get(PromptVersion, uuid.UUID(job.payload["prompt_version_id"]))
        assert prompt.version_number == 3
        assert gateway.calls[0][4]["system_prompt"] == prompt.content["system"]
        persisted = repr((invocation.usage, invocation.safe_error_code, evaluation.summary, evaluation.strengths))
        assert "sk-private" not in persisted and "Python backend role" not in persisted
        assert db.scalar(select(Application)).stage == "review"
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "screening.terminal_routed"))
        assert audit.metadata_json["evaluation_id"] == str(evaluation.id)
        assert audit.metadata_json["invocation_id"] == str(invocation.id)


@pytest.mark.parametrize(
    ("score", "expected_stage", "review_tasks", "memberships"),
    [(60, "review", 1, 0), (59, "deferred", 0, 1)],
)
def test_success_routes_real_score_boundary_once(tmp_path, score, expected_stage, review_tasks, memberships):
    app, cipher, job = prepared(tmp_path)
    pipeline = LlmScreeningPipeline(
        app.state.identity_store.sync_session,
        Gateway(screening_evaluation(score)),
        cipher,
    )

    asyncio.run(pipeline.evaluate_item(job))
    asyncio.run(pipeline.evaluate_item(job))

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        application = db.get(Application, item.application_id)
        evaluation = db.scalar(select(LlmScreeningEvaluation))
        assert application.stage == expected_stage
        assert evaluation.score == score
        assert sum(value["score"] for value in evaluation.dimensions) == score
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 1
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == review_tasks
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == memberships


def test_llm_queue_payload_carries_application_id(tmp_path):
    app, _cipher, job = prepared(tmp_path)

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert job.payload["application_id"] == str(item.application_id)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda config, run: setattr(config, "enabled", False), "llm_config_disabled"),
        (lambda config, run: setattr(config, "version", 4), "llm_config_changed"),
        (lambda config, run: setattr(config, "allowed_job_ids", [str(uuid.uuid4())]), "llm_job_not_allowed"),
    ],
)
def test_changed_disabled_or_nonallowed_config_skips_without_provider(tmp_path, change, code):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        config = db.get(LlmProviderConfig, uuid.UUID(job.payload["config_id"]))
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        change(config, db.get(ScreeningRun, item.run_id))
        db.commit()
    gateway = Gateway()

    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))

    assert gateway.calls == []
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        invocation = db.scalar(select(LlmInvocation))
        assert item.status == "scored" and item.llm_status == "skipped" and item.llm_safe_error_code == code and item.finished_at
        assert invocation.status == "failed" and invocation.safe_error_code == code
        assert db.get(ScreeningRun, item.run_id).status == "completed"
        assert db.get(Application, item.application_id).stage == "review"
        task = db.scalar(select(ApplicationReviewTask))
        assert task.ai_status == "failed" and task.safe_error_code == code
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id))) == 0


def test_deleted_config_revokes_call_and_completes_with_rule_result(tmp_path):
    app,cipher,job=prepared(tmp_path); gateway=Gateway()
    with app.state.identity_store.sync_session() as db:
        db.delete(db.get(LlmProviderConfig,uuid.UUID(job.payload["config_id"]))); db.commit()
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session,gateway,cipher).evaluate_item(job))
    assert gateway.calls==[]
    with app.state.identity_store.sync_session() as db:
        item=db.get(ScreeningItem,uuid.UUID(job.payload["screening_item_id"])); run=db.get(ScreeningRun,item.run_id)
        assert item.status=="scored" and item.llm_status=="skipped" and item.llm_safe_error_code=="llm_config_deleted" and item.finished_at
        assert run.status=="completed" and db.scalar(select(func.count(LlmInvocation.id)))==0


@pytest.mark.parametrize("safe_code", ["provider_unavailable", "provider_quota_or_rate_limited", "provider_response_invalid"])
def test_transient_provider_failure_retries_then_exhausts_to_partial(tmp_path, safe_code):
    app, cipher, job = prepared(tmp_path)
    pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, Gateway(GatewayError(safe_code)), cipher)

    with pytest.raises(RetryableJobError) as retry:
        asyncio.run(pipeline.evaluate_item(job))
    assert retry.value.safe_code == safe_code
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.status == "scored" and item.llm_status == "queued" and item.llm_finished_at is None
        assert db.get(ScreeningRun, item.run_id).status == "llm_scoring"
        assert db.get(Application, item.application_id).stage == "new"
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 0
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 0
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == 0

    job.attempts = job.max_attempts
    with pytest.raises(PermanentJobError) as exhausted:
        asyncio.run(pipeline.evaluate_item(job))
    assert exhausted.value.safe_code == safe_code
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        assert item.status == "scored" and item.llm_status == "failed" and item.llm_finished_at and item.finished_at
        assert db.get(ScreeningRun, item.run_id).status == "partial"
        assert db.scalar(select(func.count(LlmInvocation.id))) == 2
        assert db.get(Application, item.application_id).stage == "review"
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id))) == 0
        task = db.scalar(select(ApplicationReviewTask))
        assert task.ai_status == "failed" and task.safe_error_code == safe_code


@pytest.mark.parametrize(
    "safe_code",
    [
        "provider_unavailable",
        "provider_quota_or_rate_limited",
        "provider_response_invalid",
        "llm_config_disabled",
    ],
)
def test_final_llm_failure_routes_to_review_with_null_score(tmp_path, safe_code):
    app, cipher, job = prepared(tmp_path)
    job.attempts = job.max_attempts
    pipeline = LlmScreeningPipeline(
        app.state.identity_store.sync_session,
        Gateway(GatewayError(safe_code)),
        cipher,
    )

    with pytest.raises(PermanentJobError) as failed:
        asyncio.run(pipeline.evaluate_item(job))
    assert failed.value.safe_code == safe_code

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        application = db.get(Application, item.application_id)
        task = db.scalar(select(ApplicationReviewTask))
        assert item.llm_status == "failed"
        assert application.stage == "review"
        assert db.scalar(select(LlmScreeningEvaluation)) is None
        assert task.ai_status == "failed" and task.safe_error_code == safe_code


def test_final_llm_failure_normalizes_unknown_code_before_any_persistence(tmp_path):
    app, cipher, job = prepared(tmp_path)
    job.attempts = job.max_attempts
    private_code = "candidate_alice_resume_prompt"

    with pytest.raises(PermanentJobError) as failed:
        asyncio.run(
            LlmScreeningPipeline(
                app.state.identity_store.sync_session,
                Gateway(GatewayError(private_code)),
                cipher,
            ).evaluate_item(job)
        )
    assert failed.value.safe_code == "internal_error"

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        invocation = db.scalar(select(LlmInvocation))
        task = db.scalar(select(ApplicationReviewTask))
        audit = db.scalar(select(AuditLog).where(AuditLog.event_type == "screening.terminal_routed"))
        assert item.llm_safe_error_code == "internal_error"
        assert invocation.safe_error_code == "internal_error"
        assert task.safe_error_code == "internal_error"
        assert private_code not in repr(audit.metadata_json)


def test_permanent_provider_failure_is_terminal_and_tenant_payload_cannot_cross_load(tmp_path):
    app, cipher, job = prepared(tmp_path)
    gateway=Gateway(GatewayError("provider_auth_failed")); pipeline = LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher)

    with pytest.raises(PermanentJobError) as failed:
        asyncio.run(pipeline.evaluate_item(job))
    assert failed.value.safe_code == "provider_auth_failed"
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        finalize_llm_dead_letter(db,job,"provider_auth_failed",datetime.now(timezone.utc)); db.commit()
        assert item.status == "scored" and item.llm_status == "failed"
        assert item.llm_safe_error_code=="provider_auth_failed"
        assert db.get(ScreeningRun, item.run_id).status == "partial"; run_id=item.run_id

    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); response=client.get(f"/api/v1/screening-runs/{run_id}/items",headers=headers)
    assert response.status_code==200
    api_item=response.json()["data"][0]
    assert api_item["llm_evaluation"] is None
    assert api_item["rule_result"] is not None
    assert api_item["rule_result"]["score"]==db_rule_score(app,job.payload["screening_item_id"])

    job.attempts=2
    asyncio.run(pipeline.evaluate_item(job))
    assert len(gateway.calls)==1

    other_tenant_job = SimpleNamespace(**vars(job))
    other_tenant_job.payload = {**job.payload, "organization_id": str(uuid.uuid4())}
    with pytest.raises(PermanentJobError) as missing:
        asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, Gateway(), cipher).evaluate_item(other_tenant_job))
    assert missing.value.safe_code == "screening_item_missing"


def db_rule_score(app,item_id):
    with app.state.identity_store.sync_session() as db:
        return db.scalar(select(RuleResult.rule_score).where(RuleResult.item_id==uuid.UUID(item_id)))


def test_request_is_bounded_hashed_after_redaction_and_excludes_rule_facts(tmp_path):
    app, cipher, job = prepared(tmp_path)
    secret_email = "private@example.test"
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        db.get(Resume, item.resume_id).parsed_text = "r"*(MAX_RESUME_CHARS-5)+secret_email
        run = db.get(ScreeningRun, item.run_id)
        db.get(JobJdVersion, run.jd_version_id).content = {"text": "j" * 20_000}
        result = db.get(RuleResult, uuid.UUID(job.payload["screening_result_id"]))
        result.required_hits = ["SECRET-RULE-FACT"]
        db.commit()
    gateway = Gateway()

    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))

    request = gateway.calls[0][3]
    assert len(request.job_description) == 12_000 and len(request.resume_text) <= 30_000 and "private" not in request.resume_text and "[REDACTED_EMAIL]" in request.resume_text
    assert set(__import__("json").loads(request.provider_content())) == {"job_description", "resume_text"}
    assert "SECRET-RULE-FACT" not in request.provider_content()
    assert secret_email not in request.provider_content()
    with app.state.identity_store.sync_session() as db:
        invocation = db.scalar(select(LlmInvocation))
        assert invocation.input_sha256 == __import__("hashlib").sha256(request.provider_content().encode()).hexdigest()


def test_request_uses_the_persisted_jd_description_field(tmp_path):
    app, cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        run = db.get(ScreeningRun, item.run_id)
        db.get(JobJdVersion, run.jd_version_id).content = {"description": "负责企业级 AI 平台建设"}
        db.commit()
    gateway = Gateway()

    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session, gateway, cipher).evaluate_item(job))

    assert gateway.calls[0][3].job_description == "负责企业级 AI 平台建设"


def test_llm_api_schema_exposes_dimensions_without_prompt_or_provider_payloads():
    properties = LlmEvaluationOut.model_json_schema()["properties"]

    assert "dimensions" in properties
    assert all(name not in properties for name in ("prompt", "system_prompt", "provider_response", "provider_body"))


def test_historical_jd_field_failure_is_retryable_when_current_inputs_are_valid(tmp_path):
    app, _cipher, job = prepared(tmp_path)
    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        run = db.get(ScreeningRun, item.run_id)
        db.get(JobJdVersion, run.jd_version_id).content = {"description": "负责企业级 AI 平台建设"}
        db.commit()
    item_id, run_id, _application_id = terminal_llm_failure(app, job, "llm_llm_input_invalid")

    with TestClient(app) as client:
        headers = login(client, "admin@example.test")
        listing = client.get(f"/api/v1/screening-runs/{run_id}/items", headers=headers)
        listed = next(value for value in listing.json()["data"] if value["id"] == str(item_id))
        retried = client.post(
            f"/api/v1/screening-items/{item_id}/retry",
            headers={**headers, "Idempotency-Key": "retry-historical-jd-field"},
        )

    assert listed["llm_retryable"] is True
    assert retried.status_code == 200
    assert retried.json()["data"]["item"]["llm_status"] == "queued"


def test_screening_items_api_exposes_only_bounded_llm_result(tmp_path):
    app,cipher,job=prepared(tmp_path)
    asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session,Gateway(),cipher).evaluate_item(job))
    with app.state.identity_store.sync_session() as db:
        run_id=db.get(ScreeningItem,uuid.UUID(job.payload["screening_item_id"])).run_id
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); response=client.get(f"/api/v1/screening-runs/{run_id}/items",headers=headers)
    assert response.status_code==200
    item=response.json()["data"][0]
    assert item["llm_status"]=="succeeded" and item["llm_error_code"] is None and item["llm_attempts"]==1
    assert item["llm_evaluation"]=={"score":91,"recommendation":"优先评审","dimensions":[dimension.model_dump() for dimension in Gateway().outcome.result.dimensions],"summary":"Strong Python match","strengths":["Python services"],"gaps":[],"risks":["Confirm availability"],"questions":["Describe a scaling incident"]}
    assert all(value not in response.text for value in ("input_sha256","prompt_version_id","request_field_manifest","sk-private","system_prompt","provider_response"))


def test_run_api_includes_safe_llm_degradation_summary(tmp_path):
    app,cipher,job=prepared(tmp_path)
    with pytest.raises(PermanentJobError):
        asyncio.run(LlmScreeningPipeline(app.state.identity_store.sync_session,Gateway(GatewayError("provider_auth_failed")),cipher).evaluate_item(job))
    with app.state.identity_store.sync_session() as db:
        run_id=db.get(ScreeningItem,uuid.UUID(job.payload["screening_item_id"])).run_id
    with TestClient(app) as client:
        headers=login(client,"admin@example.test"); response=client.get(f"/api/v1/screening-runs/{run_id}",headers=headers)
    assert response.status_code==200 and response.json()["data"]["status"]=="partial"
    assert response.json()["data"]["error_summary"]=={"provider_auth_failed":1}


@pytest.mark.parametrize("callback_code", ["provider_unavailable", "handler_failed", "lease_expired"])
def test_llm_dead_letter_preserves_rule_result_and_is_idempotent(tmp_path, callback_code):
    app,_cipher,job=prepared(tmp_path); now=datetime.now(timezone.utc)
    run_llm_terminal_finalizer(app, job, callback_code, repeat_callback=True)
    with app.state.identity_store.sync_session() as db:
        item=db.get(ScreeningItem,uuid.UUID(job.payload["screening_item_id"])); run=db.get(ScreeningRun,item.run_id)
        expected_code = "llm_handler_failed" if callback_code in {"handler_failed", "lease_expired"} else callback_code
        assert item.status=="scored" and item.llm_status=="failed" and item.llm_safe_error_code==expected_code and item.finished_at
        assert run.status=="partial" and run.succeeded_count==1 and run.failed_count==0 and run.finished_at and run.version>=2
        assert db.get(Application,item.application_id).stage=="review" and db.scalar(select(func.count(RuleResult.id)))==1
        assert db.scalar(select(func.count(LlmScreeningEvaluation.id)))==0
        task=db.scalar(select(ApplicationReviewTask))
        assert task.ai_status=="failed" and task.safe_error_code==expected_code
        assert db.scalar(select(func.count(ApplicationStageEvent.id)))==1


def test_llm_source_terminal_callback_only_accesses_background_jobs(tmp_path):
    app, _cipher, job = prepared(tmp_path)
    statements = []
    engine = app.state.identity_store.engine
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with app.state.identity_store.sync_session() as db:
            source = db.get(BackgroundJob, job.id)
            source.status = "dead_letter"
            source.attempts = source.max_attempts
            source.last_error_code = "handler_failed"
            statements.clear()
            finalize_llm_dead_letter(db, source, "handler_failed", datetime.now(timezone.utc))
            db.commit()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    sql = "\n".join(statements)
    assert "background_jobs" in sql
    assert all(
        table not in sql
        for table in (
            "candidates",
            "screening_items",
            "applications",
            "screening_results",
            "resumes",
            "job_jd_versions",
            "prompt_versions",
            "llm_provider_configs",
        )
    )


@pytest.mark.parametrize(
    ("broken_relation", "callback_code", "expected_code"),
    [
        ("missing_result", "handler_failed", "llm_job_payload_invalid"),
        ("missing_prompt", "handler_failed", "llm_job_payload_invalid"),
        ("missing_config", "handler_failed", "llm_job_payload_invalid"),
        ("config_version_mismatch", "handler_failed", "llm_job_payload_invalid"),
        ("missing_resume", "handler_failed", "llm_job_payload_invalid"),
        ("tenant_mismatch", "handler_failed", "llm_job_payload_invalid"),
        ("application_mismatch", "handler_failed", "llm_job_payload_invalid"),
        ("application_malformed", "handler_failed", "llm_job_payload_invalid"),
        ("application_missing", "handler_failed", "internal_error"),
        ("candidate_deleted", "handler_failed", "internal_error"),
        ("unclassified_cause", "private resume provider body", "internal_error"),
    ],
)
def test_llm_dead_letter_relational_failure_is_technical_and_idempotent(
    tmp_path, broken_relation, callback_code, expected_code
):
    app, _cipher, job = prepared(tmp_path)
    now = datetime.now(timezone.utc)
    with app.state.identity_store.sync_session() as db:
        queue_job = db.get(BackgroundJob, job.id)
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        application_id = item.application_id
        if broken_relation == "missing_result":
            job.payload = {**job.payload, "screening_result_id": str(uuid.uuid4())}
        elif broken_relation == "missing_prompt":
            job.payload = {**job.payload, "prompt_version_id": str(uuid.uuid4())}
        elif broken_relation == "missing_config":
            job.payload = {**job.payload, "config_id": str(uuid.uuid4())}
        elif broken_relation == "config_version_mismatch":
            job.payload = {**job.payload, "config_version": job.payload["config_version"] + 1}
        elif broken_relation == "missing_resume":
            result = db.get(RuleResult, uuid.UUID(job.payload["screening_result_id"]))
            result.resume_id = None
            item.resume_id = None
        elif broken_relation == "tenant_mismatch":
            job.payload = {**job.payload, "organization_id": str(uuid.uuid4())}
        elif broken_relation == "application_mismatch":
            job.payload = {**job.payload, "application_id": str(uuid.uuid4())}
        elif broken_relation == "application_malformed":
            job.payload = {**job.payload, "application_id": "not-an-id"}
        elif broken_relation == "application_missing":
            result = db.get(RuleResult, uuid.UUID(job.payload["screening_result_id"]))
            result.application_id = None
            item.application_id = None
        elif broken_relation == "candidate_deleted":
            db.get(Candidate, item.candidate_id).deleted_at = now
        queue_job.status = "dead_letter"
        db.commit()

    run_llm_terminal_finalizer(
        app, job, callback_code, repeat_callback=True
    )

    with app.state.identity_store.sync_session() as db:
        item = db.get(ScreeningItem, uuid.UUID(job.payload["screening_item_id"]))
        application = db.get(Application, application_id)
        assert item.llm_status == "failed"
        assert item.llm_safe_error_code == expected_code
        assert item.finished_at and item.llm_finished_at
        assert application.stage == "new"
        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == 0
        assert db.scalar(select(func.count(ApplicationStageEvent.id))) == 0
        assert db.scalar(select(func.count(AuditLog.id)).where(AuditLog.event_type == "screening.terminal_routed")) == 0
        assert db.get(ScreeningRun, item.run_id).status == "partial"
