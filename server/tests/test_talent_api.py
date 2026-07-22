from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from server.app.core.settings import Settings
from server.app.identity.models import AuditLog, Job, JobCollaborator, Organization, User, UserRole
from server.app.identity.security import PasswordService
from server.app.llm.models import LlmInvocation
from server.app.main import create_app
from server.app.recruiting.models import (
    Application,
    ApplicationReviewTask,
    ApplicationStageEvent,
    Candidate,
    CandidateContact,
    CandidateEvent,
    FileObject,
    IdempotencyRecord,
    Resume,
)
from server.app.talent.models import TalentPool, TalentPoolMembership
from server.app.talent.api import _is_active_application_conflict
from server.tests.test_interview_api import seed_application
from server.tests.test_recruiting_api import (
    login,
    seed_llm_evaluation,
    seed_screening_results,
    seed_terminal_route_audit,
    seed_user,
)


class Probe:
    async def check(self) -> None:
        pass


class ConstraintDiagnostic:
    def __init__(self, name):
        self.constraint_name = name


class ConstraintOrigin(Exception):
    def __init__(self, name):
        self.diag = ConstraintDiagnostic(name)


def make_app(tmp_path):
    app = create_app(
        settings=Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'talent-api.db'}",
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=Probe(),
        storage_probe=Probe(),
        initialize_identity_schema=True,
    )
    app.state.identity_store.create_schema()
    return app


def pool_payload(owner_id):
    return {
        "name": "AI 技术人才",
        "purpose": "沉淀大模型和 AI 应用人才",
        "visibility": "recruiting_team",
        "owner_id": str(owner_id),
        "suitable_roles": ["AI 工程师", "算法工程师"],
        "retention_days": 730,
        "grants": [],
    }


def membership_payload(seed):
    return {
        "candidate_id": str(seed["candidate_id"]),
        "source_application_id": str(seed["application_id"]),
        "owner_id": str(seed["admin_id"]),
        "suitable_roles": ["AI 工程师"],
        "tags": ["RAG", "Agent"],
        "reason": "技术匹配，等待更合适的机会",
        "next_contact_at": "2026-08-01T02:00:00+00:00",
        "retention_until": "2028-07-31T16:00:00+00:00",
    }


def create_pool_and_membership(client, seed):
    headers = login(client, "interview-admin@example.test")
    pool = client.post(
        "/api/v1/talent-pools",
        json=pool_payload(seed["admin_id"]),
        headers={**headers, "Idempotency-Key": "create-ai-pool"},
    )
    assert pool.status_code == 201
    pool_id = pool.json()["data"]["id"]
    membership = client.post(
        f"/api/v1/talent-pools/{pool_id}/memberships",
        json=membership_payload(seed),
        headers={**headers, "Idempotency-Key": "add-ai-member"},
    )
    assert membership.status_code == 201
    return headers, pool, membership


def mark_deferred_system_pool(app, pool_id):
    with app.state.identity_store.sync_session() as database:
        pool = database.get(TalentPool, UUID(pool_id))
        pool.system_key = "ai_screening_deferred"
        database.commit()


def bind_terminal_audit_to_evaluation(audit, evaluation):
    audit.metadata_json = {
        **audit.metadata_json,
        "evaluation_id": str(evaluation.id),
        "invocation_id": str(evaluation.invocation_id),
    }


def test_talent_openapi_registers_phase_5_pool_contract(tmp_path) -> None:
    app = make_app(tmp_path)
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    expected = {
        "/api/v1/talent-pools": {"get", "post"},
        "/api/v1/talent-pools/{pool_id}": {"get", "patch"},
        "/api/v1/talent-pools/{pool_id}/memberships": {"get", "post"},
        "/api/v1/talent-pool-memberships/{membership_id}": {"patch", "delete"},
        "/api/v1/talent-pool-memberships/{membership_id}/reactivations": {"post"},
        "/api/v1/talent-pool-memberships/{membership_id}/review-referrals": {"post"},
    }
    assert {path: set(paths.get(path, {})) for path in expected} == expected


