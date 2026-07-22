#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
template_directory="$script_directory/systemd"

deploy_root=${UX09_DEPLOY_ROOT:-}
environment_file=${BACKUP_SYSTEMD_ENV_FILE:-}
output_directory=${BACKUP_SYSTEMD_OUTPUT_DIR:-/etc/systemd/system}
on_calendar=${BACKUP_ON_CALENDAR:-'*-*-* 00,12:00:00'}
randomized_delay_seconds=${BACKUP_RANDOMIZED_DELAY_SEC:-900}
service_timeout_seconds=${BACKUP_SERVICE_TIMEOUT_SEC:-21600}
systemd_analyze=${SYSTEMD_ANALYZE_BIN:-systemd-analyze}

fail() {
    printf 'backup systemd render: %s\n' "$1" >&2
    exit 1
}

require_single_line() {
    name=$1
    value=$2
    without_line_breaks=$(printf '%s' "$value" | tr -d '\r\n')
    [ "$without_line_breaks" = "$value" ] || fail "$name must be a single line"
}

require_absolute_safe_path() {
    name=$1
    value=$2
    require_single_line "$name" "$value"
    case "$value" in
        /*) ;;
        *) fail "$name must be an absolute path" ;;
    esac
    if ! printf '%s\n' "$value" | grep -Eq '^/[A-Za-z0-9._/-]+$'; then
        fail "$name contains unsupported path characters"
    fi
}

require_integer_between() {
    name=$1
    value=$2
    minimum=$3
    maximum=$4
    case "$value" in
        ''|*[!0-9]*) fail "$name must be an integer" ;;
    esac
    if [ "$value" -lt "$minimum" ] || [ "$value" -gt "$maximum" ]; then
        fail "$name must be between $minimum and $maximum"
    fi
}

require_absolute_safe_path UX09_DEPLOY_ROOT "$deploy_root"
require_absolute_safe_path BACKUP_SYSTEMD_ENV_FILE "$environment_file"
require_absolute_safe_path BACKUP_SYSTEMD_OUTPUT_DIR "$output_directory"
require_single_line BACKUP_ON_CALENDAR "$on_calendar"
require_integer_between BACKUP_RANDOMIZED_DELAY_SEC "$randomized_delay_seconds" 0 86400
require_integer_between BACKUP_SERVICE_TIMEOUT_SEC "$service_timeout_seconds" 1 86400

[ -d "$deploy_root" ] || fail "UX09_DEPLOY_ROOT does not exist"
[ -f "$environment_file" ] || fail "BACKUP_SYSTEMD_ENV_FILE does not exist"
[ ! -L "$environment_file" ] || fail "BACKUP_SYSTEMD_ENV_FILE cannot be a symlink"
environment_stat=$(stat -c '%a %h' -- "$environment_file") \
    || fail "BACKUP_SYSTEMD_ENV_FILE metadata cannot be read"
set -- $environment_stat
environment_mode=$1
environment_links=$2
case "$environment_mode" in
    ''|*[!0-7]*) fail "BACKUP_SYSTEMD_ENV_FILE has an invalid permission mode" ;;
esac
if [ $((0$environment_mode & 077)) -ne 0 ]; then
    fail "BACKUP_SYSTEMD_ENV_FILE permissions must be 0600 or stricter"
fi
[ "$environment_links" -eq 1 ] \
    || fail "BACKUP_SYSTEMD_ENV_FILE must use a single inode without hardlinks"
while IFS= read -r environment_line || [ -n "$environment_line" ]; do
    case "$environment_line" in
        ''|'#'*) continue ;;
        *=*) environment_name=${environment_line%%=*} ;;
        *) fail "BACKUP_SYSTEMD_ENV_FILE contains a malformed entry" ;;
    esac
    case "$environment_name" in
        *PASSWORD*|*SECRET*|*TOKEN*|*KEY*)
            case "$environment_name" in
                *_FILE) ;;
                *) fail "secret values must be supplied through *_FILE variables" ;;
            esac
            ;;
    esac
done < "$environment_file"
case "$on_calendar" in
    *[!A-Za-z0-9' '*,:./_~*-]*) fail "BACKUP_ON_CALENDAR contains unsupported characters" ;;
esac

if ! command -v "$systemd_analyze" >/dev/null 2>&1; then
    fail "systemd-analyze is required"
fi
if ! "$systemd_analyze" calendar "$on_calendar" >/dev/null 2>&1; then
    fail "BACKUP_ON_CALENDAR is not a valid systemd calendar"
fi

mkdir -p "$output_directory"
[ ! -L "$output_directory" ] || fail "BACKUP_SYSTEMD_OUTPUT_DIR cannot be a symlink"
temporary_directory=$(mktemp -d "$output_directory/.ux09-backup-units.XXXXXX")
cleanup() {
    rm -rf "$temporary_directory"
}
trap cleanup EXIT HUP INT TERM

service="$temporary_directory/ux09-backup.service"
timer="$temporary_directory/ux09-backup.timer"
sed \
    -e "s|@DEPLOY_ROOT@|$deploy_root|g" \
    -e "s|@ENV_FILE@|$environment_file|g" \
    -e "s|@TIMEOUT_SECONDS@|$service_timeout_seconds|g" \
    "$template_directory/ux09-backup.service.in" > "$service"
sed \
    -e "s|@ON_CALENDAR@|$on_calendar|g" \
    -e "s|@RANDOMIZED_DELAY_SECONDS@|$randomized_delay_seconds|g" \
    "$template_directory/ux09-backup.timer.in" > "$timer"

chmod 0644 "$service" "$timer"
if ! "$systemd_analyze" verify "$service" "$timer" >/dev/null; then
    fail "rendered systemd units failed verification"
fi
mv "$service" "$output_directory/ux09-backup.service"
mv "$timer" "$output_directory/ux09-backup.timer"

printf '%s\n' 'backup systemd render: units rendered and verified'
