from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from server.app.governance.deletion_models import (
    DeletionArtifact,
    DeletionRecoveryCheckpoint,
    DeletionRecoveryRun,
    DeletionRequest,
    LegalHold,
)
from server.app.governance.deletion_service import (
    DeletionDomainError,
    advance_deletion_status,
    canonical_manifest_hash,
    hold_placement_outcome,
    impact_manifest_projection,
    validate_approval,
    validate_new_request,
    validate_recovery_generation,
)
from server.app.recruiting.models import Candidate


def _constraint_columns(constraint: object) -> list[str]:
    return [column.name for column in constraint.columns]


def test_deletion_models_register_tenant_scoped_schema_contract() -> None:
    request = DeletionRequest.__table__
    artifact = DeletionArtifact.__table__
    hold = LegalHold.__table__
    recovery = DeletionRecoveryRun.__table__
    checkpoint = DeletionRecoveryCheckpoint.__table__

    assert request.name == "deletion_requests"
    assert artifact.name == "deletion_artifacts"
    assert hold.name == "legal_holds"
    assert recovery.name == "deletion_recovery_runs"
    assert checkpoint.name == "deletion_recovery_checkpoints"
    assert Candidate.__table__.c.deleted_at.nullable
    assert "reason_code" in request.c
    assert "reason" not in request.c
    assert "executed_by" not in request.c

    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and _constraint_columns(constraint) == ["organization_id", "candidate_id"]
        and [element.target_fullname for element in constraint.elements]
        == ["candidates.organization_id", "candidates.id"]
        for constraint in request.constraints
    )
    assert any(
        isinstance(constraint, ForeignKeyConstraint)
        and _constraint_columns(constraint) == ["organization_id", "request_id"]
        and [element.target_fullname for element in constraint.elements]
        == ["deletion_requests.organization_id", "deletion_requests.id"]
        for constraint in artifact.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and _constraint_columns(constraint) == ["request_id", "kind", "storage_key"]
        for constraint in artifact.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and _constraint_columns(constraint) == ["organization_id", "restore_id"]
        for constraint in recovery.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and _constraint_columns(constraint) == ["run_id", "ledger_sha256"]
        for constraint in checkpoint.constraints
    )

    checks = {
        str(constraint.sqltext)
        for table in (request, artifact, hold, recovery)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "version >= 1",
        "manifest_schema_version = 1",
        "recovery_generation >= 0",
        "attempts >= 0",
        "length(reason) <= 1000",
    } <= checks
    assert any(
        "length(manifest_hash) = 64" in check
        and "lower(manifest_hash)" in check
        and all(f"'{character}'" in check for character in "0123456789abcdef")
        for check in checks
    )

    indexes = {index.name: index for table in (request, hold) for index in table.indexes}
    assert isinstance(indexes["uq_deletion_requests_open_candidate"], Index)
    assert indexes["uq_deletion_requests_open_candidate"].unique
    assert str(indexes["uq_deletion_requests_open_candidate"].dialect_options["postgresql"]["where"]) == "status <> 'completed'"
    assert indexes["uq_legal_holds_active_candidate"].unique
    assert str(indexes["uq_legal_holds_active_candidate"].dialect_options["postgresql"]["where"]) == "released_at IS NULL"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("requested", "approved"),
        ("approved", "executing"),
        ("executing", "completed"),
        ("executing", "failed"),
        ("failed", "approved"),
        ("completed", "completed"),
    ],
)
def test_exact_deletion_state_edges_are_allowed(current: str, target: str) -> None:
    assert advance_deletion_status(current, target) == target


def test_complete_known_deletion_state_transition_matrix() -> None:
    states = ("requested", "approved", "executing", "completed", "failed")
    allowed = {
        ("requested", "approved"),
        ("approved", "executing"),
        ("executing", "completed"),
        ("executing", "failed"),
        ("failed", "approved"),
        ("completed", "completed"),
    }

    for current in states:
        for target in states:
            if (current, target) in allowed:
                assert advance_deletion_status(current, target) == target
            else:
                with pytest.raises(
                    DeletionDomainError,
                    match="^invalid_deletion_state_transition$",
                ):
                    advance_deletion_status(current, target)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"approver_id": "requester"}, "self_approval_forbidden"),
        ({"has_active_application": True}, "active_application_exists"),
        ({"has_active_hold": True}, "legal_hold_active"),
        ({"candidate_version": 4}, "stale_candidate_version"),
        ({"request_version": 6}, "stale_request_version"),
        ({"manifest_hash": "b" * 64}, "stale_manifest_hash"),
    ],
)
def test_approval_rejections_use_stable_non_disclosing_codes(
    changes: dict[str, object], code: str
) -> None:
    values: dict[str, object] = {
        "requester_id": "requester",
        "approver_id": "approver",
        "has_active_application": False,
        "has_active_hold": False,
        "candidate_version": 3,
        "expected_candidate_version": 3,
        "request_version": 5,
        "expected_request_version": 5,
        "manifest_hash": "a" * 64,
        "expected_manifest_hash": "a" * 64,
    }
    values.update(changes)

    with pytest.raises(DeletionDomainError) as raised:
        validate_approval(**values)

    assert raised.value.code == code
    assert str(raised.value) == code
    assert "requester" not in str(raised.value)
    assert "a" * 64 not in str(raised.value)


