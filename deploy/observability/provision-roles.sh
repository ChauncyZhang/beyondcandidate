#!/bin/sh
set -eu

: "${POSTGRES_DB:?Set POSTGRES_DB to the application database name}"
: "${POSTGRES_USER:?Set POSTGRES_USER to the database owner role}"
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD to the database owner password}"
: "${QUEUE_METRICS_DB_USER:?Set QUEUE_METRICS_DB_USER to the queue metrics login}"
: "${QUEUE_METRICS_DB_PASSWORD:?Set QUEUE_METRICS_DB_PASSWORD to the queue metrics password}"
: "${POSTGRES_EXPORTER_DB_USER:?Set POSTGRES_EXPORTER_DB_USER to the postgres exporter login}"
: "${POSTGRES_EXPORTER_DB_PASSWORD:?Set POSTGRES_EXPORTER_DB_PASSWORD to the postgres exporter password}"

validate_identifier() {
    case "$1" in
        ''|[0-9]*|*[!A-Za-z0-9_]*)
            printf 'observability role provisioning: invalid role identifier: %s\n' "$1" >&2
            exit 1
            ;;
    esac
}

validate_identifier "$POSTGRES_USER"
validate_identifier "$QUEUE_METRICS_DB_USER"
validate_identifier "$POSTGRES_EXPORTER_DB_USER"

if [ "$QUEUE_METRICS_DB_USER" = "$POSTGRES_EXPORTER_DB_USER" ] \
    || [ "$QUEUE_METRICS_DB_USER" = "$POSTGRES_USER" ] \
    || [ "$POSTGRES_EXPORTER_DB_USER" = "$POSTGRES_USER" ]; then
    printf '%s\n' 'observability role provisioning: owner and exporter logins must be distinct' >&2
    exit 1
fi
if [ "$QUEUE_METRICS_DB_PASSWORD" = "$POSTGRES_EXPORTER_DB_PASSWORD" ] \
    || [ "$QUEUE_METRICS_DB_PASSWORD" = "$POSTGRES_PASSWORD" ] \
    || [ "$POSTGRES_EXPORTER_DB_PASSWORD" = "$POSTGRES_PASSWORD" ]; then
    printf '%s\n' 'observability role provisioning: owner and exporter passwords must be distinct' >&2
    exit 1
fi

export PGPASSWORD=$POSTGRES_PASSWORD

psql --quiet --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv database_name POSTGRES_DB
\getenv owner_user POSTGRES_USER
\getenv queue_user QUEUE_METRICS_DB_USER
\getenv queue_password QUEUE_METRICS_DB_PASSWORD
\getenv postgres_exporter_user POSTGRES_EXPORTER_DB_USER
\getenv postgres_exporter_password POSTGRES_EXPORTER_DB_PASSWORD
SET client_min_messages = error;

SELECT format('CREATE ROLE %I', :'queue_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'queue_user')
\gexec
SELECT format('CREATE ROLE %I', :'postgres_exporter_user')
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'postgres_exporter_user'
)
\gexec

ALTER ROLE :"queue_user" LOGIN PASSWORD :'queue_password'
    NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT -1 VALID UNTIL 'infinity';
ALTER ROLE :"postgres_exporter_user" LOGIN PASSWORD :'postgres_exporter_password'
    NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    CONNECTION LIMIT -1 VALID UNTIL 'infinity';
ALTER ROLE :"queue_user" RESET ALL;
ALTER ROLE :"postgres_exporter_user" RESET ALL;

SELECT format('REVOKE %I FROM %I', granted_role.rolname, member_role.rolname)
FROM pg_catalog.pg_auth_members AS membership
JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid
JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member
WHERE member_role.rolname IN (:'queue_user', :'postgres_exporter_user')
\gexec

CREATE SCHEMA IF NOT EXISTS observability AUTHORIZATION :"owner_user";
REVOKE ALL ON SCHEMA observability FROM PUBLIC;

