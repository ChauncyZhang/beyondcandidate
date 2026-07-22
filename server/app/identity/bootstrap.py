import os

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from server.app.identity.models import AuditLog, Organization, User, UserRole, UserStatus
from server.app.identity.security import PasswordService
from server.app.identity.store import IdentityStore


def bootstrap_system_admin(store: IdentityStore, organization_slug: str, organization_name: str, email: str, display_name: str, password: str):
    normalized = email.strip().casefold()
    with store.sync_session() as db:
        organization = db.scalar(select(Organization).where(Organization.slug == organization_slug))
        if not organization:
            organization = Organization(slug=organization_slug, name=organization_name, status="active")
            db.add(organization)
            db.flush()
        user = db.scalar(select(User).options(selectinload(User.roles)).where(User.organization_id == organization.id, User.normalized_email == normalized))
        encoded = PasswordService().hash(password)
        if user:
            event_type = "bootstrap.admin_rotated"
            user.password_hash = encoded
            user.display_name = display_name
            user.status = UserStatus.ACTIVE
            user.authorization_version += 1
        else:
            event_type = "bootstrap.admin_created"
            user = User(organization_id=organization.id, email=email, normalized_email=normalized, display_name=display_name, password_hash=encoded, status=UserStatus.ACTIVE)
            user.roles.append(UserRole(role="system_admin"))
            db.add(user)
        if "system_admin" not in {role.role for role in user.roles}:
            user.roles.append(UserRole(role="system_admin"))
        db.flush()
        db.add(AuditLog(organization_id=organization.id, actor_user_id=user.id, event_type=event_type, outcome="success", metadata_json={}))
        db.commit()
        return user.id


def main() -> None:
    required = ["BOOTSTRAP_ORGANIZATION_SLUG", "BOOTSTRAP_ORGANIZATION_NAME", "BOOTSTRAP_ADMIN_EMAIL", "BOOTSTRAP_ADMIN_DISPLAY_NAME", "BOOTSTRAP_ADMIN_PASSWORD", "DATABASE_URL"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required bootstrap environment variables: " + ", ".join(missing))
    store = IdentityStore(os.environ["DATABASE_URL"])
    bootstrap_system_admin(store, *(os.environ[name] for name in required[:-1]))
    print("System administrator created or rotated.")


if __name__ == "__main__":
    main()