def test_pool_list_and_detail_expose_persisted_system_key_without_name_inference(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    payload = {**pool_payload(seed["admin_id"]), "name": "AI 筛选暂缓"}

    with TestClient(app) as client:
        headers = login(client, "interview-admin@example.test")
        created = client.post(
            "/api/v1/talent-pools",
            json=payload,
            headers={**headers, "Idempotency-Key": "pool-system-key-contract"},
        )
        pool_id = created.json()["data"]["id"]
        ordinary_list = client.get("/api/v1/talent-pools", headers=headers)
        ordinary_detail = client.get(f"/api/v1/talent-pools/{pool_id}", headers=headers)

        mark_deferred_system_pool(app, pool_id)
        system_list = client.get("/api/v1/talent-pools", headers=headers)
        system_detail = client.get(f"/api/v1/talent-pools/{pool_id}", headers=headers)

    assert ordinary_list.json()["data"][0]["system_key"] is None
    assert ordinary_detail.json()["data"]["system_key"] is None
    assert system_list.json()["data"][0]["system_key"] == "ai_screening_deferred"
    assert system_detail.json()["data"]["system_key"] == "ai_screening_deferred"


def test_deferred_membership_projects_latest_llm_evaluation_and_latest_valid_route_time(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    old_time = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
    latest_time = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    routed_at = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    malformed_at = datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc)

    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("legacy-old", 91, "优先沟通", old_time), ("legacy-latest", 99, "优先沟通", latest_time)],
        )
        old_evaluation = seed_llm_evaluation(database, source, seed["admin_id"], results[0], 61, "建议评审", old_time)
        old_evaluation.gaps = ["旧缺口"]
        latest_evaluation = seed_llm_evaluation(database, source, seed["admin_id"], results[1], 52, "暂缓", latest_time)
        latest_evaluation.gaps = ["缺少生产级 RAG 经验", "系统设计深度不足"]
        valid_audit = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=52,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(valid_audit, latest_evaluation)
        malformed = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=52,
            created_at=malformed_at,
        )
        bind_terminal_audit_to_evaluation(malformed, latest_evaluation)
        malformed.metadata_json = {**malformed.metadata_json, "ai_status": "pending"}
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    member = members.json()["data"][0]
    assert member["deferred_screening"] == {
        "final_score": 52,
        "deferred_at": routed_at.isoformat(),
        "main_gaps": ["缺少生产级 RAG 经验", "系统设计深度不足"],
    }
    assert member["owner"]["id"] == str(seed["admin_id"])
    assert member["source_application"]["job_id"] == str(seed["job_id"])
    assert member["source_application"]["stage"] == "deferred"


def test_deferred_membership_rejects_historical_terminal_audit_without_evidence_ids(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    evaluated_at = datetime(2026, 7, 18, 4, 10, tzinfo=timezone.utc)
    routed_at = datetime(2026, 7, 18, 4, 20, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("historical-route", 48, "暂缓", evaluated_at)],
        )
        evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[0],
            48,
            "暂缓",
            evaluated_at,
        )
        evaluation.gaps = ["historical gap"]
        seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=48,
            created_at=routed_at,
        )
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": None,
        "deferred_at": None,
        "main_gaps": [],
    }


def test_deferred_membership_does_not_bind_old_audit_to_later_same_score_evaluation(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    first_at = datetime(2026, 7, 18, 4, 30, tzinfo=timezone.utc)
    routed_at = datetime(2026, 7, 18, 4, 40, tzinfo=timezone.utc)
    later_at = datetime(2026, 7, 18, 4, 50, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("first-evidence", 48, "暂缓", first_at), ("later-evidence", 48, "暂缓", later_at)],
        )
        first_evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[0],
            48,
            "暂缓",
            first_at,
        )
        first_evaluation.gaps = ["first gap"]
        audit = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=48,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(audit, first_evaluation)
        later_evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[1],
            48,
            "暂缓",
            later_at,
        )
        later_evaluation.gaps = ["later gap must not project"]
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": 48,
        "deferred_at": routed_at.isoformat(),
        "main_gaps": ["first gap"],
    }


def test_deferred_membership_rejects_evaluation_created_after_its_terminal_audit(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    routed_at = datetime(2026, 7, 18, 4, 55, tzinfo=timezone.utc)
    evaluated_at = datetime(2026, 7, 18, 5, 5, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("future-evidence", 48, "暂缓", evaluated_at)],
        )
        evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[0],
            48,
            "暂缓",
            evaluated_at,
        )
        evaluation.gaps = ["future evidence must not project"]
        audit = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=48,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(audit, evaluation)
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": None,
        "deferred_at": None,
        "main_gaps": [],
    }


def test_deferred_membership_without_llm_evaluation_does_not_fall_back_to_rule_score_or_tags(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    malformed_at = datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, _ = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("legacy-only", 98, "优先沟通", malformed_at)],
        )
        seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=42,
            created_at=malformed_at,
        )
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": None,
        "deferred_at": None,
        "main_gaps": [],
    }