CREATE OR REPLACE VIEW observability.queue_metrics
WITH (security_barrier = true)
AS
WITH job_dimensions AS (
    SELECT
        CASE type
            WHEN 'reports.export' THEN type
            WHEN 'screening.llm_score_item' THEN type
            WHEN 'screening.parse_item' THEN type
            WHEN 'screening.score_item' THEN type
            ELSE 'other'
        END AS job_type,
        CASE status
            WHEN 'cancelled' THEN status
            WHEN 'dead_letter' THEN status
            WHEN 'failed' THEN status
            WHEN 'queued' THEN status
            WHEN 'running' THEN status
            WHEN 'succeeded' THEN status
            ELSE 'unknown'
        END AS job_status,
        id,
        run_after,
        lease_expires_at
    FROM public.background_jobs
    WHERE type NOT LIKE 'governance.%'
), attempt_dimensions AS (
    SELECT
        job.job_type,
        CASE
            WHEN attempt.result IS NULL THEN 'running'
            WHEN attempt.result = 'abandoned' THEN attempt.result
            WHEN attempt.result = 'cancelled' THEN attempt.result
            WHEN attempt.result = 'failed' THEN attempt.result
            WHEN attempt.result = 'running' THEN attempt.result
            WHEN attempt.result = 'succeeded' THEN attempt.result
            ELSE 'unknown'
        END AS attempt_result,
        CASE
            WHEN coalesce(attempt.safe_error_code, '') = '' THEN 'none'
            WHEN lower(attempt.safe_error_code) SIMILAR TO '%(parse|document|pdf|docx)%' THEN 'parse'
            WHEN lower(attempt.safe_error_code) SIMILAR TO '%(llm|model|provider)%' THEN 'llm'
            WHEN lower(attempt.safe_error_code) LIKE '%lease%' THEN 'lease'
            WHEN lower(attempt.safe_error_code) SIMILAR TO '%(virus|scan|malware)%' THEN 'malware_scan'
            WHEN lower(attempt.safe_error_code) SIMILAR TO '%(internal|handler|unknown)%' THEN 'internal'
            ELSE 'other'
        END AS error_class,
        attempt.duration_ms
    FROM public.job_attempts AS attempt
    JOIN job_dimensions AS job ON job.id = attempt.job_id
), outbox_dimensions AS (
    SELECT
        CASE topic WHEN 'audit.created' THEN topic ELSE 'other' END AS topic,
        CASE status
            WHEN 'failed' THEN status
            WHEN 'published' THEN status
            WHEN 'queued' THEN status
            WHEN 'running' THEN status
            ELSE 'unknown'
        END AS outbox_status,
        available_at,
        lease_expires_at
    FROM public.outbox_events
)
SELECT 'job_count'::text AS metric_name, job_type AS dimension_a,
       job_status AS dimension_b, ''::text AS dimension_c,
       count(*)::double precision AS value
FROM job_dimensions GROUP BY job_type, job_status
UNION ALL
SELECT 'job_oldest_ready_age', job_type, '', '',
       extract(epoch FROM (CURRENT_TIMESTAMP - min(run_after)))::double precision
FROM job_dimensions
WHERE job_status = 'queued' AND run_after <= CURRENT_TIMESTAMP
GROUP BY job_type
UNION ALL
SELECT 'job_attempt', job_type, attempt_result, error_class,
       count(*)::double precision
FROM attempt_dimensions GROUP BY job_type, attempt_result, error_class
UNION ALL
SELECT 'job_attempt_duration', job_type, attempt_result, error_class,
       (coalesce(sum(duration_ms), 0) / 1000.0)::double precision
FROM attempt_dimensions GROUP BY job_type, attempt_result, error_class
UNION ALL
SELECT 'expired_lease', 'job', '', '', count(*)::double precision
FROM job_dimensions
WHERE job_status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP
UNION ALL
SELECT 'expired_lease', 'outbox', '', '', count(*)::double precision
FROM outbox_dimensions
WHERE outbox_status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP
UNION ALL
SELECT 'job_dead_letter', job_type, '', '', count(*)::double precision
FROM job_dimensions WHERE job_status = 'dead_letter' GROUP BY job_type
UNION ALL
SELECT 'outbox_count', topic, outbox_status, '', count(*)::double precision
FROM outbox_dimensions GROUP BY topic, outbox_status
UNION ALL
SELECT 'outbox_oldest_ready_age', topic, '', '',
       extract(epoch FROM (CURRENT_TIMESTAMP - min(available_at)))::double precision
FROM outbox_dimensions
WHERE outbox_status = 'queued' AND available_at <= CURRENT_TIMESTAMP
GROUP BY topic;

ALTER VIEW observability.queue_metrics OWNER TO :"owner_user";
REVOKE ALL ON observability.queue_metrics FROM PUBLIC;

REVOKE ALL PRIVILEGES ON DATABASE :"database_name"
    FROM :"queue_user", :"postgres_exporter_user";
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I FROM %I, %I',
    schema_name.nspname, :'queue_user', :'postgres_exporter_user'
)
FROM pg_catalog.pg_namespace AS schema_name
WHERE schema_name.nspname !~ '^pg_temp_'
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I FROM %I, %I',
    schema_name.nspname, :'queue_user', :'postgres_exporter_user'
)
FROM pg_catalog.pg_namespace AS schema_name
WHERE schema_name.nspname !~ '^pg_temp_'
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I FROM %I, %I',
    schema_name.nspname, :'queue_user', :'postgres_exporter_user'
)
FROM pg_catalog.pg_namespace AS schema_name
WHERE schema_name.nspname !~ '^pg_temp_'
\gexec
SELECT format(
    'REVOKE ALL PRIVILEGES ON SCHEMA %I FROM %I, %I',
    schema_name.nspname, :'queue_user', :'postgres_exporter_user'
)
FROM pg_catalog.pg_namespace AS schema_name
WHERE schema_name.nspname !~ '^pg_temp_'
\gexec

GRANT CONNECT ON DATABASE :"database_name" TO :"queue_user";
GRANT USAGE ON SCHEMA observability TO :"queue_user";
GRANT SELECT ON observability.queue_metrics TO :"queue_user";

GRANT CONNECT ON DATABASE :"database_name" TO :"postgres_exporter_user";
GRANT pg_monitor TO :"postgres_exporter_user";

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM :"queue_user", :"postgres_exporter_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM :"queue_user", :"postgres_exporter_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM :"queue_user", :"postgres_exporter_user";
SQL

printf '%s\n' 'observability role provisioning: roles and safe aggregate view are ready'
