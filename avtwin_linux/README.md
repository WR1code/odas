# AV-Twin Linux 声学握手与测距

> **项目主线：C1/C2 声学握手、双向测距和 RIR 采集。**
> MID-360S 不是握手必需设备，只负责给声学事件附加可选的位置与姿态。

## 声学握手 + MID-360S 定位一键启动（主入口）

首次使用只需安装一次依赖：

```bash
cd /home/w/odas/odas
./install_dependencies.sh
```

以后每次带定位的声学实验直接运行这一条：

```bash
cd /home/w/odas/odas
./avtwin_linux/run_acoustic_handshake.sh --mid360s --gui
```

这仍然是**声学握手主程序**。同一条命令会在后台启动 MID-360S 驱动、FAST-LIO 和位姿桥，
再把 `/Odometry` 持续接入声学 GUI 内的“雷达坐标”接口。进入界面后选择
`Initiator / Paper Rx` 或 `Responder / Paper Tx`，确认 UMA-8、扬声器、C1/C2 和 Android
网络参数，然后点击“开始会话”即可进行带位置标注的声学握手。

GUI 打开后会自动把“位姿来源”设为 `udp`，**不需要先点击“开始会话”就会实时显示坐标**。
声学 GUI 将它收到的第一帧位置和姿态定义为 `(0,0,0)` 与单位姿态，界面后续显示的是雷达相对
这个启动原点的三维位移（米）。重新运行整条一键启动命令会重新建立原点。

如果启动时没有检测到 MID-360S（包括网线未接、雷达未供电或 `carrier=0`），同一条命令不会
再终止声学程序，而是跳过 Livox、FAST-LIO 和位姿桥，自动以 `manual` 手动当前坐标模式打开
GUI。这样声学握手、测距和 RIR 仍可正常使用。

界面右侧的“重置零点”会清空旧位姿时间线，并把下一帧设为新的 `(0,0,0)`。为保证一轮
声学事件的 t1/t2/t3/t4 使用同一个坐标系，采集进行时按钮会禁用；请在开始采集前重置。

实际接入链路如下：

```text
MID-360S 点云 + IMU -> FAST-LIO -> /Odometry -> ROS 位姿桥
                                               -> 声学 GUI 雷达坐标接口
                                               -> t1/t2/t3/t4 事件坐标
```

MID-360S 只提供坐标，不参与 C1/C2 检测、声学握手状态机或 ToF 公式。暂时不需要定位时，
才去掉 `--mid360s`：

```bash
./avtwin_linux/run_acoustic_handshake.sh --gui
```

## 声学握手核心

Linux 端现已实现论文两种身份，并共用同一套连续 8 通道音频、逐通道匹配滤波、播放、
RIR 和 UDP 元数据核心：

- `Initiator / Paper Rx`：持续录音，发送 C1，声学确认本地 C1，等待并检测 C2，提取远端 C2 RIR。
- `Responder / Paper Tx`：持续录音并等待 C1，实时检测后立即把预加载 C2 入队，随后精确复检
  t2/t3，提取远端 C1 RIR，并发送 turnaround 元数据。

播放 API/队列时间只作为软件事件保存；精确 t1/t2/t3/t4 来自同一端 UMA-8 PCM 时间轴。
UDP 只传 session/state/timing 元数据，不会触发或伪造声学检测。只有 `session_id` 匹配且两端
时基内部可比时才计算 ToF；缺失精确数据、负修正值都输出 `NOT AVAILABLE`。

## 双角色 GUI

GUI 顶部 `AV-Twin Role` 可真实选择：

```text
Initiator / Paper Rx — Send C1 → Wait C2
Responder / Paper Tx — Wait C1 → Send C2
```

Responder 单次会话的关键路径是：

```text
INIT_RECORDING -> PRE_ROLL -> LISTEN_C1 -> C1_DETECTED
-> C2_IMMEDIATE_RESPONSE -> POST_ROLL -> PRECISE_ANALYSIS
-> RIR_EXTRACTION -> SEND_METADATA -> DONE/FAILED
```