def test_deferred_membership_rejects_evaluation_and_audit_from_different_screening_items(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    routed_at = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        source_resume = database.get(Resume, source.resume_id)
        source_item, _ = seed_screening_results(
            database,
            source,
            source_resume.file_object_id,
            seed["admin_id"],
            [("source-rule", 50, "暂缓", routed_at)],
        )

        foreign_candidate = Candidate(
            organization_id=source.organization_id,
            display_name="Foreign candidate",
            owner_id=seed["admin_id"],
        )
        foreign_file = FileObject(
            organization_id=source.organization_id,
            storage_key="talent/foreign.pdf",
            original_filename="foreign.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="c" * 64,
            uploaded_by=seed["admin_id"],
        )
        database.add_all([foreign_candidate, foreign_file])
        database.flush()
        foreign_resume = Resume(
            organization_id=source.organization_id,
            candidate_id=foreign_candidate.id,
            file_object_id=foreign_file.id,
            version_number=1,
        )
        database.add(foreign_resume)
        database.flush()
        foreign_application = Application(
            organization_id=source.organization_id,
            candidate_id=foreign_candidate.id,
            job_id=source.job_id,
            resume_id=foreign_resume.id,
            owner_id=seed["admin_id"],
            stage="rejected",
        )
        database.add(foreign_application)
        database.flush()
        _, foreign_results = seed_screening_results(
            database,
            foreign_application,
            foreign_file.id,
            seed["admin_id"],
            [("foreign-rule", 48, "暂缓", routed_at)],
        )
        forged_result = foreign_results[0]
        forged_result.application_id = source.id
        forged_evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            forged_result,
            48,
            "暂缓",
            routed_at,
        )
        forged_evaluation.gaps = ["forged gap"]
        forged_audit = seed_terminal_route_audit(
            database,
            source,
            source_item,
            seed["admin_id"],
            route="deferred",
            score=48,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(forged_audit, forged_evaluation)
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": None,
        "deferred_at": None,
        "main_gaps": [],
    }


@pytest.mark.parametrize(
    ("item_status", "invocation_status", "score", "evaluation_recommendation", "audit_score"),
    [
        ("failed", "succeeded", 48, "暂缓", 48),
        ("succeeded", "failed", 48, "暂缓", 48),
        ("succeeded", "succeeded", 95, "暂缓", 95),
        ("succeeded", "succeeded", 48, "建议评审", 48),
        ("succeeded", "succeeded", 48, "暂缓", 47),
    ],
)
def test_deferred_membership_rejects_invalid_terminal_state(
    tmp_path,
    item_status,
    invocation_status,
    score,
    evaluation_recommendation,
    audit_score,
) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    routed_at = datetime(2026, 7, 18, 7, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("strict-route", 99, "优先沟通", routed_at)],
        )
        evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[0],
            score,
            evaluation_recommendation,
            routed_at,
        )
        evaluation.gaps = ["must not project"]
        item.llm_status = item_status
        database.get(LlmInvocation, evaluation.invocation_id).status = invocation_status
        audit = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=audit_score,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(audit, evaluation)
        database.commit()

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert members.json()["data"][0]["deferred_screening"] == {
        "final_score": None,
        "deferred_at": None,
        "main_gaps": [],
    }


def test_membership_list_query_count_is_bounded_as_pool_grows(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)

    def select_count(client, path):
        statements = []

        def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(app.state.identity_store.engine, "before_cursor_execute", record_statement)
        try:
            response = client.get(path, headers=headers)
        finally:
            event.remove(app.state.identity_store.engine, "before_cursor_execute", record_statement)
        assert response.status_code == 200
        return len(statements)

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        path = f"/api/v1/talent-pools/{pool_id}/memberships?limit=100"
        one_member_selects = select_count(client, path)

        with app.state.identity_store.sync_session() as database:
            source = database.get(Application, seed["application_id"])
            for index in range(12):
                candidate = Candidate(
                    organization_id=source.organization_id,
                    display_name=f"Query count candidate {index}",
                    owner_id=seed["admin_id"],
                )
                database.add(candidate)
                database.flush()
                database.add(
                    TalentPoolMembership(
                        organization_id=source.organization_id,
                        pool_id=UUID(pool_id),
                        candidate_id=candidate.id,
                        source_application_id=None,
                        owner_id=seed["admin_id"],
                        suitable_roles=["Engineer"],
                        tags=[],
                        reason="Query count fixture",
                        retention_until=datetime(2028, 7, 20, tzinfo=timezone.utc),
                    )
                )
            database.commit()

        many_member_selects = select_count(client, path)

    assert many_member_selects <= one_member_selects + 1


