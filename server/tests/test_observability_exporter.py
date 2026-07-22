from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
import os
from pathlib import Path
import subprocess
import uuid

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo


ROOT = Path(__file__).resolve().parents[2]


DISPOSABLE_DATABASE_NAME = "ux09_observability_test"


class _DatabaseNameConnection:
    def __init__(self, database_name: str, statements: list[str]) -> None:
        self.database_name = database_name
        self.statements = statements

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str):  # type: ignore[no-untyped-def]
        self.statements.append(statement)
        return self

    def fetchone(self) -> tuple[str]:
        return (self.database_name,)


def _confirm_disposable_database(
    database_url: str, *, connect_fn=connect  # type: ignore[no-untyped-def]
) -> None:
    if os.environ.get("DISPOSABLE_DATABASE_CONFIRMED") != "1":
        raise RuntimeError("DISPOSABLE_DATABASE_CONFIRMED=1 is required before any DDL")
    with connect_fn(database_url, autocommit=True) as connection:
        database_name = connection.execute("SELECT current_database()").fetchone()[0]
    if database_name != DISPOSABLE_DATABASE_NAME:
        raise RuntimeError(
            f"disposable database must be named {DISPOSABLE_DATABASE_NAME}; got {database_name}"
        )


def test_disposable_postgres_guard_rejects_missing_confirmation_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPOSABLE_DATABASE_CONFIRMED", raising=False)
    connections: list[str] = []

    with pytest.raises(RuntimeError, match="DISPOSABLE_DATABASE_CONFIRMED=1"):
        _confirm_disposable_database(
            "postgresql://owner:secret@postgres/postgres",
            connect_fn=lambda url, **kwargs: connections.append(url),
        )

    assert connections == []


def test_disposable_postgres_guard_rejects_wrong_database_with_zero_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPOSABLE_DATABASE_CONFIRMED", "1")
    statements: list[str] = []

    with pytest.raises(RuntimeError, match=DISPOSABLE_DATABASE_NAME):
        _confirm_disposable_database(
            "postgresql://owner:secret@postgres/production",
            connect_fn=lambda *args, **kwargs: _DatabaseNameConnection(
                "production", statements
            ),
        )

    assert statements == ["SELECT current_database()"]
    assert not any(
        token in statement.upper()
        for statement in statements
        for token in ("CREATE ", "ALTER ", "DROP ", "TRUNCATE ", "INSERT ", "UPDATE ", "DELETE ")
    )


def test_snapshot_provider_reads_only_the_safe_aggregate_view() -> None:
    from server.app.observability.collectors import PostgresQueueSnapshotProvider

    class Cursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

        def fetchall(self) -> list[dict[str, object]]:
            return [
                {
                    "metric_name": "job_count",
                    "dimension_a": "screening.parse_item",
                    "dimension_b": "queued",
                    "dimension_c": "",
                    "value": 2.0,
                },
                {
                    "metric_name": "job_attempt",
                    "dimension_a": "screening.parse_item",
                    "dimension_b": "failed",
                    "dimension_c": "parse",
                    "value": 3.0,
                },
                {
                    "metric_name": "job_attempt_duration",
                    "dimension_a": "screening.parse_item",
                    "dimension_b": "failed",
                    "dimension_c": "parse",
                    "value": 4.5,
                },
            ]

    cursor = Cursor()
    snapshot = PostgresQueueSnapshotProvider("postgresql://ignored")._snapshot(cursor)

    assert cursor.statements == [
        "SELECT metric_name, dimension_a, dimension_b, dimension_c, value "
        "FROM observability.queue_metrics"
    ]
    assert snapshot.job_counts == {("screening.parse_item", "queued"): 2}
    assert snapshot.attempt_stats[("screening.parse_item", "failed", "parse")].count == 3
    assert (
        snapshot.attempt_stats[("screening.parse_item", "failed", "parse")].duration_seconds
        == 4.5
    )


def test_exporter_emits_generic_job_attempt_lease_and_outbox_metrics() -> None:
    from server.app.observability.collectors import AttemptStats, QueueSnapshot
    from server.app.observability.exporter import build_registry

    snapshot = QueueSnapshot(
        job_counts={
            ("screening.parse_item", "queued"): 2,
            ("screening.llm_score_item", "dead_letter"): 1,
        },
        oldest_ready_age_seconds={"screening.parse_item": 125.0},
        attempt_stats={
            ("screening.parse_item", "failed", "parse"): AttemptStats(
                count=3, duration_seconds=4.5
            )
        },
        expired_leases={"job": 1, "outbox": 2},
        dead_letters={"screening.llm_score_item": 1},
        outbox_counts={("audit.created", "queued"): 4},
        oldest_outbox_age_seconds={"audit.created": 65.0},
    )

    payload = generate_latest(build_registry(lambda: snapshot)).decode("utf-8")

    assert 'ux09_jobs{job_type="screening.parse_item",status="queued"} 2.0' in payload
    assert "ux09_job_oldest_ready_age_seconds" in payload
    assert 'error_class="parse"' in payload
    assert 'queue="job"' in payload
    assert 'queue="outbox"' in payload
    assert "ux09_job_dead_letters" in payload
    assert 'topic="audit.created"' in payload
    assert "ux09_outbox_oldest_ready_age_seconds" in payload


