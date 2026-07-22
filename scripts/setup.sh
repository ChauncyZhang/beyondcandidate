#!/bin/sh
set -eu

command -v python3 >/dev/null 2>&1 || {
    printf '%s\n' '未找到 Python 3。请先安装 Python 3.10 或更高版本。' >&2
    exit 1
}
exec python3 "$(dirname "$0")/community_setup.py" "$@"
