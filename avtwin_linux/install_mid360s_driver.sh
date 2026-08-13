#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS_DIR="$PROJECT_DIR/.deps"
SDK_SOURCE="$DEPS_DIR/Livox-SDK2"
SDK_INSTALL="$DEPS_DIR/livox-sdk2-install"
ROS_WS="$DEPS_DIR/mid360s_ws"
DRIVER_SOURCE="$ROS_WS/src/livox_ros_driver2"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
SDK_TAG="v1.3.1"
DRIVER_TAG="1.2.6"

if [[ ! -r "$ROS_SETUP" ]]; then
    echo "错误：找不到 ROS 2 Jazzy：$ROS_SETUP" >&2
    exit 2
fi
for command_name in git cmake colcon; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：缺少 $command_name" >&2
        exit 2
    fi
done

mkdir -p "$DEPS_DIR" "$ROS_WS/src"

checkout_release() {
    local repository="$1"
    local directory="$2"
    local tag="$3"
    if [[ ! -d "$directory/.git" ]]; then
        git clone --branch "$tag" --depth 1 "$repository" "$directory"
        return
    fi
    if ! git -C "$directory" diff --quiet || ! git -C "$directory" diff --cached --quiet; then
        echo "错误：$directory 有本地修改，拒绝覆盖；请先保存或移走这些修改。" >&2
        exit 2
    fi
    git -C "$directory" fetch --depth 1 origin "tag" "$tag"
    git -C "$directory" checkout --detach "$tag"
}

echo "[1/3] 获取 Livox SDK $SDK_TAG 和 ROS 驱动 $DRIVER_TAG"
checkout_release https://github.com/Livox-SDK/Livox-SDK2.git "$SDK_SOURCE" "$SDK_TAG"
checkout_release https://github.com/Livox-SDK/livox_ros_driver2.git "$DRIVER_SOURCE" "$DRIVER_TAG"

echo "[2/3] 本地编译 Livox SDK（安装到项目 .deps，不写 /usr/local）"
cmake -S "$SDK_SOURCE" -B "$SDK_SOURCE/build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$SDK_INSTALL" \
    -DCMAKE_CXX_FLAGS="-include cstdint"
cmake --build "$SDK_SOURCE/build" --parallel "$(nproc)" \
    --target livox_lidar_sdk_shared livox_lidar_sdk_static
cmake --install "$SDK_SOURCE/build"

echo "[3/3] 编译 ROS 2 Jazzy livox_ros_driver2"
cp "$DRIVER_SOURCE/package_ROS2.xml" "$DRIVER_SOURCE/package.xml"
mkdir -p "$DRIVER_SOURCE/launch"
cp -a "$DRIVER_SOURCE/launch_ROS2/." "$DRIVER_SOURCE/launch/"
set +u
source "$ROS_SETUP"
set -u
(
    cd "$ROS_WS"
    colcon build --packages-select livox_ros_driver2 --cmake-clean-cache --cmake-args \
        -DROS_EDITION=ROS2 -DDISTRO_ROS=jazzy -DCMAKE_BUILD_TYPE=Release \
        -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3 \
        -DCMAKE_LIBRARY_PATH="$SDK_INSTALL/lib" \
        -DCMAKE_INCLUDE_PATH="$SDK_INSTALL/include"
)

echo "MID-360S 驱动安装完成。检查命令："
echo "  ./avtwin_linux/run_acoustic_handshake.sh --mid360s-check"
echo "安装 FAST-LIO 位姿模块："
echo "  ./avtwin_linux/install_fastlio2.sh"
echo "仅启动原始点云/IMU："
echo "  ./avtwin_linux/run_acoustic_handshake.sh --mid360s-driver-only --gui"
echo "完成 FAST-LIO 安装后的一键启动命令："
echo "  ./avtwin_linux/run_acoustic_handshake.sh --mid360s --gui"
