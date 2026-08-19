import AVFoundation
import Combine
import Darwin
import Foundation
import UIKit

struct ResponderConfiguration: Sendable {
    let linuxHost: String
    let controlPort: UInt16
    let resultPort: UInt16
    let resultRootURL: URL?
    let saveDebugAudio: Bool
    let c1: ProbeDefinition
    let c2: ProbeDefinition
    var startupCommandID: String? = nil
}

enum UDPTestState: Sendable {
    case idle
    case testing
    case passed
    case failed
}

final class AcousticResponder: ObservableObject, @unchecked Sendable {
    @Published private(set) var isRunning = false
    @Published private(set) var isPaused = false
    @Published private(set) var isTestingC2 = false
    @Published private(set) var status = "未启动"
    @Published private(set) var stateName = "STOPPED"
    @Published private(set) var localSessionID: String?
    @Published private(set) var pairedLinuxSessionID: String?
    @Published private(set) var activeMeasurement: Int64?
    @Published private(set) var pendingArmMeasurement: Int64?
    @Published private(set) var successfulResponses = 0
    @Published private(set) var c1Rejected = 0
    @Published private(set) var c2Failures = 0
    @Published private(set) var udpFailures = 0
    @Published private(set) var lastReplyDelaySamples: Int64?
    @Published private(set) var lastT3Precise = false
    @Published private(set) var inputRoute = "unavailable"
    @Published private(set) var outputRoute = "unavailable"
    @Published private(set) var logText = ""
    @Published private(set) var sessionLogPath = ""
    @Published private(set) var sessionShareURL: URL?
    @Published private(set) var c2TestProgress = ""
    @Published private(set) var captureRequestStatus = "尚未请求"
    @Published private(set) var udpTestState: UDPTestState = .idle
    @Published private(set) var udpTestSummary = "尚未测试"
    @Published private(set) var lastLinuxQuality = "尚未收到 Linux 质量结果"

    private struct CaptureAnchor { let sample: Int64; let hostTime: UInt64 }
    private let poseSnapshot: @Sendable () -> DevicePose
    private let captureCompleted: @Sendable (String, Int64, DevicePose) -> Void
    private let measurementQualityReceived: @Sendable (String, Int64, Bool, String) -> Void
    private let pairing = ArmPairingManager()
    private var detector = StreamingC1Detector()
    private let stateLock = NSLock()
    private let analysisQueue = DispatchQueue(label: "com.avtwin.ios.audio-analysis", qos: .userInteractive)
    private let responseQueue = DispatchQueue(label: "com.avtwin.ios.audio-response", qos: .userInteractive)
    private var configuration: ResponderConfiguration?
    private var preparedConfiguration: ResponderConfiguration?
    private var controlServer: UDPControlServer?
    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var c2Buffer: AVAudioPCMBuffer?
    private var storage: SessionStorage?
    private var notificationTokens: [NSObjectProtocol] = []
    private var runningValue = false
    private var pausedValue = false
    private var listening = false
    private var totalSamples: Int64 = 0
    private var cooldownUntilSample: Int64 = 0
    private var armedAtSample: Int64 = 0
    private var latestAnchor: CaptureAnchor?
    private var pendingReply: (session: String, measurement: Int64, event: String, semaphore: DispatchSemaphore)?
    private var pendingCaptureRequestID: String?
    private var pendingStartupCommandID: String?
    private var remoteStopPending = false
    private var routeGeneration: UInt64 = 0
    private var audioInterrupted = false
    private var successCount = 0, rejectedCount = 0, c2FailureCount = 0, udpFailureCount = 0
    private var replyDelayValue: Int64?
    private var t3PreciseValue = false
    private var lastPoseValue: DevicePose?
    private var testRunningValue = false
    private var debugCapture: (measurement: Int64, samples: [Float], targetCount: Int)?
    private var idleListenerEnabled = true

    init(
        poseSnapshot: @escaping @Sendable () -> DevicePose,
        captureCompleted: @escaping @Sendable (String, Int64, DevicePose) -> Void = { _, _, _ in },
        measurementQualityReceived: @escaping @Sendable (String, Int64, Bool, String) -> Void = { _, _, _, _ in }
    ) {
        self.poseSnapshot = poseSnapshot
        self.captureCompleted = captureCompleted
        self.measurementQualityReceived = measurementQualityReceived
    }

    func configureIdle(_ config: ResponderConfiguration) {
        idleListenerEnabled = true
        preparedConfiguration = config
        stateLock.lock(); let running = runningValue; stateLock.unlock()
        guard !running else { return }
        controlServer?.stop()
        controlServer = nil
        startControlServer(config, idle: true)
        DispatchQueue.main.async { self.status = "空闲控制端口监听中，可由 Linux 远程启动" }
    }

    func clearIdleConfiguration() {
        preparedConfiguration = nil
        stateLock.lock(); let running = runningValue; stateLock.unlock()
        guard !running else { return }
        controlServer?.stop()
        controlServer = nil
    }

    func disableIdleRemoteStart() {
        idleListenerEnabled = false
        preparedConfiguration = nil
        stateLock.lock(); let running = runningValue; stateLock.unlock()
        guard !running else { return }
        controlServer?.stop()
        controlServer = nil
        DispatchQueue.main.async {
            self.status = "空闲远程启动已关闭；可在本机手动开始 STRICT ARM 会话"
        }
    }

    func shutdown() {
        idleListenerEnabled = false
        stateLock.lock(); let running = runningValue; stateLock.unlock()
        if running { stop() }
        controlServer?.stop()
        controlServer = nil
    }

