import Darwin
import Foundation

final class UDPControlServer: @unchecked Sendable {
    typealias ArmHandler = @Sendable (ArmCommand, String) -> ArmAcceptResult
    typealias ReplyAckHandler = @Sendable (ReplyAcknowledgement, String) -> Void
    typealias CaptureStartHandler = @Sendable (CaptureStartCommand, String) -> CaptureCommandResult
    typealias CaptureStopHandler = @Sendable (CaptureStopCommand, String) -> CaptureCommandResult
    typealias CaptureOnceAckHandler = @Sendable (CaptureOnceAcknowledgement, String) -> Void
    typealias MeasurementQualityHandler = @Sendable (MeasurementQualityResult, String) -> MeasurementQualityAcceptResult
    typealias LidarMapCaptureUpdateHandler = @Sendable (LidarMapCaptureUpdate, String) -> Void
    typealias SharedOriginUpdateHandler = @Sendable (SharedOriginUpdate, String) -> Void
    typealias C2BandTestHandler = @Sendable (C2BandTestCommand, String) -> C2BandTestAcceptResult

    private let port: UInt16
    private let allowedHost: String
    private let onArm: ArmHandler
    private let onReplyAck: ReplyAckHandler
    private let onCaptureStart: CaptureStartHandler
    private let onCaptureStop: CaptureStopHandler
    private let onCaptureOnceAck: CaptureOnceAckHandler
    private let onMeasurementQuality: MeasurementQualityHandler
    private let onLidarMapCaptureUpdate: LidarMapCaptureUpdateHandler
    private let onSharedOriginUpdate: SharedOriginUpdateHandler
    private let onC2BandTest: C2BandTestHandler
    private let onLog: @Sendable (String) -> Void
    private let queue = DispatchQueue(label: "com.avtwin.ios.udp-control", qos: .userInitiated)
    private let stateLock = NSLock()
    private var descriptor: Int32 = -1
    private var running = false

    init(
        port: UInt16,
        allowedHost: String,
        onArm: @escaping ArmHandler,
        onReplyAck: @escaping ReplyAckHandler,
        onCaptureStart: @escaping CaptureStartHandler,
        onCaptureStop: @escaping CaptureStopHandler,
        onCaptureOnceAck: @escaping CaptureOnceAckHandler,
        onMeasurementQuality: @escaping MeasurementQualityHandler,
        onLidarMapCaptureUpdate: @escaping LidarMapCaptureUpdateHandler,
        onSharedOriginUpdate: @escaping SharedOriginUpdateHandler,
        onC2BandTest: @escaping C2BandTestHandler,
        onLog: @escaping @Sendable (String) -> Void
    ) {
        self.port = port
        self.allowedHost = allowedHost
        self.onArm = onArm
        self.onReplyAck = onReplyAck
        self.onCaptureStart = onCaptureStart
        self.onCaptureStop = onCaptureStop
        self.onCaptureOnceAck = onCaptureOnceAck
        self.onMeasurementQuality = onMeasurementQuality
        self.onLidarMapCaptureUpdate = onLidarMapCaptureUpdate
        self.onSharedOriginUpdate = onSharedOriginUpdate
        self.onC2BandTest = onC2BandTest
        self.onLog = onLog
    }

    func start() {
        stateLock.lock()
        guard !running else { stateLock.unlock(); return }
        running = true
        stateLock.unlock()
        queue.async { [weak self] in self?.receiveLoop() }
    }

    func stop() {
        stateLock.lock()
        running = false
        let socketDescriptor = descriptor
        descriptor = -1
        stateLock.unlock()
        if socketDescriptor >= 0 {
            Darwin.shutdown(socketDescriptor, SHUT_RDWR)
            Darwin.close(socketDescriptor)
        }
    }

    private func isRunning() -> Bool {
        stateLock.lock()
        defer { stateLock.unlock() }
        return running
    }

