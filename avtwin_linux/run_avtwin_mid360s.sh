#!/usr/bin/env bash
# AV-Twin Linux controller and optional MID-360S/SLAM integration entry point.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
MID360S_WS="$PROJECT_DIR/.deps/mid360s_ws"
LIVOX_SDK_INSTALL="$PROJECT_DIR/.deps/livox-sdk2-install"
MID360S_CONFIG="$PROJECT_DIR/avtwin_linux/mid360s_config.json"
MID360S_RUNTIME_CONFIG="$RUNTIME_DIR/mid360s_config.auto.json"
FASTLIO_WS="$PROJECT_DIR/.deps/fastlio2_ws"
FASTLIO_PCL_PREFIX="$PROJECT_DIR/.deps/ros-jazzy-pcl-ros/opt/ros/jazzy"
FASTLIO_CONFIG="$PROJECT_DIR/avtwin_linux/fastlio_mid360s.yaml"
START_MID360S_DRIVER=0
START_FAST_LIO=0
START_ROS_POSE_BRIDGE=0
FASTLIO_RVIZ=0
MID360S_CHECK=0
MID360S_AUTO_IP=1
POSE_TOPIC="/Odometry"
POSE_MESSAGE_TYPE="auto"
APP_ARGS=()

usage_mid360s() {
    cat <<'EOF'
MID-360S 集成参数（其余参数原样交给 AV-Twin）：
  --mid360s                    启动 Livox 驱动、FAST-LIO 和位姿桥；GUI 未发现雷达时自动转手动坐标
  --mid360s-driver-only        只启动 Livox 驱动，不启动位姿桥
  --fastlio                    启动 FAST-LIO 和位姿桥（驱动已在外部运行）
  --fastlio-config PATH        FAST-LIO YAML 配置
  --fastlio-rviz               同时启动 FAST-LIO RViz 点云可视化
  --ros-pose-bridge            只启动 ROS 位姿桥（驱动/SLAM 已在外部运行）
  --mid360s-config PATH        Livox 网络配置 JSON
  --mid360s-no-auto-ip         禁用设备发现，固定使用 JSON 中的雷达 IP
  --lidar-pose-topic TOPIC     SLAM 位姿 topic（默认 /Odometry）
  --lidar-pose-type TYPE       auto|odometry|pose_stamped|pose_with_covariance_stamped
  --mid360s-check              检查驱动、SDK、ROS 和网络配置
EOF
}

while (($#)); do
    case "$1" in
        --mid360s)
            START_MID360S_DRIVER=1
            START_FAST_LIO=1
            START_ROS_POSE_BRIDGE=1
            shift
            ;;
        --mid360s-driver-only)
            START_MID360S_DRIVER=1
            START_ROS_POSE_BRIDGE=0
            shift
            ;;
        --fastlio)
            START_FAST_LIO=1
            START_ROS_POSE_BRIDGE=1
            shift
            ;;
        --fastlio-config)
            [[ $# -ge 2 ]] || { echo "错误：--fastlio-config 缺少 PATH" >&2; exit 2; }
            FASTLIO_CONFIG="$2"
            shift 2
            ;;
        --fastlio-config=*)
            FASTLIO_CONFIG="${1#*=}"
            shift
            ;;
        --fastlio-rviz)
            FASTLIO_RVIZ=1
            shift
            ;;
        --ros-pose-bridge)
            START_ROS_POSE_BRIDGE=1
            shift
            ;;
        --mid360s-config)
            [[ $# -ge 2 ]] || { echo "错误：--mid360s-config 缺少 PATH" >&2; exit 2; }
            MID360S_CONFIG="$2"
            shift 2
            ;;
        --mid360s-config=*)
            MID360S_CONFIG="${1#*=}"
            shift
            ;;
        --mid360s-no-auto-ip)
            MID360S_AUTO_IP=0
            shift
            ;;
        --lidar-pose-topic)
            [[ $# -ge 2 ]] || { echo "错误：--lidar-pose-topic 缺少 TOPIC" >&2; exit 2; }
            POSE_TOPIC="$2"
            shift 2
            ;;
        --lidar-pose-topic=*)
            POSE_TOPIC="${1#*=}"
            shift
            ;;
        --lidar-pose-type)
            [[ $# -ge 2 ]] || { echo "错误：--lidar-pose-type 缺少 TYPE" >&2; exit 2; }
            POSE_MESSAGE_TYPE="$2"
            shift 2
            ;;
        --lidar-pose-type=*)
            POSE_MESSAGE_TYPE="${1#*=}"
            shift
            ;;
        --mid360s-check)
            MID360S_CHECK=1
            shift
            ;;
        --mid360s-help)
            usage_mid360s
            exit 0
            ;;
        *)
            APP_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ ! -x "$PYTHON" ]]; then
    echo "错误：找不到 $PYTHON，请先运行项目 install_dependencies.sh" >&2
    exit 1
