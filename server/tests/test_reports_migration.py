import os
import subprocess

import pytest
from sqlalchemy import create_engine, inspect


pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured"
)


def test_reports_export_migration_upgrades_and_downgrades() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=env,
    )
    assert {"report_exports", "report_export_download_tickets"} <= set(inspect(engine).get_table_names())
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "0014_talent_pools"],
        check=True,
        env=env,
    )
    assert not ({"report_exports", "report_export_download_tickets"} & set(inspect(engine).get_table_names()))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env=env,
    )
    engine.dispose()
