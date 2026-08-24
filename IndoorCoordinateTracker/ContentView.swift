import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @Environment(\.scenePhase) private var scenePhase
    @FocusState private var isInputFocused: Bool
    @ObservedObject var poseTracker: PoseTracker
    @StateObject private var poseSelection: PoseSelectionStore
    @StateObject private var responder: AcousticResponder
    @StateObject private var probes = ProbeSelectionStore()
    @StateObject private var folder = FolderSelectionStore()
    @StateObject private var thermalMonitor = DeviceThermalMonitor()
    @AppStorage("linuxHost") private var linuxHost = "192.168.1.100"
    @AppStorage("controlPort") private var controlPort = "5006"
    @AppStorage("resultPort") private var resultPort = "5005"
    @AppStorage("calibrationPort") private var calibrationPort = "5010"
    @AppStorage("saveDebugAudio") private var saveDebugAudio = false
    @AppStorage("linuxRemoteStartEnabled") private var linuxRemoteStartEnabled = true
    @AppStorage("sharedOriginMode") private var sharedOriginMode = "linux_microphone"
    @AppStorage("manualX") private var manualX = "0"
    @AppStorage("manualY") private var manualY = "0"
    @AppStorage("manualZ") private var manualZ = "0"
    @AppStorage("manualYaw") private var manualYaw = "0"
    @AppStorage("manualPitch") private var manualPitch = "0"
    @AppStorage("manualRoll") private var manualRoll = "0"
    @State private var importTarget: FileImportTarget = .c1
    @State private var isImportingFile = false
    @State private var importStatus: String?
    @State private var showingLog = false
    @State private var networkSnapshot = LocalNetworkInfo.snapshot()
    @State private var selectedPointCloud: PointCloudViewerSelection?
    @State private var visualOriginRevision = 0

    init(poseTracker: PoseTracker) {
        self.poseTracker = poseTracker
        let selection = PoseSelectionStore(arkitSnapshot: { [weak poseTracker] in poseTracker?.snapshot() ?? .unavailable })
        _poseSelection = StateObject(wrappedValue: selection)
        _responder = StateObject(wrappedValue: AcousticResponder(
            poseSnapshot: { selection.snapshot() },
            captureCompleted: { [weak poseTracker] sessionID, measurementID, pose in
                poseTracker?.recordCapturePoint(
                    sessionID: sessionID, measurementID: measurementID, pose: pose
                )
            },
            measurementQualityReceived: { [weak poseTracker] sessionID, measurementID, passed, detail in
                poseTracker?.updateCaptureQuality(
                    sessionID: sessionID, measurementID: measurementID,
                    passed: passed, detail: detail
                )
            }
        ))
    }

    var body: some View {
        ZStack {
            LinearGradient(colors: [Color(uiColor: .systemBackground), Color(uiColor: .secondarySystemBackground)], startPoint: .top, endPoint: .bottom)
                .ignoresSafeArea()
            ScrollView {
                VStack(spacing: 12) {
                    headerCard
                    probeCard
                    AcousticMonitorCard(monitor: responder.monitor, isRunning: responder.isRunning)
                    poseCard
                    spatialCalibrationCard
                    sessionCard
                    networkCard
                    storageCard
                    testCard
                    metricsCard
                    if showingLog { logCard }
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .onAppear {
            refreshNetworkSnapshot()
            configureIdleListener()
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { refreshNetworkSnapshot() }
        }
        .onChange(of: idleConfigurationKey) { _, _ in
            responder.resetUDPTestState()
            configureIdleListener()
        }
        .onChange(of: responder.lidarMapCaptureGeneration) { _, _ in
            if let port = UInt16(calibrationPort) {
                poseTracker.downloadLinuxLidarMap(host: linuxHost, port: port)
            }
        }
        .onChange(of: responder.phoneOriginResetGeneration) { _, _ in
            poseTracker.resetOrigin()
            visualOriginRevision += 1
        }
        .onDisappear { responder.shutdown() }
        .fileImporter(isPresented: $isImportingFile, allowedContentTypes: importTarget.allowedContentTypes) { result in
            handleImportResult(result, target: importTarget)
        }
        .fullScreenCover(item: $selectedPointCloud) { selection in
            PointCloudViewerScreen(selection: selection)
        }
        .toolbar {
            ToolbarItemGroup(placement: .keyboard) {
                Spacer()
                Button("完成") { isInputFocused = false }
            }
        }
    }

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: responder.isRunning ? "wave.3.right.circle.fill" : "iphone.gen3")
                    .font(.system(size: 34)).foregroundStyle(responder.isRunning ? .green : .cyan)
                VStack(alignment: .leading, spacing: 3) {
                    Text("AV-Twin iOS Responder v0.15.1").font(.headline)
                    Text("与 Android v0.12 对齐：远程启停、声学 t3、STRICT ARM").font(.caption2).foregroundStyle(.secondary)
                    Text(responder.status).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if responder.isRunning { ProgressView().tint(.green) }
            }
            HStack(spacing: 7) {
                Circle().fill(thermalMonitor.color).frame(width: 9, height: 9)
                Text("设备热状态：\(thermalMonitor.label)").font(.caption.bold())
                Spacer()
                Text("iOS 不提供精确 ℃").font(.caption2).foregroundStyle(.secondary)
            }
        }.card()
    }

    private var probeCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("C1 / C2 声学探针", systemImage: "waveform").font(.subheadline.bold())
            probeRow(title: "C1", probe: probes.c1, select: { beginImport(.c1) }, reset: { probes.useDefault(.c1) })
            Divider()
            probeRow(title: "C2", probe: probes.c2, select: { beginImport(.c2) }, reset: { probes.useDefault(.c2) })
            if let error = probes.lastError { Text("WAV 错误：\(error)").font(.caption).foregroundStyle(.red) }
            if let importStatus { Text(importStatus).font(.caption2).foregroundStyle(.secondary) }
        }.disabled(responder.isRunning || responder.isTestingC2).card()
    }

    private func probeRow(title: String, probe: ProbeDefinition, select: @escaping () -> Void, reset: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(title): \(probe.summary)").font(.caption)
            Text(probe.diagnostics).font(.system(size: 9, design: .monospaced)).textSelection(.enabled).foregroundStyle(.secondary)
            ProbeDetailVisualization(title: title, probe: probe, tint: title == "C1" ? .cyan : .orange)
            HStack {
                Button("选择 \(title) WAV", action: select).buttonStyle(.bordered)
                Button("恢复默认", action: reset).buttonStyle(.bordered)
            }
        }
    }

    private var poseCard: some View {
        let pose = poseTracker.currentPose
        return VStack(alignment: .leading, spacing: 9) {
            Label("响应位姿", systemImage: "viewfinder").font(.subheadline.bold())
            Picker("位姿来源", selection: Binding(
                get: { poseSelection.mode }, set: { poseSelection.setMode($0) }
            )) { ForEach(PoseSourceMode.allCases) { Text($0.rawValue).tag($0) } }
            .pickerStyle(.segmented)
            Text("世界坐标：重置时手机水平前方为 +X、左侧为 +Y，世界向上为 +Z")
                .font(.caption2).foregroundStyle(.secondary)
            if poseSelection.mode == .arkit {
                HStack { coordinate("X", pose.position.x); coordinate("Y", pose.position.y); coordinate("Z", pose.position.z) }
                Text(String(format: "yaw %.1f°  pitch %.1f°  roll %.1f° | %@", pose.yawDegrees, pose.pitchDegrees, pose.rollDegrees, poseTracker.statusText))
                    .font(.caption.monospacedDigit())
                XYHeadingView(yawDegrees: responder.sharedYawDegrees(for: pose))
                    .frame(height: 150)
                BubbleLevelView(pitchDegrees: pose.pitchDegrees, rollDegrees: pose.rollDegrees)
                    .frame(height: 205)
                VStack(alignment: .leading, spacing: 5) {
                    Label("相机预览", systemImage: "camera.fill").font(.caption.bold())
                    ZStack {
                        ARCameraView(
                            poseTracker: poseTracker,
                            visualOriginRevision: visualOriginRevision,
                            sharedCoordinates: responder.sharedVisualization
                        )
                        Image(systemName: "plus")
                            .font(.system(size: 22, weight: .light))
                            .foregroundStyle(.white.opacity(0.8))
                            .shadow(radius: 2)
                        VStack {
                            Spacer()
                            Text("红X=前 · 绿Y=左 · 蓝Z=上 · 黄线=原点距离")
                                .font(.caption2)
                                .padding(.horizontal, 8).padding(.vertical, 4)
                                .background(.black.opacity(0.55), in: Capsule())
                                .foregroundStyle(.white)
                                .padding(.bottom, 8)
                        }
                    }
                    .frame(height: 230)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .overlay { RoundedRectangle(cornerRadius: 12).stroke(.white.opacity(0.18)) }
                    HStack {
                        Text("已保留 \(poseTracker.capturePoints.count) 个采集点；黄=等待，绿=成功，红=失败")
                            .font(.caption2).foregroundStyle(.secondary)
                        Spacer()
                        if !poseTracker.capturePoints.isEmpty {
                            Button("清空标记") { poseTracker.clearCapturePoints() }
                                .font(.caption2).buttonStyle(.bordered)
                        }
                    }
                }
                Picker("共享原点", selection: $sharedOriginMode) {
                    Text("Linux / UMA-8 原点").tag("linux_microphone")
                    Text("手机当前位置原点").tag("iphone_current")
                }
                .pickerStyle(.segmented)
                Button {
                    responder.requestSharedOrigin(mode: sharedOriginMode)
                } label: {
                    Label(
                        sharedOriginMode == "linux_microphone"
                            ? "命令 Linux：以 UMA-8 中心设为共享零点"
                            : "以手机当前位置重置共享零点与 AR 原点",
                        systemImage: "scope"
                    ).frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(
                    pose.trackingState != "tracking" || responder.isRunning
                    || poseTracker.isSpatialScanning
                    || responder.sharedOriginRequestInFlight
                    || !configurationValid || !linuxRemoteStartEnabled
                )
                sharedCoordinateSummary(pose: pose)
            } else {
                HStack { numberField("X m", $manualX); numberField("Y m", $manualY); numberField("Z m", $manualZ) }
                HStack { numberField("Yaw°", $manualYaw); numberField("Pitch°", $manualPitch); numberField("Roll°", $manualRoll) }
                Button {
                    _ = poseSelection.applyManual([manualX, manualY, manualZ, manualYaw, manualPitch, manualRoll])
                } label: { Text("应用当前位姿（下一次 C1 使用）").frame(maxWidth: .infinity) }
                .buttonStyle(.bordered)
                Text(poseSelection.manualSummary).font(.caption2).foregroundStyle(.secondary)
            }
            Text("每次有效 C1 都会冻结当时选中的位姿；会话运行时仍可更新。")
                .font(.caption2).foregroundStyle(.secondary)
        }.card()
    }

    private var networkCard: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label("Wi-Fi / UDP", systemImage: "network").font(.subheadline.bold())
            Text("当前连接：\(networkSnapshot.mode)\n\(networkSnapshot.addressLabel)：\(networkSnapshot.localIPv4)\nLinux ARM/远程启停目标：\(linuxHost):\(controlPort)\n接口：\(LocalNetworkInfo.display())")
                .font(.caption.monospaced()).textSelection(.enabled)
            if let warning = networkSnapshot.linuxTargetWarning(host: linuxHost) {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption2).foregroundStyle(.orange)
            }
            TextField("Linux 当前网络 IPv4", text: $linuxHost).keyboardType(.numbersAndPunctuation).focused($isInputFocused).fieldStyle()
            HStack {
                TextField("iPhone ARM 端口", text: $controlPort).keyboardType(.numberPad).focused($isInputFocused).fieldStyle()
                TextField("Linux 结果端口", text: $resultPort).keyboardType(.numberPad).focused($isInputFocused).fieldStyle()
                TextField("标定 HTTP", text: $calibrationPort).keyboardType(.numberPad).focused($isInputFocused).fieldStyle()
            }
            Toggle("允许 Linux 在空闲时远程启动 iPhone 会话", isOn: $linuxRemoteStartEnabled)
                .font(.caption)
            Text(linuxRemoteStartEnabled ? "已开启：空闲时监听控制端口，Linux 可发送 START_CAPTURE。" : "已关闭：空闲时不监听远程启动；仍可在本机手动开始会话。")
                .font(.caption2).foregroundStyle(.secondary)
        }.disabled(responder.isRunning).card()
    }

    private func sharedCoordinateSummary(pose: DevicePose) -> some View {
        let phone = responder.sharedPhonePosition(for: pose)
        let linux = responder.sharedLinuxPosition
        return VStack(alignment: .leading, spacing: 5) {
            Text(responder.sharedOriginStatus)
                .font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
            HStack {
                Label("iPhone", systemImage: "iphone")
                Spacer()
                Text(positionText(phone)).monospacedDigit()
            }
            HStack {
                Label("Linux / UMA-8", systemImage: "desktopcomputer")
                Spacer()
                Text(positionText(linux)).monospacedDigit()
            }
            Text("共享 frame：\(responder.sharedFrameID)；AR 中黄色=共享原点，紫色=Linux/UMA-8。")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .font(.caption)
        .padding(9)
        .background(.black.opacity(0.12), in: RoundedRectangle(cornerRadius: 9))
    }

    private func positionText(_ position: SIMD3<Double>?) -> String {
        guard let position else { return "X --  Y --  Z --" }
        return String(format: "X %+.3f  Y %+.3f  Z %+.3f m", position.x, position.y, position.z)
    }

    private var spatialCalibrationCard: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label("MID-360S / iPhone 空间坐标系标定", systemImage: "point.3.connected.trianglepath.dotted")
                .font(.subheadline.bold())
            Text("保持当前 AR 原点不变，缓慢绕雷达扫描双方都能看到的静态墙角、桌面和物体。上传后 Linux 自动进行重力约束点云配准。")
                .font(.caption2).foregroundStyle(.secondary)
            Button {
                responder.requestLinuxLidarMapCapture(durationSeconds: 12)
            } label: {
                Label("让 Linux 开始采集 MID-360S 地图（12秒）", systemImage: "sensor.tag.radiowaves.forward")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent).tint(.indigo)
            .disabled(!configurationValid || !linuxRemoteStartEnabled)
            Text(responder.lidarMapCaptureStatus)
                .font(.caption.monospacedDigit()).foregroundStyle(.secondary).textSelection(.enabled)
            HStack {
                Button {
                    if let port = UInt16(calibrationPort) {
                        poseTracker.downloadLinuxLidarMap(host: linuxHost, port: port)
                    }
                } label: {
                    Label("下载/刷新 Linux 点云", systemImage: "arrow.down.circle")
                }
                .buttonStyle(.bordered)
                .disabled(UInt16(calibrationPort) == nil)
                Text(poseTracker.linuxLidarMapStatus)
                    .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            }
            if let cloud = poseTracker.linuxLidarMap {
                pointCloudPanel(
                    cloud: cloud,
                    title: "Linux MID-360S 下载点云",
                    tint: .indigo,
                    height: 300,
                    note: "已下载到手机；可直接旋转、缩放、拖动，或进入全屏查看。"
                )
            }
            HStack {
                if poseTracker.isSpatialScanning {
                    Button("停止扫描") { poseTracker.stopSpatialScan() }
                        .buttonStyle(.borderedProminent).tint(.orange)
                } else {
                    Button("开始手机空间扫描") { poseTracker.startSpatialScan() }
                        .buttonStyle(.borderedProminent).tint(.cyan)
                        .disabled(poseTracker.currentPose.trackingState != "tracking")
                }
                Button("上传并自动标定") {
                    if let port = UInt16(calibrationPort) {
                        poseTracker.uploadSpatialScan(host: linuxHost, port: port)
                    }
                }
                .buttonStyle(.bordered)
                .disabled(poseTracker.isSpatialScanning || poseTracker.spatialScanPointCount < 80 || UInt16(calibrationPort) == nil)
            }
            Text("手机点云：\(poseTracker.spatialScanPointCount) 点 | \(poseTracker.spatialCalibrationStatus)")
                .font(.caption.monospacedDigit()).textSelection(.enabled)
            spatialReadinessPanel
            if let cloud = poseTracker.phoneSpatialPreview {
                pointCloudPanel(
                    cloud: cloud,
                    title: poseTracker.isSpatialScanning ? "iPhone 实时扫描点云" : "iPhone 本次扫描点云",
                    tint: .cyan,
                    height: 300,
                    note: "扫描时约每0.5秒刷新且保留当前视角；可继续旋转、缩放、拖动，确认画面中包含非对称结构。"
                )
            }
        }
        .disabled(responder.isRunning)
        .card()
    }

    private var spatialReadinessPanel: some View {
        let readiness = poseTracker.spatialReadiness
        return VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label("统一坐标系标定就绪度", systemImage: "scope")
                    .font(.caption.bold())
                Spacer()
                Text("\(readiness.score)%")
                    .font(.headline.monospacedDigit()).foregroundStyle(readinessColor)
            }
            ProgressView(value: Double(readiness.score), total: 100)
                .tint(readinessColor)
            Text(readiness.phase).font(.caption.bold()).foregroundStyle(readinessColor)
            VStack(spacing: 3) {
                HStack {
                    Text("手机覆盖 \(readiness.coverageScore)%")
                    Spacer()
                    Text(readiness.overlapRatio.map { String(format: "重叠 %.0f%%", $0 * 100) } ?? "重叠 --")
                }
                HStack {
                    Text(readiness.rmseM.map { String(format: "RMSE %.3fm", $0) } ?? "RMSE --")
                    Spacer()
                    Text("连续稳定 \(readiness.stableUpdates)/3")
                }
            }
            .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
            Text(readiness.guidance).font(.caption2)
            Text("实时值是轻量预估；达到就绪后仍须上传，由 Linux 完整配准最终确认并激活坐标变换。")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(10)
        .background(readinessColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
        .overlay { RoundedRectangle(cornerRadius: 10).stroke(readinessColor.opacity(0.30)) }
    }

    private var readinessColor: Color {
        switch poseTracker.spatialReadiness.score {
        case 85...: return .green
        case 55...: return .orange
        default: return .cyan
        }
    }

    @ViewBuilder
    private func pointCloudPanel(
        cloud: SpatialPointCloudSnapshot,
        title: String,
        tint: Color,
        height: CGFloat,
        note: String
    ) -> some View {
        HStack {
            Label(title, systemImage: "viewfinder.circle").font(.caption.bold())
            Spacer()
            Text("\(cloud.points.count) 点").font(.caption2.monospacedDigit())
        }
        SpatialPointCloudPreview(cloud: cloud)
            .frame(height: height)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay { RoundedRectangle(cornerRadius: 12).stroke(tint.opacity(0.45)) }
        HStack(alignment: .top) {
            Text(note).font(.caption2).foregroundStyle(.secondary)
            Spacer()
            Button {
                selectedPointCloud = PointCloudViewerSelection(title: title, tint: tint, cloud: cloud)
            } label: {
                Label("全屏查看", systemImage: "arrow.up.left.and.arrow.down.right")
            }
            .font(.caption2).buttonStyle(.bordered)
        }
        Text("单指旋转 · 双指缩放 · 双指拖动 · 颜色表示高度")
            .font(.caption2).foregroundStyle(.secondary)
    }

    private func refreshNetworkSnapshot() {
        networkSnapshot = LocalNetworkInfo.snapshot()
    }

    private var storageCard: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label("结果保存", systemImage: "folder").font(.subheadline.bold())
            Text(folder.status).font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("选择结果目录") { beginImport(.folder) }.buttonStyle(.bordered)
                Button("使用 App 默认目录") { folder.clear() }.buttonStyle(.bordered)
            }
            Toggle("保存调试音频（C1 窗口和 C2 参考）", isOn: $saveDebugAudio).font(.caption)
        }.disabled(responder.isRunning).card()
    }

    private var sessionCard: some View {
        VStack(spacing: 10) {
            if responder.isRunning {
                Button { responder.requestCaptureOnce() } label: {
                    Label("命令 Linux 立即采集一次", systemImage: "record.circle").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(.blue).disabled(responder.isPaused)
                Text(responder.captureRequestStatus).font(.caption2).foregroundStyle(.secondary)
                HStack {
                    Button(responder.isPaused ? "继续监听" : "暂停监听") {
                        responder.isPaused ? responder.resumeListening() : responder.pauseListening()
                    }.buttonStyle(.borderedProminent).tint(.orange)
                    Button("同步安全停止", role: .destructive) { responder.stop() }.buttonStyle(.borderedProminent)
                }
            } else {
                Button { startSession() } label: { Label("同步开始 iOS + Linux", systemImage: "play.circle.fill").frame(maxWidth: .infinity) }
                    .buttonStyle(.borderedProminent).tint(.green).disabled(!configurationValid || responder.isTestingC2)
            }
        }.card()
    }

    private var testCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("连通与播放测试", systemImage: "checkmark.circle").font(.subheadline.bold())
            HStack {
                Button {
                    if let port = UInt16(resultPort) { responder.testUDP(host: linuxHost, port: port) }
                } label: {
                    Label(udpTestButtonTitle, systemImage: udpTestButtonIcon)
                }
                .buttonStyle(.borderedProminent)
                .tint(udpTestButtonColor)
                .disabled(responder.isRunning || responder.isTestingC2 || responder.udpTestState == .testing)
                Button("TEST C2 单次") { responder.testC2Once(probes.c2) }
                    .buttonStyle(.bordered).disabled(responder.isRunning || responder.isTestingC2)
            }
            Text("UDP：\(responder.udpTestSummary)；只表示网络往返，不代表本轮声学质量或 ToF 成功。")
                .font(.caption2).foregroundStyle(udpTestButtonColor)
            Button("TEST C2 ×20 稳定性") { responder.testC2Repeated(probes.c2) }
                .buttonStyle(.bordered).disabled(responder.isRunning || responder.isTestingC2)
            if !responder.c2TestProgress.isEmpty {
                Text(responder.c2TestProgress).font(.system(size: 9, design: .monospaced)).textSelection(.enabled)
            }
        }.card()
    }

    private var metricsCard: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack { Label("会话指标", systemImage: "gauge.with.dots.needle.67percent").font(.subheadline.bold()); Spacer(); Text(responder.stateName).font(.caption.monospaced()).foregroundStyle(.green) }
            Text("iOS 本地 session：\(responder.localSessionID ?? "--")\nLinux session：\(responder.pairedLinuxSessionID ?? "--（等待 ARM）")\nmeasurement=\(responder.activeMeasurement.map { String($0) } ?? "--") | pending ARM=\(responder.pendingArmMeasurement.map { String($0) } ?? "--")\n成功=\(responder.successfulResponses) | C1 未通过=\(responder.c1Rejected) | C2 失败=\(responder.c2Failures) | UDP 失败=\(responder.udpFailures)\nLinux质量=\(responder.lastLinuxQuality)\nreply_delay_samples=\(responder.lastReplyDelaySamples.map { String($0) } ?? "--") | t3_precise=\(responder.lastT3Precise)\ninput=\(responder.inputRoute)\noutput=\(responder.outputRoute)")
                .font(.system(size: 10, design: .monospaced)).textSelection(.enabled)
            HStack {
                Button(showingLog ? "隐藏诊断日志" : "显示诊断日志") { showingLog.toggle() }
                Button("清空界面日志") { responder.clearVisibleLog() }
            }.font(.caption)
            if let url = responder.sessionShareURL { ShareLink(item: url) { Label("导出会话目录", systemImage: "square.and.arrow.up") } }
        }.card()
    }

    private var logCard: some View {
        ScrollView(.horizontal) { Text(responder.logText.isEmpty ? "暂无日志" : responder.logText).font(.system(size: 9, design: .monospaced)).textSelection(.enabled) }
            .frame(maxHeight: 250).card()
    }

    private var configurationValid: Bool { !linuxHost.isEmpty && UInt16(controlPort) != nil && UInt16(resultPort) != nil }
    private var idleConfigurationKey: String {
        [linuxHost, controlPort, resultPort, folder.selectedURL?.path ?? "default", probes.c1.internalPCMSHA256,
         probes.c2.internalPCMSHA256, saveDebugAudio.description, linuxRemoteStartEnabled.description].joined(separator: "|")
    }
    private func currentConfiguration() -> ResponderConfiguration? {
        guard let control = UInt16(controlPort), let result = UInt16(resultPort), !linuxHost.isEmpty else { return nil }
        return .init(
            linuxHost: linuxHost, controlPort: control, resultPort: result,
            resultRootURL: folder.selectedURL, saveDebugAudio: saveDebugAudio, c1: probes.c1, c2: probes.c2
        )
    }
    private func configureIdleListener() {
        guard linuxRemoteStartEnabled else {
            responder.disableIdleRemoteStart()
            return
        }
        guard let config = currentConfiguration() else {
            responder.clearIdleConfiguration()
            return
        }
        responder.configureIdle(config)
    }
    private func beginImport(_ target: FileImportTarget) {
        importTarget = target
        importStatus = "正在打开\(target.displayName)…"
        // Present on the next main-loop turn so SwiftUI first applies the target's allowedContentTypes.
        DispatchQueue.main.async { isImportingFile = true }
    }
    private func handleImportResult(_ result: Result<URL, Error>, target: FileImportTarget) {
        switch result {
        case .success(let url):
            switch target {
            case .c1:
                probes.select(url, for: .c1)
                importStatus = probes.lastError == nil ? "已选择 C1：\(url.lastPathComponent)" : "C1 导入失败"
            case .c2:
                probes.select(url, for: .c2)
                importStatus = probes.lastError == nil ? "已选择 C2：\(url.lastPathComponent)" : "C2 导入失败"
            case .folder:
                do {
                    try folder.select(url)
                    importStatus = "已选择结果目录：\(url.lastPathComponent)"
                } catch {
                    importStatus = "结果目录选择失败：\(error.localizedDescription)"
                }
            }
        case .failure(let error):
            importStatus = "文件选择未完成：\(error.localizedDescription)"
        }
    }
    private func startSession() {
        guard let config = currentConfiguration() else { return }
        responder.start(config)
    }
    private func coordinate(_ name: String, _ value: Double) -> some View { VStack { Text(name).font(.caption.bold()); Text(String(format: "%+.3f m", value)).font(.caption.monospacedDigit()) }.frame(maxWidth: .infinity).padding(6).background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 7)) }
    private func numberField(_ label: String, _ value: Binding<String>) -> some View { VStack { Text(label).font(.caption2); TextField("0", text: value).keyboardType(.numbersAndPunctuation).focused($isInputFocused).multilineTextAlignment(.center).fieldStyle() }.frame(maxWidth: .infinity) }
    private var udpTestButtonTitle: String {
        switch responder.udpTestState {
        case .idle: return "UDP 双向检验"
        case .testing: return "UDP 检验中"
        case .passed: return "UDP 检验成功"
        case .failed: return "UDP 检验失败"
        }
    }
    private var udpTestButtonIcon: String {
        switch responder.udpTestState {
        case .idle: return "arrow.left.arrow.right"
        case .testing: return "hourglass"
        case .passed: return "checkmark.circle.fill"
        case .failed: return "xmark.circle.fill"
        }
    }
    private var udpTestButtonColor: Color {
        switch responder.udpTestState {
        case .idle: return .blue
        case .testing: return .orange
        case .passed: return .green
        case .failed: return .red
        }
    }
}

