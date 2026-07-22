from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)

ALEMBIC_TIMEOUT_SECONDS = 60


def _alembic(url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", *args],
        check=check,
        capture_output=not check,
        text=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
        env={**os.environ, "DATABASE_URL": url},
    )


def _reset_to_0016a(url: str) -> None:
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    _alembic(url, "upgrade", "0016a_audit_category_repair")


def _seed_identity(connection) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "org1",
            "org2",
            "user1",
            "user2",
            "candidate",
            "candidate2",
            "job1",
            "job2",
        )
    }
    connection.execute(
        text(
            """
            INSERT INTO organizations(
              id, slug, name, status, retention_policy_id, created_at, updated_at
            ) VALUES
              (:org1, 'deletion-one', 'Deletion One', 'active',
               md5(CAST(:org1 AS text) || '-retention-policy')::uuid, now(), now()),
              (:org2, 'deletion-two', 'Deletion Two', 'active',
               md5(CAST(:org2 AS text) || '-retention-policy')::uuid, now(), now())
            """
        ),
        ids,
    )
    connection.execute(
        text(
            """
            INSERT INTO users(
              id, organization_id, email, normalized_email, display_name,
              password_hash, status, authorization_version, created_at, updated_at
            ) VALUES
              (:user1, :org1, 'one@deletion.test', 'one@deletion.test', 'One',
               'x', 'active', 1, now(), now()),
              (:user2, :org2, 'two@deletion.test', 'two@deletion.test', 'Two',
               'x', 'active', 1, now(), now())
            """
        ),
        ids,
    )
    connection.execute(
        text(
            """
            INSERT INTO candidates(
              id, organization_id, display_name, version, created_at, updated_at
            ) VALUES
              (:candidate, :org1, 'Candidate', 3, now(), now()),
              (:candidate2, :org2, 'Other Candidate', 3, now(), now())
            """
        ),
        ids,
    )
    connection.execute(
        text(
            """
            INSERT INTO background_jobs(
              id, organization_id, type, payload, status, priority, attempts,
              max_attempts, run_after, created_at, updated_at
            ) VALUES
              (:job1, :org1, 'deletion.recovery', '{}'::jsonb, 'queued', 0, 0, 3, now(), now(), now()),
              (:job2, :org2, 'deletion.recovery', '{}'::jsonb, 'queued', 0, 0, 3, now(), now(), now())
            """
        ),
        ids,
    )
    return ids


def _insert_request(connection, ids: dict[str, uuid.UUID], **values: object) -> uuid.UUID:
    request_id = values.pop("request_id", uuid.uuid4())
    parameters = {
        **ids,
        "request_id": request_id,
        "organization_id": ids["org1"],
        "candidate_id": ids["candidate"],
        "requested_by": ids["user1"],
        "approved_by": None,
        "status": "requested",
        **values,
    }
    connection.execute(
        text(
            """
            INSERT INTO deletion_requests(
              id, organization_id, candidate_id, status, version, reason_code,
              requested_by, requested_at, approved_by, impact_manifest, manifest_hash,
              manifest_schema_version, policy_version, candidate_version,
              recovery_generation, created_at, updated_at
            ) VALUES (
              :request_id, :organization_id, :candidate_id, :status, 1,
              'administrator_request', :requested_by, now(), :approved_by, '{}'::jsonb,
              repeat('a', 64), 1, 1, 3, 0, now(), now()
            )
            """
        ),
        parameters,
    )
    return request_id


def test_0017_upgrade_registers_additive_deletion_schema() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "head")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    inspector = inspect(engine)

    assert {
        "deletion_requests",
        "deletion_artifacts",
        "legal_holds",
        "deletion_recovery_runs",
        "deletion_recovery_checkpoints",
        "report_export_candidates",
    } <= set(inspector.get_table_names())
    assert "deleted_at" in {column["name"] for column in inspector.get_columns("candidates")}
    assert "generation_token" in {
        column["name"] for column in inspector.get_columns("report_exports")
    }
    assert {
        "database_redaction_checksum",
        "ledger_completed_at",
        "ledger_object_key",
        "ledger_sha256",
    } <= {column["name"] for column in inspector.get_columns("deletion_requests")}
    assert {
        "ledger_object_key",
        "ledger_sha256",
        "target_generation",
        "queue_job_id",
    } <= {
        column["name"]
        for column in inspector.get_columns("deletion_recovery_checkpoints")
    }
    assert {
        "uq_deletion_requests_open_candidate",
        "uq_legal_holds_active_candidate",
    } <= {
        index["name"]
        for table in ("deletion_requests", "legal_holds")
        for index in inspector.get_indexes(table)
    }
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT has_function_privilege('public', 'redact_candidate_data(uuid, uuid, uuid)', 'EXECUTE')")
        ) is False
    engine.dispose()


