import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @ObservedObject var poseTracker: PoseTracker
    @StateObject private var poseSelection: PoseSelectionStore
    @StateObject private var responder: AcousticResponder
    @StateObject private var probes = ProbeSelectionStore()
    @StateObject private var folder = FolderSelectionStore()
    @AppStorage("linuxHost") private var linuxHost = "192.168.1.100"
    @AppStorage("controlPort") private var controlPort = "5006"
    @AppStorage("resultPort") private var resultPort = "5005"
    @AppStorage("saveDebugAudio") private var saveDebugAudio = false
    @AppStorage("manualX") private var manualX = "0"
    @AppStorage("manualY") private var manualY = "0"
    @AppStorage("manualZ") private var manualZ = "0"
    @AppStorage("manualYaw") private var manualYaw = "0"
    @AppStorage("manualPitch") private var manualPitch = "0"
    @AppStorage("manualRoll") private var manualRoll = "0"
    @State private var importingC1 = false
    @State private var importingC2 = false
    @State private var importingFolder = false
    @State private var showingLog = false
    @State private var localIPv4 = "unavailable"

    init(poseTracker: PoseTracker) {
        self.poseTracker = poseTracker
        let selection = PoseSelectionStore(arkitSnapshot: { [weak poseTracker] in poseTracker?.snapshot() ?? .unavailable })
        _poseSelection = StateObject(wrappedValue: selection)
        _responder = StateObject(wrappedValue: AcousticResponder(poseSnapshot: { selection.snapshot() }))
    }

    var body: some View {
        ZStack {
            ARCameraView(poseTracker: poseTracker).ignoresSafeArea()
            LinearGradient(colors: [.black.opacity(0.35), .black.opacity(0.88)], startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            ScrollView {
                VStack(spacing: 12) {
                    headerCard
                    probeCard
                    poseCard
                    networkCard
                    storageCard
                    sessionCard
                    testCard
                    metricsCard
                    if showingLog { logCard }
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
            }
        }
        .onAppear {
            localIPv4 = LocalNetworkInfo.preferredIPv4()
            configureIdleListener()
        }
        .onChange(of: idleConfigurationKey) { _, _ in configureIdleListener() }
        .onDisappear { responder.shutdown() }
        .fileImporter(isPresented: $importingC1, allowedContentTypes: [.wav, .audio]) { result in
            if case let .success(url) = result { probes.select(url, for: .c1) }
        }
        .fileImporter(isPresented: $importingC2, allowedContentTypes: [.wav, .audio]) { result in
            if case let .success(url) = result { probes.select(url, for: .c2) }
        }
        .fileImporter(isPresented: $importingFolder, allowedContentTypes: [.folder]) { result in
            if case let .success(url) = result { try? folder.select(url) }
        }
    }

    private var headerCard: some View {
        HStack(spacing: 10) {
            Image(systemName: responder.isRunning ? "wave.3.right.circle.fill" : "iphone.gen3")
                .font(.system(size: 34)).foregroundStyle(responder.isRunning ? .green : .cyan)
            VStack(alignment: .leading, spacing: 3) {
                Text("AV-Twin iOS Responder v0.13.0").font(.headline)
                Text("与 Android v0.12 对齐：远程启停、声学 t3、STRICT ARM").font(.caption2).foregroundStyle(.secondary)
                Text(responder.status).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if responder.isRunning { ProgressView().tint(.green) }
        }.card()
    }

    private var probeCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("C1 / C2 声学探针", systemImage: "waveform").font(.subheadline.bold())
            probeRow(title: "C1", probe: probes.c1, select: { importingC1 = true }, reset: { probes.useDefault(.c1) })
            Divider()
            probeRow(title: "C2", probe: probes.c2, select: { importingC2 = true }, reset: { probes.useDefault(.c2) })
            if let error = probes.lastError { Text("WAV 错误：\(error)").font(.caption).foregroundStyle(.red) }
        }.disabled(responder.isRunning || responder.isTestingC2).card()
    }

    private func probeRow(title: String, probe: ProbeDefinition, select: @escaping () -> Void, reset: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(title): \(probe.summary)").font(.caption)
            Text(probe.diagnostics).font(.system(size: 9, design: .monospaced)).textSelection(.enabled).foregroundStyle(.secondary)
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
            if poseSelection.mode == .arkit {
                HStack { coordinate("X", pose.position.x); coordinate("Y", pose.position.y); coordinate("Z", pose.position.z) }
                Text(String(format: "yaw %.1f°  pitch %.1f°  roll %.1f° | %@", pose.yawDegrees, pose.pitchDegrees, pose.rollDegrees, poseTracker.statusText))
                    .font(.caption.monospacedDigit())
                Button { poseTracker.resetOrigin() } label: { Label("将当前位置设为原点", systemImage: "scope").frame(maxWidth: .infinity) }
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
            Text("iPhone Wi-Fi IPv4：\(localIPv4)\nLinux ARM/远程启停目标：\(localIPv4):\(controlPort)\n接口：\(LocalNetworkInfo.display())")
                .font(.caption.monospaced()).textSelection(.enabled)
            TextField("Linux Wi-Fi IPv4", text: $linuxHost).keyboardType(.numbersAndPunctuation).fieldStyle()
            HStack {
                TextField("iPhone ARM 端口", text: $controlPort).keyboardType(.numberPad).fieldStyle()
                TextField("Linux 结果端口", text: $resultPort).keyboardType(.numberPad).fieldStyle()
            }
        }.disabled(responder.isRunning).card()
    }

    private var storageCard: some View {
        VStack(alignment: .leading, spacing: 9) {
            Label("结果保存", systemImage: "folder").font(.subheadline.bold())
            Text(folder.status).font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("选择结果目录") { importingFolder = true }.buttonStyle(.bordered)
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
                Button("UDP 双向检验") { if let port = UInt16(resultPort) { responder.testUDP(host: linuxHost, port: port) } }
                Button("TEST C2 单次") { responder.testC2Once(probes.c2) }
            }.buttonStyle(.bordered).disabled(responder.isRunning || responder.isTestingC2)
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
            Text("iOS 本地 session：\(responder.localSessionID ?? "--")\nLinux session：\(responder.pairedLinuxSessionID ?? "--（等待 ARM）")\nmeasurement=\(responder.activeMeasurement.map { String($0) } ?? "--") | pending ARM=\(responder.pendingArmMeasurement.map { String($0) } ?? "--")\n成功=\(responder.successfulResponses) | C1 未通过=\(responder.c1Rejected) | C2 失败=\(responder.c2Failures) | UDP 失败=\(responder.udpFailures)\nreply_delay_samples=\(responder.lastReplyDelaySamples.map { String($0) } ?? "--") | t3_precise=\(responder.lastT3Precise)\ninput=\(responder.inputRoute)\noutput=\(responder.outputRoute)")
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
         probes.c2.internalPCMSHA256, saveDebugAudio.description].joined(separator: "|")
    }
    private func currentConfiguration() -> ResponderConfiguration? {
        guard let control = UInt16(controlPort), let result = UInt16(resultPort), !linuxHost.isEmpty else { return nil }
        return .init(
            linuxHost: linuxHost, controlPort: control, resultPort: result,
            resultRootURL: folder.selectedURL, saveDebugAudio: saveDebugAudio, c1: probes.c1, c2: probes.c2
        )
    }
    private func configureIdleListener() {
        guard let config = currentConfiguration() else {
            responder.clearIdleConfiguration()
            return
        }
        responder.configureIdle(config)
    }
    private func startSession() {
        guard let config = currentConfiguration() else { return }
        responder.start(config)
    }
    private func coordinate(_ name: String, _ value: Double) -> some View { VStack { Text(name).font(.caption.bold()); Text(String(format: "%+.3f m", value)).font(.caption.monospacedDigit()) }.frame(maxWidth: .infinity).padding(6).background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 7)) }
    private func numberField(_ label: String, _ value: Binding<String>) -> some View { VStack { Text(label).font(.caption2); TextField("0", text: value).keyboardType(.numbersAndPunctuation).multilineTextAlignment(.center).fieldStyle() }.frame(maxWidth: .infinity) }
}

private extension View {
    func card() -> some View { padding(13).background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16)).overlay { RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.14)) } }
    func fieldStyle() -> some View { padding(.horizontal, 9).padding(.vertical, 8).background(.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 8)) }
}
