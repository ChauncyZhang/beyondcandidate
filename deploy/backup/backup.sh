#!/bin/sh
set -eu
# Credentials are mounted through PGPASSFILE and *_CONFIG_FILE variables.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/backupctl.py" backup
