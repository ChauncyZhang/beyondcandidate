#!/bin/sh
set -eu

minimum_compose_version=2.24.4

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_root=$(CDPATH= cd -- "$script_directory/.." && pwd)
base_compose="$repository_root/deploy/compose.yaml"
production_overlay="$repository_root/deploy/compose.production.yaml"
release_compose_validator="$repository_root/deploy/release-compose-validator.py"
compose_env_file=${COMPOSE_ENV_FILE:-"$repository_root/deploy/.env"}

raw_compose_version=$(docker compose version --short 2>/dev/null) || {
    printf '%s\n' 'production preflight: unable to read docker compose version --short' >&2
    exit 1
}
compose_version=$(printf '%s' "$raw_compose_version" | sed -e 's/^v//' -e 's/[-+].*$//')

if ! awk -v current="$compose_version" -v minimum="$minimum_compose_version" '
    function valid(version, parts, count, position) {
        count = split(version, parts, ".")
        if (count != 3) return 0
        for (position = 1; position <= 3; position++) {
            if (parts[position] !~ /^[0-9]+$/) return 0
        }
        return 1
    }
    BEGIN {
        if (!valid(current, current_parts) || !valid(minimum, minimum_parts)) exit 2
        for (position = 1; position <= 3; position++) {
            if ((current_parts[position] + 0) > (minimum_parts[position] + 0)) exit 0
            if ((current_parts[position] + 0) < (minimum_parts[position] + 0)) exit 1
        }
        exit 0
    }
'; then
    printf 'production preflight: Docker Compose %s or newer is required; found %s\n' \
        "$minimum_compose_version" "$raw_compose_version" >&2
    exit 1
fi

if [ ! -f "$compose_env_file" ]; then
    printf 'production preflight: Compose environment file not found: %s\n' \
        "$compose_env_file" >&2
    exit 1
fi

python_command=
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 \
        && "$candidate" -c 'import json, sys' >/dev/null 2>&1; then
        python_command=$candidate
        break
    fi
done
if [ -z "$python_command" ]; then
    printf '%s\n' 'production preflight: Python 3 is required for release model validation' >&2
    exit 1
fi

# The production model is always the base plus the production overlay. Never
# validate or deploy the base file alone because it publishes development HTTP.
docker compose \
    --env-file "$compose_env_file" \
    -f "$base_compose" \
    -f "$production_overlay" \
    config --format json \
    | "$python_command" "$release_compose_validator"

printf '%s\n' 'production preflight: merged production Compose model is valid'
