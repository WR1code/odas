#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
DRIVER_SETUP="$PROJECT_DIR/.deps/mid360s_ws/install/setup.bash"
FASTLIO_WS="$PROJECT_DIR/.deps/fastlio2_ws"
FASTLIO_SOURCE="$FASTLIO_WS/src/FAST_LIO_ROS2"
FASTLIO_REPOSITORY="https://github.com/Ericsii/FAST_LIO_ROS2.git"
FASTLIO_COMMIT="2fffc570a25d0df172720bac034fbdb6a13d2162"
PCL_ROS_ROOT="$PROJECT_DIR/.deps/ros-jazzy-pcl-ros"
PCL_ROS_PREFIX="$PCL_ROS_ROOT/opt/ros/jazzy"
PCL_ROS_DOWNLOAD="$PROJECT_DIR/.deps/ros-jazzy-pcl-ros-download"

for required_file in "$ROS_SETUP" "$DRIVER_SETUP"; do
    if [[ ! -r "$required_file" ]]; then
        echo "错误：找不到 $required_file；请先运行 ./avtwin_linux/install_mid360s_driver.sh" >&2
        exit 2
    fi
done
for command_name in git colcon dpkg-deb; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：缺少 $command_name" >&2
        exit 2
    fi
done

mkdir -p "$FASTLIO_WS/src" "$PCL_ROS_DOWNLOAD" "$PCL_ROS_ROOT"
if [[ ! -d "$FASTLIO_SOURCE/.git" ]]; then
    git clone --recursive "$FASTLIO_REPOSITORY" "$FASTLIO_SOURCE"
else
    if ! git -C "$FASTLIO_SOURCE" diff --quiet \
        || ! git -C "$FASTLIO_SOURCE" diff --cached --quiet; then
        echo "错误：$FASTLIO_SOURCE 有本地修改，拒绝覆盖。" >&2
        exit 2
    fi
    git -C "$FASTLIO_SOURCE" fetch --depth 1 origin "$FASTLIO_COMMIT"
fi
git -C "$FASTLIO_SOURCE" checkout --detach "$FASTLIO_COMMIT"
git -C "$FASTLIO_SOURCE" submodule update --init --recursive

if [[ ! -r /opt/ros/jazzy/share/pcl_ros/cmake/pcl_rosConfig.cmake ]] \
    && [[ ! -r "$PCL_ROS_PREFIX/share/pcl_ros/cmake/pcl_rosConfig.cmake" ]]; then
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "错误：缺少 pcl_ros，且无法使用 apt-get 下载项目本地副本。" >&2
        exit 2
    fi
    echo "系统未安装 pcl_ros；正在下载项目本地副本。"
    (
        cd "$PCL_ROS_DOWNLOAD"
        apt-get download ros-jazzy-pcl-ros
    )
    PCL_ROS_DEB="$(find "$PCL_ROS_DOWNLOAD" -maxdepth 1 -type f \
        -name 'ros-jazzy-pcl-ros_*.deb' -print -quit)"
    if [[ -z "$PCL_ROS_DEB" ]]; then
        echo "错误：未能下载 ros-jazzy-pcl-ros。" >&2
        exit 2
    fi
    dpkg-deb -x "$PCL_ROS_DEB" "$PCL_ROS_ROOT"
fi

set +u
source "$ROS_SETUP"
source "$DRIVER_SETUP"
set -u
if [[ -d "$PCL_ROS_PREFIX" ]]; then
    export CMAKE_PREFIX_PATH="$PCL_ROS_PREFIX${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
    export LD_LIBRARY_PATH="$PCL_ROS_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

(
    cd "$FASTLIO_WS"
    colcon build --symlink-install --packages-select fast_lio --parallel-workers 4 \
        --cmake-args -DCMAKE_BUILD_TYPE=Release \
        -DPython3_EXECUTABLE=/usr/bin/python3 -DPYTHON_EXECUTABLE=/usr/bin/python3
)

echo "FAST-LIO 安装完成。实机启动命令："
echo "  ./avtwin_linux/run_acoustic_handshake.sh --mid360s --gui"