    func start(_ requested: ResponderConfiguration) {
        stateLock.lock(); let testIsRunning = testRunningValue; stateLock.unlock()
        guard !testIsRunning else { publishStatus("请等待 C2 ×20 测试结束"); return }
        let host = requested.linuxHost.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { publishStatus("请填写 Linux IPv4 地址"); return }
        let config = ResponderConfiguration(
            linuxHost: host, controlPort: requested.controlPort, resultPort: requested.resultPort,
            resultRootURL: requested.resultRootURL, saveDebugAudio: requested.saveDebugAudio,
            c1: requested.c1, c2: requested.c2, startupCommandID: requested.startupCommandID
        )
        preparedConfiguration = config
        controlServer?.stop()
        controlServer = nil
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.stateLock.lock(); self.pendingStartupCommandID = nil; self.stateLock.unlock()
                self.publishStatus("麦克风权限被拒绝，请到系统设置中允许")
                self.notifyStartupFailure(config, reason: "record_audio_permission_missing")
                DispatchQueue.main.async { self.configureIdle(config) }
                return
            }
            DispatchQueue.main.async { self.startAfterPermission(config) }
        }
    }

    func requestCaptureOnce() {
        guard let config = configuration else {
            publishCaptureRequestStatus("请先启动会话")
            return
        }
        stateLock.lock(); let canRequest = runningValue && !pausedValue; stateLock.unlock()
        guard canRequest else {
            publishCaptureRequestStatus("当前会话未运行或已暂停")
            return
        }
        let requestID = UUID().uuidString
        let request: [String: Any] = [
            "type": "capture_once_request", "protocol_version": 1,
            "request_id": requestID, "sender": "ios",
            "ios_control_port": Int(config.controlPort)
        ]
        stateLock.lock(); pendingCaptureRequestID = requestID; stateLock.unlock()
        publishCaptureRequestStatus("已发送单次采集请求，等待 Linux ACK")
        storage?.appendEvent(request)
        DispatchQueue.global(qos: .userInitiated).async {
            var anySendSucceeded = false
            for attempt in 1...3 {
                self.stateLock.lock(); let stillPending = self.pendingCaptureRequestID == requestID; self.stateLock.unlock()
                guard stillPending else { return }
                do {
                    _ = try UDPControlServer.sendJSON(request, host: config.linuxHost, port: config.resultPort)
                    anySendSucceeded = true
                    self.appendLog("CAPTURE_ONCE_REQUEST_SENT request=\(requestID) attempt=\(attempt)/3")
                } catch {
                    self.appendLog("CAPTURE_ONCE_REQUEST_FAILED request=\(requestID) attempt=\(attempt)/3 error=\(error.localizedDescription)")
                }
                if attempt < 3 { Thread.sleep(forTimeInterval: 0.15) }
            }
            let didSend = anySendSucceeded
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(900)) {
                self.stateLock.lock()
                let timedOut = self.pendingCaptureRequestID == requestID
                if timedOut { self.pendingCaptureRequestID = nil }
                self.stateLock.unlock()
                if timedOut {
                    self.publishCaptureRequestStatus(didSend ? "Linux ACK 超时，请检查会话状态" : "单次采集请求发送失败")
                }
            }
        }
    }

    func pauseListening() {
        stateLock.lock()
        guard runningValue, !pausedValue else { stateLock.unlock(); return }
        pausedValue = true
        listening = false
        stateLock.unlock()
        pairing.clearPending()
        analysisQueue.async {
            self.stateLock.lock(); let sample = self.totalSamples; self.stateLock.unlock()
            self.detector.reset(nextSample: sample, generation: self.pairing.detectorGate().generation)
        }
        DispatchQueue.main.async {
            self.isPaused = true; self.stateName = "PAUSED"; self.status = "已暂停，pending ARM 已清除"
            self.pendingArmMeasurement = nil
        }
        appendLog("LISTENING_PAUSED pending ARM cleared")
    }

    func resumeListening() {
        stateLock.lock()
        guard runningValue, pausedValue else { stateLock.unlock(); return }
        pausedValue = false
        let canListen = totalSamples >= cooldownUntilSample
        listening = canListen
        let sample = totalSamples
        stateLock.unlock()
        analysisQueue.async { self.detector.reset(nextSample: sample, generation: self.pairing.detectorGate().generation) }
        DispatchQueue.main.async {
            self.isPaused = false
            self.stateName = canListen ? "LISTENING" : "COOLDOWN"
            self.status = canListen ? "继续监听，等待 ARM/C1" : "继续会话，等待冷却结束"
        }
        appendLog("LISTENING_RESUMED")
    }

    func stop() {
        stateLock.lock()
        let wasActive = runningValue
        runningValue = false; listening = false; pausedValue = false
        pendingReply?.semaphore.signal(); pendingReply = nil; pendingCaptureRequestID = nil; pendingStartupCommandID = nil
        remoteStopPending = false
        stateLock.unlock()
        controlServer?.stop(); controlServer = nil
        cleanupAudio()
        pairing.clearPending()
        if wasActive { responseQueue.sync {} }
        appendLog("SESSION_STOPPED")
        updateSessionFile(status: "stopped")
        storage?.close()
        storage = nil
        DispatchQueue.main.async {
            UIApplication.shared.isIdleTimerDisabled = false
            self.isRunning = false; self.isPaused = false; self.stateName = "STOPPED"
            self.status = wasActive ? "已安全停止，日志已保存" : self.status
            if self.idleListenerEnabled, let config = self.preparedConfiguration { self.configureIdle(config) }
        }
    }

    func testUDP(host: String, port: UInt16) {
        DispatchQueue.main.async {
            self.udpTestState = .testing
            self.udpTestSummary = "测试中…"
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let milliseconds = try UDPControlServer.bidirectionalTest(host: host.trimmingCharacters(in: .whitespaces), port: port)
                DispatchQueue.main.async {
                    self?.udpTestState = .passed
                    self?.udpTestSummary = String(format: "PASS · RTT %.2f ms", milliseconds)
                }
                self?.publishStatus(String(format: "UDP 双向检验 PASS，RTT %.2f ms（未播放声音）", milliseconds))
            } catch {
                DispatchQueue.main.async {
                    self?.udpTestState = .failed
                    self?.udpTestSummary = "FAIL · \(error.localizedDescription)"
                }
                self?.publishStatus("UDP 双向检验 FAIL：\(error.localizedDescription)")
            }
        }
    }

    func resetUDPTestState() {
        DispatchQueue.main.async {
            self.udpTestState = .idle
            self.udpTestSummary = "地址或端口已变化，请重新测试"
        }
    }

    func clearVisibleLog() {
        DispatchQueue.main.async { self.logText = "" }
    }

    func testC2Once(_ probe: ProbeDefinition) { testC2(probe, repetitions: 1) }
    func testC2Repeated(_ probe: ProbeDefinition) { testC2(probe, repetitions: 20) }

    private func testC2(_ probe: ProbeDefinition, repetitions: Int) {
        stateLock.lock()
        guard !runningValue, !testRunningValue else { stateLock.unlock(); return }
        testRunningValue = true
        stateLock.unlock()
        DispatchQueue.main.async { self.isTestingC2 = true; self.c2TestProgress = "正在准备 C2 ×\(repetitions)…" }
        responseQueue.async {
            var passed = 0
            do {
                let session = AVAudioSession.sharedInstance()
                try session.setCategory(.playback, mode: .default)
                try session.setPreferredSampleRate(ProbeDefaults.sampleRate)
                try session.setActive(true)
                let engine = AVAudioEngine(), player = AVAudioPlayerNode()
                engine.attach(player)
                let format = AVAudioFormat(standardFormatWithSampleRate: ProbeDefaults.sampleRate, channels: 1)!
                engine.connect(player, to: engine.mainMixerNode, format: format)
                let buffer = try Self.audioBuffer(probe.samples, format: format)
                engine.prepare(); try engine.start()
                for index in 1...repetitions {
                    let completion = DispatchSemaphore(value: 0)
                    player.stop()
                    player.scheduleBuffer(buffer, at: nil, options: .interrupts, completionCallbackType: .dataPlayedBack) { _ in completion.signal() }
                    player.play()
                    let verified = completion.wait(timeout: .now() + .milliseconds(Int(probe.durationMilliseconds + 500))) == .success
                    if verified { passed += 1 }
                    DispatchQueue.main.async {
                        self.c2TestProgress += "\n[\(index)/\(repetitions)] \(verified ? "PASS" : "FAIL") dataPlayedBack=\(verified)"
                    }
                }
                player.stop(); engine.stop(); try? session.setActive(false, options: .notifyOthersOnDeactivation)
            } catch {
                DispatchQueue.main.async { self.c2TestProgress += "\nERROR: \(error.localizedDescription)" }
            }
            DispatchQueue.main.async {
                self.c2TestProgress += "\nRESULT: \(passed)/\(repetitions) hardware-render callbacks"
                self.isTestingC2 = false
            }
            self.stateLock.lock(); self.testRunningValue = false; self.stateLock.unlock()
        }
    }

    private func startAfterPermission(_ config: ResponderConfiguration) {
        stateLock.lock()
        guard !runningValue else { stateLock.unlock(); return }
        runningValue = true; pausedValue = false; listening = true; totalSamples = 0
        cooldownUntilSample = 0; armedAtSample = 0; latestAnchor = nil; routeGeneration = 0
        audioInterrupted = false; successCount = 0; rejectedCount = 0; c2FailureCount = 0; udpFailureCount = 0
        replyDelayValue = nil; t3PreciseValue = false
        lastPoseValue = nil; pendingCaptureRequestID = nil; pendingStartupCommandID = nil; remoteStopPending = false
        stateLock.unlock()
        configuration = config
        pairing.reset()
        detector = StreamingC1Detector(template: config.c1.samples)
        detector.reset(nextSample: 0, generation: pairing.detectorGate().generation)
        let sessionID = UUID().uuidString
        localSessionID = sessionID
        do {
            let storage = try SessionStorage(
                root: config.resultRootURL, sessionID: sessionID, c1: config.c1, c2: config.c2,
                saveDebugAudio: config.saveDebugAudio
            )
            self.storage = storage
            storage.appendEvent(["type": "session_started", "local_session_id": sessionID, "receiver": "ios", "sample_rate": Int(ProbeDefaults.sampleRate)])
            try prepareAudio(c2: config.c2)
            installAudioNotifications()
            startControlServer(config, idle: false)
            UIApplication.shared.isIdleTimerDisabled = true
            DispatchQueue.main.async {
                self.sessionLogPath = storage.path; self.sessionShareURL = storage.shareURL
                self.isRunning = true; self.isPaused = false; self.stateName = "LISTENING"
                self.pairedLinuxSessionID = nil; self.activeMeasurement = nil; self.pendingArmMeasurement = nil
                self.status = "等待 Linux ARM，然后监听 C1"
                self.publishCounters()
            }
            appendLog("SESSION_STARTED local_session=\(sessionID) host=\(config.linuxHost) control=\(config.controlPort) result=\(config.resultPort)")
            appendLog("C1 \(config.c1.summary) SHA256=\(config.c1.sourceSHA256)")
            appendLog("C2 \(config.c2.summary) SHA256=\(config.c2.sourceSHA256)")
            updateSessionFile(status: "running")
            notifyCaptureReady(config, storageSessionID: sessionID)
        } catch {
            stateLock.lock(); runningValue = false; stateLock.unlock()
            cleanupAudio(); storage?.close(); storage = nil
            notifyStartupFailure(config, reason: error.localizedDescription)
            DispatchQueue.main.async {
                self.isRunning = false; self.stateName = "STOPPED"; self.status = "启动失败：\(error.localizedDescription)"
                self.configureIdle(config)
            }
        }
    }

    private func prepareAudio(c2: ProbeDefinition) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .measurement, options: [.defaultToSpeaker])
        try session.setPreferredSampleRate(ProbeDefaults.sampleRate)
        try session.setPreferredIOBufferDuration(0.005)
        try session.setActive(true)
        if let microphone = session.availableInputs?.first(where: { $0.portType == .builtInMic }) { try? session.setPreferredInput(microphone) }
        try session.overrideOutputAudioPort(.speaker)
        guard abs(session.sampleRate - ProbeDefaults.sampleRate) < 1 else { throw appError("设备音频采样率不是 48 kHz") }
        let engine = AVAudioEngine(), player = AVAudioPlayerNode()
        engine.attach(player)
        let format = AVAudioFormat(standardFormatWithSampleRate: ProbeDefaults.sampleRate, channels: 1)!
        engine.connect(player, to: engine.mainMixerNode, format: format)
        let buffer = try Self.audioBuffer(c2.samples, format: format)
        let input = engine.inputNode, inputFormat = input.outputFormat(forBus: 0)
        guard abs(inputFormat.sampleRate - ProbeDefaults.sampleRate) < 1 else { throw appError("麦克风输入无法使用 48 kHz") }
        input.installTap(onBus: 0, bufferSize: 240, format: inputFormat) { [weak self] audioBuffer, time in
            guard let self, let channel = audioBuffer.floatChannelData?[0] else { return }
            let count = Int(audioBuffer.frameLength)
            let copied = Array(UnsafeBufferPointer(start: channel, count: count))
            self.stateLock.lock()
            let start = self.totalSamples; self.totalSamples += Int64(count)
            self.latestAnchor = CaptureAnchor(sample: start, hostTime: time.isHostTimeValid ? time.hostTime : mach_absolute_time())
            if !self.pausedValue, !self.listening, self.cooldownUntilSample > 0, self.totalSamples >= self.cooldownUntilSample {
                self.listening = true; self.cooldownUntilSample = 0
                DispatchQueue.main.async { self.stateName = "LISTENING"; self.status = "冷却完成，等待下一次 ARM/C1" }
            }
            let running = self.runningValue
            self.stateLock.unlock()
            guard running else { return }
            self.analysisQueue.async { self.analyze(copied, absoluteStart: start) }
        }
        self.engine = engine; self.player = player; self.c2Buffer = buffer
        engine.prepare(); try engine.start()
        updateRoutes()
    }

    private static func audioBuffer(_ samples: [Float], format: AVAudioFormat) throws -> AVAudioPCMBuffer {
        guard !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)),
              let output = buffer.floatChannelData?[0] else { throw appError("无法创建播放缓冲区") }
        buffer.frameLength = buffer.frameCapacity
        samples.withUnsafeBufferPointer { output.update(from: $0.baseAddress!, count: $0.count) }
        return buffer
    }

    private func startControlServer(_ config: ResponderConfiguration, idle: Bool) {
        let server = UDPControlServer(
            port: config.controlPort, allowedHost: config.linuxHost,
            onArm: { [weak self] command, source in
                guard let self else { return .init(accepted: false, reason: "session_not_ready") }
                guard !idle else {
                    self.appendLog("ARM ignored while idle from=\(source)")
                    return .init(accepted: false, reason: "session_not_ready")
                }
                var result = ArmAcceptResult(accepted: false, reason: "session_not_ready")
                self.analysisQueue.sync {
                    result = self.pairing.accept(command, nowMilliseconds: Self.uptimeMilliseconds())
                    if result.accepted, result.reason != "duplicate_arm_reack" {
                        self.stateLock.lock(); self.armedAtSample = self.totalSamples; let boundary = self.armedAtSample; self.stateLock.unlock()
                        self.detector.reset(nextSample: boundary, generation: self.pairing.detectorGate().generation)
                    }
                }
                self.appendLog("ARM from=\(source) measurement=\(command.measurementID) accepted=\(result.accepted) reason=\(result.reason)")
                self.storage?.appendEvent([
                    "type": "arm_received", "protocol_version": command.protocolVersion,
                    "session_id": command.sessionID, "measurement_id": command.measurementID,
                    "arm_event_id": command.armEventID, "source": source,
                    "accepted": result.accepted, "reason": result.reason
                ])
                DispatchQueue.main.async {
                    self.pairedLinuxSessionID = self.pairing.pairedSessionID()
                    self.pendingArmMeasurement = self.pairing.pendingMeasurementID()
                    if result.accepted { self.status = "ARM 已接受，等待 C1（#\(command.measurementID)）" }
                }
                return result
            },
            onReplyAck: { [weak self] ack, source in self?.receiveReplyAcknowledgement(ack, source: source) },
            onCaptureStart: { [weak self] command, source in
                guard let self else { return .init(accepted: false, state: "rejected", reason: "responder_unavailable") }
                guard command.protocolVersion == 1 else {
                    return .init(accepted: false, state: "rejected", reason: "unsupported_protocol_version")
                }
                guard command.linuxResultPort == config.resultPort else {
                    return .init(accepted: false, state: "rejected", reason: "linux_result_port_mismatch")
                }
                let sourcePort = UInt16(source.split(separator: ":").last.map(String.init) ?? "")
                guard sourcePort == command.linuxResultPort else {
                    return .init(accepted: false, state: "rejected", reason: "linux_source_port_mismatch")
                }
                self.stateLock.lock()
                let active = self.runningValue
                let duplicateStarting = self.pendingStartupCommandID == command.commandID
                let anotherStarting = self.pendingStartupCommandID != nil && !duplicateStarting
                if !active, !duplicateStarting, !anotherStarting {
                    self.pendingStartupCommandID = command.commandID
                }
                self.stateLock.unlock()
                guard idle, !active else {
                    return .init(accepted: false, state: "already_running", reason: "ios_session_already_active")
                }
                if duplicateStarting {
                    return .init(accepted: true, state: "starting", reason: "duplicate_start_reack")
                }
                guard !anotherStarting else {
                    return .init(accepted: false, state: "starting", reason: "another_start_command_pending")
                }
                var remoteConfig = config
                remoteConfig.startupCommandID = command.commandID
                self.appendLog("REMOTE_CAPTURE_START from=\(source) command=\(command.commandID)")
                // Give UDPControlServer time to send START_CAPTURE_ACK before
                // start() closes the idle socket and opens the formal server.
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(50)) {
                    self.start(remoteConfig)
                }
                return .init(accepted: true, state: "starting", reason: "accepted_remote_start")
            },
            onCaptureStop: { [weak self] command, source in
                guard let self else { return .init(accepted: false, state: "rejected", reason: "responder_unavailable") }
                guard command.protocolVersion == 1 else {
                    return .init(accepted: false, state: "rejected", reason: "unsupported_protocol_version")
                }
                guard command.linuxResultPort == config.resultPort else {
                    return .init(accepted: false, state: "rejected", reason: "linux_result_port_mismatch")
                }
                let sourcePort = UInt16(source.split(separator: ":").last.map(String.init) ?? "")
                guard sourcePort == command.linuxResultPort else {
                    return .init(accepted: false, state: "rejected", reason: "linux_source_port_mismatch")
                }
                self.stateLock.lock()
                let active = self.runningValue
                let alreadyStopping = self.remoteStopPending
                if active, !alreadyStopping { self.remoteStopPending = true }
                self.stateLock.unlock()
                guard active else {
                    return .init(accepted: true, state: "already_stopped", reason: "already_stopped")
                }
                if alreadyStopping {
                    return .init(accepted: true, state: "stopping", reason: "duplicate_stop_reack")
                }
                self.appendLog("REMOTE_SAFE_STOP from=\(source) command=\(command.commandID)")
                self.storage?.appendEvent([
                    "type": "remote_safe_stop_received", "protocol_version": command.protocolVersion,
                    "command_id": command.commandID, "source": source
                ])
                // ACK is emitted by UDPControlServer immediately after this
                // handler returns; finalization follows on a different queue.
                DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + .milliseconds(50)) {
                    self.stop()
                }
                return .init(accepted: true, state: "stopping", reason: "accepted_safe_stop")
            },
            onCaptureOnceAck: { [weak self] acknowledgement, source in
                guard let self else { return }
                self.stateLock.lock()
                let matchesPending = self.pendingCaptureRequestID == acknowledgement.requestID
                if matchesPending { self.pendingCaptureRequestID = nil }
                self.stateLock.unlock()
                guard matchesPending else {
                    self.appendLog("CAPTURE_ONCE_ACK_IGNORED from=\(source) request=\(acknowledgement.requestID) reason=no_matching_request")
                    return
                }
                let detail = acknowledgement.measurementID.map { " measurement=\($0)" } ?? ""
                let message = acknowledgement.accepted
                    ? "Linux 已接受单次采集请求\(detail)"
                    : "Linux 拒绝单次采集：\(acknowledgement.reason)（\(acknowledgement.state)）"
                self.publishCaptureRequestStatus(message)
                self.appendLog("CAPTURE_ONCE_ACK from=\(source) request=\(acknowledgement.requestID) accepted=\(acknowledgement.accepted) reason=\(acknowledgement.reason)\(detail)")
                self.storage?.appendEvent([
                    "type": "capture_once_ack", "request_id": acknowledgement.requestID,
                    "accepted": acknowledgement.accepted, "state": acknowledgement.state,
                    "reason": acknowledgement.reason, "source": source,
                    "measurement_id": acknowledgement.measurementID.map { $0 as Any } ?? NSNull()
                ])
            },
            onMeasurementQuality: { [weak self] quality, source in
                guard let self else { return .init(accepted: false, reason: "responder_unavailable") }
                guard quality.protocolVersion == 1 else {
                    return .init(accepted: false, reason: "unsupported_protocol_version")
                }
                guard self.pairing.pairedSessionID() == quality.sessionID else {
                    self.appendLog("MEASUREMENT_QUALITY_REJECTED from=\(source) measurement=\(quality.measurementID) reason=session_id_mismatch")
                    return .init(accepted: false, reason: "session_id_mismatch")
                }
                let reasonText = quality.failureReasons.isEmpty
                    ? quality.overall
                    : quality.failureReasons.joined(separator: "; ")
                self.measurementQualityReceived(
                    quality.sessionID, quality.measurementID, quality.passed, reasonText
                )
                DispatchQueue.main.async {
                    self.lastLinuxQuality = "#\(quality.measurementID) \(quality.passed ? "PASS" : "FAIL") · ToF \(quality.tofAvailable ? "可用" : "不可用")"
                }
                self.appendLog("MEASUREMENT_QUALITY from=\(source) measurement=\(quality.measurementID) pass=\(quality.passed) overall=\(quality.overall) tof=\(quality.tofAvailable)")
                self.storage?.appendEvent([
                    "type": "measurement_quality", "protocol_version": quality.protocolVersion,
                    "session_id": quality.sessionID, "measurement_id": quality.measurementID,
                    "quality_pass": quality.passed, "quality_overall": quality.overall,
                    "quality_failure_reasons": quality.failureReasons,
                    "tof_available": quality.tofAvailable, "source": source
                ])
                return .init(accepted: true, reason: "quality_applied")
            },
            onLog: { [weak self] message in self?.appendLog(message) }
        )
        controlServer = server; server.start()
    }

    private func analyze(_ samples: [Float], absoluteStart: Int64) {
        stateLock.lock(); let mayAnalyze = runningValue && listening && !pausedValue; let boundary = armedAtSample; stateLock.unlock()
        guard mayAnalyze else {
            detector.appendOnly(samples, absoluteStartSample: absoluteStart)
            if var capture = debugCapture, capture.samples.count < capture.targetCount {
                capture.samples.append(contentsOf: samples.prefix(capture.targetCount - capture.samples.count))
                debugCapture = capture
            }
            return
        }
        let end = absoluteStart + Int64(samples.count)
        guard end > boundary else { return }
        let clippedStart = max(absoluteStart, boundary)
        let clipped = clippedStart == absoluteStart ? samples : Array(samples[Int(clippedStart - absoluteStart)...])
        let gate = pairing.detectorGate()
        guard let detection = detector.process(clipped, absoluteStartSample: clippedStart, armed: gate.armed, generation: gate.generation) else { return }
        if !detection.detected {
            stateLock.lock(); rejectedCount += 1; stateLock.unlock(); publishCounters()
            appendLog(String(format: "C1_REJECTED score=%.3f reason=%@", detection.score, detection.rejectionReason ?? "unknown"))
            storage?.appendEvent(["type": "c1_rejected", "score": detection.score, "reason": detection.rejectionReason ?? "unknown", "candidate_peak_sample": detection.candidateSample])
            return
        }
        guard let t2 = detection.t2Sample, let claim = pairing.claimNext(nowMilliseconds: Self.uptimeMilliseconds()) else {
            appendLog("C1_REJECTED strict ARM 已过期或被消费"); return
        }
        stateLock.lock()
        guard listening else { stateLock.unlock(); return }
        listening = false; cooldownUntilSample = .max; let routeAtDetection = routeGeneration
        stateLock.unlock()
        let pose = poseSnapshot()
        stateLock.lock(); lastPoseValue = pose; stateLock.unlock()
        DispatchQueue.main.async {
            self.activeMeasurement = claim.measurementID; self.pendingArmMeasurement = nil
            self.stateName = "C1_DETECTED"; self.status = "已检测 C1，正在响应 C2"
        }
        appendLog(String(format: "C1_DETECTED measurement=%lld t2=%lld score=%.3f pose=%@", claim.measurementID, t2, detection.score, pose.source))
        var event: [String: Any] = [
            "type": "c1_detected", "session_id": claim.sessionID, "measurement_id": claim.measurementID,
            "pairing_mode": claim.pairingMode, "t2_sample": t2, "candidate_peak_sample": detection.candidateSample,
            "c1_score": detection.score, "detection_completed_at_sample": detection.detectionCompletedAtSample,
            "detection_latency_samples": detection.detectionCompletedAtSample - t2
        ]
        event.merge(pose.wireFields) { _, new in new }
        storage?.appendEvent(event)
        var poseEvent: [String: Any] = [
            "type": "manual_pose_snapshot", "session_id": claim.sessionID,
            "measurement_id": claim.measurementID, "captured_at": "c1_detected", "t2_sample": t2
        ]
        poseEvent.merge(pose.wireFields) { _, new in new }
        storage?.appendEvent(poseEvent)
        storage?.appendPose(sessionID: claim.sessionID, measurementID: claim.measurementID, t2: t2, pose: pose)
        if configuration?.saveDebugAudio == true {
            debugCapture = (claim.measurementID, detector.window(centerSample: t2, before: 4_800, after: 16_800) ?? [], 21_600)
        }
        responseQueue.async { [weak self] in self?.playAndReport(detection: detection, claim: claim, pose: pose, routeAtDetection: routeAtDetection) }
    }

    private func playAndReport(detection: C1Detection, claim: PairingClaim, pose: DevicePose, routeAtDetection: UInt64) {
        guard let config = configuration, let player, let c2Buffer, let t2 = detection.t2Sample else { return }
        DispatchQueue.main.async { self.stateName = "C2_SCHEDULED" }
        let decisionNanoseconds = Int64(ProcessInfo.processInfo.systemUptime * 1_000_000_000)
        let targetHostTime = mach_absolute_time() + AVAudioTime.hostTime(forSeconds: 0.020)
        let playbackCompletion = DispatchSemaphore(value: 0)
        player.stop()
        player.scheduleBuffer(c2Buffer, at: nil, options: .interrupts, completionCallbackType: .dataPlayedBack) { _ in playbackCompletion.signal() }
        // This is the transmitter pose associated with the RIR received by Linux.
        // The earlier snapshot remains attached to the C1/t2 receive event.
        let txPose = poseSnapshot()
        stateLock.lock(); lastPoseValue = txPose; stateLock.unlock()
        player.play(at: AVAudioTime(hostTime: targetHostTime))
        DispatchQueue.main.async { self.stateName = "C2_PLAYING" }
        appendLog("C2_SCHEDULED measurement=\(claim.measurementID) host_time=\(targetHostTime)")
        let playbackVerified = playbackCompletion.wait(timeout: .now() + .milliseconds(Int(config.c2.durationMilliseconds + 500))) == .success
        if playbackVerified {
            captureCompleted(claim.sessionID, claim.measurementID, txPose)
        } else {
            stateLock.lock(); c2FailureCount += 1; stateLock.unlock()
        }
        stateLock.lock()
        let anchor = latestAnchor, routeStable = routeGeneration == routeAtDetection && !audioInterrupted
        stateLock.unlock()
        let projectedT3 = anchor.map { Self.project(hostTime: targetHostTime, from: $0) }
        let localC2 = detectLocalC2AcousticT3(detection: detection, template: config.c2.samples)
        let t3 = localC2?.t3Sample
        let delay = t3.map { $0 - t2 }
        let timingValid = playbackVerified && (delay.map { $0 >= 0 && $0 <= Int64(ProbeDefaults.sampleRate) } ?? false)
        stateLock.lock(); replyDelayValue = timingValid ? delay : nil; t3PreciseValue = timingValid; stateLock.unlock()
        DispatchQueue.main.async { self.stateName = "REPORTING" }

        let eventID = UUID().uuidString, acknowledgement = DispatchSemaphore(value: 0)
        stateLock.lock(); pendingReply = (claim.sessionID, claim.measurementID, eventID, acknowledgement); stateLock.unlock()
        let route = AVAudioSession.sharedInstance().currentRoute
        var reply: [String: Any] = [
            "type": "reply_timing", "protocol_version": 1, "session_id": claim.sessionID,
            "measurement_id": claim.measurementID, "android_event_id": eventID, "receiver": "ios",
            "pairing_mode": claim.pairingMode, "t3_precise": timingValid, "t2_sample": t2,
            "sample_rate": Int(ProbeDefaults.sampleRate), "c1_score": detection.score,
            "c1_detected": true, "c2_started": true, "playback_verified": playbackVerified,
            "decision_time_ns": decisionNanoseconds, "play_call_host_time": targetHostTime,
            "first_valid_audio_timestamp_ns": Int64(AVAudioTime.seconds(forHostTime: targetHostTime) * 1_000_000_000),
            "first_valid_audio_frame_position": 0, "playback_frame_zero_nano_time": Int64(AVAudioTime.seconds(forHostTime: targetHostTime) * 1_000_000_000),
            "detection_completed_at_sample": detection.detectionCompletedAtSample,
            "detection_latency_samples": detection.detectionCompletedAtSample - t2,
            "audio_track_head_before": 0, "audio_track_head_after": playbackVerified ? config.c2.samples.count : 0,
            "audio_track_timestamp_valid": true, "audio_track_underruns": -1,
            "input_route": route.inputs.first?.portName ?? "unknown", "output_route": route.outputs.first?.portName ?? "unknown",
            "route_stable": routeStable,
            "audio_track_t3_estimate_sample": projectedT3.map { $0 as Any } ?? NSNull(),
            "local_c2_acoustic_score": localC2.map { $0.score as Any } ?? NSNull(),
            "local_c2_segment_offset_samples": localC2.map { $0.segmentOffsetSamples as Any } ?? NSNull(),
            "local_c2_segment_length_samples": localC2.map { $0.segmentLengthSamples as Any } ?? NSNull(),
            "t3_method": timingValid ? "local_C2_acoustic_detection_on_AVAudioEngine_input_timeline" : "unavailable",
            "ios_pose_captured_at": "c2_playback_issued"
        ]
        if timingValid, let t3, let delay { reply["t3_sample"] = t3; reply["reply_delay_samples"] = delay; reply["error"] = NSNull() }
        else { reply["t3_sample"] = NSNull(); reply["reply_delay_samples"] = NSNull(); reply["error"] = playbackVerified ? "local_c2_acoustic_detection_unavailable" : "hardware_playback_not_verified" }
        reply.merge(txPose.wireFields) { _, new in new }
        storage?.appendEvent(reply)
        var txPoseEvent: [String: Any] = [
            "type": "manual_pose_snapshot", "session_id": claim.sessionID,
            "measurement_id": claim.measurementID, "captured_at": "c2_playback_issued",
            "t3_sample": t3.map { $0 as Any } ?? NSNull()
        ]
        txPoseEvent.merge(txPose.wireFields) { _, new in new }
        storage?.appendEvent(txPoseEvent)

        var acknowledged = false
        for attempt in 1...3 where isSessionRunning() {
            do {
                _ = try UDPControlServer.sendJSON(reply, host: config.linuxHost, port: config.resultPort)
                storage?.appendEvent(["type": "udp_send", "android_event_id": eventID, "target_ip": config.linuxHost, "target_port": config.resultPort, "attempt": attempt, "success": true])
                appendLog("REPLY_TIMING_SENT measurement=\(claim.measurementID) attempt=\(attempt)/3 event=\(eventID)")
            } catch {
                stateLock.lock(); udpFailureCount += 1; stateLock.unlock()
                storage?.appendEvent(["type": "udp_send", "android_event_id": eventID, "attempt": attempt, "success": false, "error": error.localizedDescription])
            }
            if acknowledgement.wait(timeout: .now() + .milliseconds(350)) == .success, replyWasAcknowledged(eventID) { acknowledged = true; break }
        }
        appendLog(acknowledged ? "REPLY_ACK_RECEIVED measurement=\(claim.measurementID) event=\(eventID)" : "REPLY_ACK_TIMEOUT measurement=\(claim.measurementID) event=\(eventID)")
        if !acknowledged { stateLock.lock(); udpFailureCount += 1; stateLock.unlock() }
        stateLock.lock(); pendingReply = nil; successCount += playbackVerified ? 1 : 0; let paused = pausedValue; cooldownUntilSample = totalSamples + 38_400; stateLock.unlock()
        if config.saveDebugAudio {
            let captured: [Float]? = analysisQueue.sync {
                guard let value = debugCapture, value.measurement == claim.measurementID else { return nil }
                debugCapture = nil
                return value.samples
            }
            if let captured { storage?.saveDebugWindow(name: "m\(claim.measurementID)_c1_window.wav", samples: captured) }
            storage?.saveDebugWindow(name: "m\(claim.measurementID)_c2_reference.wav", samples: config.c2.samples)
        }
        updateSessionFile(status: "running"); publishCounters()
        if isSessionRunning() {
            DispatchQueue.main.async {
                self.stateName = paused ? "PAUSED" : "COOLDOWN"
                self.status = paused ? "响应完成，仍处于暂停" : "测量 #\(claim.measurementID) 已响应，冷却 800 ms"
            }
        }
    }

    private func detectLocalC2AcousticT3(detection: C1Detection, template: [Float]) -> LocalC2Detection? {
        guard let t2 = detection.t2Sample else { return nil }
        let deadline = DispatchTime.now() + .milliseconds(900)
        var nextAnalysisAt = detection.detectionCompletedAtSample + 18_000
        while isSessionRunning(), DispatchTime.now() < deadline {
            stateLock.lock(); let captured = totalSamples; stateLock.unlock()
            if captured >= nextAnalysisAt {
                let window: [Float]? = analysisQueue.sync {
                    detector.window(centerSample: t2 + 24_000, before: 24_000, after: 24_000)
                }
                if let window,
                   let found = LocalC2AcousticDetector.detect(
                    audio: window, windowStartSample: t2, fullTemplate: template,
                    searchStartSample: detection.detectionCompletedAtSample
                   ) {
                    appendLog(String(format: "LOCAL_C2_ACOUSTIC t3_sample=%lld score=%.3f", found.t3Sample, found.score))
                    return found
                }
                nextAnalysisAt = captured + 4_800
            }
            Thread.sleep(forTimeInterval: 0.005)
        }
        appendLog("LOCAL_C2_ACOUSTIC_NOT_FOUND; AVAudioTime projection remains diagnostic only")
        return nil
    }

    private func receiveReplyAcknowledgement(_ acknowledgement: ReplyAcknowledgement, source: String) {
        stateLock.lock()
        let pending = pendingReply
        let valid = acknowledgement.accepted && pending?.session == acknowledgement.sessionID
            && pending?.measurement == acknowledgement.measurementID && pending?.event == acknowledgement.eventID
        if valid { pending?.semaphore.signal() }
        stateLock.unlock()
        storage?.appendEvent([
            "type": "reply_ack_received", "session_id": acknowledgement.sessionID,
            "measurement_id": acknowledgement.measurementID, "android_event_id": acknowledgement.eventID,
            "accepted": acknowledgement.accepted, "valid": valid, "source": source
        ])
        appendLog("REPLY_ACK from=\(source) measurement=\(acknowledgement.measurementID) valid=\(valid)")
    }

    private func installAudioNotifications() {
        let center = NotificationCenter.default
        notificationTokens.append(center.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: nil) { [weak self] note in
            guard let self else { return }
            self.stateLock.lock(); self.routeGeneration &+= 1; self.stateLock.unlock()
            self.updateRoutes(); self.appendLog("AUDIO_ROUTE_CHANGED reason=\(note.userInfo?[AVAudioSessionRouteChangeReasonKey] ?? "unknown")")
        })
        notificationTokens.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: nil) { [weak self] note in
            guard let self, let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            self.stateLock.lock(); self.audioInterrupted = type == .began; self.routeGeneration &+= 1; self.stateLock.unlock()
            self.appendLog("AUDIO_INTERRUPTION \(type == .began ? "BEGAN" : "ENDED")")
            if type == .ended { try? AVAudioSession.sharedInstance().setActive(true); try? self.engine?.start() }
        })
    }

    private func cleanupAudio() {
        notificationTokens.forEach { NotificationCenter.default.removeObserver($0) }; notificationTokens.removeAll()
        if let engine { engine.inputNode.removeTap(onBus: 0); player?.stop(); engine.stop() }
        engine = nil; player = nil; c2Buffer = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func updateRoutes() {
        let route = AVAudioSession.sharedInstance().currentRoute
        let input = route.inputs.first?.portName ?? "unavailable", output = route.outputs.first?.portName ?? "unavailable"
        DispatchQueue.main.async { self.inputRoute = input; self.outputRoute = output }
    }

    private func notifyCaptureReady(_ config: ResponderConfiguration, storageSessionID: String) {
        guard let commandID = config.startupCommandID else { return }
        sendControlRepeated([
            "type": "capture_ready", "protocol_version": 1,
            "command_id": commandID, "receiver": "ios",
            "ios_storage_session_id": storageSessionID,
            "sample_rate": Int(ProbeDefaults.sampleRate),
            "control_port": Int(config.controlPort), "audio_record_open": true
        ], config: config)
        appendLog("CAPTURE_READY command=\(commandID) -> \(config.linuxHost):\(config.resultPort)")
    }

    private func notifyStartupFailure(_ config: ResponderConfiguration, reason: String) {
        guard let commandID = config.startupCommandID else { return }
        sendControlRepeated([
            "type": "capture_start_failed", "protocol_version": 1,
            "command_id": commandID, "receiver": "ios", "reason": reason
        ], config: config)
    }

    private func sendControlRepeated(_ object: [String: Any], config: ResponderConfiguration) {
        DispatchQueue.global(qos: .userInitiated).async {
            for attempt in 0..<3 {
                _ = try? UDPControlServer.sendJSON(object, host: config.linuxHost, port: config.resultPort)
                if attempt < 2 { Thread.sleep(forTimeInterval: 0.10) }
            }
        }
    }

    private func updateSessionFile(status: String) {
        stateLock.lock()
        var values: [String: Any] = [
            "protocol_version": 1, "session_id": localSessionID ?? "", "status": status,
            "sample_rate": Int(ProbeDefaults.sampleRate), "c1_name": configuration?.c1.name ?? "",
            "c1_source_sha256": configuration?.c1.sourceSHA256 ?? "", "c1_internal_pcm_sha256": configuration?.c1.internalPCMSHA256 ?? "",
            "c2_name": configuration?.c2.name ?? "", "c2_source_sha256": configuration?.c2.sourceSHA256 ?? "",
            "c2_internal_pcm_sha256": configuration?.c2.internalPCMSHA256 ?? "", "success_responses": successCount,
            "c1_rejected": rejectedCount, "c2_failures": c2FailureCount, "udp_failures": udpFailureCount,
            "last_t3_precise": t3PreciseValue,
            "last_pose_saved_from_c1": lastPoseValue != nil,
            "input_route": inputRoute, "output_route": outputRoute
        ]
        if let lastPoseValue { values.merge(lastPoseValue.wireFields) { _, new in new } }
        if let replyDelayValue { values["last_reply_delay_samples"] = replyDelayValue }
        else { values["last_reply_delay_samples"] = NSNull() }
        stateLock.unlock(); storage?.updateSession(values)
    }

    private func publishCounters() {
        stateLock.lock(); let success = successCount, rejected = rejectedCount, c2 = c2FailureCount, udp = udpFailureCount, delay = replyDelayValue, precise = t3PreciseValue; stateLock.unlock()
        DispatchQueue.main.async {
            self.successfulResponses = success; self.c1Rejected = rejected; self.c2Failures = c2; self.udpFailures = udp
            self.lastReplyDelaySamples = delay; self.lastT3Precise = precise
        }
    }

    private func appendLog(_ message: String) {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        storage?.appendLog(message)
        DispatchQueue.main.async {
            let line = "[\(timestamp)] \(message)", combined = self.logText.isEmpty ? line : self.logText + "\n" + line
            self.logText = String(combined.suffix(24_000))
        }
    }

    private func publishStatus(_ value: String) { DispatchQueue.main.async { self.status = value } }
    private func publishCaptureRequestStatus(_ value: String) { DispatchQueue.main.async { self.captureRequestStatus = value } }
    private func isSessionRunning() -> Bool { stateLock.lock(); defer { stateLock.unlock() }; return runningValue }
    private func replyWasAcknowledged(_ eventID: String) -> Bool { stateLock.lock(); defer { stateLock.unlock() }; return pendingReply?.event == eventID }
    private static func uptimeMilliseconds() -> Int64 { Int64(ProcessInfo.processInfo.systemUptime * 1_000) }
    private static func project(hostTime: UInt64, from anchor: CaptureAnchor) -> Int64 {
        if hostTime >= anchor.hostTime { return anchor.sample + Int64((AVAudioTime.seconds(forHostTime: hostTime - anchor.hostTime) * ProbeDefaults.sampleRate).rounded()) }
        return anchor.sample - Int64((AVAudioTime.seconds(forHostTime: anchor.hostTime - hostTime) * ProbeDefaults.sampleRate).rounded())
    }
    private static func appError(_ message: String) -> NSError { NSError(domain: "AVTwin", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    private func appError(_ message: String) -> NSError { Self.appError(message) }
}
