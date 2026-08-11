# Isaac Sim + AcoustiX + ODAS 集成

## 当前本机审计

审计日期为 2026-08-09。仓库原有未提交修改均保留，没有执行 reset、checkout 或覆盖原文件。

- Isaac Sim：`/home/w/Desktop/isaac-sim-standalone-6.0.1-linux-x86_64`，实际版本 `6.0.1-rc.7+release.42383...`。本项目按 6.0.1 API 实测。
- GPU：NVIDIA GeForce RTX 4090 D 24 GB，驱动 595.84。系统没有独立 `nvcc`，但 Isaac 自带 CUDA 12.9 运行时并已成功启动 GPU。
- ROS 2：Jazzy，`/opt/ros/jazzy`；系统 Python 与 Isaac Python 均实测可导入 `rclpy`、`rosgraph_msgs` 和 `tf2_msgs`。
- Conda：26.5.3；独立环境 `odas-acoustix` 已创建并实测。
- ODAS：`build/bin/odaslive` 已存在且重新构建成功。
- AcoustiX：官方仓库已安装在 `/home/w/src/AcoustiX`，提交为 `7c0ec9a00ec1f21b618e64999edf53e449886c0b`。官方仓库不是可直接 `import acoustix` 的包；真实入口是仓库根目录的 `simu_utils.ir_simulation`，场景为 Sionna/Mitsuba XML。适配器严格调用该入口，缺失或失败时退出，不产生替代 RIR。
- 声学环境：TensorFlow 2.15.1、修改版 Sionna 0.18.0、Mitsuba 3.5.2、Dr.Jit 0.4.6。TensorFlow 已识别 `/physical_device:GPU:0`，AcoustiX 实测使用 `cuda_ad_rgb`。

## 进程架构

```text
Isaac Sim 6.0.1 process              ROS 2 Jazzy control plane
  USD / physics / visualization  -> /clock, /tf, array/source/robot poses
              |
              | scene-state JSON + static mesh/material semantics
              v
scene_converter -> Mitsuba XML + PLY
              |
              v
Conda odas-acoustix process -> official simu_utils.ir_simulation -> 7-channel RIR
              |
              v
system Python audio process -> convolution -> channel 8 zero -> S32_LE
              |                                      |
              | file                                 | TCP server
              v                                      v
          odaslive file client                   odaslive TCP client
              \______________________________________/
                                  |
                         tracked JSON + error metrics
```

PCM 不经过 ROS 2 topic。ROS 2 只承载仿真时钟、TF、位姿和控制；PCM 使用文件或 TCP。三个主要运行时分别使用 Isaac 自带 Python、独立 `odas-acoustix` Conda 环境和 ODAS 原生进程。

## 安装 AcoustiX

官方 Sionna 分支依赖 TensorFlow 2.13–2.15、Mitsuba 和 CUDA Python wheels 等，下载量可能达到数 GB。交互式安装脚本会在下载前确认：

```bash
cd /home/w/project/odas
./scripts/install_acoustix.sh
export ACOUSTIX_ROOT=/home/w/src/AcoustiX
```

脚本执行的核心官方命令是：

```bash
git clone https://github.com/penn-waves-lab/AcoustiX.git /home/w/src/AcoustiX
conda env create -f simulation/environment/acoustix.yml
conda run -n odas-acoustix python -m pip install /home/w/src/AcoustiX/sionna mitsuba==3.5.2
```

脚本不依赖 `rg`，可在当前机器直接重跑。必须固定 Mitsuba 3.5.2：当前修改版 Sionna 0.18.0 调用旧 Dr.Jit API，与 Mitsuba 3.6 及以上不兼容；错误表现为缺少 `drjit.reinterpret_array_v`。安装末尾会打印 TensorFlow GPU、Sionna/Mitsuba/Dr.Jit 版本和实际 Mitsuba variant。

本实现审阅和安装的官方 HEAD 为 `7c0ec9a00ec1f21b618e64999edf53e449886c0b`。若后续 HEAD 改变，应先重新核对 `simu_utils.py` 的 `load_cfg` 和 `ir_simulation` 参数。

## 一条命令运行

本机已经有可用的单声道 48 kHz 测试语音，因此离线演示无需参数或环境变量：

```bash
./scripts/run_offline_sim.sh
```

脚本会自动选择本机最新的 `cove.en.*.wav`，然后依次启动 Isaac、转换场景、在独立 Conda 环境计算 RIR、卷积、生成 RAW/WAV、运行 ODAS，并写入 `simulation/output/offline/report.json`。如需换成自己的语音，可选设置 `ODAS_CLEAN_SPEECH` 或传入一个 WAV 路径；没有 AcoustiX 时返回退出码 3。

完成一次离线渲染后，在线 TCP 演示为：

