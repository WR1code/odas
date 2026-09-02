#!/usr/bin/env bash
# UMA-8 / ODAS direction-of-arrival visualizer entry point.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ODAS_BIN="$PROJECT_DIR/build/bin/odaslive"
ODAS_CONFIG="$PROJECT_DIR/config/odaslive/uma8_v2_visualizer.cfg"
cd "$PROJECT_DIR"

if [[ ! -x "$ODAS_BIN" ]]; then
    echo "错误：找不到可执行的 odaslive：$ODAS_BIN" >&2
    exit 1
fi
if [[ ! -f "$ODAS_CONFIG" ]]; then
    echo "错误：找不到配置文件：$ODAS_CONFIG" >&2
    exit 1
fi
if ! python3 -c 'import tkinter, matplotlib, uma8_visualizer.main' >/dev/null 2>&1; then
    echo "错误：Python 模块不可导入。请运行 ./install_dependencies.sh" >&2
    exit 1
fi

LAUNCHES_ODAS=1
for arg in "$@"; do
    if [[ "$arg" == "--no-launch-odas" || "$arg" == "--input-file" || "$arg" == --input-file=* ]]; then
        LAUNCHES_ODAS=0
        break
    fi
done

if [[ "$LAUNCHES_ODAS" -eq 1 ]]; then
    UMA8_CARD="$(arecord -l 2>/dev/null | sed -nE 's/^card ([0-9]+): SPK .*device 0:.*/\1/p' | head -n 1)"
    if [[ -z "$UMA8_CARD" ]]; then
        echo "错误：未发现 ALSA 设备 SPK/device 0；请确认 UMA-8 已连接并运行 ./tools/check_audio_device.sh。" >&2
        exit 1
    fi

    UMA8_CAPTURE_DEVICE="/dev/snd/pcmC${UMA8_CARD}D0c"
    if command -v fuser >/dev/null 2>&1 && [[ -e "$UMA8_CAPTURE_DEVICE" ]] \
        && fuser "$UMA8_CAPTURE_DEVICE" >/dev/null 2>&1; then
        echo "警告：UMA-8 采集设备正在被进程使用；请运行 fuser -v $UMA8_CAPTURE_DEVICE 检查。" >&2
    fi
fi

exec python3 -m uma8_visualizer.main "$@"