`Debug Mode` 状态栏显示 session、状态、PCM buffer、阈值、dropped frames、播放和网络状态。
五个独立按钮可测试 C1/C2 播放、C1/C2 detector 和 UMA-8 录音，不启动完整握手。

## 声学程序的定位接入：MID-360S / SLAM

本节说明如何把 MID-360S 坐标接入声学 GUI。雷达是声学事件的位置标注来源；没有雷达时，
声学握手、测距和 RIR 采集仍可运行，但事件坐标会标记为不可用。

### 启用 MID-360S、FAST-LIO 与位姿桥

本机 Ubuntu 24.04 使用 ROS 2 Jazzy、Livox SDK `v1.3.1` 和
`livox_ros_driver2 1.2.6`。后二者是 Livox 官方首个明确支持 MID-360S 的发布组合。
安装在项目 `.deps` 内，不写 `/usr/local`：

```bash
cd /home/w/odas/odas
./avtwin_linux/install_mid360s_driver.sh
./avtwin_linux/install_fastlio2.sh
./avtwin_linux/run_acoustic_handshake.sh --mid360s-check
```

安装脚本为 SDK v1.3.1 在 GCC 13 构建时显式注入标准 `<cstdint>` 头，以规避该发布版
源码的漏包含问题；不会修改下载的官方源码。

默认网络配置在 `avtwin_linux/mid360s_config.json`：电脑网口
`192.168.1.5/24`，当前已知雷达 `192.168.1.116`。每次 `--mid360s` 或 `--mid360s-check`
都会先监听 Livox 发现广播，自动识别 MID-360S 的实际 IP，生成
`.runtime/mid360s_config.auto.json`，并在 Wi-Fi 同网段时自动补充有线 `/32` 路由；基础 JSON
不会被运行过程覆盖。需要固定 IP 排障时可加 `--mid360s-no-auto-ip`，自定义基础配置则使用
`--mid360s-config /绝对路径/配置.json`。电脑网口可用 NetworkManager 配置，例如：

```bash
nmcli connection modify "你的有线连接名" ipv4.method manual \
  ipv4.addresses 192.168.1.5/24 ipv4.gateway "" ipv4.dns "" \
  ipv4.never-default yes ipv4.route-metric 5000 \
  ipv4.routes 192.168.1.116/32
nmcli connection up "你的有线连接名"
```

这里的 `/32` 路由只把雷达地址导向有线口；即使 Wi-Fi 也在 `192.168.1.0/24`，也不会
抢走 Wi-Fi 默认路由。本机已将 `Wired connection 1` / `enp5s0` 按上述方式配置完成。
`--mid360s-check` 会同时检查地址、物理载波和雷达专用路由。

启动 GUI、官方驱动、FAST-LIO 和 ROS 位姿桥：

```bash
./avtwin_linux/run_acoustic_handshake.sh --mid360s --gui
```

驱动发布 `/livox/lidar`（Livox CustomMsg）和 `/livox/imu`，项目内安装的 FAST-LIO 将它们
融合为 `/Odometry` (`nav_msgs/msg/Odometry`)。启动时先让雷达保持静止，以便 IMU 初始化：

```text
/livox/lidar + /livox/imu -> FAST-LIO -> /Odometry
                                      -> ros_pose_bridge.py
                                      -> AVTWIN_POSE_V1 UDP :5006
```

如需同时查看 FAST-LIO 注册点云，可加 `--fastlio-rviz`。只验证原始点云/IMU、不运行 SLAM
时使用 `--mid360s-driver-only`。FAST-LIO 默认配置为
`avtwin_linux/fastlio_mid360s.yaml`，不会自动累计或保存 PCD。

如果 SLAM 使用其他 topic 或消息类型：

```bash
./avtwin_linux/run_acoustic_handshake.sh --mid360s --gui \
  --lidar-pose-topic /fast_lio/odom \
  --lidar-pose-type odometry
```

