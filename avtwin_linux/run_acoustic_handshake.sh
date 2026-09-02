#!/usr/bin/env bash
# Primary one-command launcher for the AV-Twin acoustic handshake application.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_avtwin_mid360s.sh" "$@"
