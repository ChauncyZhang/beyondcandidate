import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect,text
from sqlalchemy.exc import IntegrityError

from server.tests.test_interview_persistence_postgres import _seed_application


TABLES = {"organizations", "departments", "workflow_templates", "users", "user_roles", "user_sessions", "jobs", "job_collaborators", "audit_logs", "candidates", "candidate_contacts", "file_objects", "resumes", "resume_profiles", "job_jd_versions", "screening_rule_versions", "applications", "application_stage_events", "application_review_tasks", "notification_reads", "candidate_notes", "candidate_events", "download_tickets", "idempotency_records", "background_jobs", "job_attempts", "outbox_events", "queue_claim_cursors", "screening_runs", "screening_items", "screening_results", "candidate_duplicate_hints", "llm_provider_configs", "ocr_provider_configs", "prompt_versions", "llm_invocations", "llm_screening_evaluations", "interviews", "interview_participants", "interview_events", "interview_feedbacks", "interview_feedback_revisions", "talent_pools", "talent_pool_grants", "talent_pool_memberships"}


def test_notification_read_migration_is_latest_revision() -> None:
    script_directory = ScriptDirectory.from_config(Config("server/alembic.ini"))

    assert script_directory.get_current_head() == "0026_notification_reads"


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_migration_upgrades_and_downgrades_empty_baseline() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    sync_url = url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_url)
    assert TABLES <= set(inspect(engine).get_table_names())
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)
    assert not (TABLES & set(inspect(engine).get_table_names()))


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0021_persists_deferred_stage_and_one_open_review_task() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_application(connection)
        connection.execute(text("UPDATE applications SET stage = 'deferred' WHERE id = :application"), identifiers)
        connection.execute(
            text(
                """
                INSERT INTO application_review_tasks(
                  id, organization_id, application_id, assignee_id, status, ai_status,
                  created_at
                ) VALUES (
                  :task, :organization, :application, :owner, 'open', 'succeeded', now()
                )
                """
            ),
            {**identifiers, "task": uuid.uuid4()},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO application_review_tasks(
                      id, organization_id, application_id, assignee_id, status, ai_status,
                      created_at
                    ) VALUES (
                      :task, :organization, :application, :owner, 'open', 'failed', now()
                    )
                    """
                ),
                {**identifiers, "task": uuid.uuid4()},
            )
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0021_round_trip_preserves_historical_evaluation_without_rewrite() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0020_llm_provider_catalog"], check=True, env=env)
    ids = {name: uuid.uuid4() for name in ("org", "user", "job", "jd", "rule", "file", "run", "item", "result", "config", "prompt", "queue", "invocation", "evaluation")}
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO organizations(id,slug,name,status,created_at,updated_at) VALUES(:org,'llm-0021-history','LLM history','active',now(),now())"), ids)
        connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,created_at,updated_at) VALUES(:user,:org,'llm-0021-history@test','llm-0021-history@test','History','x','active',1,now(),now())"), ids)
        connection.execute(text("INSERT INTO jobs(id,organization_id,title,status,owner_id,headcount,priority,version,created_at,updated_at) VALUES(:job,:org,'Job','draft',:user,1,'normal',1,now(),now())"), ids)
        for table, key in (("job_jd_versions", "jd"), ("screening_rule_versions", "rule")):
            connection.execute(text(f"INSERT INTO {table}(id,organization_id,job_id,version_number,content,created_by,created_at) VALUES(:{key},:org,:job,1,'{{}}',:user,now())"), ids)
        connection.execute(text("INSERT INTO file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by,created_at) VALUES(:file,:org,'llm-history/x','x.txt','text/plain',1,repeat('0',64),:user,now())"), ids)
        connection.execute(text("INSERT INTO screening_runs(id,organization_id,job_id,jd_version_id,rule_version_id,source,status,total_count,processed_count,succeeded_count,failed_count,created_by,version,created_at,updated_at) VALUES(:run,:org,:job,:jd,:rule,'upload','completed',1,1,1,0,:user,1,now(),now())"), ids)
        connection.execute(text("INSERT INTO screening_items(id,organization_id,run_id,file_object_id,status,attempts,llm_status,llm_attempts,created_at,updated_at) VALUES(:item,:org,:run,:file,'scored',1,'succeeded',1,now(),now())"), ids)
        connection.execute(text("INSERT INTO screening_results(id,organization_id,item_id,rule_engine_version,rule_score,recommendation,required_hits,required_missing,bonus_hits,estimated_years,risks,questions,created_at,updated_at) VALUES(:result,:org,:item,'rule-v1',88,'优先沟通','[]','[]','[]',3,'[]','[]',now(),now())"), ids)
        connection.execute(text("INSERT INTO llm_provider_configs(id,organization_id,provider_id,model,encrypted_api_key,enabled,allowed_job_ids,version,created_by,updated_by,created_at,updated_at) VALUES(:config,:org,'approved','model',decode('00','hex'),false,'[]',1,:user,:user,now(),now())"), ids)
        connection.execute(text("INSERT INTO prompt_versions(id,organization_id,name,version_number,content,content_hash,created_by,created_at) VALUES(:prompt,:org,'screen',1,'{\"version\": 1}',repeat('0',64),:user,now())"), ids)
        connection.execute(text("INSERT INTO background_jobs(id,organization_id,type,payload,status,priority,attempts,max_attempts,run_after,created_at,updated_at) VALUES(:queue,:org,'screening.llm_score_item','{}','succeeded',0,1,3,now(),now(),now())"), ids)
        connection.execute(text("INSERT INTO llm_invocations(id,organization_id,config_id,prompt_version_id,screening_result_id,queue_job_id,attempt_no,config_version,input_sha256,provider_id,model,request_field_manifest,status,usage,created_at) VALUES(:invocation,:org,:config,:prompt,:result,:queue,1,1,repeat('a',64),'approved','model','[]','succeeded','{}',now())"), ids)
        connection.execute(text("INSERT INTO llm_screening_evaluations(id,organization_id,screening_result_id,invocation_id,prompt_version_id,score,recommendation,summary,strengths,gaps,risks,interview_questions,created_at) VALUES(:evaluation,:org,:result,:invocation,:prompt,88,'优先沟通','Historical evaluation','[\"strength\"]','[\"gap\"]','[\"risk\"]','[\"question\"]',now())"), ids)

    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0021_llm_only_auto_routing"], check=True, env=env)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT score,recommendation,summary,strengths,gaps,risks,interview_questions,dimensions FROM llm_screening_evaluations WHERE id=:evaluation"), ids).one() == (88, "优先沟通", "Historical evaluation", ["strength"], ["gap"], ["risk"], ["question"], [])
        assert "ck_applications_stage" in {item["name"] for item in inspect(engine).get_check_constraints("applications")}

    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "0020_llm_provider_catalog"], check=True, env=env)
    with engine.connect() as connection:
        assert "applications_stage_check" in {item["name"] for item in inspect(engine).get_check_constraints("applications")}
        assert connection.execute(text("SELECT score,recommendation,summary,strengths,gaps,risks,interview_questions FROM llm_screening_evaluations WHERE id=:evaluation"), ids).one() == (88, "优先沟通", "Historical evaluation", ["strength"], ["gap"], ["risk"], ["question"])

    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0021_llm_only_auto_routing"], check=True, env=env)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT dimensions FROM llm_screening_evaluations WHERE id=:evaluation"), ids) == []
        assert "ck_applications_stage" in {item["name"] for item in inspect(engine).get_check_constraints("applications")}
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0010_backfills_and_downgrades_data_bearing_0009() -> None:
    url=os.environ["POSTGRES_SMOKE_URL"]; env={**os.environ,"DATABASE_URL":url}; sync_url=url.replace("+asyncpg","+psycopg"); engine=create_engine(sync_url)
    subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","0009_llm_gateway_foundation"],check=True,env=env)
    ids={name:uuid.uuid4() for name in ("org","user","job","jd","rule","file","run","item")}
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO organizations(id,slug,name,status,created_at,updated_at) VALUES(:org,'migration-data','Migration','active',now(),now())"),ids)
        connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,created_at,updated_at) VALUES(:user,:org,'migration@test','migration@test','Migration','x','active',1,now(),now())"),ids)
        connection.execute(text("INSERT INTO jobs(id,organization_id,title,status,owner_id,headcount,priority,version,created_at,updated_at) VALUES(:job,:org,'Job','draft',:user,1,'normal',1,now(),now())"),ids)
        connection.execute(text("INSERT INTO job_jd_versions(id,organization_id,job_id,version_number,content,created_by,created_at) VALUES(:jd,:org,:job,1,'{}',:user,now())"),ids); connection.execute(text("INSERT INTO screening_rule_versions(id,organization_id,job_id,version_number,content,created_by,created_at) VALUES(:rule,:org,:job,1,'{}',:user,now())"),ids)
        connection.execute(text("INSERT INTO file_objects(id,organization_id,storage_key,original_filename,mime_type,size_bytes,sha256,uploaded_by,created_at) VALUES(:file,:org,'migration/x','x.txt','text/plain',1,repeat('0',64),:user,now())"),ids)
        connection.execute(text("INSERT INTO screening_runs(id,organization_id,job_id,jd_version_id,rule_version_id,source,status,total_count,processed_count,succeeded_count,failed_count,created_by,version,created_at,updated_at) VALUES(:run,:org,:job,:jd,:rule,'upload','rule_scoring',1,0,0,0,:user,1,now(),now())"),ids)
        connection.execute(text("INSERT INTO screening_items(id,organization_id,run_id,file_object_id,status,attempts,created_at,updated_at) VALUES(:item,:org,:run,:file,'parsed',1,now(),now())"),ids)
    subprocess.run(["python","-m","alembic","-c","server/alembic.ini","upgrade","0010_llm_screening_evaluations"],check=True,env=env)
    with engine.connect() as connection: assert connection.execute(text("SELECT llm_status,llm_attempts FROM screening_items WHERE id=:item"),ids).one()==("not_requested",0)
    subprocess.run(["python","-m","alembic","-c","server/alembic.ini","downgrade","0009_llm_gateway_foundation"],check=True,env=env)
    columns={column["name"] for column in inspect(engine).get_columns("screening_items")}; assert "llm_status" not in columns
    with engine.connect() as connection: assert connection.scalar(text("SELECT count(*) FROM screening_items WHERE id=:item"),ids)==1
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0013_backfills_stable_calendar_contacts_for_existing_interviews() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0012_interviews_feedback"], check=True, env=env)
    with engine.begin() as connection:
        identifiers = _seed_application(connection)
        interview_id = uuid.uuid4()
        starts_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        connection.execute(
            text(
                """
                INSERT INTO interviews(
                  id, organization_id, application_id, round_name, method, timezone,
                  starts_at, ends_at, status, notification_status, invitation_status,
                  owner_id, created_by, version, calendar_sequence, created_at, updated_at
                ) VALUES (
                  :id, :organization, :application, 'First round', 'video', 'Asia/Shanghai',
                  :starts_at, :ends_at, 'scheduled', 'not_sent', 'artifact_ready',
                  :owner, :owner, 1, 0, now(), now()
                )
                """
            ),
            {**identifiers, "id": interview_id, "starts_at": starts_at, "ends_at": starts_at + timedelta(minutes=45)},
        )
        connection.execute(
            text(
                """
                INSERT INTO interview_participants(
                  id, organization_id, interview_id, user_id, role, required_feedback,
                  attendance_status, task_status, created_at, updated_at
                ) VALUES (
                  :id, :organization, :interview, :interviewer, 'interviewer', true,
                  'invited', 'ready', now(), now()
                )
                """
            ),
            {**identifiers, "id": uuid.uuid4(), "interview": interview_id},
        )

    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    with engine.connect() as connection:
        snapshot = connection.execute(
            text(
                """
                SELECT calendar_organizer ->> 'email', calendar_attendees -> 0 ->> 'email'
                FROM interviews WHERE id = :id
                """
            ),
            {"id": interview_id},
        ).one()
        assert snapshot == ("owner@test", "interviewer@test")
    engine.dispose()
