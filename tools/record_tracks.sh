#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SECONDS_TO_RECORD="${1:-10}"
if ! [[ "$SECONDS_TO_RECORD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "用法：$0 [秒数]" >&2
    exit 2
fi
mkdir -p "$PROJECT_DIR/logs"
OUTPUT="$PROJECT_DIR/logs/tracks_$(date +%Y%m%d_%H%M%S).json"
echo "录制 $SECONDS_TO_RECORD 秒到：$OUTPUT"
set +e
timeout --signal=INT --kill-after=2s "${SECONDS_TO_RECORD}s" \
    stdbuf -oL "$PROJECT_DIR/build/bin/odaslive" -c "$PROJECT_DIR/config/odaslive/uma8_v2_visualizer.cfg" \
    >"$OUTPUT" 2>&1
status=$?
set -e
if [[ $status -ne 0 && $status -ne 124 && $status -ne 130 ]]; then
    echo "ODAS 录制异常退出，状态码：$status" >&2
    exit "$status"
fi
echo "录制完成：$OUTPUT"
