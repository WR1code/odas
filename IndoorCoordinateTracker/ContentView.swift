import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @ObservedObject var poseTracker: PoseTracker
    @StateObject private var poseSelection: PoseSelectionStore
    @StateObject private var responder: AcousticResponder
    @StateObject private var probes = ProbeSelectionStore()
    @StateObject private var folder = FolderSelectionStore()
    @StateObject private var thermalMonitor = DeviceThermalMonitor()
    @AppStorage("linuxHost") private var linuxHost = "192.168.1.100"
    @AppStorage("controlPort") private var controlPort = "5006"
    @AppStorage("resultPort") private var resultPort = "5005"
    @AppStorage("saveDebugAudio") private var saveDebugAudio = false
    @AppStorage("linuxRemoteStartEnabled") private var linuxRemoteStartEnabled = true
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
    @State private var localIPv4 = "unavailable"
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
                    poseCard
                    sessionCard
                    networkCard
                    storageCard
                    testCard
                    metricsCard
                    if showingLog { logCard }
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
            }
        }
        .onAppear {
            localIPv4 = LocalNetworkInfo.hotspotIPv4()
            configureIdleListener()
        }
        .onChange(of: idleConfigurationKey) { _, _ in
            responder.resetUDPTestState()
            configureIdleListener()
        }
        .onDisappear { responder.shutdown() }
        .fileImporter(isPresented: $isImportingFile, allowedContentTypes: importTarget.allowedContentTypes) { result in
            handleImportResult(result, target: importTarget)
        }
    }

    private var headerCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: responder.isRunning ? "wave.3.right.circle.fill" : "iphone.gen3")
                    .font(.system(size: 34)).foregroundStyle(responder.isRunning ? .green : .cyan)
                VStack(alignment: .leading, spacing: 3) {
                    Text("AV-Twin iOS Responder v0.13.3").font(.headline)
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
            ProbeSpectrumView(bins: probe.spectrumBins, tint: title == "C1" ? .cyan : .orange)
                .frame(height: 76)
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
                XYHeadingView(yawDegrees: pose.yawDegrees)
                    .frame(height: 150)
                BubbleLevelView(pitchDegrees: pose.pitchDegrees, rollDegrees: pose.rollDegrees)
                    .frame(height: 205)
                VStack(alignment: .leading, spacing: 5) {
                    Label("相机预览", systemImage: "camera.fill").font(.caption.bold())
                    ZStack {
                        ARCameraView(poseTracker: poseTracker, visualOriginRevision: visualOriginRevision)
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
                Button {
                    poseTracker.resetOrigin()
                    visualOriginRevision += 1
                } label: {
                    Label("同时设立坐标原点与 AR 可视原点", systemImage: "scope").frame(maxWidth: .infinity)
                }
                    .buttonStyle(.bordered).disabled(pose.trackingState != "tracking")
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
            Text("iPhone 热点 IPv4：\(localIPv4)\nLinux ARM/远程启停目标：\(linuxHost):\(controlPort)\n接口：\(LocalNetworkInfo.display())")
                .font(.caption.monospaced()).textSelection(.enabled)
            TextField("Linux Wi-Fi IPv4", text: $linuxHost).keyboardType(.numbersAndPunctuation).fieldStyle()
            HStack {
                TextField("iPhone ARM 端口", text: $controlPort).keyboardType(.numberPad).fieldStyle()
                TextField("Linux 结果端口", text: $resultPort).keyboardType(.numberPad).fieldStyle()
            }
            Toggle("允许 Linux 在空闲时远程启动 iPhone 会话", isOn: $linuxRemoteStartEnabled)
                .font(.caption)
            Text(linuxRemoteStartEnabled ? "已开启：空闲时监听控制端口，Linux 可发送 START_CAPTURE。" : "已关闭：空闲时不监听远程启动；仍可在本机手动开始会话。")
                .font(.caption2).foregroundStyle(.secondary)
        }.disabled(responder.isRunning).card()
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
                    Button("安全停止并保存", role: .destructive) { responder.stop() }.buttonStyle(.borderedProminent)
                }
            } else {
                Button { startSession() } label: { Label("开始 STRICT ARM 会话", systemImage: "play.circle.fill").frame(maxWidth: .infinity) }
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
    private func numberField(_ label: String, _ value: Binding<String>) -> some View { VStack { Text(label).font(.caption2); TextField("0", text: value).keyboardType(.numbersAndPunctuation).multilineTextAlignment(.center).fieldStyle() }.frame(maxWidth: .infinity) }
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

private struct XYHeadingView: View {
    let yawDegrees: Double

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("XY 坐标系 / 当前平面朝向").font(.caption.bold())
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