def test_deferred_membership_list_batches_real_screening_projections(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)

    def add_projection(database, application, file_id, index):
        evaluated_at = datetime(2026, 7, 19, index, 0, tzinfo=timezone.utc)
        routed_at = datetime(2026, 7, 19, index, 1, tzinfo=timezone.utc)
        score = 40 + index
        item, results = seed_screening_results(
            database,
            application,
            file_id,
            seed["admin_id"],
            [(f"bulk-deferred-{index}", 99, "优先沟通", evaluated_at)],
        )
        evaluation = seed_llm_evaluation(
            database,
            application,
            seed["admin_id"],
            results[0],
            score,
            "暂缓",
            evaluated_at,
        )
        evaluation.gaps = [f"gap-{index}"]
        audit = seed_terminal_route_audit(
            database,
            application,
            item,
            seed["admin_id"],
            route="deferred",
            score=score,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(audit, evaluation)
        return score

    def get_with_select_count(client, path, headers):
        statements = []

        def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(app.state.identity_store.engine, "before_cursor_execute", record_statement)
        try:
            response = client.get(path, headers=headers)
        finally:
            event.remove(app.state.identity_store.engine, "before_cursor_execute", record_statement)
        assert response.status_code == 200
        return response, len(statements)

    with TestClient(app) as client:
        headers, pool, _ = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        with app.state.identity_store.sync_session() as database:
            source = database.get(Application, seed["application_id"])
            source.stage = "deferred"
            source_resume = database.get(Resume, source.resume_id)
            expected_scores = {add_projection(database, source, source_resume.file_object_id, 1)}
            database.commit()

        path = f"/api/v1/talent-pools/{pool_id}/memberships?limit=100"
        one_response, one_member_selects = get_with_select_count(client, path, headers)
        assert len(one_response.json()["data"]) == 1

        with app.state.identity_store.sync_session() as database:
            source = database.get(Application, seed["application_id"])
            for index in range(2, 8):
                candidate = Candidate(
                    organization_id=source.organization_id,
                    display_name=f"Deferred bulk candidate {index}",
                    owner_id=seed["admin_id"],
                )
                file_object = FileObject(
                    organization_id=source.organization_id,
                    storage_key=f"talent/deferred-bulk-{index}.pdf",
                    original_filename=f"deferred-bulk-{index}.pdf",
                    mime_type="application/pdf",
                    size_bytes=10,
                    sha256=(f"{index:x}" * 64)[:64],
                    uploaded_by=seed["admin_id"],
                )
                database.add_all([candidate, file_object])
                database.flush()
                resume = Resume(
                    organization_id=source.organization_id,
                    candidate_id=candidate.id,
                    file_object_id=file_object.id,
                    version_number=1,
                )
                database.add(resume)
                database.flush()
                application = Application(
                    organization_id=source.organization_id,
                    candidate_id=candidate.id,
                    job_id=source.job_id,
                    resume_id=resume.id,
                    owner_id=seed["admin_id"],
                    stage="deferred",
                )
                database.add(application)
                database.flush()
                expected_scores.add(add_projection(database, application, file_object.id, index))
                database.add(
                    TalentPoolMembership(
                        organization_id=source.organization_id,
                        pool_id=UUID(pool_id),
                        candidate_id=candidate.id,
                        source_application_id=application.id,
                        owner_id=seed["admin_id"],
                        suitable_roles=["Engineer"],
                        tags=[],
                        reason="Deferred projection fixture",
                        retention_until=datetime(2028, 7, 20, tzinfo=timezone.utc),
                    )
                )
            database.commit()

        many_response, many_member_selects = get_with_select_count(client, path, headers)

    rows = many_response.json()["data"]
    assert len(rows) == 7
    assert {row["deferred_screening"]["final_score"] for row in rows} == expected_scores
    assert all(row["deferred_screening"]["main_gaps"] for row in rows)
    assert many_member_selects <= one_member_selects + 1


def test_deferred_membership_referral_reuses_application_and_is_idempotent(tmp_path) -> None:
    app=make_app(tmp_path); seed=seed_application(app)
    with app.state.identity_store.sync_session() as db:
        source=db.get(Application,seed["application_id"]); source.stage="deferred"; source.version=1
        job=db.get(Job,source.job_id); job.status="open"; job.hiring_owner_id=None
        db.commit()
    with TestClient(app) as client:
        headers,_,membership=create_pool_and_membership(client,seed); membership_id=membership.json()["data"]["id"]
        request_headers={**headers,"Idempotency-Key":"referral-1","If-Match":'"1"'}
        first=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers=request_headers)
        replay=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers=request_headers)
        changed_precondition=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers={**headers,"Idempotency-Key":"referral-1","If-Match":'"2"'})

    assert first.status_code==replay.status_code==200
    assert first.json()==replay.json()
    assert changed_precondition.status_code==409 and changed_precondition.json()["code"]=="idempotency_conflict"
    assert first.json()["data"]["application"]["id"]==str(seed["application_id"])
    assert first.json()["data"]["application"]["stage"]=="review"
    assert first.json()["data"]["membership"]["id"]==membership_id
    with app.state.identity_store.sync_session() as db:
        source=db.get(Application,seed["application_id"])
        membership_row=db.get(TalentPoolMembership,UUID(membership_id))
        task=db.scalar(select(ApplicationReviewTask).where(ApplicationReviewTask.application_id==source.id,ApplicationReviewTask.status=="open"))
        assert source.stage=="review" and source.version==2
        assert membership_row is not None and membership_row.status=="active"
        assert task is not None and task.assignee_id==seed["admin_id"]
        assert db.query(ApplicationStageEvent).filter_by(application_id=source.id,event_type="application.stage_changed").count()==1
        assert db.query(AuditLog).filter_by(event_type="talent_pool.review_referred").count()==1
        assert db.query(IdempotencyRecord).filter_by(operation="talent_pool.review_referral").count()==1


