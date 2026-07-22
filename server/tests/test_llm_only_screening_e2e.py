import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from server.app.llm.gateway import GatewayError
from server.app.llm.models import LlmScreeningEvaluation
from server.app.llm.screening import ScreeningEvaluation, ScreeningResult
from server.app.queue.service import PermanentJobError
from server.app.recruiting.models import Application, ApplicationReviewTask
from server.app.screening.llm_pipeline import LlmScreeningPipeline
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.tests.test_llm_pipeline import Gateway, prepared


DIMENSION_LIMITS = (
    ("core_capability", 35),
    ("experience_depth", 25),
    ("role_seniority", 20),
    ("transferability", 10),
    ("explicit_constraints", 10),
)


def screening_evaluation(score: int) -> ScreeningEvaluation:
    remaining = score
    dimensions = []
    for key, limit in DIMENSION_LIMITS:
        dimension_score = min(remaining, limit)
        remaining -= dimension_score
        dimensions.append(
            {
                "key": key,
                "score": dimension_score,
                "evidence": [f"{key} evidence"] if dimension_score else [],
                "gaps": [] if dimension_score else [f"{key} gap"],
            }
        )
    return ScreeningEvaluation(
        ScreeningResult(
            score=score,
            dimensions=dimensions,
            summary="端到端自动筛选结果",
            strengths=["可验证优势"],
            gaps=[] if score >= 60 else ["需要进一步验证"],
            risks=[],
            questions=["请说明相关项目经验"],
        ),
        1,
        {},
    )


@pytest.mark.parametrize(
    ("outcome", "expected_stage", "expected_task_count", "expected_membership_count"),
    [
        (screening_evaluation(60), "review", 1, 0),
        (screening_evaluation(59), "deferred", 0, 1),
        (GatewayError("provider_unavailable"), "review", 1, 0),
    ],
    ids=("score-60-manager-review", "score-59-deferred", "provider-failure-fail-open"),
)
def test_import_to_terminal_route(
    tmp_path,
    outcome,
    expected_stage,
    expected_task_count,
    expected_membership_count,
):
    app, cipher, job = prepared(tmp_path)
    pipeline = LlmScreeningPipeline(
        app.state.identity_store.sync_session,
        Gateway(outcome),
        cipher,
    )

    if isinstance(outcome, GatewayError):
        job.attempts = job.max_attempts
        with pytest.raises(PermanentJobError) as failed:
            asyncio.run(pipeline.evaluate_item(job))
        assert failed.value.safe_code == "provider_unavailable"
    else:
        asyncio.run(pipeline.evaluate_item(job))

    with app.state.identity_store.sync_session() as db:
        application = db.get(Application, uuid.UUID(job.payload["application_id"]))
        evaluations = list(db.scalars(select(LlmScreeningEvaluation)))
        review_tasks = list(db.scalars(select(ApplicationReviewTask)))
        memberships = list(db.scalars(select(TalentPoolMembership)))

        assert application.stage == expected_stage
        assert application.human_conclusion is None
        assert len(review_tasks) == expected_task_count
        assert len(memberships) == expected_membership_count
        assert all(task.status == "open" for task in review_tasks)

        if isinstance(outcome, GatewayError):
            assert evaluations == []
            assert review_tasks[0].ai_status == "failed"
            assert review_tasks[0].safe_error_code == "provider_unavailable"
        else:
            assert len(evaluations) == 1
            evaluation = evaluations[0]
            assert evaluation.score == outcome.result.score
            assert [dimension["key"] for dimension in evaluation.dimensions] == [
                key for key, _limit in DIMENSION_LIMITS
            ]
            assert sum(dimension["score"] for dimension in evaluation.dimensions) == evaluation.score

        if expected_membership_count:
            pool = db.get(TalentPool, memberships[0].pool_id)
            assert pool.system_key == "ai_screening_deferred"
            assert memberships[0].source_application_id == application.id

        assert db.scalar(select(func.count(ApplicationReviewTask.id))) == expected_task_count
        assert db.scalar(select(func.count(TalentPoolMembership.id))) == expected_membership_count
