from __future__ import annotations

import hashlib
import os
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, ProgrammingError


pytestmark = pytest.mark.skipif(
    not all(
        os.getenv(name)
        for name in (
            "POSTGRES_SMOKE_URL",
            "POSTGRES_APP_URL",
            "POSTGRES_GOVERNANCE_URL",
        )
    ),
    reason="separated PostgreSQL smoke URLs not configured",
)


@pytest.fixture(scope="module")
def databases():
    owner_url = os.environ["POSTGRES_SMOKE_URL"]
    owner = create_engine(owner_url.replace("+asyncpg", "+psycopg"))
    with owner.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    subprocess.run(
        ["python", "-m", "alembic", "-c", "server/alembic.ini", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": owner_url},
    )
    subprocess.run(
        ["sh", "deploy/postgres/provision-app-role.sh"],
        check=True,
        env={
            **os.environ,
            "PGHOST": os.environ.get("POSTGRES_SMOKE_HOST", "host.docker.internal"),
            "PGPORT": os.environ.get("POSTGRES_SMOKE_PORT", "55433"),
            "POSTGRES_DB": "ux09",
            "POSTGRES_USER": "ux09_owner",
            "POSTGRES_PASSWORD": "owner-b2b1-password",
            "APP_DB_USER": "ux09_app",
            "APP_DB_PASSWORD": "app-b2b1-password",
            "GOVERNANCE_DB_USER": "ux09_governance",
            "GOVERNANCE_DB_PASSWORD": "governance-b2b1-password",
        },
    )
    engines = {
        "owner": owner,
        "app": create_engine(os.environ["POSTGRES_APP_URL"]),
        "governance": create_engine(os.environ["POSTGRES_GOVERNANCE_URL"]),
    }
    yield engines
    for engine in engines.values():
        engine.dispose()


@pytest.fixture
def seeded(databases):
    with databases["owner"].begin() as connection:
        connection.execute(text("TRUNCATE organizations CASCADE"))
        ids = {name: uuid4() for name in (
            "organization", "user", "candidate", "other_candidate", "job", "jd", "rule",
            "file", "resume", "application", "screening_run", "screening_item",
            "screening_result", "llm_config", "prompt", "invocation", "evaluation",
            "queue", "interview", "feedback", "pool", "membership", "note", "request", "audit",
            "application_audit", "resume_audit", "interview_audit", "feedback_audit",
            "screening_item_audit", "file_audit", "note_audit", "membership_audit",
            "malicious_audit", "other_audit", "idempotency", "upload_idempotency",
            "file_idempotency", "other_idempotency",
        )}
        seed_sql = r"""
            INSERT INTO organizations(id, slug, name, status, retention_policy_id, created_at, updated_at)
            VALUES (:organization, 'b2b1-redaction', 'B2B1', 'active',
                    md5(CAST(:organization AS text) || '-retention-policy')::uuid, now(), now());
            INSERT INTO users(id, organization_id, email, normalized_email, display_name,
                              password_hash, status, authorization_version, created_at, updated_at)
            VALUES (:user, :organization, 'owner@b2b1.test', 'owner@b2b1.test', 'Owner',
                    'hash', 'active', 1, now(), now());
            INSERT INTO jobs(id, organization_id, title, owner_id, status, created_at, updated_at)
            VALUES (:job, :organization, 'Engineer', :user, 'open', now(), now());
            INSERT INTO job_jd_versions(id, organization_id, job_id, version_number, content, created_by)
            VALUES (:jd, :organization, :job, 1, '{"text"\:"JD"}', :user);
            INSERT INTO screening_rule_versions(id, organization_id, job_id, version_number, content, created_by)
            VALUES (:rule, :organization, :job, 1, '{}', :user);
            INSERT INTO candidates(id, organization_id, display_name, current_title, location, owner_id,
                                   version, retention_due_at, created_at, updated_at)
            VALUES (:candidate, :organization, 'Private Person', 'Secret title', 'Secret city', :user,
                    7, now() + interval '30 days', now(), now()),
                   (:other_candidate, :organization, 'Other Person', NULL, NULL, :user, 1, NULL, now(), now());
            INSERT INTO candidate_contacts(id, organization_id, candidate_id, kind, ciphertext, lookup_hash, masked_value)
            VALUES (gen_random_uuid(), :organization, :candidate, 'email', decode('01','hex'), repeat('a',64), 'p***@example.test');
            INSERT INTO file_objects(id, organization_id, storage_key, original_filename, mime_type,
                                     size_bytes, sha256, uploaded_by, storage_state, detected_type,
                                     scan_status, quarantine_cleanup_key)
            VALUES (:file, :organization, 'clean/private-person.pdf', 'private-person.pdf',
                    'application/pdf', 123, repeat('b',64), :user, 'clean', 'pdf', 'clean',
                    'quarantine/private-person.pdf');
            INSERT INTO resumes(id, organization_id, candidate_id, file_object_id, version_number, parsed_text)
            VALUES (:resume, :organization, :candidate, :file, 1, 'private resume text');
            INSERT INTO applications(id, organization_id, candidate_id, job_id, resume_id, owner_id,
                                     stage, source, human_conclusion, version)
            VALUES (:application, :organization, :candidate, :job, :resume, :user,
                    'rejected', 'manual', 'private human conclusion', 4);
            INSERT INTO idempotency_records(
              id, organization_id, user_id, operation, idempotency_key, request_hash,
              status_code, response_json, expires_at
            ) VALUES
              (:idempotency, :organization, :user, 'application.create', 'candidate-related',
               repeat('7',64), 201,
               jsonb_build_object('data', jsonb_build_object(
                 'id', CAST(:application AS text), 'candidate_id', CAST(:candidate AS text)
               )), now() + interval '1 day'),
              (:upload_idempotency, :organization, :user, 'screening.item.upload', 'upload-related',
               repeat('6',64), 201,
               jsonb_build_object('data', jsonb_build_object(
                 'id', CAST(:screening_item AS text),
                 'filename', 'private-person.pdf'
               )), now() + interval '1 day'),
              (:file_idempotency, :organization, :user, 'legacy.file.import', 'file-related',
               repeat('5',64), 201,
               jsonb_build_object('data', jsonb_build_object(
                 'file_object_id', CAST(:file AS text)
               )), now() + interval '1 day'),
              (:other_idempotency, :organization, :user, 'job.create', 'unrelated',
               repeat('8',64), 201, '{"data":{"id":"unrelated"}}', now() + interval '1 day');
            INSERT INTO application_stage_events(id, organization_id, application_id, actor_user_id, event_type, payload)
            VALUES (gen_random_uuid(), :organization, :application, :user, 'application.stage_changed',
                    '{"reason"\:"private reason","candidate_id"\:"private"}');
            INSERT INTO candidate_notes(id, organization_id, candidate_id, actor_user_id, event_type, payload)
            VALUES (:note, :organization, :candidate, :user, 'candidate.note_added', '{"content"\:"private note"}');
            INSERT INTO candidate_events(id, organization_id, candidate_id, actor_user_id, event_type, payload)
            VALUES (gen_random_uuid(), :organization, :candidate, :user, 'candidate.updated', '{"address"\:"private address"}');
            INSERT INTO download_tickets(id, organization_id, token_hash, user_id, resume_id, expires_at)
            VALUES (gen_random_uuid(), :organization, repeat('c',64), :user, :resume, now() + interval '1 hour');
            INSERT INTO screening_runs(id, organization_id, job_id, jd_version_id, rule_version_id,
                                       source, status, total_count, processed_count, succeeded_count,
                                       failed_count, created_by, version)
            VALUES (:screening_run, :organization, :job, :jd, :rule, 'upload', 'completed', 1, 1, 1, 0, :user, 1);
            INSERT INTO screening_items(id, organization_id, run_id, file_object_id, candidate_id,
                                        resume_id, application_id, status, attempts, llm_status, llm_attempts)
            VALUES (:screening_item, :organization, :screening_run, :file, :candidate,
                    :resume, :application, 'scored', 1, 'succeeded', 1);
            INSERT INTO screening_results(id, organization_id, item_id, application_id, resume_id,
                                          rule_engine_version, rule_score, recommendation, required_hits,
                                          required_missing, bonus_hits, estimated_years, risks, questions,
                                          human_override_recommendation, human_override_reason_code,
                                          human_override_by, human_override_at)
            VALUES (:screening_result, :organization, :screening_item, :application, :resume,
                    'v1', 88, '优先沟通', '["private skill"]', '["private gap"]', '["private bonus"]',
                    9, '["private risk"]', '["private question"]', '可沟通', 'private_reason', :user, now());
            INSERT INTO llm_provider_configs(id, organization_id, provider_id, model, encrypted_api_key,
                                             enabled, allowed_job_ids, version, created_by, updated_by)
            VALUES (:llm_config, :organization, 'provider', 'model', decode('02','hex'), true, '[]', 1, :user, :user);
            INSERT INTO prompt_versions(id, organization_id, name, version_number, content, content_hash, created_by)
            VALUES (:prompt, :organization, 'screen', 1, '{}', repeat('d',64), :user);
            INSERT INTO background_jobs(
              id, organization_id, type, payload, status, attempts, max_attempts
            ) VALUES (:queue, :organization, 'screening.llm_score_item', '{}', 'succeeded', 1, 3);
            INSERT INTO llm_invocations(id, organization_id, config_id, prompt_version_id, screening_result_id,
                                        queue_job_id, attempt_no, config_version, provider_id, model,
                                        request_field_manifest, status, usage, input_sha256)
            VALUES (:invocation, :organization, :llm_config, :prompt, :screening_result,
                    :queue, 1, 1, 'provider', 'model', '["resume_text"]', 'succeeded',
                    '{"total_tokens"\:12}', repeat('9',64));
            INSERT INTO llm_screening_evaluations(id, organization_id, screening_result_id, invocation_id,
                                                  prompt_version_id, score, recommendation, summary,
                                                  strengths, gaps, risks, interview_questions)
            VALUES (:evaluation, :organization, :screening_result, :invocation, :prompt, 91, '优先沟通',
                    'private summary', '["private strength"]', '["private gap"]',
                    '["private risk"]', '["private interview question"]');
            INSERT INTO candidate_duplicate_hints(id, organization_id, left_candidate_id, right_candidate_id,
                                                  file_object_id, candidate_id, signals, status)
            VALUES (gen_random_uuid(), :organization, :candidate, :other_candidate, :file, :candidate,
                    '{"email"\:"private@example.test"}', 'pending');
            INSERT INTO interviews(id, organization_id, application_id, round_name, method, timezone,
                                   starts_at, ends_at, location, meeting_url, status, notification_status,
                                   invitation_status, owner_id, created_by, version, calendar_sequence,
                                   calendar_organizer, calendar_attendees)
            VALUES (:interview, :organization, :application, 'Round', 'video', 'UTC', now(),
                    now() + interval '1 hour', 'private room', 'https://private.test/meeting',
                    'feedback_completed', 'sent', 'artifact_ready', :user, :user, 1, 1,
                    '{"email"\:"private@example.test"}', '[{"email"\:"private@example.test"}]');
            INSERT INTO interview_participants(id, organization_id, interview_id, user_id, role,
                                               required_feedback, attendance_status, task_status)
            VALUES (gen_random_uuid(), :organization, :interview, :user, 'interviewer', true, 'attended', 'completed');
            INSERT INTO interview_events(id, organization_id, interview_id, actor_user_id, event_type, payload)
            VALUES (gen_random_uuid(), :organization, :interview, :user, 'interview.updated', '{"details"\:"private"}');
            INSERT INTO interview_feedbacks(id, organization_id, interview_id, author_id, status, ratings,
                                            strengths, risks, conclusion, notes, version, submitted_at)
            VALUES (:feedback, :organization, :interview, :user, 'submitted', '{"ability"\:5}',
                    'private strengths', 'private risks', 'strong_recommend', 'private notes', 2, now());
            INSERT INTO interview_feedback_revisions(id, organization_id, feedback_id, revision_number,
                                                     previous_payload, new_payload, reason, actor_id)
            VALUES (gen_random_uuid(), :organization, :feedback, 1, '{"notes"\:"old private"}',
                    '{"notes"\:"new private"}', 'private correction', :user);
            INSERT INTO talent_pools(id, organization_id, name, purpose, visibility, owner_id,
                                     suitable_roles, retention_days, version)
            VALUES (:pool, :organization, 'Pool', 'Purpose', 'private', :user, '[]', 730, 1);
            INSERT INTO talent_pool_memberships(id, organization_id, pool_id, candidate_id, source_application_id,
                                                owner_id, suitable_roles, tags, reason, retention_until,
                                                status, version)
            VALUES (:membership, :organization, :pool, :candidate, :application, :user, '["private role"]',
                    '["private tag"]', 'private reason', now() + interval '1 year', 'active', 1);
            INSERT INTO deletion_requests(id, organization_id, candidate_id, status, version, reason_code,
                                          requested_by, requested_at, approved_by, approved_at,
                                          execution_started_at, impact_manifest, manifest_hash,
                                          manifest_schema_version, policy_version, candidate_version,
                                          recovery_generation, created_at, updated_at)
            VALUES (:request, :organization, :candidate, 'executing', 3, 'administrator_request',
                    :user, now(), :user, now(), now(),
                    '{"counts"\:{"contacts"\:1,"resumes"\:1,"applications"\:1,"screening_records"\:3,"interviews"\:1,"feedback_records"\:2,"talent_memberships"\:1,"resume_objects"\:1,"temporary_exports"\:1}}',
                    repeat('e',64), 1, 1, 7, 0, now(), now());
            INSERT INTO audit_logs(id, organization_id, actor_user_id, category, event_type, outcome,
                                   resource_type, resource_id, trace_id, metadata_json, created_at)
            VALUES (:audit, :organization, :user, 'recruiting', 'candidate.updated', 'success',
                    'candidate', :candidate, 'trace-keep',
                    jsonb_build_object('candidate_id', CAST(:candidate AS text), 'safe_error_code', 'none'), now()),
                   (:application_audit, :organization, :user, 'recruiting', 'application.updated', 'success',
                    'application', :application, 'trace-application',
                    jsonb_build_object('application_id', CAST(:application AS text), 'safe_error_code', 'none'), now()),
                   (:resume_audit, :organization, :user, 'recruiting', 'resume.updated', 'success',
                    'resume', :resume, 'trace-resume',
                    jsonb_build_object('resume_id', CAST(:resume AS text), 'safe_error_code', 'none'), now()),
                   (:interview_audit, :organization, :user, 'interview', 'interview.updated', 'success',
                    'interview', :interview, 'trace-interview',
                    jsonb_build_object('interview_id', CAST(:interview AS text), 'safe_error_code', 'none'), now()),
                   (:feedback_audit, :organization, :user, 'interview', 'feedback.updated', 'success',
                    'interview_feedback', :feedback, 'trace-feedback',
                    jsonb_build_object('feedback_id', CAST(:feedback AS text), 'safe_error_code', 'none'), now()),
                   (:screening_item_audit, :organization, :user, 'recruiting', 'screening.item_accepted', 'success',
                    'screening_item', :screening_item, 'trace-screening-item',
                    jsonb_build_object(
                      'item_id', CAST(:screening_item AS text),
                      'file_object_id', CAST(:file AS text),
                      'safe_error_code', 'none'
                    ), now()),
                   (:file_audit, :organization, :user, 'recruiting', 'resume.uploaded', 'success',
                    'file_object', :file, 'trace-file',
                    jsonb_build_object('file_object_id', CAST(:file AS text), 'safe_error_code', 'none'), now()),
                   (:note_audit, :organization, :user, 'recruiting', 'candidate.note_added', 'success',
                    'candidate', :candidate, 'trace-note',
                    jsonb_build_object(
                      'candidate_id', CAST(:candidate AS text),
                      'note_id', CAST(:note AS text),
                      'safe_error_code', 'none'
                    ), now()),
                   (:membership_audit, :organization, :user, 'recruiting', 'talent_pool.member_added', 'success',
                    'talent_pool_membership', :membership, 'trace-membership',
                    jsonb_build_object(
                      'membership_id', CAST(:membership AS text),
                      'candidate_id', CAST(:candidate AS text),
                      'source_application_id', CAST(:application AS text),
                      'safe_error_code', 'none'
                    ), now()),
                   (:other_audit, :organization, :user, 'recruiting', 'job.updated', 'success',
                    'job', :job, 'trace-other', '{"new_version"\:2}', now());
        """
        for statement in seed_sql.split(";"):
            if statement.strip():
                connection.execute(text(statement), ids)
    return ids


def _scalar(engine, sql: str, **values):
    with engine.connect() as connection:
        return connection.execute(text(sql), values).scalar_one()


def test_executor_role_is_real_least_privilege_boundary(databases) -> None:
    owner = databases["owner"]
    with owner.connect() as connection:
        role = connection.execute(text("""
            SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
            FROM pg_roles WHERE rolname = 'ux09_governance'
        """)).one()
        assert role == (True, False, False, False, False, False)
        assert connection.scalar(text(
            "SELECT has_function_privilege('ux09_governance', "
            "'redact_candidate_data(uuid,uuid,uuid)', 'EXECUTE')"
        )) is True
        assert connection.scalar(text(
            "SELECT has_function_privilege('ux09_app', "
            "'redact_candidate_data(uuid,uuid,uuid)', 'EXECUTE')"
        )) is False
        assert connection.scalar(text(
            "SELECT has_function_privilege('public', "
            "'redact_candidate_data(uuid,uuid,uuid)', 'EXECUTE')"
        )) is False

    for engine, sql in (
        (databases["governance"], "SELECT count(*) FROM candidates"),
        (databases["governance"], "UPDATE candidates SET display_name='x'"),
        (databases["app"], "SET ROLE ux09_governance_executor"),
        (databases["governance"], "SET ROLE ux09_owner"),
        (databases["governance"], "SET ROLE ux09_app"),
    ):
        with engine.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text(sql))


