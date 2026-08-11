#!/usr/bin/env bash
set -euo pipefail

DEVICE="hw:CARD=SPK,DEV=0"
echo "=== ALSA 录音设备 ==="
arecord -l
echo
echo "=== $DEVICE 硬件参数（最多等待 3 秒） ==="
set +e
timeout 3s arecord --dump-hw-params -D "$DEVICE" -f S32_LE -r 48000 -c 8 -d 1 /dev/null
status=$?
set -e
if [[ $status -ne 0 && $status -ne 124 ]]; then
    echo "无法打开 $DEVICE；设备编号可能变化或正被占用。" >&2
fi

card_number="$(arecord -l | sed -nE 's/^card ([0-9]+): SPK .*device 0:.*/\1/p' | head -n 1)"
if [[ -n "$card_number" ]]; then
    echo "匹配：UMA-8 当前为 card $card_number / device 0。"
    echo "配置使用稳定名称 $DEVICE，不依赖 card 编号。"
else
    echo "未发现 ALSA 名称为 SPK、device 0 的 UMA-8。" >&2
    exit 2
fi