def test_0017_enforces_tenant_fks_partial_uniqueness_and_privileged_boundaries() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))

    with engine.begin() as connection:
        ids = _seed_identity(connection)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_request(
                connection,
                ids,
                organization_id=ids["org2"],
                requested_by=ids["user2"],
            )

    with engine.begin() as connection:
        first_request = _insert_request(connection, ids)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_request(connection, ids)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE deletion_requests
                SET status = 'completed', completed_at = now(),
                    database_redaction_checksum = repeat('b', 64),
                    ledger_completed_at = now(),
                    ledger_object_key = 'deletions/test.json',
                    ledger_sha256 = repeat('c', 64)
                WHERE id = :id
                """
            ),
            {"id": first_request},
        )
        _insert_request(connection, ids)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO legal_holds(
                  id, organization_id, candidate_id, reason, placed_by, placed_at,
                  version, created_at, updated_at
                ) VALUES (gen_random_uuid(), :org1, :candidate, 'First', :user1, now(), 1, now(), now())
                """
            ),
            ids,
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO legal_holds(
                      id, organization_id, candidate_id, reason, placed_by, placed_at,
                      version, created_at, updated_at
                    ) VALUES (gen_random_uuid(), :org1, :candidate, 'Second', :user1, now(), 1, now(), now())
                    """
                ),
                ids,
            )

    audit_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_logs(
                  id, organization_id, actor_user_id, category, event_type, outcome,
                  resource_type, resource_id, trace_id, metadata_json, created_at
                ) VALUES (
                  :audit, :org1, :user1, 'governance', 'governance.deletion_requested',
                  'success', 'candidate', :candidate, 'task-b1', '{}'::jsonb, now()
                )
                """
            ),
            {**ids, "audit": audit_id},
        )
        connection.execute(
            text(
                """
                DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'task_b1_app') THEN
                    CREATE ROLE task_b1_app NOLOGIN;
                  END IF;
                END $$;
                GRANT USAGE ON SCHEMA public TO task_b1_app;
                GRANT SELECT, UPDATE ON audit_logs TO task_b1_app;
                """
            )
        )

    with pytest.raises(DBAPIError, match="permission denied"):
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE task_b1_app"))
            connection.execute(
                text("SELECT redact_candidate_data(:org1, :request, :candidate)"),
                {**ids, "request": first_request},
            )
    with pytest.raises(DBAPIError, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL ROLE task_b1_app"))
            connection.execute(
                text("UPDATE audit_logs SET outcome = 'failure' WHERE id = :audit"),
                {"audit": audit_id},
            )
    with pytest.raises(DBAPIError, match="redaction_not_authorized"):
        with engine.begin() as connection:
            connection.execute(
                text("SELECT redact_candidate_data(:org1, :request, :candidate)"),
                {**ids, "request": first_request},
            )

    engine.dispose()


def test_0017_dynamically_enforces_every_task_b_tenant_fk_family() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))

    with engine.begin() as connection:
        ids = _seed_identity(connection)
        request1 = _insert_request(
            connection,
            ids,
            approved_by=ids["user1"],
        )
        request2 = _insert_request(
            connection,
            ids,
            organization_id=ids["org2"],
            candidate_id=ids["candidate2"],
            requested_by=ids["user2"],
            approved_by=ids["user2"],
        )
        artifact = uuid.uuid4()
        hold = uuid.uuid4()
        recovery = uuid.uuid4()
        parameters = {
            **ids,
            "request1": request1,
            "artifact": artifact,
            "hold": hold,
            "recovery": recovery,
        }
        connection.execute(
            text(
                """
                INSERT INTO deletion_artifacts(
                  id, organization_id, request_id, kind, storage_key, status,
                  attempts, created_at, updated_at
                ) VALUES (
                  :artifact, :org1, :request1, 'resume_object', 'private/key',
                  'pending', 0, now(), now()
                )
                """
            ),
            parameters,
        )
        connection.execute(
            text(
                """
                INSERT INTO legal_holds(
                  id, organization_id, candidate_id, reason, placed_by, placed_at,
                  released_by, released_at, released_reason, version, created_at, updated_at
                ) VALUES (
                  :hold, :org1, :candidate, 'Hold', :user1, now(), :user1, now(),
                  'Released', 1, now(), now()
                )
                """
            ),
            parameters,
        )
        connection.execute(
            text(
                """
                INSERT INTO deletion_recovery_runs(
                  id, organization_id, restore_id, restored_at, status,
                  restored_candidate_count, requeued_request_count, queued_at,
                  queue_job_id, created_at, updated_at
                ) VALUES (
                  :recovery, :org1, gen_random_uuid(), now(), 'queued', 0, 0,
                  now(), :job1, now(), now()
                )
                """
            ),
            parameters,
        )

        families = connection.execute(
            text(
                """
                SELECT child.relname, child_column.attname, parent.relname
                FROM pg_constraint constraint_row
                JOIN pg_class child ON child.oid = constraint_row.conrelid
                JOIN pg_class parent ON parent.oid = constraint_row.confrelid
                JOIN LATERAL unnest(constraint_row.conkey, constraint_row.confkey)
                  WITH ORDINALITY AS key_pair(child_attnum, parent_attnum, ordinal)
                  ON true
                JOIN pg_attribute child_column
                  ON child_column.attrelid = child.oid
                 AND child_column.attnum = key_pair.child_attnum
                WHERE constraint_row.contype = 'f'
                  AND child.relname IN (
                    'deletion_requests', 'deletion_artifacts',
                    'legal_holds', 'deletion_recovery_runs'
                  )
                  AND array_length(constraint_row.conkey, 1) = 2
                  AND key_pair.ordinal = 2
                ORDER BY child.relname, child_column.attname
                """
            )
        ).all()

        expected = {
            ("deletion_requests", "candidate_id", "candidates"),
            ("deletion_requests", "requested_by", "users"),
            ("deletion_requests", "approved_by", "users"),
            ("deletion_artifacts", "request_id", "deletion_requests"),
            ("legal_holds", "candidate_id", "candidates"),
            ("legal_holds", "placed_by", "users"),
            ("legal_holds", "released_by", "users"),
            ("deletion_recovery_runs", "queue_job_id", "background_jobs"),
        }
        assert set(families) == expected

        row_ids = {
            "deletion_requests": request1,
            "deletion_artifacts": artifact,
            "legal_holds": hold,
            "deletion_recovery_runs": recovery,
        }
        mismatches = {
            "candidates": ids["candidate2"],
            "users": ids["user2"],
            "deletion_requests": request2,
            "background_jobs": ids["job2"],
        }
        for child_table, child_column, parent_table in families:
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            f'UPDATE "{child_table}" SET "{child_column}" = :mismatch '
                            "WHERE id = :row_id"
                        ),
                        {
                            "mismatch": mismatches[parent_table],
                            "row_id": row_ids[child_table],
                        },
                    )

    engine.dispose()