def test_provisioning_removes_legacy_governance_role_paths(databases) -> None:
    owner = databases["owner"]
    with owner.begin() as connection:
        connection.execute(text("DROP ROLE IF EXISTS ux09_legacy_governance_member"))
        connection.execute(text("DROP ROLE IF EXISTS ux09_unrelated_role"))
        connection.execute(text("CREATE ROLE ux09_legacy_governance_member NOLOGIN"))
        connection.execute(text("CREATE ROLE ux09_unrelated_role NOLOGIN"))
        connection.execute(text(
            "GRANT ux09_governance_executor TO ux09_app, ux09_owner, "
            "ux09_legacy_governance_member"
        ))
        connection.execute(text(
            "GRANT ux09_governance_executor TO ux09_governance WITH ADMIN OPTION"
        ))
        connection.execute(text("GRANT ux09_unrelated_role TO ux09_governance"))
        connection.execute(text("GRANT ux09_governance TO ux09_app"))
        connection.execute(text("GRANT ux09_unrelated_role TO ux09_governance_executor"))
    try:
        subprocess.run(
            ["sh", "deploy/postgres/provision-app-role.sh"],
            check=True,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PGHOST": os.environ.get("POSTGRES_SMOKE_HOST", "host.docker.internal"),
                "PGPORT": os.environ.get("POSTGRES_SMOKE_PORT", "55433"),
                "POSTGRES_DB": "ux09",
                "POSTGRES_USER": "ux09_owner",
                "POSTGRES_PASSWORD": "owner-b2b1-password",
                "APP_DB_USER": "ux09_app",
                "APP_DB_PASSWORD": "app-b2b1-password",
                "GOVERNANCE_DB_USER": "ux09_governance",
                "GOVERNANCE_DB_PASSWORD": "governance-b2b1-password",
            },
        )
        with owner.connect() as connection:
            unauthorized_paths = connection.scalar(text("""
                SELECT count(*)
                FROM pg_auth_members membership
                JOIN pg_roles granted_role ON granted_role.oid=membership.roleid
                JOIN pg_roles member_role ON member_role.oid=membership.member
                WHERE
                  (granted_role.rolname='ux09_governance_executor'
                   AND member_role.rolname<>'ux09_governance')
                  OR (member_role.rolname='ux09_governance'
                      AND granted_role.rolname<>'ux09_governance_executor')
                  OR granted_role.rolname='ux09_governance'
                  OR member_role.rolname='ux09_governance_executor'
            """))
            assert unauthorized_paths == 0
            assert connection.scalar(text("""
                SELECT membership.admin_option
                FROM pg_auth_members membership
                JOIN pg_roles granted_role ON granted_role.oid=membership.roleid
                JOIN pg_roles member_role ON member_role.oid=membership.member
                WHERE granted_role.rolname='ux09_governance_executor'
                  AND member_role.rolname='ux09_governance'
            """)) is False
        with databases["governance"].connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text(
                "GRANT ux09_governance_executor TO ux09_legacy_governance_member"
            ))
    finally:
        with owner.begin() as connection:
            connection.execute(text(
                "REVOKE ux09_governance_executor FROM ux09_app, ux09_owner"
            ))
            connection.execute(text("REVOKE ux09_governance FROM ux09_app"))
            connection.execute(text("DROP ROLE IF EXISTS ux09_legacy_governance_member"))
            connection.execute(text("DROP ROLE IF EXISTS ux09_unrelated_role"))


