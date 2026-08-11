# miniDSP UMA-8 v2 实时 AoA 可视化

本项目把 UMA-8 的 8 通道 USB 音频送入 ODAS，稳健解析连续 JSON tracks，选择并平滑主声源方向，再用 matplotlib 极坐标盘实时显示 0～360° Angle of Arrival。关闭窗口或按 Ctrl+C 时会终止本程序启动的 ODAS 子进程。

## 当前硬件状态

- miniDSP UMA-8 v2，RAW 固件 `vf-raw-v1.3`，USB VID:PID `2752:001d`
- 48 kHz、8 通道、`S32_LE`（24 位有效数据）
- ALSA card 编号可能随重启或 USB 插拔变化；配置使用稳定名称 `hw:CARD=SPK,DEV=0`
- CH1～CH7 为实体麦克风；未连接且恒为零的 CH8 禁用

项目配置由现有 `odas/config/odaslive/uma8_v2.cfg` 复制而来；仅将不参与 AoA 的 separated/postfiltered RAW 文件输出改为 `blackhole`，避免实时运行持续占用磁盘。原配置未修改。

通道俯视映射（机器人正前方朝上）：

```text
                         0° Front (+X)
                    CH7 30°     CH6 330°
             CH2 90°       CH1       CH5 270°
                    CH3 150°    CH4 210°
                       180° Rear / USB
```

初始坐标（米）：

| 通道 | X | Y | Z | 方位 |
|---|---:|---:|---:|---|
| CH1 | 0.0000 | 0.0000 | 0.0000 | 中心 |
| CH2 | 0.0000 | 0.0422 | 0.0000 | 90° 左侧 |
| CH3 | -0.0366 | 0.0211 | 0.0000 | 150° 左后 |
| CH4 | -0.0366 | -0.0211 | 0.0000 | 210° 右后 |
| CH5 | 0.0000 | -0.0422 | 0.0000 | 270° 右侧 |
| CH6 | 0.0366 | -0.0211 | 0.0000 | 330° 右前 |
| CH7 | 0.0366 | 0.0211 | 0.0000 | 30° 左前 |

当前半径 42.2 mm 是初始几何模型，不是官方精确出厂标定。

坐标定义为 +X=机器人前方/0°、+Y=左侧/90°、-X=后方/180°、-Y=右侧/270°；俯视时角度从顶部开始逆时针增加。计算式为 `degrees(atan2(y, x)) % 360`。

## 安装与启动

首次使用：

```bash
cd /home/w/project/odas
./install_dependencies.sh
./tools/check_audio_device.sh
./run.sh
```

安装脚本沿用当前 `python3`（包括已激活的 conda 环境），不会删除或重建环境。若系统组件缺失，按提示安装 `python3-tk` 和 `python3-pip`。也可直接运行 `python3 -m uma8_visualizer.main`。

窗口中红色粗箭头是主声源，浅蓝细线是其他有效候选；底部显示 AoA、Track ID、activity、原始 x/y/z 和有效 track 数量。超过 0.8 秒没有有效源时隐藏箭头并显示等待提示。

常用参数：

```bash
./run.sh --angle-offset 9
./run.sh --input-file logs/tracks_20260804_120000.json --no-launch-odas
some_json_producer | ./run.sh --no-launch-odas
./run.sh --activity-threshold 0.08 --smoothing-alpha 0.18
```

最终显示角为 `(原始角 + angle_offset) % 360`。例如实测正前方为 351°，使用 `--angle-offset 9` 校准到 0°。离线文件到结尾后，界面保留最近源至超时，仍可正常关闭。

## 方位测试与诊断

依次在远场从机器人前、左、后、右发出稳定声音，界面应分别接近 0°、90°、180°、270°。测试时远离墙面，避免反射，并观察 activity，而不要只看瞬时箭头。可先记录原始输出：

```bash
./tools/record_tracks.sh 10
python3 tools/check_odas_output.py --seconds 4
python3 -m unittest discover -s tests -v
```

常见问题：

- **找不到 UMA-8**：运行 `arecord -l` 和 `./tools/check_audio_device.sh`；配置按稳定名称 `SPK` 打开设备，不需要随 card 编号变化手动修改。
- **Device or resource busy**：运行 `fuser -v /dev/snd/*` 找到占用者，关闭对应录音/音频服务；不要用 sudo 启动本程序。
- **找不到 odaslive**：确认 `/home/w/project/odas/build/bin/odaslive` 已构建且可执行，或传入 `--odas-bin PATH`。
- **终端只输出大量 `}`**：运行输出检查工具。若确认损坏，检查 ODAS 的 `src/sink/snk_tracks.c` 并备份后重建；本项目不会自动改 ODAS 源码。
- **没有 activity**：确认已刷 RAW 固件、CH1～CH7 有信号、采样格式/通道数正确，并在阵列附近制造稳定声音。
- **GUI 无法打开**：确认有图形桌面和 `DISPLAY`/Wayland 会话，检查 `python3-tk`、matplotlib；SSH 使用 X 转发或离线记录后在桌面环境查看。

## 实现与后续 ROS 2 接入

读取线程只向有界队列保留最新帧，避免 GUI 随 ODAS 高速输出而积累延迟。解析器按括号深度组装跨行/分块 JSON，跳过普通日志和损坏对象。轨迹先按 ID、activity、水平分量过滤；主 Track 达到保持阈值时优先保持，否则按 activity、水平可信度和 Z 降权评分。方向在单位向量上做指数平滑，正确跨越 0°。

后续可把 `TrackSelector` 的结果封装为 ROS 2 节点，发布带时间戳的 `aoa_deg`、track ID、activity 与方向向量；可视化作为订阅节点复用。建议再加入 TF 坐标变换、参数服务器、诊断消息和录包回放测试，而不把 GUI 耦合进采集节点。
