import pytest
from sqlalchemy.exc import IntegrityError

from server.app.governance.models import RetentionPolicy
from server.app.identity.bootstrap import bootstrap_system_admin
from server.app.identity.models import AuditLog, Organization, User
from server.app.identity.security import PasswordService
from server.app.identity.store import IdentityStore
from server.tests.test_identity import identity_app


def test_bootstrap_is_explicit_and_rotates_selected_admin(identity_app) -> None:
    app, _, _ = identity_app
    first = bootstrap_system_admin(app.state.identity_store, "acme", "Acme", "admin@example.test", "Admin", "first-secret")
    second = bootstrap_system_admin(app.state.identity_store, "acme", "Acme", "admin@example.test", "Admin", "second-secret")
    assert first == second
    with app.state.identity_store.sync_session() as session:
        user = session.query(User).one()
        assert PasswordService().verify(user.password_hash, "second-secret")
        assert user.authorization_version == 2
        assert [role.role for role in user.roles] == ["system_admin"]
        assert [audit.event_type for audit in session.query(AuditLog).order_by(AuditLog.created_at)] == [
            "bootstrap.admin_created",
            "bootstrap.admin_rotated",
        ]
        organization = session.query(Organization).one()
        policy = session.query(RetentionPolicy).one()
        assert organization.retention_policy_id == policy.id
        assert policy.organization_id == organization.id
        assert policy.version == 1
        assert policy.updated_by is None


def test_orm_schema_creation_rejects_non_sqlite_dialects() -> None:
    store = IdentityStore("sqlite://")
    store.engine.dialect.name = "postgresql"
    with pytest.raises(RuntimeError, match="Alembic"):
        store.create_schema()


def test_sqlite_rejects_cross_tenant_retention_policy_pointer(identity_app) -> None:
    app, _, _ = identity_app
    with app.state.identity_store.sync_session() as session:
        first = Organization(slug="first", name="First")
        second = Organization(slug="second", name="Second")
        session.add_all([first, second])
        session.commit()

        first.retention_policy_id = second.retention_policy_id
        with pytest.raises(IntegrityError):
            session.commit()