def test_deferred_membership_referral_allows_visible_hr_with_source_job_transition_access(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    authorized_recruiter_id = seed_user(app, "recruiter", "referral-authorized@example.test")
    seed_user(app, "recruiter", "referral-denied@example.test")
    routed_at = datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "deferred"
        source.version = 1
        resume = database.get(Resume, source.resume_id)
        item, results = seed_screening_results(
            database,
            source,
            resume.file_object_id,
            seed["admin_id"],
            [("referral-rule", 97, "优先沟通", routed_at)],
        )
        evaluation = seed_llm_evaluation(
            database,
            source,
            seed["admin_id"],
            results[0],
            48,
            "暂缓",
            routed_at,
        )
        evaluation.gaps = ["分布式系统经验不足"]
        audit = seed_terminal_route_audit(
            database,
            source,
            item,
            seed["admin_id"],
            route="deferred",
            score=48,
            created_at=routed_at,
        )
        bind_terminal_audit_to_evaluation(audit, evaluation)
        database.add(
            JobCollaborator(
                organization_id=source.organization_id,
                job_id=source.job_id,
                user_id=authorized_recruiter_id,
                access_role="job_recruiter",
            )
        )
        database.commit()

    with TestClient(app) as client:
        _, pool, membership = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        membership_id = membership.json()["data"]["id"]

        authorized_headers = login(client, "referral-authorized@example.test")
        ordinary_pool_denied = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",
            json={},
            headers={**authorized_headers, "Idempotency-Key": "ordinary-pool-referral", "If-Match": '"1"'},
        )
        mark_deferred_system_pool(app, pool_id)

        denied_headers = login(client, "referral-denied@example.test")
        denied = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",
            json={},
            headers={**denied_headers, "Idempotency-Key": "referral-denied", "If-Match": '"1"'},
        )
        authorized_headers = login(client, "referral-authorized@example.test")
        referred = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",
            json={},
            headers={**authorized_headers, "Idempotency-Key": "referral-authorized", "If-Match": '"1"'},
        )

    assert ordinary_pool_denied.status_code == 404
    assert denied.status_code == 404
    assert referred.status_code == 200
    assert referred.json()["data"]["application"]["stage"] == "review"
    assert referred.json()["data"]["membership"]["source_application"]["stage"] == "review"
    assert referred.json()["data"]["membership"]["deferred_screening"] == {
        "final_score": 48,
        "deferred_at": routed_at.isoformat(),
        "main_gaps": ["分布式系统经验不足"],
    }


