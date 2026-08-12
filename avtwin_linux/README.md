# AV-Twin Linux Controller

Linux 端负责连续 8 通道录音、从用户指定的输出设备播放 C1、接收 Android UDP、
在同一 PCM 时间轴中检测 C1/C2、估计 C2 RIR，并保存完整实验数据。Android
`t3_precise=false` 时不会输出虚假的精确 ToF 或距离。

## 采集模式与时间基准

界面保留原来的 `single` 单次握手，并新增：

- `manual_continuous`：点击“开始会话”只启动持续录音；每次点击“采集一次”才播放
  C1。一轮完成或超时后回到 `ARMED`，输入流不关闭。
- `timed_continuous`：确认自动发声后先倒计时 3 秒，随后按配置间隔自动触发。默认
  2.0 秒。间隔锚定相邻 C1 的实际声学检测 sample，而不是播放 API 调用时间；若到期时
  仍处于 `WAIT_C2`、`CAPTURE_TAIL` 或 `FINALIZE`，该周期会被跳过并写入日志和
  `session.json`。

持续模式使用显式状态机：

```text
IDLE -> ARMED -> C1_PLAYING -> WAIT_C2 -> CAPTURE_TAIL -> FINALIZE -> ARMED
```

会话期间输入和输出资源各打开一次；直接 ALSA 模式只在整个会话边界释放/恢复匹配的
PipeWire card profile。持续 Float32 原始录音直接流式写盘，有限环形缓冲区只用于当前轮
分析。`t1_sample` 和 `t4_sample` 都位于同一个 Linux PCM sample 时间轴。

## 图形界面（推荐）

```bash
cd /home/w/project/odas
./avtwin_linux/run.sh --gui
```

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
./avtwin_linux/run.sh --list-devices
```

预期推荐身份（数字 card 可变，stable name 不变）：

```text
Input : UMA-8 micArray RAW SPK | alsa:SPK:0 | plughw:CARD=SPK,DEV=0 | 8 ch
Output: ALC897 Analog / 3.5mm   | alsa:PCH:0 | plughw:CARD=PCH,DEV=0 | 2 ch
```

真实实验（设备编号按上一条输出填写）：

```bash
./avtwin_linux/run.sh \
  --input-device alsa:SPK:0 \
  --output-device alsa:PCH:0 \
  --output-channel 1 \
  --playback-gain 0.5 \
  --c1 /path/to/c1.wav \
  --c2 /path/to/c2.wav \
  --udp-port 5005 \
  --reply-timeout 5
```

推荐 C1/C2 为 48 kHz、mono、PCM16。其他采样率/通道数会自动转换并警告。
单次兼容模式仍生成 `output/YYYYMMDD_HHMMSS/`，包括完整 `raw_linux_8ch.wav`、
实际使用模板、每通道 Float32 RIR、检测/RIR 图、UDP JSONL、日志和 `result.json`。

持续 CLI 示例：

```bash
./avtwin_linux/run.sh \
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
{"type":"arm","protocol_version":1,"session_id":"...","measurement_id":12}
```

只接受当前 outstanding measurement 的版本 1 `reply_timing`：

```json
{"type":"reply_timing","protocol_version":1,"session_id":"...","measurement_id":12,"t3_precise":true,"reply_delay_samples":4821,"sample_rate":48000}
```

重复包去重；其他 measurement、其他 session 和已完成轮次的迟到包只写 UDP 审计日志，
不会关联到下一轮。未配置 ARM 目标时，可兼容“恰好一个 outstanding measurement”的旧
Android 消息，但结果会标记 `single_outstanding_compatibility` 和低可信度。

只有匹配消息明确给出 `t3_precise=true` 以及有效精确延迟时才计算：

```text
ToF = ((t4 - t1) / Linux_sample_rate - Android_reply_delay) / 2
```

否则 `exact_tof` 始终是 `NOT AVAILABLE`。

## 持续会话输出

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

1. 运行 `./avtwin_linux/run.sh --list-devices`，确认上述两个 stable name。
2. 运行 `./avtwin_linux/run.sh --gui`。输出下拉框应自动选择
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
