#!/usr/bin/env bash
set -euo pipefail
python -m api.db_init
exec "$@"