```bash
./scripts/run_online_sim.sh
```

动态 Isaac ROS 2 位姿发布为：

```bash
./scripts/run_isaac_ros_bridge.sh simulation/output/offline/test_room.usda
```

其他消费节点必须设置 `use_sim_time:=true`。桥接器发布 `/clock`、`/tf`、`/acoustics/array_pose`、`/acoustics/source_pose` 和 `/robot/pose`。

## 坐标系与角度

全链路采用米、右手系、Z 向上：

- +X：阵列正前方；+Y：阵列左方；+Z：上方。
- Isaac/USD 到 AcoustiX/Sionna 的几何转换是恒等变换，没有厘米缩放或轴交换。
- ROS 2 frame 使用 `world -> uma8_array -> mic_N`。
- 阵列局部点变换为 `p_world = R_world_array p_array_mic + t_world_array`。
- ODAS 的 `x,y,z` 是阵列坐标中的单位方向，不是距离。
- 方位角为 `atan2(y,x)`：正前方 0°，左方 +90°，右方 -90°；从 +Z 向下看，正角为逆时针。
- 俯仰角为 `atan2(z,hypot(x,y))`。

`UMA8_ACTIVE_MICS_M` 与现有配置一致：CH1 是中心，CH2–CH7 是半径约 42.2 mm 的六个圆周麦克风。原始硬件流保留 8 通道，ODAS mapping 为 `(1,2,3,4,5,6,7)`。本仿真将 CH8 **严格置零**；它既不复制其他麦克风，也不进入 ODAS mapping。

## PCM 和 TCP 协议

- 48000 Hz、8 通道、signed 32-bit little-endian。
- 无 WAV/文件头，逐采样点交错：`s0c0...s0c7,s1c0...`。
- 一个 ODAS hop 是 `512 * 8 * 4 = 16384` 字节。
- TCP 仿真端默认监听 `127.0.0.1:10000`；ODAS 是客户端。`python3 -m simulation.online_demo --port 10001` 会在输出目录生成匹配端口的运行时 ODAS 配置，无需修改模板。
- server 在一个 hop 内循环处理 partial send；断线后从未完成块的边界等待重连。EOF 通过关闭连接表示。
- 指标区分音频欠载、调度 deadline miss、丢块、partial send、重连、最大发送延迟和实时因子。

## 场景与材质

`simulation/scenes/test_room.json` 包含地板、天花板、四面墙、桌面和柜子。Isaac 脚本把声学材质和对象语义写为 USD custom attributes，并从 USD world transform 读回声源、阵列和每个麦克风坐标。转换器将静态轴对齐盒网格三角化为 PLY，再生成 AcoustiX 实际支持的 Mitsuba XML。

`simulation/configs/materials.json` 保存 125–4000 Hz 吸声系数、散射参数、来源说明及 Isaac 到 AcoustiX 名称映射。官方 AcoustiX 当前实现会把各频带值平均后映射为 Sionna 材料；它把 scattering coefficient 固定为 0，因此映射文件中的散射值被保留用于数据记录，但当前后端不能逐材质准确使用。这个限制不会被静默隐藏。

几何简化保留房间尺寸、大反射面和家具主要遮挡面。小物体、薄板双面传播、边缘绕射、低频波动/模态和频带内细节不能由当前几何射线模型完整描述。默认关闭 diffraction，避免宣称未经验证的可听频段绕射精度。

## 动态 RIR

`RIRUpdatePolicy` 默认位置阈值 5 cm、最大 2 Hz、2 cm 缓存量化和 128 条 LRU 缓存。`DynamicAudioEngine` 在块边界调用真实 RIR callback，记录仿真时间、源/麦克风位姿、RIR 版本、缓存命中和计算耗时。`CrossfadingConvolver` 默认使用 50 ms 交叉淡化，避免切换爆音；音频按块卷积，不按采样点重算 RIR。

## 数据集

入口：

```bash
python3 -m simulation.datasets.generate \
  --speech /data/speech/a.wav /data/speech/b.wav \
  --samples 100 --seed 20260809
```

选择“多通道 32-bit WAV + JSONL manifest + compressed NPZ RIR”，原因是 WAV 可被常见音频工具直接检查，JSONL 可流式追加和版本控制，NPZ 能无损保存 `[source,microphone,time]` 浮点 RIR。每个样本保存随机种子、语音来源、声源和麦克风位置、阵列局部几何、房间/家具/材质、真值角度/距离、AcoustiX 统计、ODAS 结果与传感器损伤参数。

当前随机化包括家具位置、材质吸声、1 至 N 个声源、源/阵列位置和 yaw、语音、SNR/噪声、麦克风增益、亚采样延迟、位置误差、时钟漂移、量化和削波。轨迹可由 Isaac ROS bridge 的运动参数控制；RGB、深度和机器人状态保留为后续可选记录项。