def test_redaction_clears_full_pii_inventory_and_retains_aggregate_facts(
    databases, seeded
) -> None:
    from server.app.governance.deletion_service import execute_database_redaction

    with databases["owner"].connect() as connection:
        upload_record = connection.execute(text("""
            SELECT operation, response_json
            FROM idempotency_records
            WHERE id=:upload_idempotency
        """), seeded).one()
        assert upload_record.operation == "screening.item.upload"
        assert upload_record.response_json == {
            "data": {
                "id": str(seeded["screening_item"]),
                "filename": "private-person.pdf",
            }
        }

    with databases["app"].begin() as connection:
        connection.execute(text("""
            INSERT INTO audit_logs(
              id, organization_id, actor_user_id, category, event_type, outcome,
              resource_type, resource_id, trace_id, metadata_json, created_at
            ) VALUES (
              :malicious_audit, :organization, :user, 'recruiting', 'job.updated',
              'success', 'job', :candidate, 'trace-malicious',
              jsonb_build_object('new_version', 99), now()
            )
        """), seeded)
    with databases["governance"].begin() as connection:
        result = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )
    assert len(result.checksum) == 64
    assert result.facts == (1, 1, 1, 3, 1, 2, 1, 1, 1)

    owner = databases["owner"]
    with owner.connect() as connection:
        candidate = connection.execute(text("""
            SELECT display_name, current_title, location, owner_id, deleted_at IS NOT NULL,
                   retention_due_at, version FROM candidates WHERE id=:candidate
        """), seeded).one()
        assert candidate == ("已删除候选人", None, None, None, True, None, 8)
        for table in ("candidate_contacts", "download_tickets", "talent_pool_memberships", "candidate_duplicate_hints"):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert connection.execute(text(
            "SELECT parsed_text FROM resumes WHERE id=:resume"
        ), seeded).scalar_one() is None
        file_row = connection.execute(text("""
            SELECT storage_key, original_filename, quarantine_cleanup_key, size_bytes, sha256,
                   storage_state, detected_type, scan_status
            FROM file_objects WHERE id=:file
        """), seeded).one()
        deleted_fingerprint = hashlib.sha256(
            f"deleted:{seeded['file']}".encode("utf-8")
        ).hexdigest()
        assert file_row == (
            f"deleted/{seeded['file']}", "deleted", None, 123,
            deleted_fingerprint, "deleted", "pdf", "clean"
        )
        assert connection.execute(text(
            "SELECT source, human_conclusion FROM applications WHERE id=:application"
        ), seeded).one() == ("deleted", None)
        for table in ("application_stage_events", "candidate_notes", "candidate_events", "interview_events"):
            assert connection.scalar(text(f"SELECT payload FROM {table}")) == {}
        screening = connection.execute(text("""
            SELECT rule_score, recommendation, required_hits, required_missing, bonus_hits,
                   estimated_years, risks, questions, human_override_recommendation,
                   human_override_reason_code, human_override_by, human_override_at
            FROM screening_results WHERE id=:screening_result
        """), seeded).one()
        assert screening == (88, "需人工复核", [], [], [], 0, [], [], None, None, None, None)
        evaluation = connection.execute(text("""
            SELECT score, recommendation, summary, strengths, gaps, risks, interview_questions
            FROM llm_screening_evaluations WHERE id=:evaluation
        """), seeded).one()
        assert evaluation == (91, "需人工复核", "", [], [], [], [])
        invocation_fingerprint = connection.scalar(text(
            "SELECT input_sha256 FROM llm_invocations WHERE id=:invocation"
        ), seeded)
        assert invocation_fingerprint == hashlib.sha256(
            f"deleted:{seeded['invocation']}".encode("utf-8")
        ).hexdigest()
        interview = connection.execute(text("""
            SELECT round_name, location, meeting_url, calendar_organizer, calendar_attendees,
                   status, starts_at IS NOT NULL
            FROM interviews WHERE id=:interview
        """), seeded).one()
        assert interview == ("deleted", None, None, {}, [], "feedback_completed", True)
        feedback = connection.execute(text("""
            SELECT ratings, strengths, risks, conclusion, notes, status, submitted_at IS NOT NULL
            FROM interview_feedbacks WHERE id=:feedback
        """), seeded).one()
        assert feedback == ({}, None, None, None, None, "submitted", True)
        revision = connection.execute(text("""
            SELECT previous_payload, new_payload, reason, revision_number
            FROM interview_feedback_revisions
        """)).one()
        assert revision == ({}, {}, "", 1)
        audit = connection.execute(text("""
            SELECT resource_id, metadata_json, actor_user_id, category, event_type,
                   outcome, trace_id, created_at IS NOT NULL
            FROM audit_logs WHERE id=:audit
        """), seeded).one()
        assert audit == (
            None, {"safe_error_code": "none"}, seeded["user"], "recruiting",
            "candidate.updated", "success", "trace-keep", True
        )
        other_audit = connection.execute(text(
            "SELECT resource_id, metadata_json FROM audit_logs WHERE id=:other_audit"
        ), seeded).one()
        assert other_audit == (seeded["job"], {"new_version": 2})
        malicious_audit = connection.execute(text(
            "SELECT resource_type, resource_id, metadata_json "
            "FROM audit_logs WHERE id=:malicious_audit"
        ), seeded).one()
        assert malicious_audit == ("job", seeded["candidate"], {"new_version": 99})
        linked_audit = connection.execute(text("""
            SELECT id, resource_id, metadata_json
            FROM audit_logs
            WHERE id IN (
              :application_audit, :resume_audit, :interview_audit, :feedback_audit,
              :screening_item_audit, :file_audit, :note_audit, :membership_audit
            )
            ORDER BY id
        """), seeded).all()
        assert len(linked_audit) == 8
        assert all(row.resource_id is None for row in linked_audit)
        assert all(row.metadata_json == {"safe_error_code": "none"} for row in linked_audit)
        assert connection.scalar(text(
            "SELECT count(*) FROM idempotency_records WHERE id=:idempotency"
        ), seeded) == 0
        assert connection.scalar(text(
            "SELECT count(*) FROM idempotency_records WHERE id=:upload_idempotency"
        ), seeded) == 0
        assert connection.scalar(text(
            "SELECT count(*) FROM idempotency_records WHERE id=:other_idempotency"
        ), seeded) == 1
        assert connection.scalar(text(
            "SELECT count(*) FROM idempotency_records WHERE id=:file_idempotency"
        ), seeded) == 0
        remaining_idempotency_payloads = connection.scalars(text("""
            SELECT response_json::text
            FROM idempotency_records
            WHERE organization_id=:organization
        """), seeded).all()
        assert all("private-person.pdf" not in payload for payload in remaining_idempotency_payloads)
        assert all(str(seeded["screening_item"]) not in payload for payload in remaining_idempotency_payloads)
        assert all(str(seeded["file"]) not in payload for payload in remaining_idempotency_payloads)

    with databases["governance"].begin() as connection:
        connection.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
        repeated = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )
    assert repeated == result


