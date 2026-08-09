import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect,text
from sqlalchemy.exc import DBAPIError, IntegrityError

from server.tests.test_interview_persistence_postgres import _seed_application


TABLES = {"organizations", "departments", "workflow_templates", "users", "user_roles", "user_sessions", "user_recruiting_department_scopes", "jobs", "job_collaborators", "audit_logs", "candidates", "candidate_contacts", "file_objects", "resumes", "resume_profiles", "job_jd_versions", "screening_rule_versions", "applications", "application_stage_events", "application_review_tasks", "notification_reads", "user_notifications", "candidate_notes", "candidate_events", "download_tickets", "idempotency_records", "background_jobs", "job_attempts", "outbox_events", "queue_claim_cursors", "screening_runs", "screening_items", "screening_results", "candidate_duplicate_hints", "llm_provider_configs", "ocr_provider_configs", "email_provider_configs", "email_templates", "email_deliveries", "prompt_versions", "llm_invocations", "llm_screening_evaluations", "interviews", "interview_participants", "interview_events", "interview_feedbacks", "interview_feedback_revisions", "talent_pools", "talent_pool_grants", "talent_pool_memberships", "offer_templates", "organization_special_offer_approvers", "offers", "offer_versions", "offer_approvals", "offer_responses", "offer_events"}


def test_latest_migration_revision_is_current() -> None:
    script_directory = ScriptDirectory.from_config(Config("server/alembic.ini"))

    assert script_directory.get_current_head() == "0036_email_sender_identity"


