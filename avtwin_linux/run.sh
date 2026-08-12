#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PORTAUDIO_LIB="$PROJECT_DIR/.deps/libportaudio2/usr/lib/x86_64-linux-gnu"

if [[ ! -x "$PYTHON" ]]; then
    echo "错误：找不到 $PYTHON，请先运行项目 install_dependencies.sh" >&2
    exit 1
fi
if [[ -e "$PORTAUDIO_LIB/libportaudio.so.2" ]]; then
    export LD_LIBRARY_PATH="$PORTAUDIO_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LIBRARY_PATH="$PORTAUDIO_LIB${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi
exec "$PYTHON" "$PROJECT_DIR/avtwin_linux/main.py" "$@"
