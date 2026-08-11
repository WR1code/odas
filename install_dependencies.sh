#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -r /etc/os-release ]] || ! grep -q '^ID=ubuntu' /etc/os-release; then
    echo "警告：此安装脚本按 Ubuntu 编写，当前系统可能不是 Ubuntu。" >&2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：找不到 python3。请先安装 Python 3。" >&2
    exit 1
fi

echo "系统组件（如缺失）：sudo apt install python3-tk python3-pip"
echo "正在使用当前 python3：$(command -v python3)"
python3 -m pip install -r "$PROJECT_DIR/requirements.txt"
python3 - <<'PY'
import tkinter
import matplotlib
print(f"依赖检查成功：matplotlib {matplotlib.__version__}，tkinter 可用")
PY