def test_first_redaction_rejects_candidate_version_changed_after_approval(
    databases, seeded
) -> None:
    from server.app.governance.deletion_service import (
        DeletionDomainError,
        execute_database_redaction,
    )

    with databases["owner"].begin() as connection:
        connection.execute(text(
            "UPDATE candidates SET version=version+1 WHERE id=:candidate"
        ), seeded)
    with databases["governance"].begin() as connection:
        with pytest.raises(DeletionDomainError) as error:
            execute_database_redaction(
                connection,
                organization_id=seeded["organization"],
                request_id=seeded["request"],
                candidate_id=seeded["candidate"],
            )

    assert error.value.code == "redaction_manifest_stale"
    assert _scalar(
        databases["owner"],
        "SELECT display_name FROM candidates WHERE id=:id",
        id=seeded["candidate"],
    ) == "Private Person"
    assert _scalar(databases["owner"], "SELECT count(*) FROM candidate_contacts") == 1


def test_tombstone_retry_recleans_restored_candidate_pii(databases, seeded) -> None:
    from server.app.governance.deletion_service import execute_database_redaction

    with databases["governance"].begin() as connection:
        first = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )
    with databases["owner"].begin() as connection:
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        connection.execute(text("""
            INSERT INTO candidate_contacts(
              id, organization_id, candidate_id, kind, ciphertext, lookup_hash, masked_value
            ) VALUES (
              gen_random_uuid(), :organization, :candidate, 'email', decode('01','hex'),
              repeat('a',64), 'restored@example.test'
            )
        """), seeded)
        for sql in (
            "UPDATE candidates SET display_name='Restored Person', "
            "current_title='Restored title', location='Restored city', owner_id=:user "
            "WHERE id=:candidate",
            "UPDATE resumes SET parsed_text='restored resume text' WHERE id=:resume",
            "UPDATE applications SET source='restored-private', "
            "human_conclusion='restored' WHERE id=:application",
            "UPDATE interviews SET round_name='Restored private round', "
            "location='Restored room' WHERE id=:interview",
            "UPDATE llm_invocations SET input_sha256=repeat('f',64) "
            "WHERE id=:invocation",
            "UPDATE interview_feedbacks SET notes='restored private notes' "
            "WHERE id=:feedback",
            "UPDATE audit_logs SET resource_id=:application, "
            "metadata_json=jsonb_build_object("
            "'application_id', CAST(:application AS text), "
            "'safe_error_code', 'none') WHERE id=:application_audit",
        ):
            connection.execute(text(sql), seeded)
        connection.execute(text("""
            INSERT INTO idempotency_records(
              id, organization_id, user_id, operation, idempotency_key,
              request_hash, status_code, response_json, expires_at
            ) VALUES (
              :idempotency, :organization, :user, 'application.create', 'restored',
              repeat('7',64), 201,
              jsonb_build_object('data', jsonb_build_object(
                'id', CAST(:application AS text),
                'candidate_id', CAST(:candidate AS text)
              )), now() + interval '1 day'
            )
        """), seeded)
    with databases["governance"].begin() as connection:
        repeated = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )

    assert repeated == first
    with databases["owner"].connect() as connection:
        assert connection.execute(text("""
            SELECT display_name, current_title, location, owner_id, version
            FROM candidates WHERE id=:candidate
        """), seeded).one() == ("已删除候选人", None, None, None, 8)
        assert connection.scalar(text(
            "SELECT count(*) FROM candidate_contacts WHERE candidate_id=:candidate"
        ), seeded) == 0
        assert connection.execute(text(
            "SELECT source, human_conclusion FROM applications WHERE id=:application"
        ), seeded).one() == ("deleted", None)
        assert connection.scalar(text(
            "SELECT parsed_text FROM resumes WHERE id=:resume"
        ), seeded) is None
        assert connection.execute(text(
            "SELECT round_name, location FROM interviews WHERE id=:interview"
        ), seeded).one() == ("deleted", None)
        assert connection.scalar(text(
            "SELECT input_sha256 FROM llm_invocations WHERE id=:invocation"
        ), seeded) == hashlib.sha256(
            f"deleted:{seeded['invocation']}".encode("utf-8")
        ).hexdigest()
        assert connection.scalar(text(
            "SELECT notes FROM interview_feedbacks WHERE id=:feedback"
        ), seeded) is None
        assert connection.scalar(text(
            "SELECT resource_id FROM audit_logs WHERE id=:application_audit"
        ), seeded) is None
        assert connection.scalar(text(
            "SELECT count(*) FROM idempotency_records WHERE id=:idempotency"
        ), seeded) == 0


