#!/bin/sh
set -eu
# Drill secrets remain in mounted *_FILE / *_CONFIG_FILE inputs.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$SCRIPT_DIR/backupctl.py" preflight-drill
: "${B2B3_CLI_COMMAND:?Set the released real B2B3 CLI executable path}"
: "${B2B3_WORKER_COMMAND:?Set the released real B2B3 Worker executable path}"
exec python3 "$SCRIPT_DIR/backupctl.py" drill