支持 `odometry`、`pose_stamped`、`pose_with_covariance_stamped`，默认 `auto` 从 ROS graph
识别并持续等待该 topic 出现。若驱动和 SLAM 已由其他终端启动，只启位姿桥：

```bash
./avtwin_linux/run_acoustic_handshake.sh --ros-pose-bridge --gui \
  --lidar-pose-topic /Odometry
```

`run_acoustic_handshake.sh` 会自动给 AV-Twin 加上 `--pose-source udp`，并在 GUI/CLI 退出时
停止由它启动的驱动、FAST-LIO 和桥接进程。原始 ROS 时间戳可能是 wall clock、传感器时钟
或仿真时钟；桥接程序在
收到每条位姿的回调时采集本机 `CLOCK_MONOTONIC`，从而能与本机音频 sample clock 安全
对齐，但精度仍包含 ROS/DDS 传输延迟。

GUI 的坐标与声学外参区域可选择 `udp` 或 `manual`；使用雷达时输入：

```text
雷达坐标系原点 → 扬声器声学中心：(x,y,z)，米
雷达坐标系原点 → UMA-8 阵列中心：(x,y,z)，米
```

GUI 的坐标来源分为：

- `udp`：直接读取 MID-360S + FAST-LIO 实时坐标。
- `manual`：在“手动当前坐标 x,y,z(m)”输入位置并点击“应用手动坐标”；该值持续有效，直到
  下一次手动应用。每次应用都有本机单调时钟记录，因此在连续采集中更新位置后，各个声学事件
  仍会使用事件发生时有效的那组手动坐标。
- `disabled`：不为声学事件附加位置。

手动模式的朝向固定为单位姿态；扬声器和 UMA-8 偏移仍由下面两个声学外参计算。“重置零点”
在手动模式中会把当前坐标设为 `(0,0,0)`，在雷达模式中则把下一帧 SLAM 位姿设为新零点。

程序会使用雷达世界姿态旋转外参，按以下刚体关系计算，而不是直接相加：

```text
p_speaker_world = p_radar_world + R_radar_world × offset_speaker_in_radar
p_mic_world     = p_radar_world + R_radar_world × offset_mic_in_radar
```

AV-Twin 默认在 `0.0.0.0:5006` 监听；本机 SLAM/ROS 桥接程序向
`127.0.0.1:5006` 持续发送：

```json
{
  "protocol": "AVTWIN_POSE_V1",
  "type": "lidar_pose",
  "timestamp_basis": "monotonic_ns",
  "timestamp_ns": 123456789000,
  "position_m": [1.2, 2.3, 0.8],
  "orientation_xyzw": [0.0, 0.0, 0.382683, 0.923880],
  "frame_id": "map",
  "child_frame_id": "livox_frame",
  "tracking_status": "TRACKING",
  "source": "fast_lio"
}
```

`timestamp_ns` 必须是同一台 Linux 主机的 `CLOCK_MONOTONIC`。若省略，接收端使用 UDP
到达时间并明确标为低精度 `udp_receive_monotonic_ns`；Unix/ROS wall-clock 不会被冒险地与
音频 sample 直接相减。程序缓存位姿并在每个精确 t1/t2/t3/t4 时刻插值，将
`radar_pose`、`speaker_pose`、`microphone_pose` 写入 `local_spatial_events`。

GUI 把相对启动原点坐标明确分成两类：

- `最新实时相对坐标`：只要 SLAM 位姿 UDP 持续到达，界面约每 80 ms 刷新 Radar、
  Speaker 和 UMA-8 的最新位置，同时显示 `frame`、tracking 状态和位姿年龄 `age`；它用于
  观察，不要求声学会话已经开始，也不代表某次 chirp 的最终标注。
- `本次采集冻结坐标`：每轮精确声学分析结束后显示。Initiator 保存 C1 发出时的 `t1` 和
  C2 收到时的 `t4`；Responder 保存 C1 收到时的 `t2` 和 C2 发出时的 `t3`。单次模式写入
  `metadata.json`，连续模式写入 `measurements/<measurement_id>/result.json` 及
  `measurements.jsonl`。