def test_valid_approval_guard_returns_none() -> None:
    assert (
        validate_approval(
            requester_id="requester",
            approver_id="approver",
            has_active_application=False,
            has_active_hold=False,
            candidate_version=3,
            expected_candidate_version=3,
            request_version=5,
            expected_request_version=5,
            manifest_hash="a" * 64,
            expected_manifest_hash="a" * 64,
        )
        is None
    )


def test_completed_candidate_cannot_receive_new_request() -> None:
    with pytest.raises(DeletionDomainError, match="^candidate_deletion_completed$"):
        validate_new_request(has_completed_request=True)
    assert validate_new_request(has_completed_request=False) is None


def test_hold_placement_semantics_are_exact() -> None:
    assert hold_placement_outcome("requested") == ("requested", None)
    assert hold_placement_outcome("approved") == ("failed", "legal_hold_active")
    with pytest.raises(DeletionDomainError, match="^deletion_already_executing$"):
        hold_placement_outcome("executing")


def test_impact_manifest_projection_is_fixed_and_private() -> None:
    request_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    backup_window_ends_at = datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc)
    private_manifest = {
        "candidate_id": str(candidate_id),
        "display_name": "Private Name",
        "email": "private@example.test",
        "notes": "private notes",
        "resume_text": "private resume",
        "filename": "private.pdf",
        "storage_key": "private/object",
        "url": "https://private.example.test",
        "feedback": "private feedback",
        "credential": "secret",
        "counts": {
            "contacts": 1,
            "resumes": 2,
            "applications": 3,
            "screening_records": 4,
            "interviews": 5,
            "feedback_records": 6,
            "talent_memberships": 7,
            "resume_objects": 8,
            "temporary_exports": 9,
            "database_rows": 99,
            "report_export_objects": 99,
            "unexpected_private_count": 99,
        },
    }

    projection = impact_manifest_projection(
        private_manifest,
        request_id=request_id,
        candidate_version=7,
        policy_version=4,
        backup_window_ends_at=backup_window_ends_at,
    )

    assert projection == {
        "schema_version": 1,
        "candidate_ref": str(request_id),
        "candidate_version": 7,
        "policy_version": 4,
        "counts": {
            "contacts": 1,
            "resumes": 2,
            "applications": 3,
            "screening_records": 4,
            "interviews": 5,
            "feedback_records": 6,
            "talent_memberships": 7,
            "resume_objects": 8,
            "temporary_exports": 9,
        },
        "backup_window_ends_at": "2026-10-12T08:30:00Z",
    }
    rendered = repr(projection)
    for private_value in (
        str(candidate_id),
        "Private Name",
        "private@example.test",
        "private notes",
        "private resume",
        "private.pdf",
        "private/object",
        "https://private.example.test",
        "private feedback",
        "secret",
    ):
        assert private_value not in rendered


@pytest.mark.parametrize(
    "invalid_count",
    [True, 1.5, "1", None, -1, 2_147_483_648],
)
def test_impact_projection_rejects_invalid_counts_without_disclosing_values(
    invalid_count: object,
) -> None:
    with pytest.raises(DeletionDomainError) as raised:
        impact_manifest_projection(
            {"counts": {"contacts": invalid_count}},
            request_id=uuid.uuid4(),
            candidate_version=1,
            policy_version=1,
            backup_window_ends_at=datetime.now(timezone.utc),
        )

    assert raised.value.code == "invalid_impact_manifest"
    assert str(raised.value) == "invalid_impact_manifest"


def test_impact_projection_accepts_maximum_manifest_count() -> None:
    projection = impact_manifest_projection(
        {"counts": {"contacts": 2_147_483_647}},
        request_id=uuid.uuid4(),
        candidate_version=1,
        policy_version=1,
        backup_window_ends_at=datetime.now(timezone.utc),
    )

    assert projection["counts"]["contacts"] == 2_147_483_647


def test_canonical_manifest_hash_is_deterministic_and_does_not_return_manifest() -> None:
    first = {"z": [3, {"b": 2, "a": 1}], "private": "value"}
    second = {"private": "value", "z": [3, {"a": 1, "b": 2}]}

    digest = canonical_manifest_hash(first)

    assert digest == canonical_manifest_hash(second)
    assert len(digest) == 64
    assert "private" not in digest
    assert "value" not in digest


def test_canonical_manifest_hash_rejects_non_json_without_disclosure() -> None:
    with pytest.raises(DeletionDomainError) as raised:
        canonical_manifest_hash({"private": object()})

    assert raised.value.code == "invalid_impact_manifest"
    assert str(raised.value) == "invalid_impact_manifest"


@pytest.mark.parametrize("generation", [0, 1, 100])
def test_recovery_generation_accepts_non_negative_integers(generation: int) -> None:
    assert validate_recovery_generation(generation) == generation


@pytest.mark.parametrize("generation", [-1, 1.5, True])
def test_recovery_generation_rejects_invalid_values(generation: object) -> None:
    with pytest.raises(DeletionDomainError, match="^invalid_recovery_generation$"):
        validate_recovery_generation(generation)