private final class DeviceThermalMonitor: ObservableObject {
    @Published private(set) var state = ProcessInfo.processInfo.thermalState
    private var token: NSObjectProtocol?

    init() {
        _ = ProcessInfo.processInfo.thermalState
        token = NotificationCenter.default.addObserver(
            forName: ProcessInfo.thermalStateDidChangeNotification,
            object: ProcessInfo.processInfo,
            queue: .main
        ) { [weak self] _ in
            self?.state = ProcessInfo.processInfo.thermalState
        }
    }

    deinit {
        if let token { NotificationCenter.default.removeObserver(token) }
    }

    var label: String {
        switch state {
        case .nominal: return "正常"
        case .fair: return "偏热"
        case .serious: return "过热，建议暂停"
        case .critical: return "严重过热，请停止并冷却"
        @unknown default: return "未知"
        }
    }

    var color: Color {
        switch state {
        case .nominal: return .green
        case .fair: return .yellow
        case .serious: return .orange
        case .critical: return .red
        @unknown default: return .gray
        }
    }
}

private struct PointCloudViewerSelection: Identifiable {
    let title: String
    let tint: Color
    let cloud: SpatialPointCloudSnapshot

    var id: UUID { cloud.id }
}

private struct PointCloudViewerScreen: View {
    @Environment(\.dismiss) private var dismiss
    let selection: PointCloudViewerSelection

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            SpatialPointCloudPreview(cloud: selection.cloud)
                .ignoresSafeArea()
            VStack(spacing: 12) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(selection.title).font(.headline)
                        Text("\(selection.cloud.points.count) 点 · frame=\(selection.cloud.frameID)")
                            .font(.caption2.monospaced()).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 30)).symbolRenderingMode(.hierarchical)
                    }
                    .tint(.white)
                }
                .padding(12)
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
                Spacer()
                Text("单指旋转 · 双指缩放 · 双指拖动 · 颜色表示高度")
                    .font(.caption).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(.ultraThinMaterial, in: Capsule())
            }
            .padding()
        }
        .preferredColorScheme(.dark)
    }
}

