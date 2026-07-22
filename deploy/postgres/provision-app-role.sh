#!/bin/sh
set -eu

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"
: "${GOVERNANCE_DB_USER:?GOVERNANCE_DB_USER is required}"
: "${GOVERNANCE_DB_PASSWORD:?GOVERNANCE_DB_PASSWORD is required}"

if [ "$APP_DB_USER" = "$POSTGRES_USER" ]; then
  echo "APP_DB_USER must differ from POSTGRES_USER" >&2
  exit 1
fi
if [ "$APP_DB_PASSWORD" = "$POSTGRES_PASSWORD" ]; then
  echo "APP_DB_PASSWORD must differ from POSTGRES_PASSWORD" >&2
  exit 1
fi
if [ "$GOVERNANCE_DB_USER" = "$POSTGRES_USER" ]; then
  echo "GOVERNANCE_DB_USER must differ from POSTGRES_USER" >&2
  exit 1
fi
if [ "$GOVERNANCE_DB_USER" = "$APP_DB_USER" ]; then
  echo "GOVERNANCE_DB_USER must differ from APP_DB_USER" >&2
  exit 1
fi
if [ "$GOVERNANCE_DB_PASSWORD" = "$POSTGRES_PASSWORD" ]; then
  echo "GOVERNANCE_DB_PASSWORD must differ from POSTGRES_PASSWORD" >&2
  exit 1
fi
if [ "$GOVERNANCE_DB_PASSWORD" = "$APP_DB_PASSWORD" ]; then
  echo "GOVERNANCE_DB_PASSWORD must differ from APP_DB_PASSWORD" >&2
  exit 1
fi

export PGPASSWORD="$POSTGRES_PASSWORD"

psql \
  --host "${PGHOST:-/var/run/postgresql}" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set database="$POSTGRES_DB" \
  --set owner_user="$POSTGRES_USER" \
  --set app_user="$APP_DB_USER" \
  --set app_password="$APP_DB_PASSWORD" \
  --set governance_user="$GOVERNANCE_DB_USER" \
  --set governance_password="$GOVERNANCE_DB_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
  :'app_user', :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec

SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
  :'app_user', :'app_password'
)
\gexec

SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
  :'governance_user', :'governance_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'governance_user')
\gexec

SELECT format(
  'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
  :'governance_user', :'governance_password'
)
\gexec

SELECT 'CREATE ROLE ux09_governance_executor NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ux09_governance_executor')
\gexec
ALTER ROLE ux09_governance_executor WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT format(
  'REVOKE ADMIN OPTION FOR ux09_governance_executor FROM %I',
  :'governance_user'
)
FROM pg_auth_members membership
JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
JOIN pg_roles member_role ON member_role.oid = membership.member
WHERE granted_role.rolname = 'ux09_governance_executor'
  AND member_role.rolname = :'governance_user'
  AND membership.admin_option
\gexec
SELECT format('GRANT ux09_governance_executor TO %I', :'governance_user')
\gexec

SELECT format('REVOKE %I FROM %I', granted_role.rolname, member_role.rolname)
FROM pg_auth_members membership
JOIN pg_roles granted_role ON granted_role.oid = membership.roleid
JOIN pg_roles member_role ON member_role.oid = membership.member
WHERE
  (granted_role.rolname = 'ux09_governance_executor'
   AND member_role.rolname <> :'governance_user')
  OR (member_role.rolname = :'governance_user'
      AND granted_role.rolname <> 'ux09_governance_executor')
  OR granted_role.rolname = :'governance_user'
  OR member_role.rolname = 'ux09_governance_executor'
ORDER BY granted_role.rolname, member_role.rolname
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', :'app_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_user')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', :'governance_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'governance_user')
\gexec

SELECT format(
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
  schemaname, tablename, :'app_user'
)
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename !~ '^audit_logs(?:_|$)'
ORDER BY tablename
\gexec

SELECT format(
  'GRANT SELECT, INSERT ON TABLE %I.%I TO %I',
  schemaname, tablename, :'app_user'
)
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename ~ '^audit_logs(?:_|$)'
ORDER BY tablename
\gexec

SELECT format(
  'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO %I',
  sequence_schema, sequence_name, :'app_user'
)
FROM information_schema.sequences
WHERE sequence_schema = 'public'
ORDER BY sequence_name
\gexec

SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT, INSERT ON TABLES TO %I',
  :'owner_user', :'app_user'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'owner_user', :'app_user'
)
\gexec

SELECT format(
  'REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM %I',
  :'app_user'
)
\gexec

SELECT format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', :'governance_user')
\gexec
SELECT format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', :'governance_user')
\gexec
SELECT format('REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM %I', :'governance_user')
\gexec
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ux09_governance_executor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ux09_governance_executor;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM ux09_governance_executor;
SELECT format(
  'GRANT EXECUTE ON FUNCTION public.redact_candidate_data(uuid, uuid, uuid) TO ux09_governance_executor'
)
WHERE to_regprocedure('public.redact_candidate_data(uuid,uuid,uuid)') IS NOT NULL
\gexec

SELECT format('REVOKE %I FROM %I', :'owner_user', :'governance_user')
WHERE pg_has_role(:'governance_user', :'owner_user', 'MEMBER')
\gexec
SELECT format('REVOKE %I FROM %I', :'app_user', :'governance_user')
WHERE pg_has_role(:'governance_user', :'app_user', 'MEMBER')
\gexec
SQL