def test_email_delivery_schema_has_versioned_provider_and_dedupe_guards() -> None:
    from server.app.communications.models import EmailDelivery, EmailProviderConfig

    provider_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in EmailProviderConfig.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("organization_id", "version") in provider_unique_columns
    assert {"sender_address", "sender_name", "default_reply_to_email", "default_reply_to_name"} <= set(EmailProviderConfig.__table__.columns.keys())
    provider_check_names = {
        constraint.name
        for constraint in EmailProviderConfig.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert "ck_email_provider_configs_sender_pair" in provider_check_names
    assert {
        "request_fingerprint",
        "version",
        "attachment_filename",
        "attachment_content_type",
        "attachment_ciphertext",
    } <= set(EmailDelivery.__table__.columns.keys())
    check_names = {
        constraint.name
        for constraint in EmailDelivery.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    assert {
        "ck_email_deliveries_version",
        "ck_email_deliveries_template_version_pair",
        "ck_email_deliveries_attachment_triplet",
    } <= check_names


def test_0036_sender_identity_migration_preserves_legacy_provider_fallback() -> None:
    migration = Path("server/migrations/versions/0036_email_sender_identity.py").read_text(encoding="utf-8")

    assert 'sa.Column("sender_address", sa.String(320))' in migration
    assert 'sa.Column("sender_name", sa.String(200))' in migration
    assert "server_default" not in migration
    assert "ck_email_provider_configs_sender_pair" in migration


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0030_backfills_existing_candidate_contacts_as_legacy_unconfirmed() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0029_separate_job_owners"], check=True, env=env)
    ids = {name: uuid.uuid4() for name in ("organization", "user", "candidate", "contact")}
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO organizations(id,slug,name,status,created_at,updated_at) VALUES(:organization,'contact-0030','Contact migration','active',now(),now())"), ids)
        connection.execute(text("INSERT INTO users(id,organization_id,email,normalized_email,display_name,password_hash,status,authorization_version,created_at,updated_at) VALUES(:user,:organization,'contact-0030@example.test','contact-0030@example.test','Contact migration','x','active',1,now(),now())"), ids)
        connection.execute(text("INSERT INTO candidates(id,organization_id,display_name,version,created_at,updated_at) VALUES(:candidate,:organization,'Candidate',1,now(),now())"), ids)
        connection.execute(text("INSERT INTO candidate_contacts(id,organization_id,candidate_id,kind,ciphertext,lookup_hash,masked_value,created_at) VALUES(:contact,:organization,:candidate,'email',decode('00','hex'),repeat('0',64),'c***@example.test',now())"), ids)

    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0030_candidate_contact_confirmation"], check=True, env=env)
    with engine.connect() as connection:
        version_column = next(column for column in inspect(engine).get_columns("alembic_version") if column["name"] == "version_num")
        assert version_column["type"].length >= len("0030_candidate_contact_confirmation")
        assert connection.execute(text("SELECT source,confirmation_status,confirmed_by,confirmed_at,version FROM candidate_contacts WHERE id=:contact"), ids).one() == ("legacy", "unconfirmed", None, None, 1)
        constraints = {item["name"] for item in inspect(engine).get_check_constraints("candidate_contacts")}
        assert {"ck_candidate_contacts_source", "ck_candidate_contacts_confirmation_status"} <= constraints
        for source in ("legacy", "manual", "native", "ocr"):
            connection.execute(text("UPDATE candidate_contacts SET source=:source WHERE id=:contact"), {**ids, "source": source})
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE candidate_contacts SET source='extracted' WHERE id=:contact"), ids)
    engine.dispose()


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


OFFER_TABLES = {
    "offer_templates", "organization_special_offer_approvers", "offers", "offer_versions",
    "offer_approvals", "offer_responses", "offer_events",
}


def _seed_offer_rows(connection):
    identifiers = _seed_application(connection)
    identifiers.update({name: uuid.uuid4() for name in ("template", "offer", "version", "approval")})
    connection.execute(text("UPDATE applications SET stage='passed' WHERE id=:application"), identifiers)
    connection.execute(text("INSERT INTO user_roles(id,user_id,role,created_at,updated_at) VALUES(:approval,:owner,'hiring_manager',now(),now())"), identifiers)
    connection.execute(text("INSERT INTO offer_templates(id,organization_id,name,content,status,version,created_at,updated_at) VALUES(:template,:organization,'Standard','{}','active',1,now(),now())"), identifiers)
    connection.execute(text("UPDATE jobs SET offer_approver_id=:owner,offer_template_id=:template WHERE id=:job"), identifiers)
    connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    connection.execute(text("""
        INSERT INTO offers(id,organization_id,application_id,job_id,template_id,current_version_id,status,is_special,special_reason,candidate_response_deadline,version,created_at,updated_at)
        VALUES(:offer,:organization,:application,:job,:template,:version,'draft',false,null,now() + interval '7 days',1,now(),now())
    """), identifiers)
    connection.execute(text("""
        INSERT INTO offer_versions(id,organization_id,offer_id,version_number,content,template_id,candidate_response_deadline,is_special,special_reason,created_by,submitted_at,created_at,updated_at)
        VALUES(:version,:organization,:offer,1,'{\"body\":\"offer\"}',:template,now() + interval '7 days',false,null,:owner,null,now(),now())
    """), identifiers)
    return identifiers


PDF_RECEIPT_CASE_VERSIONS = (
    "partial_pdf_version",
    "invalid_digest_pdf_version",
    "invalid_size_pdf_version",
    "content_mutation_pdf_version",
    "template_mutation_pdf_version",
    "deadline_mutation_pdf_version",
)


def _seed_offer_pdf_receipt_cases(connection):
    identifiers = _seed_offer_rows(connection)
    identifiers.update({name: uuid.uuid4() for name in PDF_RECEIPT_CASE_VERSIONS})
    connection.execute(text("UPDATE offer_versions SET submitted_at=now() WHERE id=:version"), identifiers)
    for version_number, name in enumerate(PDF_RECEIPT_CASE_VERSIONS, start=2):
        connection.execute(
            text(
                """
                INSERT INTO offer_versions(
                  id,organization_id,offer_id,version_number,content,template_id,
                  candidate_response_deadline,is_special,special_reason,created_by,
                  submitted_at,created_at,updated_at
                ) VALUES(
                  :case_version,:organization,:offer,:version_number,'{"body":"offer"}',:template,
                  now() + interval '7 days',false,null,:owner,now(),now(),now()
                )
                """
            ),
            {**identifiers, "case_version": identifiers[name], "version_number": version_number},
        )
    return identifiers


def _assert_postgres_statement_rejected(engine, statement, identifiers):
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DBAPIError):
            connection.execute(text(statement), identifiers)
        transaction.rollback()


def _assert_offer_pdf_receipt_guards(engine, identifiers):
    receipt = {
        **identifiers,
        "pdf_key": f"offers/{identifiers['organization']}/offers/{identifiers['offer']}/versions/{identifiers['version']}.pdf",
        "pdf_digest": "a" * 64,
        "pdf_size": 1024,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE offer_versions
                SET pdf_object_key=:pdf_key,pdf_sha256=:pdf_digest,
                    pdf_size_bytes=:pdf_size,pdf_rendered_at=now(),updated_at=now()
                WHERE id=:version
                """
            ),
            receipt,
        )
        assert connection.execute(
            text("SELECT pdf_object_key,pdf_sha256,pdf_size_bytes,pdf_rendered_at IS NOT NULL FROM offer_versions WHERE id=:version"),
            receipt,
        ).one() == (receipt["pdf_key"], receipt["pdf_digest"], receipt["pdf_size"], True)

    _assert_postgres_statement_rejected(
        engine,
        "UPDATE offer_versions SET pdf_sha256=repeat('b',64),updated_at=now() WHERE id=:version",
        receipt,
    )
    _assert_postgres_statement_rejected(
        engine,
        "UPDATE offer_versions SET pdf_object_key='offers/partial.pdf',updated_at=now() WHERE id=:partial_pdf_version",
        receipt,
    )
    _assert_postgres_statement_rejected(
        engine,
        """
        UPDATE offer_versions SET pdf_object_key='offers/invalid-digest.pdf',pdf_sha256=repeat('g',64),
          pdf_size_bytes=1,pdf_rendered_at=now(),updated_at=now()
        WHERE id=:invalid_digest_pdf_version
        """,
        receipt,
    )
    _assert_postgres_statement_rejected(
        engine,
        """
        UPDATE offer_versions SET pdf_object_key='offers/invalid-size.pdf',pdf_sha256=repeat('c',64),
          pdf_size_bytes=0,pdf_rendered_at=now(),updated_at=now()
        WHERE id=:invalid_size_pdf_version
        """,
        receipt,
    )
    business_mutations = (
        ("content_mutation_pdf_version", "content=json_build_object('changed',true)"),
        ("template_mutation_pdf_version", "template_id=null"),
        ("deadline_mutation_pdf_version", "candidate_response_deadline=candidate_response_deadline + interval '1 day'"),
    )
    for index, (version_parameter, mutation) in enumerate(business_mutations, start=1):
        _assert_postgres_statement_rejected(
            engine,
            f"""
            UPDATE offer_versions SET pdf_object_key='offers/mutation-{index}.pdf',pdf_sha256=repeat('d',64),
              pdf_size_bytes=1,pdf_rendered_at=now(),{mutation},updated_at=now()
            WHERE id=:{version_parameter}
            """,
            receipt,
        )


def test_0034_offer_pdf_receipt_trigger_static_contract() -> None:
    migration = Path("server/migrations/versions/0034_offer_version_pdf_receipts.py").read_text(encoding="utf-8")

    for guard in (
        "NEW.content IS NOT DISTINCT FROM OLD.content",
        "NEW.template_id IS NOT DISTINCT FROM OLD.template_id",
        "NEW.candidate_response_deadline IS NOT DISTINCT FROM OLD.candidate_response_deadline",
        "OLD.pdf_object_key IS NULL",
        "NEW.pdf_object_key IS NOT NULL",
        "NEW.pdf_sha256 IS NOT NULL",
        "NEW.pdf_size_bytes IS NOT NULL",
        "NEW.pdf_rendered_at IS NOT NULL",
    ):
        assert guard in migration
    assert "pdf_sha256 !~ '[^0-9a-f]'" in migration
    assert "pdf_size_bytes > 0" in migration
    assert migration.count("CREATE TRIGGER offer_versions_immutable_after_submission") == 2
    assert migration.count("RETURN OLD;") == 2
    assert migration.count("RETURN NEW;") == 3


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0034_offer_pdf_receipt_trigger_round_trip_guards() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0034_offer_version_pdf_receipts"], check=True, env=env)
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_offer_pdf_receipt_cases(connection)

    _assert_offer_pdf_receipt_guards(engine, identifiers)
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "0033_offer_workflow"], check=True, env=env)
    assert not ({"pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at"} & {
        column["name"] for column in inspect(engine).get_columns("offer_versions")
    })
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0034_offer_version_pdf_receipts"], check=True, env=env)
    assert {"pdf_object_key", "pdf_sha256", "pdf_size_bytes", "pdf_rendered_at"} <= {
        column["name"] for column in inspect(engine).get_columns("offer_versions")
    }
    _assert_offer_pdf_receipt_guards(engine, identifiers)
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_0033_offer_round_trip_and_schema_contract() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "base"], check=True, env=env)
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0032_interview_email_attachment"], check=True, env=env)
    assert not (OFFER_TABLES & set(inspect(engine).get_table_names()))
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "0033_offer_workflow"], check=True, env=env)
    assert OFFER_TABLES <= set(inspect(engine).get_table_names())
    assert "submitted_at" in {column["name"] for column in inspect(engine).get_columns("offer_versions")}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "downgrade", "0032_interview_email_attachment"], check=True, env=env)
    assert not (OFFER_TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_offer_postgres_constraints_and_history_triggers() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_offer_rows(connection)

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(Exception):
            connection.execute(text("INSERT INTO organization_special_offer_approvers(id,organization_id,approver_id,position,created_at,updated_at) VALUES(:response,:organization,:interviewer,1,now(),now())"), {**identifiers, "response": uuid.uuid4()})
        transaction.rollback()

    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            duplicate = {**identifiers, "offer2": uuid.uuid4(), "version2": uuid.uuid4()}
            connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
            connection.execute(text("INSERT INTO offers(id,organization_id,application_id,job_id,current_version_id,status,is_special,candidate_response_deadline,version,created_at,updated_at) VALUES(:offer2,:organization,:application,:job,:version2,'draft',false,now(),1,now(),now())"), duplicate)
        transaction.rollback()

    with engine.begin() as connection:
        unsubmitted = {**identifiers, "unsubmitted_version": uuid.uuid4()}
        connection.execute(text("INSERT INTO offer_versions(id,organization_id,offer_id,version_number,content,candidate_response_deadline,is_special,created_by,created_at,updated_at) VALUES(:unsubmitted_version,:organization,:offer,2,'{\"body\":\"draft\"}',now(),false,:owner,now(),now())"), unsubmitted)
        connection.execute(text("DELETE FROM offer_versions WHERE id=:unsubmitted_version"), unsubmitted)
        assert connection.scalar(text("SELECT count(*) FROM offer_versions WHERE id=:unsubmitted_version"), unsubmitted) == 0
        connection.execute(text("UPDATE offer_versions SET submitted_at=now() WHERE id=:version"), identifiers)
        connection.execute(text("INSERT INTO offer_approvals(id,organization_id,offer_id,offer_version_id,round_number,version_number,sequence,assignee_id,status,created_at,updated_at) VALUES(:approval,:organization,:offer,:version,1,1,1,:owner,'pending',now(),now())"), identifiers)

    illegal_statements = [
        "UPDATE offer_versions SET content='{\"changed\":true}' WHERE id=:version",
        "DELETE FROM offer_versions WHERE id=:version",
        "DELETE FROM offer_approvals WHERE id=:approval",
        "UPDATE offer_approvals SET assignee_id=:interviewer WHERE id=:approval",
        "UPDATE offer_approvals SET status='waiting' WHERE id=:approval",
    ]
    for statement in illegal_statements:
        with engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(Exception):
                connection.execute(text(statement), identifiers)
            transaction.rollback()

    with engine.begin() as connection:
        connection.execute(text("UPDATE offer_approvals SET status='approved',decided_at=now() WHERE id=:approval"), identifiers)
        response = {**identifiers, "response": uuid.uuid4()}
        connection.execute(text("INSERT INTO offer_responses(id,organization_id,offer_id,status,responded_at,created_at,updated_at) VALUES(:response,:organization,:offer,'accepted',now(),now(),now())"), response)
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(Exception):
            connection.execute(text("UPDATE offer_responses SET status='declined' WHERE id=:response"), response)
        transaction.rollback()
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO offer_responses(id,organization_id,offer_id,status,responded_at,created_at,updated_at) VALUES(:approval,:organization,:offer,'declined',now(),now(),now())"), identifiers)
        transaction.rollback()
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_offer_postgres_composite_and_exact_version_foreign_keys() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_offer_rows(connection)
        identifiers.update({name: uuid.uuid4() for name in ("other_job", "other_offer", "other_version", "bad_approval")})
        connection.execute(text("INSERT INTO jobs(id,organization_id,title,owner_id,status,headcount,priority,version,created_at,updated_at) VALUES(:other_job,:organization,'Other',:owner,'open',1,'normal',1,now(),now())"), identifiers)
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO offers(id,organization_id,application_id,job_id,current_version_id,status,is_special,candidate_response_deadline,version,created_at,updated_at) VALUES(:other_offer,:organization,:application,:other_job,:other_version,'withdrawn',false,now(),1,now(),now())"), identifiers)
        transaction.rollback()
    with engine.begin() as connection:
        connection.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        connection.execute(text("INSERT INTO offers(id,organization_id,application_id,job_id,current_version_id,status,is_special,candidate_response_deadline,version,created_at,updated_at) VALUES(:other_offer,:organization,:application,:job,:other_version,'withdrawn',false,now(),1,now(),now())"), identifiers)
        connection.execute(text("INSERT INTO offer_versions(id,organization_id,offer_id,version_number,content,candidate_response_deadline,is_special,created_by,created_at,updated_at) VALUES(:other_version,:organization,:other_offer,1,'{}',now(),false,:owner,now(),now())"), identifiers)
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text("UPDATE offers SET current_version_id=:other_version WHERE id=:offer"), identifiers)
            connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        transaction.rollback()
    with engine.connect() as connection:
        transaction = connection.begin()
        with pytest.raises(IntegrityError):
            connection.execute(text("INSERT INTO offer_approvals(id,organization_id,offer_id,offer_version_id,round_number,version_number,sequence,assignee_id,status,created_at,updated_at) VALUES(:bad_approval,:organization,:offer,:other_version,1,1,1,:owner,'pending',now(),now())"), identifiers)
        transaction.rollback()
    engine.dispose()


@pytest.mark.skipif(not os.getenv("POSTGRES_SMOKE_URL"), reason="PostgreSQL smoke URL not configured")
def test_offer_postgres_concurrent_exact_assignee_decision_and_expiry_locking() -> None:
    url = os.environ["POSTGRES_SMOKE_URL"]
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url.replace("+asyncpg", "+psycopg"))
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        identifiers = _seed_offer_rows(connection)
        connection.execute(text("UPDATE offer_versions SET submitted_at=now() WHERE id=:version"), identifiers)
        connection.execute(text("INSERT INTO offer_approvals(id,organization_id,offer_id,offer_version_id,round_number,version_number,sequence,assignee_id,status,created_at,updated_at) VALUES(:approval,:organization,:offer,:version,1,1,1,:owner,'pending',now(),now())"), identifiers)

    def decide_once():
        with engine.begin() as connection:
            return connection.execute(text("""
                UPDATE offer_approvals SET status='approved',decided_at=now(),updated_at=now()
                WHERE id=:approval AND assignee_id=:owner AND status='pending'
                RETURNING id
            """), identifiers).first() is not None

    with ThreadPoolExecutor(max_workers=2) as executor:
        decisions = list(executor.map(lambda _: decide_once(), range(2)))
    assert sorted(decisions) == [False, True]

    with engine.begin() as connection:
        connection.execute(text("UPDATE offers SET status='sent',candidate_response_deadline=now() - interval '1 day' WHERE id=:offer"), identifiers)

    def expire_once():
        with engine.begin() as connection:
            row = connection.execute(text("""
                SELECT id FROM offers
                WHERE id=:offer AND status='sent' AND candidate_response_deadline <= now()
                  AND NOT EXISTS (SELECT 1 FROM offer_responses WHERE offer_responses.organization_id=offers.organization_id AND offer_responses.offer_id=offers.id)
                FOR UPDATE SKIP LOCKED
            """), identifiers).first()
            if row is None:
                return False
            connection.execute(text("UPDATE offers SET status='expired',updated_at=now() WHERE id=:offer"), identifiers)
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        expiries = list(executor.map(lambda _: expire_once(), range(2)))
    assert sorted(expiries) == [False, True]
    engine.dispose()