private struct XYHeadingView: View {
    let yawDegrees: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("共享 XY 坐标系 / iPhone 当前朝向").font(.caption.bold())
                Spacer()
                Text(String(format: "%+.1f°", yawDegrees)).font(.caption.monospacedDigit()).foregroundStyle(.cyan)
            }
            Canvas { context, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2 + 5)
                let radius = min(size.width, size.height) * 0.38
                var axes = Path()
                axes.move(to: CGPoint(x: center.x - radius, y: center.y))
                axes.addLine(to: CGPoint(x: center.x + radius, y: center.y))
                axes.move(to: CGPoint(x: center.x, y: center.y + radius))
                axes.addLine(to: CGPoint(x: center.x, y: center.y - radius))
                context.stroke(axes, with: .color(.secondary.opacity(0.55)), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))

                let radians = yawDegrees * .pi / 180
                let tip = CGPoint(x: center.x - sin(radians) * radius, y: center.y - cos(radians) * radius)
                var arrow = Path()
                arrow.move(to: center)
                arrow.addLine(to: tip)
                let wing: CGFloat = 9
                let angle: CGFloat = atan2(tip.y - center.y, tip.x - center.x)
                arrow.move(to: tip)
                arrow.addLine(to: CGPoint(x: tip.x - wing * cos(angle - CGFloat.pi / 6), y: tip.y - wing * sin(angle - CGFloat.pi / 6)))
                arrow.move(to: tip)
                arrow.addLine(to: CGPoint(x: tip.x - wing * cos(angle + CGFloat.pi / 6), y: tip.y - wing * sin(angle + CGFloat.pi / 6)))
                context.stroke(arrow, with: .color(.cyan), style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))

                context.draw(Text("+X 前").font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: center.x, y: 8), anchor: .top)
                context.draw(Text("+Y 左").font(.caption2).foregroundStyle(.secondary), at: CGPoint(x: center.x - radius - 6, y: center.y), anchor: .trailing)
            }
            .background(.black.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
        }
    }
}

