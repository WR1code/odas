# AV-Twin Android v0.12 与 iOS v0.13 功能比对

基准 APK：`avtwin_android_build/dist/AVTwinAndroidResponder-v0.12.0-remote-safe-stop-debug.apk`，基准实现为同目录 Android v0.12 源码。`apk-output/` 中的 v0.9.x 是旧包，不作为当前功能基线。

| 功能 | Android v0.12 | iOS v0.13 | 说明 |
|---|---:|---:|---|
| 48 kHz 持续录音、C1 匹配与频带门控 | 是 | 是 | iOS 使用 AVAudioEngine 输入 tap |
| 内置/自定义 WAV、右声道选择、48 kHz 重采样、SHA-256 | 是 | 是 | 协议和诊断字段对齐 |
| STRICT ARM、来源 IP、session 绑定、事件幂等、ARM ACK | 是 | 是 | 一个 ARM 最多授权一次 C2 |
| `reply_timing` 三次发送、350 ms ACK、稳定 event ID | 是 | 是 | 保留 `android_event_id` 兼容字段 |
| 空闲 UDP 双向检验监听 | 是 | 是 | App 打开且未采集时仍监听控制端口 |
| Linux `start_capture` 远程启动与 `capture_ready` | 是 | 是 | iOS 在音频输入和控制端口就绪后通知 Linux |
| Linux `stop_capture` 幂等安全停止 | 是 | 是 | 已停止时返回 `already_stopped` |
| C2 播放失败/硬件回调诊断 | 是 | 是 | iOS 另保留 ×20 稳定性测试 |
| 持续麦克风时间线上的本机 C2 声学 t3 | 是 | 是 | iOS 不再把播放计划时间当精确 t3 |
| C2 发声时冻结 Tx 位姿 | 是 | 是 | iOS 同时保留 C1/t2 Rx 位姿事件 |
| 暂停、继续、800 ms 冷却、安全保存 | 是 | 是 | pending ARM 在暂停时清除 |
| 手动 6DoF 位姿运行中更新 | 是 | 是 | ZYX yaw/pitch/roll |
| 调试 WAV、events、CSV、logs、session、probe metadata | 是 | 是 | iOS 默认保存到 Documents/AVTwin，也可选目录 |
| 单次 C2 与 ×20 播放测试 | 单次 | 单次 + ×20 | iOS 是超集 |
| 自动 SLAM/LiDAR 位姿 | 否 | 是 | iOS 的 ARKit/LiDAR 扩展 |
| 手机按钮命令 Linux 插入一次采集 | 否 | 是 | iOS v0.13 新增，定时自动模式也可用 |
| 手机按钮同步启停 Linux 连续会话 | 是 | 是 | 发送幂等 `linux_session_start_request` / `linux_session_stop_request`；Linux 校验来源 IP 与双端口 |

## “立即采集一次”协议

1. iOS 向 Linux 结果端口发送 `capture_once_request`，携带唯一 `request_id` 和 iOS 控制端口。
2. Linux 只接受配置的移动端 IP，校验协议版本与控制端口，并用 request-id 缓存结果以防重复触发。
3. Linux 仅在连续采集状态为 `ARMED` 且未暂停时排队一轮；忙碌、暂停或未启动会拒绝。
4. Linux 向 iOS 控制端口发送 `capture_once_ack`。在 `timed_continuous` 中，该轮立即插入，既有自动调度仍遵守不重叠规则。

## 尚需真机验收

- iPhone 内置扬声器到内置麦克风的 C2 相关分数是否稳定高于 0.25。
- 音频 route change、来电/系统中断以及锁屏前台策略。
- ARKit `sceneDepth`、手动位姿和 Linux 坐标系的现场对齐。
- Development/Ad Hoc Profile 是否包含目标 iPhone UDID，签名 IPA 能否安装并首次授权相机、麦克风和局域网。
