import os
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from server.app.core.settings import Settings
from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.main import create_app


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


class _Probe:
    async def check(self) -> None:
        pass


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", *args],
        check=True,
        env={**os.environ, "DATABASE_URL": url},
    )


def test_0016_empty_upgrade_and_downgrade() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "0015_reports_exports")
    _alembic(url, "upgrade", "0016_governance_audit_retention")

    inspector = inspect(engine)
    assert "retention_policies" in inspector.get_table_names()
    assert {"category", "resource_type", "resource_id", "ip_hash"} <= {
        column["name"] for column in inspector.get_columns("audit_logs")
    }

    _alembic(url, "downgrade", "0015_reports_exports")
    inspector.clear_cache()
    assert "retention_policies" not in inspector.get_table_names()
    assert "category" not in {
        column["name"] for column in inspector.get_columns("audit_logs")
    }
    _alembic(url, "upgrade", "head")
    engine.dispose()


def test_0016_backfills_populated_0015_and_preserves_audit_on_downgrade() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "0015_reports_exports")
    ids = {name: uuid.uuid4() for name in ("org", "user", "candidate", "audit", "idem")}

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES (:org, 'governance-migration', 'Governance migration', 'active', now(), now())
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
                ) VALUES (
                  :user, :org, 'governance@test', 'governance@test', 'Governance',
                  'x', 'active', 1, now(), now()
                )
                """
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO candidates(
                  id, organization_id, display_name, version, created_at, updated_at
                ) VALUES (:candidate, :org, 'Candidate', 1, now(), now())
                """
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO idempotency_records(
                  id, organization_id, user_id, operation, idempotency_key,
                  request_hash, status_code, response_json, created_at
                ) VALUES (
                  :idem, :org, :user, 'candidate.create', 'migration-key',
                  repeat('a', 64), 201, '{}', now() - interval '1 hour'
                )
                """
            ),
            ids,
        )
        connection.execute(
            text(
                """
                INSERT INTO audit_logs(
                  id, organization_id, actor_user_id, event_type, outcome,
                  trace_id, metadata_json, created_at
                ) VALUES (
                  :audit, :org, :user, 'candidate.created', 'success',
                  'trace-migration', jsonb_build_object('candidate_id', CAST(:candidate AS text)), now()
                )
                """
            ),
            ids,
        )

    _alembic(url, "upgrade", "0016_governance_audit_retention")
    with engine.connect() as connection:
        policy = connection.execute(
            text(
                """
                SELECT p.id, p.terminal_days, p.talent_pool_days, p.backup_window_days,
                       p.version, p.updated_by, o.retention_policy_id
                FROM retention_policies p
                JOIN organizations o ON o.id = p.organization_id
                WHERE p.organization_id = :org
                """
            ),
            ids,
        ).one()
        assert policy[1:5] == (365, 730, 90, 1)
        assert policy.updated_by is None
        assert policy.id == policy.retention_policy_id
        assert connection.scalar(
            text("SELECT retention_due_at IS NULL FROM candidates WHERE id = :candidate"), ids
        )
        assert connection.scalar(
            text(
                """
                SELECT expires_at = created_at + interval '24 hours'
                FROM idempotency_records WHERE id = :idem
                """
            ),
            ids,
        )
        audit = connection.execute(
            text(
                """
                SELECT category, resource_type, resource_id,
                       metadata_json ? 'candidate_id', tableoid::regclass::text
                FROM audit_logs WHERE id = :audit
                """
            ),
            ids,
        ).one()
        assert audit[:4] == ("recruiting", "candidate", ids["candidate"], False)
        assert audit[4].startswith("audit_logs_") and audit[4] != "audit_logs_default"

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_logs SET outcome = 'failure' WHERE id = :audit"), ids
            )
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_logs WHERE id = :audit"), ids)

    _alembic(url, "downgrade", "0015_reports_exports")
    with engine.connect() as connection:
        legacy = connection.execute(
            text(
                """
                SELECT event_type, metadata_json ->> 'candidate_id'
                FROM audit_logs WHERE id = :audit
                """
            ),
            ids,
        ).one()
        assert legacy == ("candidate.created", str(ids["candidate"]))
        assert connection.scalar(
            text("SELECT count(*) FROM candidates WHERE id = :candidate"), ids
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM idempotency_records WHERE id = :idem"), ids
        ) == 1

    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_logs SET outcome = 'failure' WHERE id = :audit"), ids
            )
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM audit_logs WHERE id = :audit"), ids)

    _alembic(url, "upgrade", "head")
    engine.dispose()


def test_0016_seeds_zero_user_organization_with_system_actor_policy() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "0015_reports_exports")
    organization_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES (:id, 'zero-user', 'Zero user', 'active', now(), now())
                """
            ),
            {"id": organization_id},
        )

    _alembic(url, "upgrade", "0016_governance_audit_retention")
    with engine.connect() as connection:
        policy = connection.execute(
            text(
                """
                SELECT p.version, p.updated_by, o.retention_policy_id = p.id
                FROM organizations o
                JOIN retention_policies p ON p.organization_id = o.id
                WHERE o.id = :id
                """
            ),
            {"id": organization_id},
        ).one()
        assert policy == (1, None, True)

    with pytest.raises(Exception, match="ck_retention_policies_updated_by_version"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE retention_policies SET version = 2 WHERE organization_id = :id"
                ),
                {"id": organization_id},
            )

    updater_id = uuid.uuid4()
    other_organization_id = uuid.uuid4()
    other_updater_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO users(
                  id, organization_id, email, normalized_email, display_name,
                  password_hash, status, authorization_version, created_at, updated_at
                ) VALUES (
                  :user, :org, 'updater@test', 'updater@test', 'Updater',
                  'x', 'active', 1, now(), now()
                )
                """
            ),
            {"org": organization_id, "user": updater_id},
        )
        connection.execute(
            text(
                """
                UPDATE retention_policies
                SET version = 2, updated_by = :user
                WHERE organization_id = :org
                """
            ),
            {"org": organization_id, "user": updater_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES (:org, 'other-org', 'Other org', 'active', now(), now())
                """
            ),
            {"org": other_organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO users(
                  id, organization_id, email, normalized_email, display_name,
                  password_hash, status, authorization_version, created_at, updated_at
                ) VALUES (
                  :user, :org, 'other-updater@test', 'other-updater@test', 'Other updater',
                  'x', 'active', 1, now(), now()
                )
                """
            ),
            {"org": other_organization_id, "user": other_updater_id},
        )
    with pytest.raises(Exception, match="fk_retention_policies_updated_by"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE retention_policies
                    SET version = 3, updated_by = :user
                    WHERE organization_id = :org
                    """
                ),
                {"org": organization_id, "user": other_updater_id},
            )
    engine.dispose()


