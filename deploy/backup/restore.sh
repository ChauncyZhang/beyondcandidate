#!/bin/sh
set -eu
# Restore identities are mounted through PGPASSFILE and *_CONFIG_FILE variables.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$SCRIPT_DIR/backupctl.py" guard-disposable
exec python3 "$SCRIPT_DIR/backupctl.py" restore