实时电平条下方提供 `CH0`–`CH7` 波形显示复选框以及“全选/全不选”。选择同时作用于“实时
输入波形”和 remote RIR 图，且会保存到 GUI 偏好设置；它只控制绘图，不会停用任何 UMA-8
录音通道，也不会改变多通道 C1/C2 检测和保存的原始数据。顶部 8 个实时电平始终全部显示。

若事件时刻没有足够接近的有效位姿（超过“最大位姿时差”、SLAM 未接入或 tracking 无效），
该事件会明确显示“不可用”和原因，不会用当前坐标冒充 chirp 时刻坐标。

如果坐标突然出现数百米或数千米，通常不是正常 IMU 漂移，而是旧 ROS 节点残留或 SLAM
重新初始化后坐标系发生切换。位姿入口会拒绝超过室内设备合理速度的瞬时跳变，保留上一有效
坐标；若错误流持续超过“最大位姿时差”，声学事件坐标会标记为不可用，不会把错误坐标写入
结果。GUI 此时显示 `tracking=REJECTED` 和具体警告。先关闭旧窗口，确认只运行一个一键启动
实例；恢复正常后可在采集开始前点击“重置零点”。启动脚本会按进程组清理 Livox、FAST-LIO
和位姿桥，正常退出后不会留下派生节点。

项目的 MID-360S FAST-LIO 配置针对近距离室内声学装置保留更多点（`blind: 0.2`、0.25 m
体素），并使用固定的雷达—IMU 外参而不在线估计外参。修改安装姿态不需要改这组内部外参；
雷达与扬声器、UMA-8 的安装偏移仍应填写在 GUI 的两个声学外参输入框中。

未接 SLAM 时可用静态测试发送器检查接口和外参方向：

```bash
.venv/bin/python -m avtwin_linux.pose_sender \
  --position 1.2 2.3 0.8 \
  --quaternion 0 0 0 1 \
  --rate 10
```

CLI 启用示例：

```bash
./avtwin_linux/run_acoustic_handshake.sh --role initiator \
  --pose-source udp --pose-udp-port 5006 --pose-max-age 0.25 \
  --speaker-offset 0.12,0.00,-0.05 \
  --microphone-offset -0.08,0.00,-0.03 \
  --input-device alsa:SPK:0 --output-device alsa:PCH:0 \
  --c1 wav/c1_mono.wav --c2 wav/c2_mono.wav
```

GUI 中 UDP 端口分为两个独立设置：

```text
Linux 本机监听/结果端口：接收 Android 握手元数据（默认 5005）
Android/远端监听端口：Linux 向 Android 发送 ARM/测试包（默认 5006）
```

例如本机监听 `5005`、远端应用监听 `7001` 时，将“Android/远端监听端口”设置为
`7001`。CLI 对应参数为 `--udp-port 5005 --android-port 7001`。

与仓库内 Android App 的默认设置对应关系是：Linux `udp-port=5005` 对应 Android
“Linux 结果接收端口=5005”；Linux `android-port=5006` 对应 Android
“安卓 ARM 监听端口=5006”。MID-360S 位姿默认也发到 Linux 本机 `5006`，但它和位于
Android 设备上的远端 `5006` 属于不同 IP，不会冲突。

实验参数区域会每 2 秒刷新 `Android通信本机IP` 和 `Linux全部IPv4`。前者是 Linux 内核按
当前路由选择、实际用于访问所填远端 IP 的源地址；例如本机可能同时显示 Wi-Fi
`wlp4s0=192.168.1.199` 和雷达有线口 `enp5s0=192.168.1.5`。`0.0.0.0` 仅表示监听所有
本机网卡，不是可填写到 Android 的 Linux 地址，因此 GUI 不再把它显示为通信端点。