def test_0016_rejects_cross_tenant_organization_policy_pointer() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "head")
    first_organization_id = uuid.uuid4()
    second_organization_id = uuid.uuid4()

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES
                  (:first, 'first-tenant', 'First tenant', 'active', now(), now()),
                  (:second, 'second-tenant', 'Second tenant', 'active', now(), now())
                """
            ),
            {"first": first_organization_id, "second": second_organization_id},
        )
        second_policy_id = connection.scalar(
            text(
                """
                SELECT retention_policy_id
                FROM organizations
                WHERE id = :organization_id
                """
            ),
            {"organization_id": second_organization_id},
        )

    with pytest.raises(Exception, match="fk_organizations_retention_policy"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE organizations
                    SET retention_policy_id = :policy_id
                    WHERE id = :organization_id
                    """
                ),
                {
                    "organization_id": first_organization_id,
                    "policy_id": second_policy_id,
                },
            )
    engine.dispose()


def test_0016_round_trips_every_normalized_resource_type() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "0015_reports_exports")
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    resources = [
        ("candidate.created", "candidate", "candidate_id"),
        ("application.created", "application", "application_id"),
        ("job.created", "job", "job_id"),
        ("resume.previewed", "resume", "resume_id"),
        ("screening.run_created", "screening_run", "run_id"),
        ("screening.item_accepted", "screening_item", "item_id"),
        ("screening.item_rejected", "screening_run", "run_id"),
        ("interview.created", "interview", "interview_id"),
        ("talent_pool.created", "talent_pool", "pool_id"),
        ("talent_pool.member_added", "talent_pool_membership", "membership_id"),
        ("report_export.created", "report_export", "export_id"),
        ("llm.config_updated", "llm_config", "config_id"),
    ]
    rows = [
        {
            "audit_id": uuid.uuid4(),
            "resource_id": uuid.uuid4(),
            "event_type": event,
            "resource_type": resource_type,
            "legacy_key": legacy_key,
        }
        for event, resource_type, legacy_key in resources
    ]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES (:org, 'resource-roundtrip', 'Resource roundtrip', 'active', now(), now())
                """
            ),
            {"org": organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO users(
                  id, organization_id, email, normalized_email, display_name,
                  password_hash, status, authorization_version, created_at, updated_at
                ) VALUES (
                  :user, :org, 'roundtrip@test', 'roundtrip@test', 'Roundtrip',
                  'x', 'active', 1, now(), now()
                )
                """
            ),
            {"org": organization_id, "user": user_id},
        )
        for row in rows:
            connection.execute(
                text(
                    """
                    INSERT INTO audit_logs(
                      id, organization_id, actor_user_id, event_type, outcome,
                      trace_id, metadata_json, created_at
                    ) VALUES (
                      :audit_id, :org, :user, :event_type, 'success', 'trace-roundtrip',
                      jsonb_build_object(
                        CAST(:legacy_key AS text), CAST(:resource_id AS text)
                      ), now()
                    )
                    """
                ),
                {**row, "org": organization_id, "user": user_id},
            )

    _alembic(url, "upgrade", "0016_governance_audit_retention")
    with engine.connect() as connection:
        normalized = connection.execute(
            text(
                """
                SELECT id, resource_type, resource_id, metadata_json
                FROM audit_logs WHERE organization_id = :org
                """
            ),
            {"org": organization_id},
        ).all()
    by_id = {row.id: row for row in normalized}
    for expected in rows:
        actual = by_id[expected["audit_id"]]
        assert (actual.resource_type, actual.resource_id) == (
            expected["resource_type"],
            expected["resource_id"],
        )
        assert expected["legacy_key"] not in actual.metadata_json

    _alembic(url, "downgrade", "0015_reports_exports")
    with engine.connect() as connection:
        legacy_rows = connection.execute(
            text("SELECT id, metadata_json FROM audit_logs WHERE organization_id = :org"),
            {"org": organization_id},
        ).all()
    by_id = {row.id: row.metadata_json for row in legacy_rows}
    for expected in rows:
        assert by_id[expected["audit_id"]] == {
            expected["legacy_key"]: str(expected["resource_id"])
        }
    engine.dispose()


