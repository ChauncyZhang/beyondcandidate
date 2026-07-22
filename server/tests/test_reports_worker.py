import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from server.app.identity.models import User, UserStatus
from server.app.queue.models import BackgroundJob
from server.app.queue.service import PermanentJobError, RetryableJobError
from server.app.recruiting.models import Application
from server.app.reports.models import ExportCandidateMembership, ExportRecord
from server.app.worker import main as worker_main
from server.app.worker.main import build_screening_handlers
from server.tests.test_recruiting_api import login
from server.tests.test_reports_api import NOW, _seed_job_facts, make_app


@dataclass
class FakeMinio:
    objects: dict[str, bytes] = field(default_factory=dict)
    fail_writes: bool = False

    def put_object(self, bucket, key, stream, length, *, content_type):
        if self.fail_writes:
            raise RuntimeError("private storage unavailable")
        assert bucket == "reports"
        assert content_type == "text/csv; charset=utf-8"
        content = stream.read(length)
        assert len(content) == length
        self.objects[key] = content


def _job(organization_id, record_id, **payload_changes):
    payload = {"organization_id": str(organization_id), "export_id": str(record_id)}
    payload.update(payload_changes)
    return SimpleNamespace(
        id=uuid4(),
        organization_id=organization_id,
        type="reports.export",
        payload=payload,
        attempts=1,
        trace_id="reports-worker-trace",
    )


def _create_export(app, seed):
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/exports",
            json={"job_id": str(seed["allowed_job_id"])},
            headers={"Idempotency-Key": "worker-export", **login(client, "recruiter@reports.test")},
        )
    assert response.status_code == 201
    return UUID(response.json()["data"]["id"])


def test_registered_report_handler_completes_persisted_export_and_stores_scoped_csv(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)
    storage = FakeMinio()
    handlers = build_screening_handlers(app.state.settings, storage, "reports")

    asyncio.run(handlers["reports.export"](_job(seed["organization_id"], export_id)))

    with app.state.identity_store.sync_session() as db:
        export = db.get(ExportRecord, export_id)
        assert export.status == "succeeded"
        assert export.row_count == 2
        assert export.object_key in storage.objects
        memberships = db.scalars(
            select(ExportCandidateMembership).where(
                ExportCandidateMembership.export_id == export_id
            )
        ).all()
        expected_candidates = set(
            db.scalars(
                select(Application.candidate_id).where(
                    Application.job_id == seed["allowed_job_id"]
                )
            )
        )
        assert {row.candidate_id for row in memberships} == expected_candidates
        content = storage.objects[export.object_key].decode("utf-8-sig")
    assert "Allowed new" in content
    assert "Allowed review" in content
    assert "Denied candidate" not in content
    assert "private resume text" not in content


def test_stale_export_finalize_cannot_resurrect_cancelled_export(tmp_path) -> None:
    from server.app.reports.service import finalize_export, prepare_export

    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)

    with app.state.identity_store.sync_session.begin() as db:
        prepared = prepare_export(db, export_id)
    assert prepared.object_key == f"exports/{seed['organization_id']}/{export_id}.csv"

    with app.state.identity_store.sync_session.begin() as db:
        export = db.get(ExportRecord, export_id)
        export.status = "failed"
        export.safe_error_code = "deletion_in_progress"

    app.state.export_storage.write(
        prepared.object_key, prepared.content, "text/csv; charset=utf-8"
    )
    with app.state.identity_store.sync_session.begin() as db:
        assert finalize_export(db, prepared) is False
        export = db.get(ExportRecord, export_id)
        assert export.status == "failed"
        assert export.safe_error_code == "deletion_in_progress"


@pytest.mark.parametrize(
    "payload_changes",
    [
        {"export_id": "not-a-uuid"},
        {"unexpected": "field"},
    ],
)
def test_report_handler_rejects_malformed_payload_with_permanent_safe_code(tmp_path, payload_changes) -> None:
    app = make_app(tmp_path)
    handler = build_screening_handlers(app.state.settings, FakeMinio(), "reports")["reports.export"]
    with pytest.raises(PermanentJobError) as raised:
        asyncio.run(handler(_job(uuid4(), uuid4(), **payload_changes)))
    assert raised.value.safe_code == "report_export_payload_invalid"


def test_report_handler_maps_missing_and_unauthorized_exports_to_permanent_safe_failure(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)
    handler = build_screening_handlers(app.state.settings, FakeMinio(), "reports")["reports.export"]

    with pytest.raises(PermanentJobError) as missing:
        asyncio.run(handler(_job(seed["organization_id"], uuid4())))
    assert missing.value.safe_code == "report_export_unavailable"

    with app.state.identity_store.sync_session() as db:
        db.get(User, seed["user_id"]).status = UserStatus.DISABLED
        db.commit()
    with pytest.raises(PermanentJobError) as unauthorized:
        asyncio.run(handler(_job(seed["organization_id"], export_id)))
    assert unauthorized.value.safe_code == "report_export_unavailable"


def test_report_handler_maps_storage_failure_to_retryable_safe_failure(tmp_path) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)
    handler = build_screening_handlers(
        app.state.settings, FakeMinio(fail_writes=True), "reports"
    )["reports.export"]
    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(handler(_job(seed["organization_id"], export_id)))
    assert raised.value.safe_code == "report_export_storage_unavailable"
    with app.state.identity_store.sync_session() as db:
        assert db.get(ExportRecord, export_id).status == "running"


def test_report_handler_maps_export_limits_to_permanent_safe_failure(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)
    handler = build_screening_handlers(app.state.settings, FakeMinio(), "reports")["reports.export"]

    def reject_large_export(*_args, **_kwargs):
        from server.app.reports.service import ExportLimitExceeded

        raise ExportLimitExceeded("bounded")

    monkeypatch.setattr(worker_main, "prepare_export", reject_large_export)
    with pytest.raises(PermanentJobError) as raised:
        asyncio.run(handler(_job(seed["organization_id"], export_id)))
    assert raised.value.safe_code == "report_export_too_large"


def test_report_terminal_callback_marks_the_export_failed(tmp_path) -> None:
    from server.app.reports.terminal import report_terminal_callbacks

    app = make_app(tmp_path)
    seed = _seed_job_facts(app)
    export_id = _create_export(app, seed)
    callbacks = report_terminal_callbacks()
    assert set(callbacks) == {"reports.export"}
    assert "reports.export" in worker_main.build_terminal_callbacks()

    with app.state.identity_store.sync_session() as db:
        export = db.get(ExportRecord, export_id)
        job = db.get(BackgroundJob, export.background_job_id)
        callbacks["reports.export"](db, job, "report_export_too_large", NOW)
        db.commit()

    with app.state.identity_store.sync_session() as db:
        export = db.get(ExportRecord, export_id)
        assert export.status == "failed"
        assert export.safe_error_code == "report_export_too_large"
        assert export.completed_at.replace(tzinfo=NOW.tzinfo) == NOW


class FailingSessions:
    def begin(self):
        raise OperationalError("begin", {}, RuntimeError("database unavailable"))


def test_report_handler_maps_transient_database_failure_to_retryable_safe_failure() -> None:
    handler = worker_main.ReportExportJobHandler(FailingSessions(), FakeMinio())
    with pytest.raises(RetryableJobError) as raised:
        asyncio.run(handler(_job(uuid4(), uuid4())))
    assert raised.value.safe_code == "report_export_database_unavailable"