fi

GUI_REQUESTED=0
for argument in "${APP_ARGS[@]}"; do
    if [[ "$argument" == "--gui" ]]; then
        GUI_REQUESTED=1
        break
    fi
done

fallback_to_manual_position() {
    START_MID360S_DRIVER=0
    START_FAST_LIO=0
    START_ROS_POSE_BRIDGE=0
    FASTLIO_RVIZ=0
    APP_ARGS+=(--pose-source manual)
    echo "警告：未检测到可用 MID-360S，声学程序仍将启动。" >&2
    echo "定位模式已自动切换为 manual；请在 GUI 输入当前 x,y,z 后点击“应用手动坐标”。" >&2
}

prepare_mid360s_auto_config() {
    local base_config="$MID360S_CONFIG"
    local configured_host
    configured_host="$($PYTHON - "$base_config" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print(value["Mid360s"]["host_net_info"][0]["host_ip"])
PY
    )" || { echo "错误：无法读取 MID-360S host_ip：$base_config" >&2; return 2; }

    local active_interface=""
    active_interface="$(ip -o -4 address show 2>/dev/null \
        | awk -v host="$configured_host" '$4 ~ ("^" host "/") {print $2; exit}')"
    if [[ -z "$active_interface" ]] && command -v nmcli >/dev/null 2>&1; then
        local saved_profile=""
        local profile_name
        while IFS= read -r profile_name; do
            if nmcli -g ipv4.addresses connection show "$profile_name" 2>/dev/null \
                | grep -Fq "$configured_host/"; then
                saved_profile="$profile_name"
                break
            fi
        done < <(nmcli -t --escape no -f NAME connection show 2>/dev/null)
        if [[ -n "$saved_profile" ]]; then
            echo "自动激活雷达网卡配置：$saved_profile" >&2
            nmcli connection up "$saved_profile" >/dev/null
            active_interface="$(ip -o -4 address show 2>/dev/null \
                | awk -v host="$configured_host" '$4 ~ ("^" host "/") {print $2; exit}')"
        fi
    fi
    if [[ -z "$active_interface" ]]; then
        echo "错误：没有网卡使用 MID-360S 主机地址 $configured_host" >&2
        return 2
    fi
    if [[ -r "/sys/class/net/$active_interface/carrier" ]] \
        && [[ "$(<"/sys/class/net/$active_interface/carrier")" != "1" ]]; then
        if ((GUI_REQUESTED && !MID360S_CHECK)); then
            echo "未检测到 MID-360S：$active_interface 物理链路未连接（carrier=0）" >&2
        else
            echo "错误：$active_interface 物理链路未连接（carrier=0）" >&2
        fi
        return 2
    fi

    echo "监听 $active_interface 上的 MID-360S 发现广播..." >&2
    local detected_ip
    if ! detected_ip="$($PYTHON "$PROJECT_DIR/avtwin_linux/mid360s_discovery.py" \
        --host-ip "$configured_host" --timeout 6 \
        --base-config "$base_config" --output-config "$MID360S_RUNTIME_CONFIG")"; then
        return 2
    fi

    local lidar_route=""
    lidar_route="$(ip route get "$detected_ip" 2>/dev/null || true)"
    if [[ "$lidar_route" != *" dev $active_interface "* ]] \
        || [[ "$lidar_route" != *" src $configured_host "* ]]; then
        if ! command -v nmcli >/dev/null 2>&1; then
            echo "错误：$detected_ip 未路由到 $active_interface，且找不到 nmcli 自动修正。" >&2
            return 2
        fi
        local active_profile
        active_profile="$(nmcli -g GENERAL.CONNECTION device show "$active_interface" 2>/dev/null)"
        if [[ -z "$active_profile" || "$active_profile" == "--" ]]; then
            echo "错误：找不到 $active_interface 对应的 NetworkManager 配置。" >&2
            return 2
        fi
        if ! nmcli -g ipv4.routes connection show "$active_profile" 2>/dev/null \
            | grep -Fq "$detected_ip/32"; then
            echo "自动添加雷达专用路由：$detected_ip/32 -> $active_interface" >&2
            nmcli connection modify "$active_profile" +ipv4.routes "$detected_ip/32"
            nmcli connection up "$active_profile" >/dev/null
        fi
    fi
    MID360S_CONFIG="$MID360S_RUNTIME_CONFIG"
}