GUI 的 `Test UDP Roundtrip` 使用这两个端口做独立双向检验，不录音、不播放 chirp：Linux
从“Linux 本机监听/结果端口”发出带随机 `nonce` 的 ping 到 Android 的“Android/远端监听端口”，
只有从 Android 收到相同 `nonce` 的 reply 才显示 `PASS`，并显示回包来源和 RTT。显示
`FAIL` 说明至少一个方向、IP、端口、监听状态或防火墙存在问题；UDP 的 `sendto()` 成功本身
不算连通成功。

Linux GUI 空闲时会常驻监听“Linux 本机监听/结果端口”，所以 Android 可以直接点击自己的
`UDP 双向检验`，无需先在 Linux 开始声学会话。Linux 会原路返回相同 `nonce`。正式声学会话
开始前，空闲测试监听会释放端口并由会话监听器接管；会话结束后自动恢复，因此不会同时绑定
同一个端口。

GUI 的 Chirp、角色、音频设备、坐标、实验参数、波形、会话控制、Debug 和运行日志均位于
可调整的纵向模块中。拖动模块之间的横向分隔条即可分别改变高度；当前分隔位置会保存到 GUI
偏好设置并在下次启动时恢复。

测试协议如下，Android 应回复到收到 ping 的源 IP/源端口：

```json
{"protocol":"AVTWIN_UDP_TEST_V1","type":"udp_test_ping","nonce":"随机值"}
```

```json
{"protocol":"AVTWIN_UDP_TEST_V1","type":"udp_test_reply","nonce":"原样返回随机值","receiver":"android"}
```

当前仓库内的 Android Responder 已实现同一协议：Android 按钮可验证
`Android → Linux → Android`；Android 声学会话启动后，其控制端口也会自动响应 Linux 发起的
测试。Linux 正在完整握手时不另行绑定同一端口，但现有握手监听器仍会自动回复 Android 发来的
测试 ping。

当前 PCM→单调时钟映射来自音频 block 到达时间，因此 metadata 会标记
`hardware_timestamp=false`。该接口已经可用于接入和验证；若要追求厘米级动态标注，后续仍需
接入 ALSA 硬件时间戳并标定固定采集延迟。

## 采集模式与时间基准

界面保留原来的 `single` 单次握手，并新增：

- `manual_continuous`：点击“开始会话”只启动持续录音；每次点击“采集一次”才播放
  C1。一轮完成或超时后回到 `ARMED`，输入流不关闭。
- `timed_continuous`：确认自动发声后先倒计时 3 秒，随后按配置间隔自动触发。默认
  2.0 秒。间隔锚定相邻 C1 的实际声学检测 sample，而不是播放 API 调用时间；若到期时
  仍处于 `WAIT_C2`、`CAPTURE_TAIL` 或 `FINALIZE`，该周期会被跳过并写入日志和
  `session.json`。

配置 Android IP 时，持续模式使用显式 ACK 状态机：

```text
IDLE -> ARMED -> WAIT_ARM_ACK -> C1_PLAYING -> WAIT_C2
     -> CAPTURE_TAIL -> FINALIZE -> ARMED
```

Linux 使用同一个已绑定的结果监听套接字发送 ARM。每个 ARM 带稳定的 `arm_event_id`，默认
等待 0.5 秒并最多尝试 3 次；只有收到匹配且 `accepted=true` 的 `arm_ack` 才播放 C1。
明确拒绝或全部超时会直接保存失败原因，不再浪费一轮等待 C2。CLI 可用
`--arm-ack-timeout` 和 `--udp-ack-retries` 调整。

会话期间输入和输出资源各打开一次；直接 ALSA 模式只在整个会话边界释放/恢复匹配的
PipeWire card profile。持续 Float32 原始录音直接流式写盘，有限环形缓冲区只用于当前轮
分析。`t1_sample` 和 `t4_sample` 都位于同一个 Linux PCM sample 时间轴。

## 声学握手 GUI 说明