def test_0017_rejects_non_lowercase_hex_manifest_hashes() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        ids = _seed_identity(connection)
        for manifest_hash in (
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            ("a" * 63) + "-",
        ):
            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            """
                            INSERT INTO deletion_requests(
                              id, organization_id, candidate_id, status, version,
                              reason_code, requested_by, requested_at, impact_manifest,
                              manifest_hash, manifest_schema_version, policy_version,
                              candidate_version, recovery_generation, created_at, updated_at
                            ) VALUES (
                              gen_random_uuid(), :org1, :candidate, 'requested', 1,
                              'administrator_request', :user1, now(), '{}'::jsonb,
                              :manifest_hash, 1, 1, 3, 0, now(), now()
                            )
                            """
                        ),
                        {**ids, "manifest_hash": manifest_hash},
                    )
    engine.dispose()


def test_provisioned_application_role_is_unprivileged_and_cannot_mutate_evidence() -> None:
    role = os.getenv("APP_DB_TEST_USER")
    password = os.getenv("APP_DB_TEST_PASSWORD")
    if not role or not password:
        pytest.skip("provisioned application role test credentials not configured")

    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    owner_url = make_url(url)
    subprocess.run(
        ["sh", "deploy/postgres/provision-app-role.sh"],
        check=True,
        env={
            **os.environ,
            "PGHOST": str(owner_url.host),
            "PGPORT": str(owner_url.port or 5432),
            "POSTGRES_DB": str(owner_url.database),
            "POSTGRES_USER": str(owner_url.username),
            "POSTGRES_PASSWORD": str(owner_url.password),
            "APP_DB_USER": role,
            "APP_DB_PASSWORD": password,
            "GOVERNANCE_DB_USER": "task_b2b1_governance",
            "GOVERNANCE_DB_PASSWORD": "task-b2b1-governance-password",
        },
    )
    owner_engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with owner_engine.begin() as connection:
        ids = _seed_identity(connection)
        audit_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO audit_logs(
                  id, organization_id, actor_user_id, category, event_type, outcome,
                  resource_type, resource_id, trace_id, metadata_json, created_at
                ) VALUES (
                  :audit, :org1, :user1, 'governance',
                  'governance.deletion_requested', 'success', 'candidate',
                  :candidate, 'task-b1-app-role', '{}'::jsonb, now()
                )
                """
            ),
            {**ids, "audit": audit_id},
        )
        properties = connection.execute(
            text(
                """
                SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole,
                       rolreplication, rolcanlogin
                FROM pg_roles WHERE rolname = :role
                """
            ),
            {"role": role},
        ).one()
        assert properties == (False, False, False, False, False, True)

    app_url = make_url(url.replace("+asyncpg", "+psycopg")).set(
        username=role,
        password=password,
    )
    app_engine = create_engine(app_url)
    with app_engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT has_function_privilege(current_user, "
                "'redact_candidate_data(uuid, uuid, uuid)', 'EXECUTE')"
            )
        ) is False
    with pytest.raises(DBAPIError, match="permission denied|append-only"):
        with app_engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_logs SET outcome = 'failure' WHERE id = :audit"),
                {"audit": audit_id},
            )
    with pytest.raises(DBAPIError, match="permission denied"):
        with app_engine.begin() as connection:
            connection.execute(
                text("SELECT redact_candidate_data(:org1, :request, :candidate)"),
                {**ids, "request": uuid.uuid4()},
            )

    app_engine.dispose()
    owner_engine.dispose()


def test_0017_empty_downgrade_removes_additive_schema() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    _alembic(url, "downgrade", "0016a_audit_category_repair")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    inspector = inspect(engine)

    assert not (
        {
            "deletion_requests",
            "deletion_artifacts",
            "legal_holds",
            "deletion_recovery_runs",
            "deletion_recovery_checkpoints",
            "report_export_candidates",
        }
        & set(inspector.get_table_names())
    )
    assert "deleted_at" not in {
        column["name"] for column in inspector.get_columns("candidates")
    }
    assert "generation_token" not in {
        column["name"] for column in inspector.get_columns("report_exports")
    }
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT to_regprocedure('redact_candidate_data(uuid, uuid, uuid)')")
        ) is None
    engine.dispose()


def test_0017_downgrade_waits_for_writer_then_refuses_without_dropping() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    writer = engine.connect()
    transaction = writer.begin()
    ids = _seed_identity(writer)
    writer.execute(
        text(
            """
            INSERT INTO audit_logs(
              id, organization_id, actor_user_id, category, event_type, outcome,
              resource_type, resource_id, trace_id, metadata_json, created_at
            ) VALUES (
              gen_random_uuid(), :org1, :user1, 'governance',
              'governance.deletion_completed', 'success', 'candidate',
              :candidate, 'task-b1-downgrade-race', '{}'::jsonb, now()
            )
            """
        ),
        ids,
    )

    process = subprocess.Popen(
        [
            "python",
            "-m",
            "alembic",
            "-c",
            "server/alembic.ini",
            "downgrade",
            "0016a_audit_category_repair",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    observer = engine.connect()
    deadline = time.monotonic() + 15
    waiting = False
    while time.monotonic() < deadline:
        waiting = bool(
            observer.scalar(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM pg_locks
                      WHERE relation = 'audit_logs'::regclass
                        AND mode = 'AccessExclusiveLock'
                        AND NOT granted
                    )
                    """
                )
            )
        )
        if waiting:
            break
        time.sleep(0.05)

    try:
        assert waiting, "downgrade did not wait on the audit evidence table"
        assert process.poll() is None
        transaction.commit()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode != 0
        assert "refusing 0017 downgrade: deletion governance evidence exists" in (
            stdout + stderr
        )
        assert inspect(engine).has_table("deletion_requests")
        assert "deleted_at" in {
            column["name"] for column in inspect(engine).get_columns("candidates")
        }
        assert observer.scalar(
            text("SELECT to_regprocedure('redact_candidate_data(uuid, uuid, uuid)')")
        ) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if transaction.is_active:
            transaction.rollback()
        writer.close()
        observer.close()
        engine.dispose()
        _reset_to_0016a(url)
        _alembic(url, "upgrade", "head")