def test_redaction_checksum_is_stable_across_session_time_zones(
    databases, seeded
) -> None:
    from server.app.governance.deletion_service import execute_database_redaction

    with databases["governance"].begin() as connection:
        connection.execute(text("SET LOCAL TIME ZONE 'Pacific/Honolulu'"))
        first = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )
    with databases["governance"].begin() as connection:
        connection.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
        repeated = execute_database_redaction(
            connection,
            organization_id=seeded["organization"],
            request_id=seeded["request"],
            candidate_id=seeded["candidate"],
        )

    assert repeated == first


@pytest.mark.parametrize("invalid", ["organization", "request", "candidate", "state"])
def test_wrong_redaction_context_or_state_is_safe_and_zero_mutation(
    databases, seeded, invalid
) -> None:
    from server.app.governance.deletion_service import DeletionDomainError, execute_database_redaction

    values = {
        "organization_id": seeded["organization"],
        "request_id": seeded["request"],
        "candidate_id": seeded["candidate"],
    }
    if invalid == "state":
        with databases["owner"].begin() as connection:
            connection.execute(text(
                "UPDATE deletion_requests SET status='approved' WHERE id=:request"
            ), seeded)
    else:
        values[f"{invalid}_id"] = uuid4()
    with databases["governance"].begin() as connection:
        with pytest.raises(DeletionDomainError) as error:
            execute_database_redaction(connection, **values)
    assert error.value.code in {"redaction_context_invalid", "redaction_state_invalid"}
    assert all(str(value) not in str(error.value) for value in values.values())
    assert _scalar(
        databases["owner"], "SELECT display_name FROM candidates WHERE id=:id", id=seeded["candidate"]
    ) == "Private Person"
    assert _scalar(databases["owner"], "SELECT count(*) FROM candidate_contacts") == 1