def test_deferred_membership_referral_requires_version_deferred_stage_and_open_job(tmp_path) -> None:
    app=make_app(tmp_path); seed=seed_application(app)
    with app.state.identity_store.sync_session() as db:
        source=db.get(Application,seed["application_id"]); source.stage="deferred"
        job=db.get(Job,source.job_id); job.status="open"; db.commit()
    with app.state.identity_store.sync_session() as db:
        other=Organization(slug="other",name="Other",status="active")
        other_admin=User(organization=other,email="other-admin@example.test",normalized_email="other-admin@example.test",display_name="Other Admin",password_hash=PasswordService().hash("correct horse")); other_admin.roles.append(UserRole(role="recruiting_admin")); db.add(other_admin); db.commit()
    with TestClient(app) as client:
        headers,_,membership=create_pool_and_membership(client,seed); membership_id=membership.json()["data"]["id"]
        missing=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers={**headers,"Idempotency-Key":"missing-version"})
        stale=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers={**headers,"Idempotency-Key":"stale-version","If-Match":'"2"'})
        with app.state.identity_store.sync_session() as db:
            db.get(Job,seed["job_id"]).status="closed"; db.commit()
        closed=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers={**headers,"Idempotency-Key":"closed-job","If-Match":'"1"'})
        client.post("/api/v1/auth/logout",headers=headers)
        authenticated=client.post("/api/v1/auth/login",json={"organization_slug":"other","email":"other-admin@example.test","password":"correct horse"},headers={"Origin":"https://hr.example.test"})
        other_headers={"Origin":"https://hr.example.test","X-CSRF-Token":authenticated.headers["X-CSRF-Token"],"Idempotency-Key":"cross-tenant","If-Match":'"1"'}
        cross_tenant=client.post(f"/api/v1/talent-pool-memberships/{membership_id}/review-referrals",json={},headers=other_headers)
    assert missing.status_code==428 and missing.json()["code"]=="precondition_required"
    assert stale.status_code==409 and stale.json()["code"]=="version_conflict"
    assert closed.status_code==404 and closed.json()["code"]=="resource_not_found"
    assert cross_tenant.status_code==404 and cross_tenant.json()["code"]=="resource_not_found"
    assert str(seed["application_id"]) not in cross_tenant.text
    assert "deferred_screening" not in cross_tenant.text


def test_only_named_active_application_constraint_is_translated() -> None:
    active = IntegrityError("insert", {}, ConstraintOrigin("uq_applications_active"))
    unrelated = IntegrityError("insert", {}, ConstraintOrigin("uq_talent_pool_membership_candidate"))

    assert _is_active_application_conflict(active) is True
    assert _is_active_application_conflict(unrelated) is False


def test_pool_membership_round_trip_is_tenant_scoped_and_does_not_mutate_application(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "rejected"
        source.version += 1
        database.commit()
        source_version = source.version

    with TestClient(app) as client:
        headers, pool, membership = create_pool_and_membership(client, seed)
        pool_id = pool.json()["data"]["id"]
        listed = client.get("/api/v1/talent-pools", headers=headers)
        members = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=headers)

    assert listed.status_code == members.status_code == 200
    assert listed.headers["Cache-Control"] == members.headers["Cache-Control"] == "no-store"
    assert listed.json()["data"][0]["member_count"] == 1
    member = members.json()["data"][0]
    assert member["candidate"]["display_name"] == "李嘉明"
    assert member["source_application"]["id"] == str(seed["application_id"])
    assert member["deferred_screening"] is None
    assert member["tags"] == ["RAG", "Agent"]
    assert "email" not in members.text
    assert "phone" not in members.text

    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        assert source.stage == "rejected"
        assert source.version == source_version