```bash
cd /home/w/odas/odas
./avtwin_linux/run_acoustic_handshake.sh --gui
```

若设备列表为空，请确认 UMA-8 和扬声器已连接，并且当前进程可以访问宿主机的
`/dev/snd` 设备。

界面可主动选择 C1/C2 WAV，并分别选择 UMA-8 输入设备与扬声器输出设备；还可选择
采集模式、输出声道、播放增益、阈值、UDP 端口、RIR 方法、PASS 严格程度、自动间隔、
最大条数和最大会话时长（两个上限的 0 均表示不限制）。运行时显示 8 通道输入电平、原始
波形与日志；检测到 C2 后在 tail 录音期间刷新 RIR 预览，最终分析完成后显示完整 RIR，
并持续显示状态、measurement id、成功/失败/跳过数、下次触发倒计时、最近质量和结果目录。

“选择结果保存目录”使用目录选择器，界面显示解析后的绝对路径并持久化上次选择。开始
会话前会实际创建目录和临时文件验证可写性；失败会阻止开始，不会回退到其他位置。

设备下拉框使用 ALSA 稳定身份恢复选择，而不是保存临时 PortAudio index。推荐项为
`alsa:SPK:0`（UMA-8 输入）和 `alsa:PCH:0`（ALC897 Analog/3.5mm 输出）；实际 card
编号即使因 USB 插拔变化也会重新解析。物理播放直接打开
`plughw:CARD=PCH,DEV=0`。若 PipeWire 正占用该声卡，程序仅在自检/播放期间临时
将匹配声卡的 PipeWire profile 切换为 off，完成后恢复原 profile，不会改系统默认输出。
UMA-8 物理录音同样直接从 `plughw:CARD=SPK,DEV=0` 读取 48 kHz/8ch float PCM；
即使 PipeWire 占用导致 PortAudio 隐藏该设备，GUI 仍会依据 `arecord -l` 保留它。

“测试输出”按钮会以 48 kHz、5% 数字幅度先播放左声道 440 Hz，再播放右声道
660 Hz，不启动录音或握手。RIR 页采用与 `rir_capture` 一致的 Matplotlib 图：实际
毫秒时间轴、网格、CH0–CH7 图例、各通道共用幅值尺度，并同时显示全长及前 50 ms。

## 命令行

先列设备：

```bash
./avtwin_linux/run_acoustic_handshake.sh --list-devices
```

预期推荐身份（数字 card 可变，stable name 不变）：

```text
Input : UMA-8 micArray RAW SPK | alsa:SPK:0 | plughw:CARD=SPK,DEV=0 | 8 ch
Output: ALC897 Analog / 3.5mm   | alsa:PCH:0 | plughw:CARD=PCH,DEV=0 | 2 ch
```

真实实验（设备编号按上一条输出填写）：

```bash
./avtwin_linux/run_acoustic_handshake.sh \
  --role initiator \
  --input-device alsa:SPK:0 \
  --output-device alsa:PCH:0 \
  --output-channel right \
  --playback-gain 0.5 \
  --c1 /path/to/c1.wav \
  --c2 /path/to/c2.wav \
  --udp-port 5005 \
  --reply-timeout 5
```

交换角色（Linux 等待 Android C1，检测后立即发送 C2）：

```bash
./avtwin_linux/run_acoustic_handshake.sh \
  --role responder \
  --input-device alsa:SPK:0 \
  --output-device alsa:PCH:0 \
  --output-channel right \
  --playback-gain 0.2 \
  --c1 wav/c1_mono.wav --c2 wav/c2_mono.wav \
  --android-host 192.168.1.20 --android-port 5005 \
  --udp-port 5005 --reply-timeout 5
```

输出声道支持 `left`、`right`、`both`；只有显式选择 `both` 才复制到两个扬声器声道。

推荐 C1/C2 为 48 kHz、mono、PCM16。其他采样率/通道数会自动转换并警告。
单次兼容模式仍生成 `output/YYYYMMDD_HHMMSS/`，包括完整 `raw_linux_8ch.wav`、
实际使用模板、每通道 Float32 RIR、检测/RIR 图、UDP JSONL、日志和 `result.json`。