def test_app_and_owner_cannot_emulate_audit_redaction(databases, seeded) -> None:
    for engine in (databases["app"], databases["owner"]):
        with engine.begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text("""
                UPDATE audit_logs SET resource_id=NULL,
                  metadata_json=metadata_json-'candidate_id'
                WHERE id=:audit
            """), seeded)
    assert _scalar(
        databases["owner"],
        "SELECT count(*) FROM audit_logs WHERE id=:audit AND resource_id=:candidate",
        audit=seeded["audit"],
        candidate=seeded["candidate"],
    ) == 1


def test_executor_cannot_redact_audit_without_routine_plan(databases, seeded) -> None:
    with databases["owner"].begin() as connection:
        connection.execute(text(
            "GRANT SELECT, UPDATE ON ALL TABLES IN SCHEMA public TO ux09_governance"
        ))
    try:
        with databases["governance"].begin() as connection, pytest.raises(DBAPIError):
            connection.execute(text("""
                SELECT set_config(
                  'ux09.audit_redaction_plan',
                  jsonb_build_object(
                    CAST(:audit AS text),
                    jsonb_build_object(
                      'clear_resource_id', true,
                      'metadata_keys', jsonb_build_array('candidate_id')
                    )
                  )::text,
                  true
                )
            """), seeded)
            connection.execute(text("""
                UPDATE audit_logs
                SET resource_id=NULL, metadata_json=metadata_json-'candidate_id'
                WHERE id=:audit
            """), seeded)
    finally:
        with databases["owner"].begin() as connection:
            connection.execute(text(
                "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ux09_governance"
            ))

    assert _scalar(
        databases["owner"],
        "SELECT count(*) FROM audit_logs WHERE id=:audit AND resource_id=:candidate",
        audit=seeded["audit"],
        candidate=seeded["candidate"],
    ) == 1
