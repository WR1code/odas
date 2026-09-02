#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PORTAUDIO_LIB="$PROJECT_DIR/.deps/libportaudio2/usr/lib/x86_64-linux-gnu"

if [[ ! -x "$PYTHON" ]]; then
    echo "错误：找不到项目 Python 环境：$PYTHON" >&2
    echo "请在项目根目录创建 .venv 并安装 requirements.txt。" >&2
    exit 1
fi
if [[ ! -e "$PORTAUDIO_LIB/libportaudio.so.2" ]]; then
    echo "错误：找不到项目本地 PortAudio：$PORTAUDIO_LIB/libportaudio.so.2" >&2
    exit 1
fi

export LD_LIBRARY_PATH="$PORTAUDIO_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LIBRARY_PATH="$PORTAUDIO_LIB${LIBRARY_PATH:+:$LIBRARY_PATH}"
exec "$PYTHON" "$PROJECT_DIR/rir_capture/capture_rir.py" "$@"
