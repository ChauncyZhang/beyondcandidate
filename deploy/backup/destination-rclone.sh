#!/bin/sh
set -eu
# rclone reads credentials only from the supplied CONFIG_FILE; values never enter argv.

operation=${1:?operation is required}
shift
config_file=
destination=
run_id=
source_path=
output_path=
signing_key_file=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config-file) config_file=${2:?}; shift 2 ;;
    --destination) destination=${2:?}; shift 2 ;;
    --run-id) run_id=${2:?}; shift 2 ;;
    --source) source_path=${2:?}; shift 2 ;;
    --output) output_path=${2:?}; shift 2 ;;
    --signing-key-file) signing_key_file=${2:?}; shift 2 ;;
    *) printf '%s\n' "unknown destination-client argument" >&2; exit 2 ;;
  esac
done
: "${config_file:?CONFIG_FILE is required}"
: "${destination:?destination is required}"
export RCLONE_CONFIG=$config_file
case "$operation" in
  stage-group)
    printf '%s\n' "rclone is not an atomic publisher; an external destination lease/publisher is required" >&2
    exit 78
    ;;
  publish-group)
    printf '%s\n' "rclone is not an atomic publisher; an external destination lease/publisher is required" >&2
    exit 78
    ;;
  abort-group)
    printf '%s\n' "rclone is not an atomic publisher; there is no foundation remote staging state" >&2
    exit 78
    ;;
  fetch-complete-group)
    : "${run_id:?run id is required}" "${output_path:?output is required}"
    python3 /opt/ux09-backup/backupctl.py validate-run-id "$run_id"
    complete=${destination%/}/${run_id}
    rclone lsf "$complete/COMPLETE" --files-only --quiet | grep -q '^COMPLETE$'
    rclone copy "$complete" "$output_path" --quiet
    ;;
  delete-complete-group)
    : "${run_id:?run id is required}"
    python3 /opt/ux09-backup/backupctl.py validate-run-id "$run_id"
    complete=${destination%/}/${run_id}
    rclone lsf "$complete/COMPLETE" --files-only --quiet | grep -q '^COMPLETE$'
    rclone purge "$complete" --quiet
    ;;
  catalog)
    : "${output_path:?output is required}" "${signing_key_file:?signing key file is required}"
    temporary=$(mktemp -d)
    trap 'rm -rf "$temporary"' EXIT HUP INT TERM
    # Prune catalog validity is based on downloaded payload hashes, inventory,
    # and pg_restore listing, not only on the presence of a marker.
    rclone copy "$destination" "$temporary" --exclude '.incomplete/**' --quiet
    python3 /opt/ux09-backup/backupctl.py catalog-local "$temporary" "$output_path" "$signing_key_file"
    ;;
  *) printf '%s\n' "unsupported destination-client operation" >&2; exit 2 ;;
esac