@pytest.mark.parametrize(
    "evidence",
    ["request_and_artifact", "hold", "recovery", "ledger", "tombstone"],
)
def test_0017_downgrade_refuses_when_deletion_evidence_exists(evidence: str) -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    _reset_to_0016a(url)
    _alembic(url, "upgrade", "0017_governance_deletion")
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        ids = _seed_identity(connection)
        if evidence == "request_and_artifact":
            request_id = _insert_request(connection, ids)
            connection.execute(
                text(
                    """
                    INSERT INTO deletion_artifacts(
                      id, organization_id, request_id, kind, storage_key, status,
                      attempts, created_at, updated_at
                    ) VALUES (
                      gen_random_uuid(), :org1, :request, 'resume_object',
                      'private/key', 'pending', 0, now(), now()
                    )
                    """
                ),
                {**ids, "request": request_id},
            )
        elif evidence == "hold":
            connection.execute(
                text(
                    """
                    INSERT INTO legal_holds(
                      id, organization_id, candidate_id, reason, placed_by, placed_at,
                      version, created_at, updated_at
                    ) VALUES (gen_random_uuid(), :org1, :candidate, 'Hold', :user1, now(), 1, now(), now())
                    """
                ),
                ids,
            )
        elif evidence == "recovery":
            connection.execute(
                text(
                    """
                    INSERT INTO deletion_recovery_runs(
                      id, organization_id, restore_id, restored_at, status,
                      restored_candidate_count, requeued_request_count, queued_at,
                      created_at, updated_at
                    ) VALUES (
                      gen_random_uuid(), :org1, gen_random_uuid(), now(), 'queued',
                      0, 0, now(), now(), now()
                    )
                    """
                ),
                ids,
            )
        elif evidence == "ledger":
            connection.execute(
                text(
                    """
                    INSERT INTO audit_logs(
                      id, organization_id, actor_user_id, category, event_type, outcome,
                      resource_type, resource_id, trace_id, metadata_json, created_at
                    ) VALUES (
                      gen_random_uuid(), :org1, :user1, 'governance',
                      'governance.deletion_completed', 'success', 'candidate',
                      :candidate, 'task-b1-ledger', '{}'::jsonb, now()
                    )
                    """
                ),
                ids,
            )
        else:
            connection.execute(
                text("UPDATE candidates SET deleted_at = now() WHERE id = :candidate"), ids
            )

    result = _alembic(
        url, "downgrade", "0016a_audit_category_repair", check=False
    )
    try:
        assert result.returncode != 0
        assert "refusing 0017 downgrade: deletion governance evidence exists" in (
            result.stdout + result.stderr
        )
    finally:
        engine.dispose()
        _reset_to_0016a(url)
        _alembic(url, "upgrade", "head")