if ((MID360S_AUTO_IP)) && ((START_MID360S_DRIVER || MID360S_CHECK)); then
    if ! prepare_mid360s_auto_config; then
        if ((START_MID360S_DRIVER && GUI_REQUESTED && !MID360S_CHECK)); then
            fallback_to_manual_position
        else
            exit 2
        fi
    fi
fi

check_mid360s() {
    local failed=0
    echo "MID-360S 环境检查："
    if [[ -r "$ROS_SETUP" ]]; then
        echo "  PASS  ROS 2 Jazzy: $ROS_SETUP"
    else
        echo "  FAIL  缺少 ROS 2 Jazzy: $ROS_SETUP"
        failed=1
    fi
    if [[ -r "$MID360S_WS/install/setup.bash" ]]; then
        echo "  PASS  livox_ros_driver2 workspace"
    else
        echo "  FAIL  驱动未编译；运行 ./avtwin_linux/install_mid360s_driver.sh"
        failed=1
    fi
    if [[ -r "$LIVOX_SDK_INSTALL/lib/liblivox_lidar_sdk_shared.so" ]]; then
        echo "  PASS  Livox SDK shared library"
    else
        echo "  FAIL  缺少 Livox SDK shared library"
        failed=1
    fi
    if [[ -r "$FASTLIO_WS/install/setup.bash" ]]; then
        echo "  PASS  FAST-LIO ROS 2 workspace"
    else
        echo "  WARN  FAST-LIO 未安装；运行 ./avtwin_linux/install_fastlio2.sh"
    fi
    if [[ -r "$MID360S_CONFIG" ]]; then
        local network_values
        if network_values="$($PYTHON - "$MID360S_CONFIG" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
host = value["Mid360s"]["host_net_info"][0]["host_ip"]
lidar = value["lidar_configs"][0]["ip"]
print(host, lidar)
PY
        )"; then
            echo "  PASS  配置：$MID360S_CONFIG（host/lidar: $network_values）"
            local configured_host="${network_values%% *}"
            local configured_lidar="${network_values##* }"
            local saved_profile=""
            local profile_name
            if command -v nmcli >/dev/null 2>&1; then
                while IFS= read -r profile_name; do
                    if nmcli -g ipv4.addresses connection show "$profile_name" 2>/dev/null \
                        | grep -Fq "$configured_host/"; then
                        saved_profile="$profile_name"
                        break
                    fi
                done < <(nmcli -t --escape no -f NAME connection show 2>/dev/null)
            fi
            local active_interface=""
            if command -v ip >/dev/null 2>&1; then
                active_interface="$(ip -o -4 address show 2>/dev/null \
                    | awk -v host="$configured_host" '$4 ~ ("^" host "/") {print $2; exit}')"
            fi
            if [[ -n "$active_interface" ]]; then
                echo "  PASS  网卡已配置 $configured_host（$active_interface）"
                if [[ -r "/sys/class/net/$active_interface/carrier" ]] \
                    && [[ "$(<"/sys/class/net/$active_interface/carrier")" != "1" ]]; then
                    echo "  FAIL  $active_interface 无物理链路（carrier=0）"
                    failed=1
                else
                    echo "  PASS  $active_interface 物理链路已连接"
                fi
                local lidar_route=""
                lidar_route="$(ip route get "$configured_lidar" 2>/dev/null || true)"
                if [[ "$lidar_route" == *" dev $active_interface "* ]] \
                    && [[ "$lidar_route" == *" src $configured_host "* ]]; then
                    echo "  PASS  雷达路由：$configured_lidar -> $active_interface"
                else
                    echo "  FAIL  $configured_lidar 未经 $active_interface/$configured_host 路由"
                    failed=1
                fi
            elif [[ -n "$saved_profile" ]]; then
                echo "  PASS  NetworkManager 已保存 $configured_host 配置（$saved_profile）"
                echo "  FAIL  有线口尚未激活；请检查雷达供电和网线"
                failed=1
            else
                echo "  FAIL  当前网卡未发现 $configured_host；连接雷达的网口需设为 $configured_host/24"
                failed=1
            fi
        else
            echo "  FAIL  配置 JSON 无效：$MID360S_CONFIG"
            failed=1
        fi
    else
        echo "  FAIL  找不到配置：$MID360S_CONFIG"
        failed=1
    fi
    return "$failed"
}

