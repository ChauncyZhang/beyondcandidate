from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from server.app.identity.models import AuditLog, Base, PasswordInvitation, UserSession


def _enable_sqlite_foreign_keys(dbapi_connection, _) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class IdentityStore:
    SessionModel = UserSession
    AuditModel = AuditLog
    PasswordInvitationModel = PasswordInvitation

    def __init__(self, database_url: str) -> None:
        sync_url = database_url.replace("postgresql+asyncpg", "postgresql+psycopg").replace("sqlite+aiosqlite", "sqlite")
        self.engine = create_engine(sync_url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        self.sync_session: sessionmaker[Session] = sessionmaker(self.engine, expire_on_commit=False)

    def create_schema(self) -> None:
        if self.engine.dialect.name != "sqlite":
            raise RuntimeError(
                "ORM schema creation is SQLite-only; use Alembic for non-SQLite databases"
            )
        Base.metadata.create_all(self.engine)