def test_governance_job_metrics_are_disabled() -> None:
    from server.app.observability.collectors import QueueSnapshot
    from server.app.observability.exporter import build_registry

    snapshot = QueueSnapshot(
        job_counts={("governance.delete_candidate", "queued"): 9},
        oldest_ready_age_seconds={"governance.delete_candidate": 99.0},
        dead_letters={"governance.delete_candidate": 2},
    )

    payload = generate_latest(build_registry(lambda: snapshot)).decode("utf-8")

    assert "governance" not in payload


def test_real_postgres_snapshot_exports_queue_metrics_without_private_dimensions() -> None:
    database_url = os.environ.get("POSTGRES_OBSERVABILITY_SMOKE_URL")
    if not database_url:
        pytest.skip("POSTGRES_OBSERVABILITY_SMOKE_URL is required for the real collector gate")

    from server.app.observability.collectors import PostgresQueueSnapshotProvider
    from server.app.observability.exporter import build_registry

    _confirm_disposable_database(database_url)
    parse_job = uuid.uuid4()
    canary_type = "alice.canary@example.test"
    with connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DROP VIEW IF EXISTS observability.queue_metrics;
                DROP TABLE IF EXISTS job_attempts, outbox_events, background_jobs;
                CREATE TABLE background_jobs (
                    id uuid PRIMARY KEY, type text NOT NULL, status text NOT NULL,
                    run_after timestamptz NOT NULL, lease_expires_at timestamptz
                );
                CREATE TABLE job_attempts (
                    job_id uuid NOT NULL, result text, safe_error_code text,
                    duration_ms integer
                );
                CREATE TABLE outbox_events (
                    topic text NOT NULL, status text NOT NULL,
                    available_at timestamptz NOT NULL, lease_expires_at timestamptz
                );
                """
            )
            cursor.execute(
                """
                INSERT INTO background_jobs (id, type, status, run_after, lease_expires_at)
                VALUES (%s, 'screening.parse_item', 'queued', now() - interval '2 minutes', NULL),
                       (%s, 'screening.llm_score_item', 'dead_letter', now(), NULL),
                       (%s, 'governance.delete_candidate', 'queued', now() - interval '1 hour', NULL),
                       (%s, %s, 'queued', now(), NULL)
                """,
                (parse_job, uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), canary_type),
            )
            cursor.execute(
                """INSERT INTO job_attempts (job_id, result, safe_error_code, duration_ms)
                   VALUES (%s, 'failed', 'parser_timeout', 1500)""",
                (parse_job,),
            )
            cursor.execute(
                """INSERT INTO outbox_events (topic, status, available_at, lease_expires_at)
                   VALUES ('audit.created', 'queued', now() - interval '1 minute', NULL)"""
            )

    info = conninfo_to_dict(database_url)
    queue_user = "ux09_queue_metrics_smoke"
    queue_password = f"queue-{uuid.uuid4().hex}"
    postgres_user = "ux09_postgres_exporter_smoke"
    postgres_password = f"postgres-{uuid.uuid4().hex}"
    provisioning_environment = os.environ.copy()
    provisioning_environment.update(
        {
            "PGHOST": info.get("host", "localhost"),
            "PGPORT": info.get("port", "5432"),
            "POSTGRES_DB": info["dbname"],
            "POSTGRES_USER": info["user"],
            "POSTGRES_PASSWORD": info["password"],
            "QUEUE_METRICS_DB_USER": queue_user,
            "QUEUE_METRICS_DB_PASSWORD": queue_password,
            "POSTGRES_EXPORTER_DB_USER": postgres_user,
            "POSTGRES_EXPORTER_DB_PASSWORD": postgres_password,
        }
    )
    provision = subprocess.run(
        ["sh", str(ROOT / "deploy" / "observability" / "provision-roles.sh")],
        cwd=ROOT,
        env=provisioning_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert provision.returncode == 0, provision.stdout + provision.stderr
    queue_url = make_conninfo(database_url, user=queue_user, password=queue_password)
    payload = generate_latest(
        build_registry(PostgresQueueSnapshotProvider(queue_url))
    ).decode("utf-8")
    samples = [
        sample
        for family in text_string_to_metric_families(payload)
        for sample in family.samples
    ]

    assert 'job_type="screening.parse_item",status="queued"' in payload
    assert 'job_type="screening.llm_score_item"' in payload
    assert 'error_class="parse"' in payload
    assert any(
        sample.name == "ux09_outbox_events"
        and sample.labels == {"topic": "audit.created", "status": "queued"}
        and sample.value == 1
        for sample in samples
    )
    assert "governance" not in payload
    assert canary_type not in payload