if ((MID360S_CHECK)); then
    check_mid360s
    exit $?
fi

if ((START_MID360S_DRIVER)) && ! check_mid360s; then
    if ((GUI_REQUESTED)); then
        fallback_to_manual_position
    else
        exit 2
    fi
fi
PORTAUDIO_LIBRARY="$(find "$PROJECT_DIR/.deps/libportaudio2" -type f -name 'libportaudio.so.2*' -print -quit 2>/dev/null || true)"
if [[ -n "$PORTAUDIO_LIBRARY" ]]; then
    PORTAUDIO_LIB="$(dirname "$PORTAUDIO_LIBRARY")"
    export LD_LIBRARY_PATH="$PORTAUDIO_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LIBRARY_PATH="$PORTAUDIO_LIB${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi

CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME:-$RUNTIME_DIR}/.config}"
if ! mkdir -p "$CONFIG_HOME" 2>/dev/null || [[ ! -w "$CONFIG_HOME" ]]; then
    CONFIG_HOME="$RUNTIME_DIR/config"
    mkdir -p "$CONFIG_HOME"
fi
export XDG_CONFIG_HOME="$CONFIG_HOME"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$RUNTIME_DIR/matplotlib}"
export ROS_LOG_DIR="${ROS_LOG_DIR:-$RUNTIME_DIR/ros-log}"
mkdir -p "$MPLCONFIGDIR" "$ROS_LOG_DIR"

