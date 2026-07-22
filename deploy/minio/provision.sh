#!/bin/sh
set -eu

for name in MINIO_ROOT_USER MINIO_ROOT_PASSWORD APP_OBJECT_STORAGE_ACCESS_KEY \
  APP_OBJECT_STORAGE_SECRET_KEY OBJECT_STORAGE_BUCKET GOVERNANCE_DELETE_ACCESS_KEY \
  GOVERNANCE_DELETE_SECRET_KEY GOVERNANCE_RESUME_BUCKET GOVERNANCE_RESUME_PREFIX \
  GOVERNANCE_EXPORT_BUCKET GOVERNANCE_EXPORT_PREFIX GOVERNANCE_LEDGER_ACCESS_KEY \
  GOVERNANCE_LEDGER_SECRET_KEY GOVERNANCE_LEDGER_BUCKET GOVERNANCE_LEDGER_PREFIX
do
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    echo "$name is required" >&2
    exit 1
  fi
done

for name in MINIO_ROOT_USER MINIO_ROOT_PASSWORD APP_OBJECT_STORAGE_ACCESS_KEY \
  APP_OBJECT_STORAGE_SECRET_KEY GOVERNANCE_DELETE_ACCESS_KEY \
  GOVERNANCE_DELETE_SECRET_KEY GOVERNANCE_LEDGER_ACCESS_KEY \
  GOVERNANCE_LEDGER_SECRET_KEY
do
  eval "value=\${$name}"
  normalized=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  case "$normalized" in
    change-me*|changeme*|placeholder*|replace-me*|example|password|secret)
      echo "MinIO credentials must not use example placeholders" >&2
      exit 1
      ;;
  esac
done

for prefix in "$GOVERNANCE_RESUME_PREFIX" "$GOVERNANCE_EXPORT_PREFIX" \
  "$GOVERNANCE_LEDGER_PREFIX"