private struct BubbleLevelView: View {
    let pitchDegrees: Double
    let rollDegrees: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("手机水平仪").font(.caption.bold())
                Spacer()
                Text(String(format: "俯仰 %+.1f°  横滚 %+.1f°", pitchDegrees, rollDegrees))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(abs(pitchDegrees) < 1 && abs(rollDegrees) < 1 ? .green : .orange)
            }
            Canvas { context, size in
                let center = CGPoint(x: size.width / 2, y: size.height / 2 + 5)
                let radius = min(size.width, size.height) * 0.39
                let outer = Path(ellipseIn: CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2))
                context.fill(outer, with: .color(.green.opacity(0.08)))
                context.stroke(outer, with: .color(.green.opacity(0.65)), lineWidth: 2)

                for fraction in [0.33, 0.66] as [CGFloat] {
                    let ringRadius = radius * fraction
                    let ring = Path(ellipseIn: CGRect(x: center.x - ringRadius, y: center.y - ringRadius, width: ringRadius * 2, height: ringRadius * 2))
                    context.stroke(ring, with: .color(.secondary.opacity(0.35)), lineWidth: 1)
                }
                var crosshair = Path()
                crosshair.move(to: CGPoint(x: center.x - radius, y: center.y))
                crosshair.addLine(to: CGPoint(x: center.x + radius, y: center.y))
                crosshair.move(to: CGPoint(x: center.x, y: center.y - radius))
                crosshair.addLine(to: CGPoint(x: center.x, y: center.y + radius))
                context.stroke(crosshair, with: .color(.secondary.opacity(0.4)), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))

                let maxTilt = 30.0
                let normalizedRoll = CGFloat(max(-1, min(1, rollDegrees / maxTilt)))
                let normalizedPitch = CGFloat(max(-1, min(1, -pitchDegrees / maxTilt)))
                let dot = CGPoint(x: center.x + normalizedRoll * radius, y: center.y + normalizedPitch * radius)
                let isLevel = abs(pitchDegrees) < 1 && abs(rollDegrees) < 1
                let centerMark = Path(ellipseIn: CGRect(x: center.x - 7, y: center.y - 7, width: 14, height: 14))
                context.fill(centerMark, with: .color(.green.opacity(0.8)))
                context.stroke(centerMark, with: .color(.white), lineWidth: 1.5)
                let currentMark = Path(ellipseIn: CGRect(x: dot.x - 10, y: dot.y - 10, width: 20, height: 20))
                context.fill(currentMark, with: .color(isLevel ? .green : .orange))
                context.stroke(currentMark, with: .color(.white), lineWidth: 2)

                context.draw(Text("水平中心").font(.caption2.bold()).foregroundStyle(.green), at: CGPoint(x: center.x, y: center.y + 12), anchor: .top)
                context.draw(Text("当前倾斜点").font(.caption2.bold()).foregroundStyle(isLevel ? .green : .orange), at: CGPoint(x: dot.x, y: dot.y - 13), anchor: .bottom)
            }
            .background(.black.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
            Text("当前倾斜点与水平中心重合时，手机处于水平状态（±1°）。圆周表示约 30° 倾斜。")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }
}