    private func receiveLoop() {
        let socketDescriptor = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard socketDescriptor >= 0 else {
            onLog("UDP control socket 创建失败：\(String(cString: strerror(errno)))")
            stateLock.lock(); running = false; stateLock.unlock()
            return
        }
        stateLock.lock()
        guard running else { stateLock.unlock(); Darwin.close(socketDescriptor); return }
        descriptor = socketDescriptor
        stateLock.unlock()
        var reuse: Int32 = 1
        setsockopt(socketDescriptor, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout.size(ofValue: reuse)))
        var localAddress = sockaddr_in()
        localAddress.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        localAddress.sin_family = sa_family_t(AF_INET)
        localAddress.sin_port = port.bigEndian
        localAddress.sin_addr = in_addr(s_addr: INADDR_ANY)
        let bindResult = withUnsafePointer(to: &localAddress) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(socketDescriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bindResult == 0 else {
            onLog("UDP control 端口 \(port) 绑定失败：\(String(cString: strerror(errno)))")
            stateLock.lock()
            let ownsDescriptor = descriptor == socketDescriptor
            if ownsDescriptor { descriptor = -1 }
            running = false
            stateLock.unlock()
            if ownsDescriptor { Darwin.close(socketDescriptor) }
            return
        }
        onLog("UDP control 正在监听 0.0.0.0:\(port)")

        var buffer = [UInt8](repeating: 0, count: 8_192)
        while isRunning() {
            var sourceAddress = sockaddr_storage()
            var sourceLength = socklen_t(MemoryLayout<sockaddr_storage>.size)
            let received = withUnsafeMutablePointer(to: &sourceAddress) { addressPointer in
                addressPointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { socketAddress in
                    Darwin.recvfrom(socketDescriptor, &buffer, buffer.count, 0, socketAddress, &sourceLength)
                }
            }
            if received <= 0 {
                if isRunning(), errno != EINTR { onLog("UDP control 接收错误：\(String(cString: strerror(errno)))") }
                break
            }
            let sourceHost = Self.ipv4String(sourceAddress)
            let sourcePort = Self.port(sourceAddress)
            let source = "\(sourceHost):\(sourcePort)"
            guard allowedHost.isEmpty || sourceHost == allowedHost else {
                onLog("ARM 来源已拒绝：\(source)，期望 \(allowedHost)")
                continue
            }
            let payload = Data(buffer.prefix(received))
            guard let json = JSONWire.decode(payload) else {
                onLog("收到无效 UDP JSON：\(source)")
                continue
            }
            if JSONWire.string(json, "protocol") == "AVTWIN_UDP_TEST_V1",
               JSONWire.string(json, "type") == "udp_test_ping",
               let nonce = JSONWire.string(json, "nonce") {
                let reply: [String: Any] = [
                    "protocol": "AVTWIN_UDP_TEST_V1",
                    "type": "udp_test_reply",
                    "nonce": nonce,
                    "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(reply) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                }
                onLog("UDP_TEST_REPLY 已发送至 \(source)")
                continue
            }
            if let command = C2BandTestCommand(json: json) {
                let result = onC2BandTest(command, sourceHost)
                let acknowledgement: [String: Any] = [
                    "protocol": "AVTWIN_C2_BAND_TEST_V1",
                    "type": "c2_band_test_ack",
                    "protocol_version": 1,
                    "test_id": command.testID,
                    "accepted": result.accepted,
                    "reason": result.reason,
                    "actual_c2_pcm_sha256": result.actualC2PCMHash,
                    "expected_c2_pcm_sha256": command.expectedC2PCMHash,
                    "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(acknowledgement) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                }
                onLog("C2_BAND_TEST_ACK test=\(command.testID) accepted=\(result.accepted) reason=\(result.reason)")
                continue
            }
            if let acknowledgement = ReplyAcknowledgement(json: json) {
                onReplyAck(acknowledgement, source)
                continue
            }
            if let acknowledgement = CaptureOnceAcknowledgement(json: json) {
                onCaptureOnceAck(acknowledgement, source)
                continue
            }
            if let update = LidarMapCaptureUpdate(json: json) {
                onLidarMapCaptureUpdate(update, source)
                continue
            }
            if let update = SharedOriginUpdate(json: json) {
                onSharedOriginUpdate(update, source)
                continue
            }
            if let quality = MeasurementQualityResult(json: json) {
                let result = onMeasurementQuality(quality, source)
                let acknowledgement: [String: Any] = [
                    "type": "measurement_quality_ack", "protocol_version": 1,
                    "session_id": quality.sessionID,
                    "measurement_id": quality.measurementID,
                    "accepted": result.accepted, "reason": result.reason,
                    "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(acknowledgement) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                }
                onLog("MEASUREMENT_QUALITY_ACK measurement=\(quality.measurementID) accepted=\(result.accepted) reason=\(result.reason)")
                continue
            }
            if let command = CaptureStopCommand(json: json) {
                let result = onCaptureStop(command, source)
                let acknowledgement: [String: Any] = [
                    "type": "capture_stop_ack", "protocol_version": 1,
                    "command_id": command.commandID, "accepted": result.accepted,
                    "state": result.state, "reason": result.reason, "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(acknowledgement) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                }
                onLog("CAPTURE_STOP_ACK_SENT command=\(command.commandID) accepted=\(result.accepted) state=\(result.state)")
                continue
            }
            if let command = CaptureStartCommand(json: json) {
                let result = onCaptureStart(command, source)
                let acknowledgement: [String: Any] = [
                    "type": "start_capture_ack", "protocol_version": 1,
                    "command_id": command.commandID, "accepted": result.accepted,
                    "state": result.state, "reason": result.reason, "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(acknowledgement) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                }
                onLog("START_CAPTURE_ACK_SENT command=\(command.commandID) accepted=\(result.accepted) state=\(result.state)")
                continue
            }
            if let command = ArmCommand(json: json) {
                let result = onArm(command, source)
                let acknowledgement: [String: Any] = [
                    "type": "arm_ack",
                    "protocol_version": 1,
                    "session_id": command.sessionID,
                    "measurement_id": command.measurementID,
                    "arm_event_id": command.armEventID,
                    "accepted": result.accepted,
                    "reason": result.reason,
                    "receiver": "ios"
                ]
                if let data = try? JSONWire.encode(acknowledgement) {
                    Self.send(data, descriptor: socketDescriptor, to: &sourceAddress, length: sourceLength)
                    onLog("ARM_ACK_SENT measurement=\(command.measurementID) accepted=\(result.accepted) reason=\(result.reason)")
                }
                continue
            }
            if let type = JSONWire.string(json, "type"),
               type == "linux_session_start_ack" || type == "linux_session_stop_ack" {
                let accepted = JSONWire.bool(json, "accepted") ?? false
                let commandID = JSONWire.string(json, "command_id") ?? "unknown"
                let reason = JSONWire.string(json, "reason") ?? "unknown"
                onLog("LINUX_SESSION_ACK type=\(type) command=\(commandID) accepted=\(accepted) reason=\(reason)")
                continue
            }
            onLog("未知 UDP 消息：\(source)")
        }
        stateLock.lock()
        let shouldClose = descriptor == socketDescriptor
        if shouldClose { descriptor = -1 }
        running = false
        stateLock.unlock()
        if shouldClose { Darwin.close(socketDescriptor) }
    }

    @discardableResult
    static func sendJSON(_ object: [String: Any], host: String, port: UInt16) throws -> Int {
        let data = try JSONWire.encode(object)
        let socketDescriptor = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard socketDescriptor >= 0 else { throw POSIXError(.ENOTSOCK) }
        defer { Darwin.close(socketDescriptor) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        guard inet_pton(AF_INET, host, &address.sin_addr) == 1 else { throw POSIXError(.EADDRNOTAVAIL) }
        let sent = data.withUnsafeBytes { bytes in
            withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.sendto(socketDescriptor, bytes.baseAddress, data.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        guard sent >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        return sent
    }

    static func bidirectionalTest(host: String, port: UInt16) throws -> Double {
        let socketDescriptor = Darwin.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard socketDescriptor >= 0 else { throw POSIXError(.ENOTSOCK) }
        defer { Darwin.close(socketDescriptor) }
        var timeout = timeval(tv_sec: 2, tv_usec: 0)
        setsockopt(socketDescriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout.size(ofValue: timeout)))
        var target = sockaddr_in()
        target.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        target.sin_family = sa_family_t(AF_INET)
        target.sin_port = port.bigEndian
        guard inet_pton(AF_INET, host, &target.sin_addr) == 1 else { throw POSIXError(.EADDRNOTAVAIL) }
        let nonce = UUID().uuidString
        let request = try JSONWire.encode([
            "protocol": "AVTWIN_UDP_TEST_V1",
            "type": "udp_test_ping",
            "nonce": nonce,
            "plays_audio": false
        ])
        let start = ProcessInfo.processInfo.systemUptime
        _ = request.withUnsafeBytes { bytes in
            withUnsafePointer(to: &target) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.sendto(socketDescriptor, bytes.baseAddress, request.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        var buffer = [UInt8](repeating: 0, count: 8_192)
        let count = Darwin.recv(socketDescriptor, &buffer, buffer.count, 0)
        guard count > 0,
              let json = JSONWire.decode(Data(buffer.prefix(count))),
              JSONWire.string(json, "type") == "udp_test_reply",
              JSONWire.string(json, "nonce") == nonce
        else { throw POSIXError(.ETIMEDOUT) }
        return (ProcessInfo.processInfo.systemUptime - start) * 1_000
    }

    private static func send(_ data: Data, descriptor: Int32, to address: inout sockaddr_storage, length: socklen_t) {
        data.withUnsafeBytes { bytes in
            withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    _ = Darwin.sendto(descriptor, bytes.baseAddress, data.count, 0, $0, length)
                }
            }
        }
    }

    private static func ipv4String(_ storage: sockaddr_storage) -> String {
        var copy = storage
        return withUnsafePointer(to: &copy) { pointer -> String in
            pointer.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { address in
                var value = address.pointee.sin_addr
                var output = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                inet_ntop(AF_INET, &value, &output, socklen_t(INET_ADDRSTRLEN))
                return String(cString: output)
            }
        }
    }

    private static func port(_ storage: sockaddr_storage) -> UInt16 {
        var copy = storage
        return withUnsafePointer(to: &copy) { pointer in
            pointer.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { UInt16(bigEndian: $0.pointee.sin_port) }
        }
    }
}