def test_private_pool_is_hidden_from_other_recruiters_and_cross_tenant_admins(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as database:
        admin = database.get(User, seed["admin_id"])
        recruiter = User(
            organization_id=admin.organization_id,
            email="pool-recruiter@example.test",
            normalized_email="pool-recruiter@example.test",
            display_name="Pool Recruiter",
            password_hash=PasswordService().hash("correct horse"),
        )
        recruiter.roles.append(UserRole(role="recruiter"))
        other_org = Organization(slug="pool-other", name="Pool Other", status="active")
        other_admin = User(
            organization=other_org,
            email="pool-other@example.test",
            normalized_email="pool-other@example.test",
            display_name="Other Admin",
            password_hash=PasswordService().hash("correct horse"),
        )
        other_admin.roles.append(UserRole(role="recruiting_admin"))
        database.add_all([recruiter, other_admin])
        database.commit()

    payload = {**pool_payload(seed["admin_id"]), "name": "仅负责人可见", "visibility": "private"}
    with TestClient(app) as client:
        owner_headers = login(client, "interview-admin@example.test")
        created = client.post(
            "/api/v1/talent-pools",
            json=payload,
            headers={**owner_headers, "Idempotency-Key": "private-pool"},
        )
        pool_id = created.json()["data"]["id"]
        recruiter_headers = login(client, "pool-recruiter@example.test")
        assert client.get("/api/v1/talent-pools", headers=recruiter_headers).json()["data"] == []
        denied = client.get(f"/api/v1/talent-pools/{pool_id}", headers=recruiter_headers)

        other_login = client.post(
            "/api/v1/auth/login",
            json={"organization_slug": "pool-other", "email": "pool-other@example.test", "password": "correct horse"},
            headers={"Origin": "https://hr.example.test"},
        )
        cross_tenant = client.get(
            f"/api/v1/talent-pools/{pool_id}",
            headers={"Origin": "https://hr.example.test", "X-CSRF-Token": other_login.headers["X-CSRF-Token"]},
        )

    assert denied.status_code == cross_tenant.status_code == 404
    assert denied.json()["code"] == cross_tenant.json()["code"] == "resource_not_found"


def test_reactivation_creates_linked_application_preserves_history_and_rejects_active_duplicate(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "rejected"
        source.version += 1
        target = Job(
            organization_id=source.organization_id,
            title="RAG Engineer",
            owner_id=seed["admin_id"],
            status="open",
        )
        database.add(target)
        database.flush()
        database.add(
            JobCollaborator(
                organization_id=source.organization_id,
                job_id=target.id,
                user_id=seed["admin_id"],
                access_role="job_owner",
            )
        )
        database.commit()
        target_id = target.id
        source_version = source.version

    with TestClient(app) as client:
        headers, _, membership = create_pool_and_membership(client, seed)
        membership_id = membership.json()["data"]["id"]
        with app.state.identity_store.sync_session() as database:
            pii_counts = {
                "contacts": database.query(CandidateContact).count(),
                "files": database.query(FileObject).count(),
                "resumes": database.query(Resume).count(),
            }
        first = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(target_id)},
            headers={**headers, "Idempotency-Key": "reactivate-rag"},
        )
        replay = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(target_id)},
            headers={**headers, "Idempotency-Key": "reactivate-rag"},
        )
        duplicate = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(target_id)},
            headers={**headers, "Idempotency-Key": "reactivate-rag-again"},
        )
        timeline = client.get(
            f"/api/v1/candidates/{seed['candidate_id']}/timeline",
            headers=headers,
        )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "active_application_exists"
    assert timeline.status_code == 200
    assert any(
        event["event_type"] == "application.reactivated"
        and event["summary"] == "Application reactivated from talent pool"
        for event in timeline.json()["data"]
    )
    created_id = first.json()["data"]["id"]
    assert first.json()["data"]["source_application_id"] == str(seed["application_id"])
    assert first.json()["data"]["source"] == "talent_pool_reactivation"

    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        created = database.get(Application, UUID(created_id))
        membership_row = database.get(TalentPoolMembership, UUID(membership_id))
        assert source.stage == "rejected"
        assert source.version == source_version
        assert created.candidate_id == source.candidate_id
        assert created.job_id == target_id
        assert created.resume_id == source.resume_id
        assert created.source_application_id == source.id
        assert membership_row.status == "active"
        assert database.query(ApplicationStageEvent).filter(
            ApplicationStageEvent.event_type == "application.reactivated"
        ).count() == 1
        assert database.query(CandidateEvent).filter(
            CandidateEvent.event_type == "candidate.reactivated"
        ).count() == 1
        assert database.query(AuditLog).filter(
            AuditLog.event_type == "talent_pool.member_reactivated"
        ).count() == 1
        assert database.query(IdempotencyRecord).filter(
            IdempotencyRecord.operation == "talent_pool.reactivate"
        ).count() == 1
        assert database.query(CandidateContact).count() == pii_counts["contacts"]
        assert database.query(FileObject).count() == pii_counts["files"]
        assert database.query(Resume).count() == pii_counts["resumes"]


