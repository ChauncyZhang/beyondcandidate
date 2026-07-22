#!/bin/sh
set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_directory/.." && pwd)
production_preflight="$script_directory/production-preflight.sh"
base_compose="$script_directory/compose.yaml"
production_overlay="$script_directory/compose.production.yaml"
observability_overlay="$script_directory/compose.observability.yaml"
release_compose_validator="$script_directory/release-compose-validator.py"
compose_env_file=${COMPOSE_ENV_FILE:-"$script_directory/.env"}
alert_rules="$script_directory/observability/alerts/ux09.rules.yml"
local_runbook="$script_directory/observability/runbook.md"
preflight_mode=${OBSERVABILITY_PREFLIGHT_MODE:-development}
canonical_runbook_url=https://github.com/ChauncyZhang/beyondcandidate/blob/main/deploy/observability/runbook.md
canonical_raw_runbook_url=https://raw.githubusercontent.com/ChauncyZhang/beyondcandidate/main/deploy/observability/runbook.md

runbook_anchors() {
    sed -n 's/^###[[:space:]]*//p' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's/[^a-z0-9 -]//g' -e 's/[[:space:]][[:space:]]*/-/g'
}

# This must remain the first validation layer. It enforces the supported
# Compose version and proves the base + production model independently.
sh "$production_preflight"

python_command=
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import json, sys' >/dev/null 2>&1; then
        python_command=$candidate
        break
    fi
done
if [ -z "$python_command" ]; then
    printf '%s\n' 'observability preflight: Python 3 is required for release model validation' >&2
    exit 1
fi

docker compose \
    --env-file "$compose_env_file" \
    -f "$base_compose" \
    -f "$production_overlay" \
    -f "$observability_overlay" \
    config --format json \
    | "$python_command" "$release_compose_validator"

case "$preflight_mode" in
    development)
        ;;
    production)
        command -v curl >/dev/null 2>&1 || {
            printf '%s\n' 'observability preflight: curl is required in production mode' >&2
            exit 1
        }
        command -v cmp >/dev/null 2>&1 || {
            printf '%s\n' 'observability preflight: cmp is required in production mode' >&2
            exit 1
        }
        remote_runbook=
        local_runbook_normalized=
        remote_runbook_normalized=
        cleanup_runbook_files() {
            [ -z "$remote_runbook" ] || rm -f "$remote_runbook"
            [ -z "$local_runbook_normalized" ] || rm -f "$local_runbook_normalized"
            [ -z "$remote_runbook_normalized" ] || rm -f "$remote_runbook_normalized"
        }
        trap cleanup_runbook_files EXIT HUP INT TERM
        remote_runbook=$(mktemp)
        local_runbook_normalized=$(mktemp)
        remote_runbook_normalized=$(mktemp)
        curl --fail --location --silent --show-error \
            --output /dev/null "$canonical_runbook_url"
        curl --fail --location --silent --show-error \
            "$canonical_raw_runbook_url" > "$remote_runbook"
        for runbook_url in $(
            sed -n 's/^[[:space:]]*runbook_url:[[:space:]]*//p' "$alert_rules"
        ); do
            case "$runbook_url" in
                "$canonical_runbook_url"\#*)
                    ;;
                *)
                    printf 'observability preflight: non-canonical runbook URL: %s\n' \
                        "$runbook_url" >&2
                    exit 1
                    ;;
            esac
            anchor=${runbook_url##*#}
            if ! runbook_anchors "$local_runbook" | grep -F -x "$anchor" >/dev/null; then
                printf 'observability preflight: local runbook anchor is missing: %s\n' \
                    "$anchor" >&2
                exit 1
            fi
            if ! runbook_anchors "$remote_runbook" | grep -F -x "$anchor" >/dev/null; then
                printf 'observability preflight: published runbook anchor is missing: %s\n' \
                    "$anchor" >&2
                exit 1
            fi
        done
        sed 's/\r$//' "$local_runbook" > "$local_runbook_normalized"
        sed 's/\r$//' "$remote_runbook" > "$remote_runbook_normalized"
        if ! cmp -s "$local_runbook_normalized" "$remote_runbook_normalized"; then
            printf '%s\n' \
                'observability preflight: published runbook content differs from local runbook' >&2
            exit 1
        fi
        cleanup_runbook_files
        trap - EXIT HUP INT TERM
        ;;
    *)
        printf 'observability preflight: unsupported mode: %s\n' "$preflight_mode" >&2
        exit 1
        ;;
esac

printf '%s\n' 'observability preflight: merged three-file Compose model is valid'
