#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ISAAC_ROOT="${ISAAC_ROOT:-/home/w/Desktop/isaac-sim-standalone-6.0.1-linux-x86_64}"
USD_PATH="${1:-$PROJECT_DIR/simulation/output/offline/test_room.usda}"
exec "$ISAAC_ROOT/python.sh" "$PROJECT_DIR/simulation/isaac_bridge/ros2_pose_bridge.py" \
    --usd "$USD_PATH" --log "$PROJECT_DIR/simulation/output/online/pose_log.jsonl" --headless