持续 CLI 示例：

```bash
./avtwin_linux/run_acoustic_handshake.sh \
  --capture-mode timed_continuous \
  --interval 2.0 --max-measurements 100 --max-session-duration 0 \
  --android-host 192.168.1.20 --android-port 5005 \
  --input-device alsa:SPK:0 --output-device alsa:PCH:0 \
  --c1 /path/to/c1.wav --c2 /path/to/c2.wav \
  --output-root /absolute/path/to/results
```

`manual_continuous` 的 CLI 在每次按 Enter 时触发；Ctrl+C 会安全停止。GUI 提供独立的
“采集一次”、“暂停自动采集/继续自动采集”和“安全停止并保存”按钮。

## Android UDP 协议与 ToF

配置 Android IP 后，Linux 每轮播放前发送：

```json
{"type":"arm","protocol_version":1,"session_id":"...","measurement_id":12,"arm_event_id":"..."}
```

Android 必须原路返回，重复收到相同 `arm_event_id` 时只重复 ACK，不重复 ARM：

```json
{"type":"arm_ack","protocol_version":1,"session_id":"...","measurement_id":12,"arm_event_id":"...","accepted":true,"reason":"accepted_strict"}
```

Android 每次启动后由第一条有效 ARM 绑定 Linux `session_id`；后续其他 session 会被明确
拒绝。Android 自己用于保存目录的本地 session 与协议 session 分开显示。

只接受当前 outstanding measurement 的版本 1 `reply_timing`：

```json
{"type":"reply_timing","protocol_version":1,"session_id":"...","measurement_id":12,"android_event_id":"...","t3_precise":true,"reply_delay_samples":4821,"sample_rate":48000}
```

Linux 校验 session、measurement 和 event 后返回：

```json
{"type":"reply_ack","protocol_version":1,"session_id":"...","measurement_id":12,"android_event_id":"...","accepted":true,"reason":"accepted","receiver":"linux"}
```

Android 未收到 `reply_ack` 时使用完全相同的 `android_event_id` 和 payload 最多重发 3 次；
Linux 对重复结果只重复 ACK，不重复计数或触发声学动作。ACK 和重试时间只用于控制与诊断，
绝不参与 t1/t2/t3/t4 或 ToF 计算。

重复包去重；其他 measurement、其他 session 和已完成轮次的迟到包只写 UDP 审计日志，
不会关联到下一轮。未配置 ARM 目标时，可兼容“恰好一个 outstanding measurement”的旧
Android 消息，但结果会标记 `single_outstanding_compatibility` 和低可信度。

只有匹配消息明确给出 `t3_precise=true` 以及有效精确延迟时才计算：

```text
ToF = ((t4 - t1) / Linux_sample_rate - Android_reply_delay) / 2
```

否则 `exact_tof` 始终是 `NOT AVAILABLE`。

## 持续会话输出

双角色单次会话使用论文术语明确分开的目录：

```text
output/YYYYMMDD_HHMMSS_<initiator|responder>/
  raw/uma8_8ch.wav
  references/c1.wav, c2.wav
  rir/remote/rir_ch0.wav ... rir_ch7.wav, rir_fused.wav
  rir/local/rir_ch0.wav ... rir_ch7.wav, rir_fused.wav
  analysis/c1_correlation.npy, c2_correlation.npy, peaks.json
  metadata.json
  events.json
  log.txt
```

恒零通道仍保存在 `raw/uma8_8ch.wav`，但标为 `inactive_zero`，不参与检测、融合或 RIR fused。

运行 Initiator ×20 和 Responder ×20 后可生成验收统计 CSV：

```bash
.venv/bin/python -m avtwin_linux.batch_stats avtwin_linux/output
```

