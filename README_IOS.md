# AV-Twin iPhone Responder v0.13.1

## 本次界面修复

- 热点地址固定从 `bridge100` 读取，不再把蜂窝接口 `pdp_ip0` 显示成 iPhone Wi-Fi/热点地址。
- Linux ARM/远程启停目标显示用户填写的 Linux 地址，而不是 iPhone 本机地址。
- ARKit 相机改为位姿坐标下方的独立预览，不再作为整个页面背景。
- 新增 XY 平面当前朝向图，以及手机相对水平面的俯仰/横滚视图。
- 相机预览内新增固定在 AR 世界空间中的 XYZ 可视原点，可在视野中央重新放置。
- 开始会话与采集控制移动到相机预览之后。
- C1/C2 模块新增 0–24 kHz 频谱缩略图，选择 WAV 后随探针更新。

## v0.13.1 修复

- 修复 C1/C2/结果目录分别叠加多个 `fileImporter` 时，iPhone 上按钮看似可用但文件选择器可能不弹出的情况；现在统一使用一个文件选择入口。
- 新增“允许 Linux 在空闲时远程启动 iPhone 会话”开关；关闭后不会在空闲状态监听 `START_CAPTURE`，但仍可在 iPhone 上手动开始 STRICT ARM 会话。
- 文件选择按钮增加状态提示，便于判断点击事件是否已触发。


面向 iPhone 15 Pro Max / iOS 17 的原生 SwiftUI + AVFoundation + ARKit 工程。当前功能已对齐 Android `v0.12.0-remote-safe-stop`，并增加 iPhone LiDAR/ARKit 自动位姿和由 iPhone 命令 Linux 立即插入一轮采集。

## 已实现功能

- 48 kHz 连续单声道采集；C1 归一化匹配和高频能量门限
- C1 默认 11–19 kHz/0.2 s，C2 默认 50 Hz–9 kHz/0.2 s
- 自定义 PCM 8/16/24/32-bit 或 IEEE float32 WAV
- 立体声/多声道 WAV 固定选择右声道，线性重采样到 48 kHz
- 显示源文件与内部 PCM SHA-256、左右声道峰值和内部峰值
- STRICT ARM、来源 IPv4 限制、session 绑定、event 幂等和 ARM ACK
- 稳定 `android_event_id`、最多三次 `reply_timing`、350 ms ACK 等待及 `reply_ack`
- UDP 双向 nonce 检验；运行中也能回复 Linux 发起的测试
- App 空闲时保持控制端口监听，支持 Linux 远程启动、`capture_ready` 和幂等远程安全停止
- C2 从持续打开的麦克风输入时间线做自声相关，声学检测成功才报告 `t3_precise=true`
- C1/t2 与 C2/t3 分别冻结接收位姿和发射位姿；回传 Linux 的 Tx 位姿取 C2 发声时刻
- Linux 定时自动采集运行时，可在 iPhone 点击“命令 Linux 立即采集一次”插入一轮；请求有来源校验、request-id 幂等和 ACK
- ARKit/LiDAR `sceneDepth` 自动位置与姿态，支持将当前位置设为原点和 +Z 向前
- Android 等价的手动 X/Y/Z、Yaw/Pitch/Roll 输入；可在运行中更新
- C1 接受瞬间冻结所选位姿并写入 JSON/CSV
- 暂停/继续监听、安全停止、800 ms 最小冷却
- C2 单次和 ×20 `dataPlayedBack` 硬件渲染回调测试
- 音频路由变化和系统中断监测；发生变化时本轮 `t3_precise=false`
- 可选择结果目录并持久恢复安全书签；未选择时使用 Documents/AVTwin
- 可选调试音频；保存 C1 窗口和 C2 参考 WAV
- 保存 `events.jsonl`、`manual_pose_records.csv`、`logs.txt`、`session.json`
- 保存 `probes/c1_used.wav`、`c2_used.wav` 和 `probe_metadata.json`
- 会话指标：状态、双方 session、measurement/pending ARM、成功/拒绝/失败计数、时序和路由

## 构建与真机运行

1. 在 macOS 上用 Xcode 16 或更高版本打开 `IndoorCoordinateTracker.xcodeproj`。
2. Target → Signing & Capabilities 中选择你的 Apple Developer Team；如 Bundle ID 冲突，改为自己的唯一 ID。
3. 连接 iPhone 15 Pro Max，选择真机后 Run。模拟器无法验证麦克风、扬声器、ARKit 或局域网握手。
4. 首次启动允许相机、麦克风和本地网络权限。
5. 填写 Linux Wi-Fi IPv4，默认 iPhone control `5006`、Linux result `5005`。
6. 先运行 UDP 双向检验和 C2 ×20，再启动 STRICT ARM 会话。
7. Linux 端把 ARM 发到界面显示的 `iPhone 热点 IPv4:5006`。

## GitHub Actions 构建 IPA

`.github/workflows/build-avtwin-ios.yml` 在 `macos-15` 上用 Xcode 编译：

- PR 或手动运行且 `sign_ipa=false`：生成 `AVTwinIOSResponder-v0.13.1-unsigned.ipa`。它用于验证和后续重签，不能直接装到普通未越狱 iPhone。
- 手动运行且 `sign_ipa=true`：导入你自己的证书和 Provisioning Profile，生成可安装的 `AVTwinIOSResponder-v0.13.1-signed.ipa`。

已签名模式需要在 GitHub 仓库配置以下 Actions Secrets：

- `APPSTORE_CERTIFICATES_FILE_BASE64`：包含私钥的 `.p12` 文件 Base64
- `APPSTORE_CERTIFICATES_PASSWORD`：`.p12` 密码
- `IOS_PROVISIONING_PROFILE_BASE64`：匹配目标 iPhone UDID 和 Bundle ID 的 `.mobileprovision` Base64
- `IOS_PROVISIONING_PROFILE_NAME`：Profile 名称
- `APPLE_TEAM_ID`：Apple Developer Team ID
- `IOS_BUNDLE_ID`：Profile 中登记的 Bundle ID

Development 与 Ad Hoc 安装都要求目标 iPhone 的 UDID 已包含在 Profile 中。证书和私钥不能提交到 Git；只能放在加密 Secrets。

## 坐标与兼容性

ARKit 模式是相对坐标，不是 GPS 经纬度：X 向右、Y 向上、Z 向前，单位米。手动模式使用 Android 相同的 ZYX yaw/pitch/roll 约定。线协议保留 `android_event_id` 和 `android_pose_*` 兼容字段，同时增加 `ios_*` 字段，因此现有 Linux v0.9 不需要改协议解析。

## 验证边界

当前工作环境不是 macOS，没有 Apple SDK 和 `xcodebuild`。Linux 协议测试已覆盖按钮插入定时自动采集；最终 Swift 编译由上述 GitHub Actions 完成。音频路由、C1/C2 阈值、麦克风自声 t3 和 ARKit 位姿仍必须在 iPhone 15 Pro Max 真机验收。
