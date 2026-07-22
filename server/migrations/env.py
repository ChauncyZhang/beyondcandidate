import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from server.app.core.settings import Settings
from server.app.identity.models import Base
from server.app.queue import models as queue_models  # noqa: F401
from server.app.screening import models as screening_models  # noqa: F401
from server.app.llm import models as llm_models  # noqa: F401
from server.app.interviews import models as interview_models  # noqa: F401
from server.app.integrations.feishu import models as feishu_models  # noqa: F401
from server.app.recruiting import models as recruiting_models  # noqa: F401
from server.app.talent import models as talent_models  # noqa: F401
from server.app.notifications import models as notification_models  # noqa: F401


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", Settings.from_environment().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