默认输出 `avtwin_linux/output/dual_role_stats.csv`，包含 C1/C2 detection rate、false trigger
rate、turnaround mean/std、RIR extraction success rate 和 ToF valid rate。

```text
<output-root>/<session_timestamp>_<session_id>/
  session.json
  measurements.jsonl
  raw/
    continuous_float32.wav
  measurements/
    000001/
      result.json
      rir_float32_ch0.wav ... rir_float32_ch7.wav
      rir_float32.npy
      plots/
    000002/
      ...
  logs/
    run.log
    android_udp.jsonl
```

`session.json` 在会话开始时立即创建，采集结束后原子更新结束时间、设备身份、C1/C2
SHA256、参数、统计、跳过原因和中断/终止状态。每轮先完整写 `result.json` 和 Float32
RIR，再 fsync 追加 `measurements.jsonl`。原始和 RIR 不做逐通道归一化；RIR 保留默认
10 ms pre-arrival，绝对 `t4_sample` 与相对 RIR 时间轴分别记录。

质量结果将 `protocol_pass`、`tof_pass`、`rir_pass` 和 `overall_pass` 分开。可选择
`protocol`、`rir`、`tof` 或 `strict` 综合策略；默认 `strict`。指标包括 C1/C2 有效通道、
峰值/削波、直达峰可信度、峰尾噪声比、多通道到达一致性、有效衰减长度，以及数据允许时
的 C50、EDT 和 T60。“RIR 非零”不再视为质量通过。

注意：默认播放增益虽为 1.0，第一次真实实验建议显式使用较低的
`--playback-gain 0.2`，确认输出设备和音量后逐步提高。

当前 `wav/c1.wav`、`wav/c2.wav` 是 CH0 静音、CH1 有效的双声道文件。项目已生成
不降幅的正式 mono 版本；Linux 和 Android 应复制并使用完全相同的这两个文件：

```text
wav/c1_mono.wav  SHA256 786d43187b4a50270a0a788d3beaa12ed0730f7b97a769398f36b7116239bf49
wav/c2_mono.wav  SHA256 10b5f387ea95b1343b764055d2f89608b0838dca411f1a57aea73bcdd6faf7ff
```

C1 主要位于约 11–19 kHz，人耳听感很弱是正常现象。是否实际发送以 Linux 日志中的
`C1 SENT: ACOUSTICALLY CONFIRMED` 为准。输出声道 `0` 表示左扬声器，`1` 表示右扬声器；
当前默认是 `1`，即仅从右边发出 C1。

## Ubuntu 硬件验证

1. 运行 `./avtwin_linux/run_acoustic_handshake.sh --list-devices`，确认上述两个 stable name。
2. 运行 `./avtwin_linux/run_acoustic_handshake.sh --gui`。输出下拉框应自动选择
   `ALC897 Analog / 3.5mm Output`，而不是 Digital、HDMI、default 或 UMA-8。
3. 点击“测试输出”：EDIFIER 应先响左声道 440 Hz，再响右声道 660 Hz；UMA-8
   不应播放。此步骤不录音、不发送 C1。
4. 选择两端完全相同的 C1/C2，第一次将播放增益设为 `0.2`，开始实验。
5. 日志应打印 48000 Hz 自检、稳定 ALSA identity、当次 PortAudio index，以及
   C1/C2 源文件/内部模板格式。
6. 关闭界面并重新打开，确认选择仍按 `alsa:SPK:0`、`alsa:PCH:0` 恢复；即使
   `hw:X,0` 中的 X 变化也无需修改配置。
7. 选择 `manual_continuous`，开始会话后连续点击三次“采集一次”，确认只有一个持续
   `continuous_float32.wav` 且生成 `000001` 至 `000003`。
8. 选择 `timed_continuous`，核对自动发声确认和倒计时，然后验证暂停、继续与安全停止。

## 测试

```bash
cd /home/w/project/odas
LD_LIBRARY_PATH="$PWD/.deps/libportaudio2/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  .venv/bin/python -m pytest -q avtwin_linux/tests
```