do
  case "$prefix" in
    ""|*[!/]) echo "object prefixes must be non-empty and end with /" >&2; exit 1 ;;
  esac
  case "$prefix" in
    /*|*//*|./*|../*|*/./*|*/../*|*[!A-Za-z0-9_./-]*)
      echo "invalid object prefix" >&2
      exit 1
      ;;
  esac
done

if [ "$GOVERNANCE_EXPORT_BUCKET" != "$OBJECT_STORAGE_BUCKET" ]; then
  echo "governance export bucket must match object storage bucket" >&2
  exit 1
fi
if [ "$GOVERNANCE_EXPORT_PREFIX" != "exports/" ]; then
  echo "governance export prefix must be exports/" >&2
  exit 1
fi

if [ "$MINIO_ROOT_USER" = "$APP_OBJECT_STORAGE_ACCESS_KEY" ] ||
   [ "$MINIO_ROOT_USER" = "$GOVERNANCE_DELETE_ACCESS_KEY" ] ||
   [ "$MINIO_ROOT_USER" = "$GOVERNANCE_LEDGER_ACCESS_KEY" ] ||
   [ "$APP_OBJECT_STORAGE_ACCESS_KEY" = "$GOVERNANCE_DELETE_ACCESS_KEY" ] ||
   [ "$APP_OBJECT_STORAGE_ACCESS_KEY" = "$GOVERNANCE_LEDGER_ACCESS_KEY" ] ||
   [ "$GOVERNANCE_DELETE_ACCESS_KEY" = "$GOVERNANCE_LEDGER_ACCESS_KEY" ]; then
  echo "MinIO access keys must be pairwise distinct" >&2
  exit 1
fi
if [ "$MINIO_ROOT_PASSWORD" = "$APP_OBJECT_STORAGE_SECRET_KEY" ] ||
   [ "$MINIO_ROOT_PASSWORD" = "$GOVERNANCE_DELETE_SECRET_KEY" ] ||
   [ "$MINIO_ROOT_PASSWORD" = "$GOVERNANCE_LEDGER_SECRET_KEY" ] ||
   [ "$APP_OBJECT_STORAGE_SECRET_KEY" = "$GOVERNANCE_DELETE_SECRET_KEY" ] ||
   [ "$APP_OBJECT_STORAGE_SECRET_KEY" = "$GOVERNANCE_LEDGER_SECRET_KEY" ] ||
   [ "$GOVERNANCE_DELETE_SECRET_KEY" = "$GOVERNANCE_LEDGER_SECRET_KEY" ]; then
  echo "MinIO secret keys must be pairwise distinct" >&2
  exit 1
fi
if { [ -n "${PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY:-}" ] &&
     [ "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY" != "$GOVERNANCE_DELETE_ACCESS_KEY" ] &&
     { [ "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY" = "$MINIO_ROOT_USER" ] ||
       [ "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY" = "$APP_OBJECT_STORAGE_ACCESS_KEY" ] ||
       [ "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY" = "$GOVERNANCE_LEDGER_ACCESS_KEY" ]; }; } ||
   { [ -n "${PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY:-}" ] &&
     [ "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY" != "$GOVERNANCE_LEDGER_ACCESS_KEY" ] &&
     { [ "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY" = "$MINIO_ROOT_USER" ] ||
       [ "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY" = "$APP_OBJECT_STORAGE_ACCESS_KEY" ] ||
       [ "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY" = "$GOVERNANCE_DELETE_ACCESS_KEY" ]; }; }; then
  echo "retired MinIO access key conflicts with an active identity" >&2
  exit 1
fi

for bucket in "$OBJECT_STORAGE_BUCKET" "$GOVERNANCE_RESUME_BUCKET" \
  "$GOVERNANCE_EXPORT_BUCKET" "$GOVERNANCE_LEDGER_BUCKET"
do
  case "$bucket" in
    *[!A-Za-z0-9.-]*) echo "invalid bucket name" >&2; exit 1 ;;
  esac
done

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mb --ignore-existing "local/$OBJECT_STORAGE_BUCKET"
mc mb --ignore-existing "local/$GOVERNANCE_RESUME_BUCKET"
mc mb --ignore-existing "local/$GOVERNANCE_EXPORT_BUCKET"
mc mb --ignore-existing "local/$GOVERNANCE_LEDGER_BUCKET"

printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation"],"Resource":["arn:aws:s3:::%s"]},{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":["arn:aws:s3:::%s/*"]}]}' \
  "$OBJECT_STORAGE_BUCKET" "$OBJECT_STORAGE_BUCKET" >/tmp/app-policy.json
printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetBucketLocation"],"Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s"]},{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s"],"Condition":{"StringLike":{"s3:prefix":["%s*","%s*"]}}},{"Effect":"Allow","Action":["s3:DeleteObject"],"Resource":["arn:aws:s3:::%s/%s*","arn:aws:s3:::%s/%s*"]}]}' \
  "$GOVERNANCE_RESUME_BUCKET" "$GOVERNANCE_EXPORT_BUCKET" \
  "$GOVERNANCE_RESUME_BUCKET" "$GOVERNANCE_EXPORT_BUCKET" \
  "$GOVERNANCE_RESUME_PREFIX" "$GOVERNANCE_EXPORT_PREFIX" \
  "$GOVERNANCE_RESUME_BUCKET" "$GOVERNANCE_RESUME_PREFIX" \
  "$GOVERNANCE_EXPORT_BUCKET" "$GOVERNANCE_EXPORT_PREFIX" >/tmp/delete-policy.json
printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetBucketLocation"],"Resource":["arn:aws:s3:::%s"]},{"Effect":"Allow","Action":["s3:ListBucket"],"Resource":["arn:aws:s3:::%s"],"Condition":{"StringLike":{"s3:prefix":["%s*"]}}},{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":["arn:aws:s3:::%s/%s*"]}]}' \
  "$GOVERNANCE_LEDGER_BUCKET" \
  "$GOVERNANCE_LEDGER_BUCKET" "$GOVERNANCE_LEDGER_PREFIX" \
  "$GOVERNANCE_LEDGER_BUCKET" "$GOVERNANCE_LEDGER_PREFIX" >/tmp/ledger-policy.json

mc admin user add local "$APP_OBJECT_STORAGE_ACCESS_KEY" "$APP_OBJECT_STORAGE_SECRET_KEY"
mc admin user add local "$GOVERNANCE_DELETE_ACCESS_KEY" "$GOVERNANCE_DELETE_SECRET_KEY"
mc admin user add local "$GOVERNANCE_LEDGER_ACCESS_KEY" "$GOVERNANCE_LEDGER_SECRET_KEY"
mc admin policy create local ux09-app /tmp/app-policy.json
mc admin policy create local ux09-governance-delete /tmp/delete-policy.json
mc admin policy create local ux09-governance-ledger /tmp/ledger-policy.json
mc admin policy attach local ux09-app --user "$APP_OBJECT_STORAGE_ACCESS_KEY"
mc admin policy attach local ux09-governance-delete --user "$GOVERNANCE_DELETE_ACCESS_KEY"
mc admin policy attach local ux09-governance-ledger --user "$GOVERNANCE_LEDGER_ACCESS_KEY"

remove_retired_user() {
  retired_access_key=$1
  if lookup_output=$(mc admin user info local "$retired_access_key" 2>&1); then
    mc admin user remove local "$retired_access_key"
    return
  fi
  case "$lookup_output" in
    *"The specified user does not exist"*|*"the specified user does not exist"*) return ;;
    *)
      echo "unable to verify retired MinIO user state" >&2
      exit 1
      ;;
  esac
}

if [ -n "${PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY:-}" ] &&
   [ "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY" != "$GOVERNANCE_DELETE_ACCESS_KEY" ]; then
  remove_retired_user "$PREVIOUS_GOVERNANCE_DELETE_ACCESS_KEY"
fi
if [ -n "${PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY:-}" ] &&
   [ "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY" != "$GOVERNANCE_LEDGER_ACCESS_KEY" ]; then
  remove_retired_user "$PREVIOUS_GOVERNANCE_LEDGER_ACCESS_KEY"
fi