POSE_UDP_PORT=5006
POSE_SOURCE_EXPLICIT=0
for ((index=0; index < ${#APP_ARGS[@]}; index++)); do
    case "${APP_ARGS[$index]}" in
        --pose-udp-port)
            if ((index + 1 < ${#APP_ARGS[@]})); then
                POSE_UDP_PORT="${APP_ARGS[$((index + 1))]}"
            fi
            ;;
        --pose-udp-port=*) POSE_UDP_PORT="${APP_ARGS[$index]#*=}" ;;
        --pose-source|--pose-source=*) POSE_SOURCE_EXPLICIT=1 ;;
    esac
done
if ((START_ROS_POSE_BRIDGE && !POSE_SOURCE_EXPLICIT)); then
    APP_ARGS+=(--pose-source udp)
fi

CHILD_PIDS=()
cleanup_children() {
    local pid
    trap - EXIT INT TERM
    for pid in "${CHILD_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            # Each ROS command is started in its own session below. Killing the
            # process group also stops ros2 launch descendants instead of
            # leaving Livox/FAST-LIO nodes alive after the GUI closes.
            kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "${CHILD_PIDS[@]}"; do
        wait "$pid" 2>/dev/null || true
    done
}

if ((START_MID360S_DRIVER || START_FAST_LIO || START_ROS_POSE_BRIDGE)); then
    if ((!START_MID360S_DRIVER)) && [[ ! -r "$ROS_SETUP" ]]; then
        echo "错误：ROS 位姿桥需要 ROS 2 Jazzy：$ROS_SETUP" >&2
        exit 2
    fi
    set +u
    source "$ROS_SETUP"
    if ((START_MID360S_DRIVER || START_FAST_LIO)); then
        source "$MID360S_WS/install/setup.bash"
    fi
    if ((START_MID360S_DRIVER)); then
        export LD_LIBRARY_PATH="$LIVOX_SDK_INSTALL/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    if ((START_FAST_LIO)); then
        if [[ ! -r "$FASTLIO_WS/install/setup.bash" ]]; then
            echo "错误：FAST-LIO 未安装；运行 ./avtwin_linux/install_fastlio2.sh" >&2
            exit 2
        fi
        if [[ ! -r "$FASTLIO_CONFIG" ]]; then
            echo "错误：找不到 FAST-LIO 配置：$FASTLIO_CONFIG" >&2
            exit 2
        fi
        if [[ -d "$FASTLIO_PCL_PREFIX/lib" ]]; then
            export LD_LIBRARY_PATH="$FASTLIO_PCL_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        fi
        source "$FASTLIO_WS/install/setup.bash"
    fi
    set -u
    trap cleanup_children EXIT INT TERM
fi

if ((START_MID360S_DRIVER)); then
    echo "启动 MID-360S 驱动：点云 /livox/lidar，IMU /livox/imu"
    setsid ros2 run livox_ros_driver2 livox_ros_driver2_node --ros-args \
        -p xfer_format:=1 -p multi_topic:=0 -p data_src:=0 \
        -p publish_freq:=10.0 -p output_data_type:=0 -p frame_id:=livox_frame \
        -p user_config_path:="$MID360S_CONFIG" &
    CHILD_PIDS+=("$!")
fi

if ((START_FAST_LIO)); then
    FASTLIO_RVIZ_VALUE=false
    if ((FASTLIO_RVIZ)); then
        FASTLIO_RVIZ_VALUE=true
    fi
    echo "启动 FAST-LIO：/livox/lidar + /livox/imu -> /Odometry"
    setsid ros2 launch fast_lio mapping.launch.py \
        config_path:="$(dirname "$FASTLIO_CONFIG")" \
        config_file:="$(basename "$FASTLIO_CONFIG")" \
        rviz:="$FASTLIO_RVIZ_VALUE" &
    CHILD_PIDS+=("$!")
fi

if ((START_ROS_POSE_BRIDGE)); then
    echo "启动 SLAM 位姿桥：$POSE_TOPIC -> AV-Twin UDP 127.0.0.1:$POSE_UDP_PORT"
    setsid /usr/bin/python3 "$PROJECT_DIR/avtwin_linux/ros_pose_bridge.py" \
        --topic "$POSE_TOPIC" --message-type "$POSE_MESSAGE_TYPE" \
        --host 127.0.0.1 --port "$POSE_UDP_PORT" &
    CHILD_PIDS+=("$!")
fi

if ((${#CHILD_PIDS[@]})); then
    sleep 1
    for pid in "${CHILD_PIDS[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "错误：MID-360S 子进程启动后立即退出，请检查上方日志和网络配置。" >&2
            exit 2
        fi
    done
fi

wait_for_topic() {
    local topic="$1"
    local timeout_seconds="$2"
    local start_seconds="$SECONDS"
    while ((SECONDS - start_seconds < timeout_seconds)); do
        if ros2 topic list 2>/dev/null | grep -Fxq "$topic"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

if ((START_MID360S_DRIVER)) && ! wait_for_topic /livox/lidar 10; then
    echo "错误：10 秒内未收到 /livox/lidar；请运行 --mid360s-check 检查物理链路和路由。" >&2
    exit 2
fi
if ((START_FAST_LIO)) && ! wait_for_topic "$POSE_TOPIC" 30; then
    echo "错误：30 秒内未收到 FAST-LIO 位姿 $POSE_TOPIC；请保持雷达静止完成 IMU 初始化并检查上方日志。" >&2
    exit 2
fi

"$PYTHON" "$PROJECT_DIR/avtwin_linux/main.py" "${APP_ARGS[@]}"