def test_source_application_requires_job_access_and_is_redacted_after_access_is_lost(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    recruiter_id = seed_user(app, "recruiter", "talent-scoped@example.test")
    with app.state.identity_store.sync_session() as database:
        source = database.get(Application, seed["application_id"])
        source.stage = "rejected"
        source.version += 1
        denied_job = Job(
            organization_id=source.organization_id,
            title="Restricted source",
            owner_id=seed["admin_id"],
            status="open",
        )
        target_job = Job(
            organization_id=source.organization_id,
            title="Authorized target",
            owner_id=recruiter_id,
            status="open",
        )
        denied_file = FileObject(
            organization_id=source.organization_id,
            storage_key="talent/restricted-resume.pdf",
            original_filename="restricted.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="b" * 64,
            uploaded_by=seed["admin_id"],
        )
        database.add_all([denied_job, target_job, denied_file])
        database.flush()
        denied_resume = Resume(
            organization_id=source.organization_id,
            candidate_id=source.candidate_id,
            file_object_id=denied_file.id,
            version_number=2,
            parsed_text="restricted resume contents",
        )
        database.add(denied_resume)
        database.flush()
        denied_application = Application(
            organization_id=source.organization_id,
            candidate_id=source.candidate_id,
            job_id=denied_job.id,
            resume_id=denied_resume.id,
            owner_id=seed["admin_id"],
            stage="rejected",
        )
        source_grant = JobCollaborator(
            organization_id=source.organization_id,
            job_id=source.job_id,
            user_id=recruiter_id,
            access_role="job_recruiter",
        )
        target_grant = JobCollaborator(
            organization_id=source.organization_id,
            job_id=target_job.id,
            user_id=recruiter_id,
            access_role="job_owner",
        )
        database.add_all([denied_application, source_grant, target_grant])
        database.commit()
        denied_application_id = denied_application.id
        denied_resume_id = denied_resume.id
        target_job_id = target_job.id
        source_resume_id = source.resume_id

    with TestClient(app) as client:
        admin_headers = login(client, "interview-admin@example.test")
        pool = client.post(
            "/api/v1/talent-pools",
            json={**pool_payload(recruiter_id), "name": "Scoped talent", "visibility": "private"},
            headers={**admin_headers, "Idempotency-Key": "scoped-talent-pool"},
        )
        assert pool.status_code == 201
        pool_id = pool.json()["data"]["id"]
        mark_deferred_system_pool(app, pool_id)
        recruiter_headers = login(client, "talent-scoped@example.test")
        denied_membership = client.post(
            f"/api/v1/talent-pools/{pool_id}/memberships",
            json={**membership_payload(seed), "source_application_id": str(denied_application_id), "owner_id": str(recruiter_id)},
            headers={**recruiter_headers, "Idempotency-Key": "denied-source-membership"},
        )
        assert denied_membership.status_code == 404
        membership = client.post(
            f"/api/v1/talent-pools/{pool_id}/memberships",
            json={**membership_payload(seed), "owner_id": str(recruiter_id)},
            headers={**recruiter_headers, "Idempotency-Key": "allowed-source-membership"},
        )
        assert membership.status_code == 201
        membership_id = membership.json()["data"]["id"]

        with app.state.identity_store.sync_session() as database:
            database.delete(database.get(JobCollaborator, source_grant.id))
            database.commit()

        listed = client.get(f"/api/v1/talent-pools/{pool_id}/memberships", headers=recruiter_headers)
        default_resume = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(target_job_id)},
            headers={**recruiter_headers, "Idempotency-Key": "default-source-resume"},
        )
        override_resume = client.post(
            f"/api/v1/talent-pool-memberships/{membership_id}/reactivations",
            json={"job_id": str(target_job_id), "resume_id": str(denied_resume_id)},
            headers={**recruiter_headers, "Idempotency-Key": "override-source-resume"},
        )
        source_preview = client.get(f"/api/v1/resumes/{source_resume_id}/preview", headers=recruiter_headers)
        override_preview = client.get(f"/api/v1/resumes/{denied_resume_id}/preview", headers=recruiter_headers)

    assert listed.status_code == 200
    assert listed.json()["data"][0]["source_application"] == {
        "id": str(seed["application_id"]),
        "redacted": True,
    }
    assert listed.json()["data"][0]["deferred_screening"] is None
    assert default_resume.status_code == override_resume.status_code == 404
    assert source_preview.status_code == override_preview.status_code == 404
    with app.state.identity_store.sync_session() as database:
        assert database.query(Application).filter(
            Application.organization_id == source.organization_id,
            Application.candidate_id == seed["candidate_id"],
            Application.job_id == target_job_id,
        ).count() == 0


def test_membership_removal_audit_does_not_store_user_reason(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = seed_application(app)
    removal_reason = "候选人要求停止联系，内部备注不得进入审计元数据"

    with TestClient(app) as client:
        headers, _, membership = create_pool_and_membership(client, seed)
        member = membership.json()["data"]
        removed = client.request(
            "DELETE",
            f"/api/v1/talent-pool-memberships/{member['id']}",
            json={"reason": removal_reason},
            headers={**headers, "If-Match": f'"{member["version"]}"'},
        )

    assert removed.status_code == 204
    with app.state.identity_store.sync_session() as database:
        audit = database.scalar(
            select(AuditLog).where(AuditLog.event_type == "talent_pool.member_removed")
        )
        assert audit is not None
        assert audit.metadata_json["reason_provided"] is True
        assert removal_reason not in str(audit.metadata_json)