def test_bootstrap_creates_matching_default_policy_at_head() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "head")

    app = create_app(
        settings=Settings(
            environment="test",
            database_url=url,
            cors_origins=["https://hr.example.test"],
        ),
        database_probe=_Probe(),
        storage_probe=_Probe(),
    )
    user_id = bootstrap_system_admin(
        app.state.identity_store,
        "bootstrap-policy",
        "Bootstrap policy",
        "bootstrap-policy@test",
        "Bootstrap policy admin",
        "correct horse battery staple",
    )
    with engine.connect() as connection:
        policy = connection.execute(
            text(
                """
                SELECT p.version, p.updated_by, o.retention_policy_id = p.id
                FROM users u
                JOIN organizations o ON o.id = u.organization_id
                JOIN retention_policies p ON p.organization_id = o.id
                WHERE u.id = :user
                """
            ),
            {"user": user_id},
        ).one()
    assert policy == (1, None, True)
    engine.dispose()


def test_post_0016_audit_category_repair_preserves_append_only_trigger() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    _alembic(url, "downgrade", "base")
    _alembic(url, "upgrade", "0016_governance_audit_retention")
    organization_id = uuid.uuid4()
    recruiting_id = uuid.uuid4()
    system_id = uuid.uuid4()
    llm_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO organizations(id, slug, name, status, created_at, updated_at)
                VALUES (:org, 'category-repair', 'Category repair', 'active', now(), now())
                """
            ),
            {"org": organization_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO audit_logs(
                  id, organization_id, category, event_type, outcome, metadata_json, created_at
                ) VALUES
                  (:recruiting, :org, 'system', 'candidate.created', 'success', '{}', now()),
                  (:llm, :org, 'recruiting', 'llm.config_updated', 'success', '{}', now()),
                  (:system, :org, 'system', 'authentication.login', 'success', '{}', now())
                """
            ),
            {
                "org": organization_id,
                "recruiting": recruiting_id,
                "llm": llm_id,
                "system": system_id,
            },
        )

    _alembic(url, "upgrade", "head")
    with engine.connect() as connection:
        categories = dict(
            connection.execute(
                text("SELECT id, category FROM audit_logs WHERE organization_id = :org"),
                {"org": organization_id},
            ).all()
        )
    assert categories == {
        recruiting_id: "recruiting",
        llm_id: "system",
        system_id: "system",
    }
    with pytest.raises(Exception, match="append-only"):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_logs SET outcome = 'failure' WHERE id = :id"),
                {"id": recruiting_id},
            )
    _alembic(url, "downgrade", "0016_governance_audit_retention")
    _alembic(url, "upgrade", "head")
    engine.dispose()
