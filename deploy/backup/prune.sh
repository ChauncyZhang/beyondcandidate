#!/bin/sh
set -eu
# The deletion identity is mounted only through BACKUP_PRUNE_CONFIG_FILE.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/backupctl.py" prune
