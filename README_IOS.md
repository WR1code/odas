# AV-Twin iPhone Responder v0.14.0

## v0.14.0 空间坐标系自动标定

- 新增 iPhone `sceneDepth` 空间扫描：在当前用户 AR 原点下抽取深度点、过滤低置信度值，
  并以 8 cm 体素实时累积，避免保存每帧重复点。
- 新增“开始手机空间扫描”“停止扫描”“上传并自动标定”。点云通过独立 HTTP `5010`
  上传到 Linux，不与声学 UDP 端口复用。
- 新增“让 Linux 开始采集 MID-360S 地图（12秒）”。它复用远程启停的可信 UDP 控制
  配置，使用稳定 `command_id`、三次发送、ACK、最终状态和 Linux 端幂等缓存；Linux 完成
  雷达地图后会自动启动 `5010` 标定服务。
- Linux 将手机地图与固定 MID-360S/FAST-LIO 地图进行重力约束多初值 ICP；只有重叠率、
  残差和旋转多解检查全部通过才生成可应用的 `active_transform.json`。
- 扫描期间禁止重置手机原点；后续声学采集也必须保持同一次 AR 原点，否则旧变换立即失效。

## iOS 同步控制 Linux 会话

- 在 iPhone 点击“同步开始 iOS + Linux”后，iOS 会先完成麦克风、音频引擎和 UDP 控制端口初始化，再向 Linux 结果端口重复发送 `linux_session_start_request`。
- Linux GUI 必须保持打开、处于空闲状态，并选择 `manual_continuous` 或 `timed_continuous`；Linux 配置的移动端 IP、Linux 结果端口和 iPhone 控制端口必须与 iOS 一致。
- 点击“同步安全停止”会重复发送 `linux_session_stop_request`，随后安全保存 iOS 本地数据；Linux 收到后也会结束连续采集并保存。
- Linux 主动远程启动或停止 iOS 时，iOS 不会把同一命令反向回送，避免启停回环。

## 本次界面修复

- 热点地址固定从 `bridge100` 读取，不再把蜂窝接口 `pdp_ip0` 显示成 iPhone Wi-Fi/热点地址。
- Linux ARM/远程启停目标显示用户填写的 Linux 地址，而不是 iPhone 本机地址。
- ARKit 相机改为位姿坐标下方的独立预览，不再作为整个页面背景。
- 新增 XY 平面当前朝向图和圆形手机水平仪；倾斜点回到明确标注的水平中心即为水平。
- 相机预览内的 XYZ 轴严格放在重置瞬间的手机 AR 位置；同一个按钮同时重设位姿与可视原点。
- 相机视野动态绘制当前位置到原点的黄色连线，并在线上标注实时距离。
- 开始会话与采集控制移动到相机预览之后。
- C1/C2 模块新增 0–24 kHz 频谱缩略图，选择 WAV 后随探针更新。

## v0.13.1 修复

- 修复 C1/C2/结果目录分别叠加多个 `fileImporter` 时，iPhone 上按钮看似可用但文件选择器可能不弹出的情况；现在统一使用一个文件选择入口。
- 新增“允许 Linux 在空闲时远程启动 iPhone 会话”开关；关闭后不会在空闲状态监听 `START_CAPTURE`，但仍可在 iPhone 上手动开始 STRICT ARM 会话。
- 文件选择按钮增加状态提示，便于判断点击事件是否已触发。

## v0.13.2 改进

- 修正竖屏手机机身坐标映射：手机竖直、摄像头朝向水平时横滚角为 0°，不再偏置约 90°。
- 每次 C1 → C2 硬件播放成功后，在相机预览中保留手机当时位置的 AR 坐标点。
- 采集点显示采集序号、measurement ID 和 XYZ，小号标签并按六种颜色循环，支持一键清空。

## v0.13.3 改进

- 顶部显示 iOS 系统热状态：正常、偏热、过热或严重过热；公开 API 不提供虚假的摄氏温度。
- UDP 双向检验拥有独立状态，成功后按钮保持绿色，并明确提示它不等于声学质量或 ToF 成功。
- Linux 每轮完成后向 iOS 回传 `measurement_quality`；AR 点黄色表示等待判定、绿色表示质量通过、红色表示失败。
- 质量消息使用 session 与 measurement 双重关联、三次幂等发送及 iOS ACK，避免串轮或偶发 UDP 丢包。

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
- 原生 SwiftUI 实时声学四图：iPhone 麦克风波形、C1 匹配滤波 remote RIR、最近 2 秒 Chirp Hann-STFT 时频图、0–24 kHz 接收频谱
- C1/C2 探针显示实际 WAV 的 Peak、RMS、能量、峰均比、估计扫频范围与线性度，并分别显示 dBFS 频谱和时频图
- Linux 定时自动采集运行时，可在 iPhone 点击“命令 Linux 立即采集一次”插入一轮；请求有来源校验、request-id 幂等和 ACK
- ARKit/LiDAR `sceneDepth` 自动位置与姿态，使用 X 朝前、Y 朝左、Z 朝上的 FLU 世界坐标
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

- PR 或手动运行且 `sign_ipa=false`：生成 `AVTwinIOSResponder-v0.14.0-unsigned.ipa`。它用于验证和后续重签，不能直接装到普通未越狱 iPhone。
- 手动运行且 `sign_ipa=true`：导入你自己的证书和 Provisioning Profile，生成可安装的 `AVTwinIOSResponder-v0.14.0-signed.ipa`。

已签名模式需要在 GitHub 仓库配置以下 Actions Secrets：

- `APPSTORE_CERTIFICATES_FILE_BASE64`：包含私钥的 `.p12` 文件 Base64
- `APPSTORE_CERTIFICATES_PASSWORD`：`.p12` 密码
- `IOS_PROVISIONING_PROFILE_BASE64`：匹配目标 iPhone UDID 和 Bundle ID 的 `.mobileprovision` Base64
- `IOS_PROVISIONING_PROFILE_NAME`：Profile 名称
- `APPLE_TEAM_ID`：Apple Developer Team ID
- `IOS_BUNDLE_ID`：Profile 中登记的 Bundle ID

Development 与 Ad Hoc 安装都要求目标 iPhone 的 UDID 已包含在 Profile 中。证书和私钥不能提交到 Git；只能放在加密 Secrets。

## 坐标与兼容性

ARKit 模式是相对坐标，不是 GPS 经纬度：每次重置时，以手机投影到水平面的前方为 +X、左侧为 +Y，世界重力反方向为 +Z，单位米，构成右手 FLU 坐标系。Yaw 绕 +Z 且朝 +Y（左转）为正，Pitch 机头上抬为正，Roll 绕前向 +X。手动模式使用同一套导航角约定。线协议保留 `android_event_id` 和 `android_pose_*` 兼容字段，同时增加 `ios_*` 字段；接收端应以 `ios_pose_frame_id=arkit_user_origin_x_forward_y_left_z_up` 识别该坐标语义。

## 验证边界

当前工作环境不是 macOS，没有 Apple SDK 和 `xcodebuild`。Linux 协议测试已覆盖按钮插入定时自动采集；最终 Swift 编译由上述 GitHub Actions 完成。音频路由、C1/C2 阈值、麦克风自声 t3 和 ARKit 位姿仍必须在 iPhone 15 Pro Max 真机验收。