private struct ProbeSpectrumView: View {
    let bins: [Float]
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Canvas { context, size in
                guard bins.count > 1 else { return }
                var fill = Path()
                fill.move(to: CGPoint(x: 0, y: size.height))
                for (index, magnitude) in bins.enumerated() {
                    let x = CGFloat(index) / CGFloat(bins.count - 1) * size.width
                    let y = size.height * (1 - CGFloat(magnitude))
                    fill.addLine(to: CGPoint(x: x, y: y))
                }
                fill.addLine(to: CGPoint(x: size.width, y: size.height))
                fill.closeSubpath()
                context.fill(fill, with: .linearGradient(Gradient(colors: [tint.opacity(0.75), tint.opacity(0.08)]), startPoint: .zero, endPoint: CGPoint(x: 0, y: size.height)))
                context.stroke(fill, with: .color(tint), lineWidth: 1.2)
            }
            HStack { Text("0"); Spacer(); Text("频谱"); Spacer(); Text("24 kHz") }
                .font(.system(size: 8, design: .monospaced)).foregroundStyle(.secondary)
        }
        .padding(6)
        .background(.black.opacity(0.18), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityLabel("探针频谱，0 到 24 千赫")
    }
}

private enum FileImportTarget {
    case c1, c2, folder

    var allowedContentTypes: [UTType] {
        switch self {
        case .c1, .c2: return [.wav, .audio]
        case .folder: return [.folder]
        }
    }

    var displayName: String {
        switch self {
        case .c1: return " C1 WAV 选择器"
        case .c2: return " C2 WAV 选择器"
        case .folder: return "结果目录选择器"
        }
    }
}

private extension View {
    func card() -> some View { padding(13).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16)).overlay { RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.14)) } }
    func fieldStyle() -> some View { padding(.horizontal, 9).padding(.vertical, 8).background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 8)) }
}