## 验证

快速回归：

```bash
python3 -m unittest discover -s tests -v
PYTHONPATH=/home/w/project/odas ACOUSTIX_ROOT=/home/w/src/AcoustiX \
  RUN_ACOUSTIX_TESTS=1 python3 tests/test_acoustix_backend.py -v
cmake --build build -j4
```

`validate_direct_delays` 对每个接收器在解析到达时间附近找直达峰，并强制：

```text
expected_delay_samples = distance / 343.8 * 48000
max(abs(observed - expected)) <= 1 sample
max(abs(observed_TDOA - expected_TDOA)) <= 1 sample
```

PCM 测试给八个通道放置不同帧位置的脉冲，验证 8×4 字节帧宽、little-endian、交错顺序和 CH8 全零。固定种子的场景转换、RIR 和最终 RAW 均可复现。

2026-08-09 的真实 AcoustiX + Isaac + ODAS 闭环结果如下。输入是本机已有的 48 kHz、单声道、8.32 s 语音；没有使用合成或 mock RIR。

| 项目 | 实测结果 |
|---|---:|
| RIR shape | 7 × 24000 samples |
| 解析直达延迟最大误差 | 0.6232 sample |
| 解析麦克风对 TDOA 最大误差 | 0.6116 sample |
| GPU RIR 计算 | 6.36 s |
| CPU RIR 计算（对照） | 4.19–4.23 s |
| 输出 PCM | 423424 frames / 13,549,568 bytes / 0 clipped samples |
| ODAS 有效帧率 | 905 / 1104 = 81.97% |
| 方位角 MAE / median / p95 | 6.16° / 1.34° / 19.25° |
| 俯仰角 MAE / median / p95 | 24.01° / 5.51° / 87.06° |
| ODAS 距离误差 | 不适用；SST 不输出距离 |

第二次 GPU 运行的 RIR 数组 SHA-256 仍为 `62f1fd28dfbc0eddc19919cf885aa565231493e208bf60ae3e6803c4966fec30`，最终 RAW SHA-256 仍为 `51d67bfca96b145dd5214f1f864bc2b485f2c01888fd456ad85fc2c5605f0696`。固定种子、同一软件栈的位级复现通过。

在线静态流在默认端口和非默认端口 10001 均通过：827 blocks、8.8213 s 音频、RTF 约 0.99996、0 欠载、0 丢块、0 重连、0 partial send；两次调度 deadline miss 为 15–16，最大发送延迟不超过 0.359 ms。file 和 TCP 对同一 RAW 得到字节完全相同的跟踪 JSON。

正前方解析信号和坐标单元测试明确验证：ODAS `(x,y,z)=(1,0,0)` 对应 0°，`+y` 是正角/左侧/从 `+z` 向下看逆时针。真实房间闭环声源真值为方位 21.2505°、俯仰 0°、距离 1.9313 m。少量帧出现接近竖直方向的错误峰，导致俯仰 p95 较差；报告没有删除这些异常帧。

## 已知限制

- 官方 AcoustiX 使用 Monte Carlo 射线和随机反射符号；固定 NumPy/TensorFlow seed 可复现同一软件栈，但 GPU/驱动升级仍可能改变末位结果。
- 当前 worker 为每次 RIR 启动一个隔离 Conda 进程。RTX 4090 D 上一次性 GPU 任务的 CUDA/JIT 启动成本使 6.36 s GPU 结果慢于约 4.2 s CPU 对照。动态生产运行应改为常驻 worker，才能摊销初始化成本。
- ODAS SST 输出单位方向，不能直接报告声源距离；manifest 中的距离来自 Isaac 真值，`distance_error_m` 对 ODAS 为 null。
- 动态分块卷积、RIR 阈值/频率限制、LRU 缓存、交叉淡化、时间戳元数据和 Isaac ROS 2 位姿发布已实现并单元测试；真实 AcoustiX 移动轨迹的长时间端到端性能验收尚未完成。当前在线演示复用离线静态 RIR，不应解释为动态声学验收。
- 当前测试房间转换器支持静态盒体。任意 USD Mesh 的拓扑清理、实例展开和材质子集导出仍需扩展后再用于生产场景。
- 数据集 smoke test 已生成真实双声源 RIR、WAV/RAW/NPZ/manifest 和 ODAS JSON；两路相关语音加随机传感器损伤时 ODAS 未形成有效 track，该结果如实保存在 manifest 中，尚不能作为多声源定位质量验收。
- 静态带家具房间和解析直达/TDOA 已验收；完整无边界自由场的 AcoustiX 专项场景、真实动态轨迹及 RGB/深度同步数据仍待补充。
