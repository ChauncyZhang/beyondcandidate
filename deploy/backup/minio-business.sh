#!/bin/sh
set -eu
# mc reads credentials from a copied CONFIG_FILE; object names are never logged.

operation=${1:?operation is required}
shift
config_file=
buckets=
output_path=
inventory_path=
snapshot_path=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config-file) config_file=${2:?}; shift 2 ;;
    --buckets) buckets=${2:?}; shift 2 ;;
    --output) output_path=${2:?}; shift 2 ;;
    --inventory) inventory_path=${2:?}; shift 2 ;;
    --snapshot) snapshot_path=${2:?}; shift 2 ;;
    *) printf '%s\n' "unknown business-client argument" >&2; exit 2 ;;
  esac
done
: "${config_file:?CONFIG_FILE is required}"
: "${MINIO_ALIAS:?MINIO_ALIAS must name the preconfigured least-privilege alias}"
python3 /opt/ux09-backup/backupctl.py validate-minio-alias "$MINIO_ALIAS"
python3 /opt/ux09-backup/backupctl.py validate-buckets "${buckets:?business buckets are required}"
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
mkdir -m 700 "$temporary/config" "$temporary/objects"
cp "$config_file" "$temporary/config/config.json"
chmod 600 "$temporary/config/config.json"

case "$operation" in
  snapshot)
    : "${buckets:?business buckets are required}" "${output_path:?output is required}" "${inventory_path:?inventory is required}"
    old_ifs=$IFS
    IFS=,
    for bucket in $buckets; do
      case "$bucket" in governance-ledger) printf '%s\n' "ledger bucket is forbidden" >&2; exit 1 ;; esac
      mkdir -p "$temporary/objects/$bucket"
      mc --config-dir "$temporary/config" mirror --overwrite --quiet "$MINIO_ALIAS/$bucket" "$temporary/objects/$bucket"
    done
    IFS=$old_ifs
    python3 /opt/ux09-backup/backupctl.py inventory "$temporary/objects" "$inventory_path"
    tar -cf "$output_path" -C "$temporary" objects
    ;;
  restore)
    : "${snapshot_path:?snapshot is required}" "${buckets:?business buckets are required}"
    python3 /opt/ux09-backup/backupctl.py safe-extract "$snapshot_path" "$temporary/extracted" "$buckets"
    for bucket_path in "$temporary"/extracted/objects/*; do
      [ -d "$bucket_path" ] || continue
      bucket=${bucket_path##*/}
      case "$bucket" in governance-ledger) printf '%s\n' "ledger bucket is forbidden" >&2; exit 1 ;; esac
      mc --config-dir "$temporary/config" mirror --overwrite --remove --quiet "$bucket_path" "$MINIO_ALIAS/$bucket"
    done
    ;;
  *) printf '%s\n' "unsupported business-client operation" >&2; exit 2 ;;
esac
