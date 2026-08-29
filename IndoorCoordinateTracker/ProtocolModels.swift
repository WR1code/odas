import Foundation
import simd

enum JSONWire {
    static func decode(_ data: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    static func encode(_ object: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    static func string(_ object: [String: Any], _ key: String) -> String? {
        object[key] as? String
    }

    static func int64(_ object: [String: Any], _ key: String) -> Int64? {
        if let value = object[key] as? NSNumber { return value.int64Value }
        return nil
    }

    static func bool(_ object: [String: Any], _ key: String) -> Bool? {
        object[key] as? Bool
    }

    static func strings(_ object: [String: Any], _ key: String) -> [String] {
        object[key] as? [String] ?? []
    }

    static func doubles(_ object: [String: Any], _ key: String, count: Int) -> [Double]? {
        guard let values = object[key] as? [NSNumber], values.count == count else { return nil }
        let result = values.map(\.doubleValue)
        return result.allSatisfy { $0.isFinite } ? result : nil
    }

    static func matrix4x4(_ object: [String: Any], _ key: String) -> [[Double]]? {
        guard let rows = object[key] as? [[NSNumber]], rows.count == 4,
              rows.allSatisfy({ $0.count == 4 })
        else { return nil }
        let result = rows.map { $0.map(\.doubleValue) }
        return result.flatMap { $0 }.allSatisfy { $0.isFinite } ? result : nil
    }
}

struct SharedCoordinateVisualization: Sendable {
    let mode: String
    let sharedFrameID: String
    let alignmentID: String
    let phoneSourceFromSharedOrigin: simd_float4x4
    let phoneSourceFromLinuxMicrophone: simd_float4x4

    static func transform(_ rows: [[Double]]) -> simd_float4x4 {
        simd_float4x4(columns: (
            SIMD4<Float>(Float(rows[0][0]), Float(rows[1][0]), Float(rows[2][0]), Float(rows[3][0])),
            SIMD4<Float>(Float(rows[0][1]), Float(rows[1][1]), Float(rows[2][1]), Float(rows[3][1])),
            SIMD4<Float>(Float(rows[0][2]), Float(rows[1][2]), Float(rows[2][2]), Float(rows[3][2])),
            SIMD4<Float>(Float(rows[0][3]), Float(rows[1][3]), Float(rows[2][3]), Float(rows[3][3]))
        ))
    }
}

struct SharedOriginUpdate: Sendable {
    let commandID: String
    let accepted: Bool
    let reason: String
    let mode: String
    let sharedFrameID: String
    let phoneResetRequired: Bool
    let phonePosition: SIMD3<Double>?
    let linuxMicrophonePosition: SIMD3<Double>?
    let sharedFromPhoneSource: [[Double]]?
    let visualization: SharedCoordinateVisualization?

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "shared_origin_set_ack",
              let version = JSONWire.int64(json, "protocol_version"), version == 1,
              let commandID = JSONWire.string(json, "command_id"), !commandID.isEmpty,
              let accepted = JSONWire.bool(json, "accepted")
        else { return nil }
        self.commandID = commandID
        self.accepted = accepted
        reason = JSONWire.string(json, "reason") ?? "unknown"
        mode = JSONWire.string(json, "mode") ?? "unknown"
        sharedFrameID = JSONWire.string(json, "shared_frame_id") ?? "unknown"
        phoneResetRequired = JSONWire.bool(json, "phone_reset_required") ?? false
        phonePosition = JSONWire.doubles(json, "phone_position_m", count: 3).map {
            SIMD3<Double>($0[0], $0[1], $0[2])
        }
        linuxMicrophonePosition = JSONWire.doubles(json, "linux_microphone_position_m", count: 3).map {
            SIMD3<Double>($0[0], $0[1], $0[2])
        }
        sharedFromPhoneSource = JSONWire.matrix4x4(json, "shared_from_phone_source")
        if let shared = JSONWire.matrix4x4(json, "phone_source_from_shared_origin"),
           let linux = JSONWire.matrix4x4(json, "phone_source_from_linux_microphone") {
            visualization = SharedCoordinateVisualization(
                mode: mode, sharedFrameID: sharedFrameID,
                alignmentID: JSONWire.string(json, "calibration_signature") ?? "legacy",
                phoneSourceFromSharedOrigin: SharedCoordinateVisualization.transform(shared),
                phoneSourceFromLinuxMicrophone: SharedCoordinateVisualization.transform(linux)
            )
        } else {
            visualization = nil
        }
    }
}

struct MeasurementQualityResult: Sendable {
    let protocolVersion: Int
    let sessionID: String
    let measurementID: Int64
    let passed: Bool
    let overall: String
    let failureReasons: [String]
    let tofAvailable: Bool

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "measurement_quality",
              let version = JSONWire.int64(json, "protocol_version"),
              let sessionID = JSONWire.string(json, "session_id"), !sessionID.isEmpty,
              let measurementID = JSONWire.int64(json, "measurement_id"),
              let passed = JSONWire.bool(json, "quality_pass")
        else { return nil }
        protocolVersion = Int(version)
        self.sessionID = sessionID
        self.measurementID = measurementID
        self.passed = passed
        overall = JSONWire.string(json, "quality_overall") ?? (passed ? "PASS" : "FAIL")
        failureReasons = JSONWire.strings(json, "quality_failure_reasons")
        tofAvailable = JSONWire.bool(json, "tof_available") ?? false
    }
}

struct LinuxCaptureStateUpdate: Sendable {
    let state: String
    let readyForCapture: Bool
    let queuedRequests: Int
    let measurementID: Int64?
    let paused: Bool

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "linux_capture_state",
              let version = JSONWire.int64(json, "protocol_version"), version == 1,
              let state = JSONWire.string(json, "state"), !state.isEmpty,
              let readyForCapture = JSONWire.bool(json, "ready_for_capture"),
              let queuedRequests = JSONWire.int64(json, "queued_requests"), queuedRequests >= 0
        else { return nil }
        self.state = state
        self.readyForCapture = readyForCapture
        self.queuedRequests = Int(queuedRequests)
        measurementID = JSONWire.int64(json, "measurement_id")
        paused = JSONWire.bool(json, "paused") ?? false
    }
}

struct LidarMapCaptureUpdate: Sendable {
    let commandID: String
    let messageType: String
    let accepted: Bool
    let state: String
    let reason: String
    let pointCount: Int64?
    let calibrationHTTPPort: UInt16?

    init?(json: [String: Any]) {
        guard let type = JSONWire.string(json, "type"),
              type == "lidar_map_capture_ack" || type == "lidar_map_capture_status",
              let version = JSONWire.int64(json, "protocol_version"), version == 1,
              let commandID = JSONWire.string(json, "command_id"), !commandID.isEmpty,
              let accepted = JSONWire.bool(json, "accepted")
        else { return nil }
        self.commandID = commandID
        messageType = type
        self.accepted = accepted
        state = JSONWire.string(json, "state") ?? "unknown"
        reason = JSONWire.string(json, "reason") ?? "unknown"
        pointCount = JSONWire.int64(json, "point_count")
        if let rawPort = JSONWire.int64(json, "calibration_http_port") {
            calibrationHTTPPort = UInt16(exactly: rawPort)
        } else {
            calibrationHTTPPort = nil
        }
    }
}

struct MeasurementQualityAcceptResult: Sendable {
    let accepted: Bool
    let reason: String
}

struct ArmCommand: Sendable {
    let protocolVersion: Int
    let sessionID: String
    let measurementID: Int64
    let armEventID: String

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "arm",
              let version = JSONWire.int64(json, "protocol_version"),
              let sessionID = JSONWire.string(json, "session_id"), !sessionID.isEmpty,
              let measurementID = JSONWire.int64(json, "measurement_id"),
              let armEventID = JSONWire.string(json, "arm_event_id"), !armEventID.isEmpty
        else { return nil }
        self.protocolVersion = Int(version)
        self.sessionID = sessionID
        self.measurementID = measurementID
        self.armEventID = armEventID
    }
}

struct ArmAcceptResult: Sendable {
    let accepted: Bool
    let reason: String
}

struct CaptureStartCommand: Sendable {
    let protocolVersion: Int
    let commandID: String
    let linuxResultPort: UInt16

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "start_capture",
              let version = JSONWire.int64(json, "protocol_version"),
              let commandID = JSONWire.string(json, "command_id"), !commandID.isEmpty,
              let resultPort = JSONWire.int64(json, "linux_result_port"),
              let port = UInt16(exactly: resultPort), port > 0
        else { return nil }
        protocolVersion = Int(version)
        self.commandID = commandID
        linuxResultPort = port
    }
}

struct CaptureStopCommand: Sendable {
    let protocolVersion: Int
    let commandID: String
    let linuxResultPort: UInt16

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "stop_capture",
              let version = JSONWire.int64(json, "protocol_version"),
              let commandID = JSONWire.string(json, "command_id"), !commandID.isEmpty,
              let resultPort = JSONWire.int64(json, "linux_result_port"),
              let port = UInt16(exactly: resultPort), port > 0
        else { return nil }
        protocolVersion = Int(version)
        self.commandID = commandID
        linuxResultPort = port
    }
}

struct CaptureCommandResult: Sendable {
    let accepted: Bool
    let state: String
    let reason: String
}

struct C2BandTestCommand: Sendable {
    let protocolVersion: Int
    let testID: String
    let linuxResultPort: UInt16
    let repetitions: Int
    let intervalSeconds: Double
    let preRollSeconds: Double
    let tailSeconds: Double
    let expectedC2PCMHash: String

    init(localTestID: String, repetitions: Int) {
        protocolVersion = 1
        testID = localTestID
        linuxResultPort = 5005
        self.repetitions = min(20, max(1, repetitions))
        intervalSeconds = 2.0
        preRollSeconds = 0.25
        tailSeconds = 0.50
        expectedC2PCMHash = ""
    }

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "protocol") == "AVTWIN_C2_BAND_TEST_V1",
              JSONWire.string(json, "type") == "c2_band_test_start",
              let version = JSONWire.int64(json, "protocol_version"), version == 1,
              let testID = JSONWire.string(json, "test_id"), !testID.isEmpty,
              let rawPort = JSONWire.int64(json, "linux_result_port"),
              let port = UInt16(exactly: rawPort), port > 0,
              let rawRepetitions = JSONWire.int64(json, "repetitions"),
              (1...20).contains(rawRepetitions)
        else { return nil }
        let interval = (json["interval_s"] as? NSNumber)?.doubleValue ?? 2.0
        let preRoll = (json["pre_roll_s"] as? NSNumber)?.doubleValue ?? 0.25
        let tail = (json["tail_s"] as? NSNumber)?.doubleValue ?? 0.50
        guard interval.isFinite, interval >= 1.0, interval <= 10.0,
              preRoll.isFinite, preRoll >= 0.1, preRoll <= 2.0,
              tail.isFinite, tail >= 0.2, tail <= 5.0
        else { return nil }
        protocolVersion = Int(version)
        self.testID = testID
        linuxResultPort = port
        repetitions = Int(rawRepetitions)
        intervalSeconds = interval
        preRollSeconds = preRoll
        tailSeconds = tail
        expectedC2PCMHash = JSONWire.string(json, "c2_pcm_sha256") ?? ""
    }
}

struct C2BandTestAcceptResult: Sendable {
    let accepted: Bool
    let reason: String
    let actualC2PCMHash: String
}

struct CaptureOnceAcknowledgement: Sendable {
    let requestID: String
    let accepted: Bool
    let state: String
    let reason: String
    let measurementID: Int64?

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "capture_once_ack",
              let requestID = JSONWire.string(json, "request_id"), !requestID.isEmpty,
              let accepted = JSONWire.bool(json, "accepted")
        else { return nil }
        self.requestID = requestID
        self.accepted = accepted
        state = JSONWire.string(json, "state") ?? "unknown"
        reason = JSONWire.string(json, "reason") ?? "unknown"
        measurementID = JSONWire.int64(json, "measurement_id")
    }
}

struct PairingClaim: Sendable {
    let sessionID: String
    let measurementID: Int64
    let pairingMode: String
}

final class ArmPairingManager: @unchecked Sendable {
    private let lock = NSLock()
    private var pending: (command: ArmCommand, receivedMilliseconds: Int64)?
    private var boundSessionID: String?
    private var lastClaimedMeasurementID = Int64.min
    private var acceptedEvents: [String: Int64] = [:]
    private var acceptedEventOrder: [String] = []
    private var generationValue: UInt64 = 0
    private let maxArmAgeMilliseconds: Int64 = 10_000

    func reset() {
        lock.lock()
        pending = nil
        boundSessionID = nil
        lastClaimedMeasurementID = .min
        acceptedEvents.removeAll(keepingCapacity: true)
        acceptedEventOrder.removeAll(keepingCapacity: true)
        generationValue &+= 1
        lock.unlock()
    }

    func accept(_ command: ArmCommand, nowMilliseconds: Int64) -> ArmAcceptResult {
        lock.lock()
        defer { lock.unlock() }
        guard command.protocolVersion == 1 else { return .init(accepted: false, reason: "unsupported_protocol_version") }
        guard command.measurementID > 0 else { return .init(accepted: false, reason: "invalid_measurement_id") }
        if let boundSessionID, command.sessionID != boundSessionID {
            return .init(accepted: false, reason: "session_id_mismatch")
        }
        if let measurement = acceptedEvents[command.armEventID] {
            return .init(
                accepted: measurement == command.measurementID,
                reason: measurement == command.measurementID ? "duplicate_arm_reack" : "arm_event_id_reused"
            )
        }
        if command.measurementID <= lastClaimedMeasurementID {
            return .init(
                accepted: false,
                reason: command.measurementID == lastClaimedMeasurementID ? "duplicate_measurement_new_event" : "old_measurement_id"
            )
        }
        if let current = pending?.command, command.measurementID <= current.measurementID {
            return .init(
                accepted: false,
                reason: command.measurementID == current.measurementID ? "duplicate_measurement_new_event" : "older_than_pending_arm"
            )
        }
        if boundSessionID == nil { boundSessionID = command.sessionID }
        let superseding = pending != nil
        pending = (command, nowMilliseconds)
        acceptedEvents[command.armEventID] = command.measurementID
        acceptedEventOrder.append(command.armEventID)
        if acceptedEventOrder.count > 256 {
            acceptedEvents.removeValue(forKey: acceptedEventOrder.removeFirst())
        }
        generationValue &+= 1
        return .init(accepted: true, reason: superseding ? "accepted_superseding_pending_strict" : "accepted_strict")
    }

    func claimNext(nowMilliseconds: Int64) -> PairingClaim? {
        lock.lock()
        defer { lock.unlock() }
        guard let value = pending else { return nil }
        pending = nil
        let age = nowMilliseconds - value.receivedMilliseconds
        guard age >= 0, age <= maxArmAgeMilliseconds else { return nil }
        lastClaimedMeasurementID = max(lastClaimedMeasurementID, value.command.measurementID)
        return .init(
            sessionID: value.command.sessionID,
            measurementID: value.command.measurementID,
            pairingMode: "strict_armed"
        )
    }

    func clearPending() {
        lock.lock()
        pending = nil
        generationValue &+= 1
        lock.unlock()
    }

    func detectorGate() -> (armed: Bool, generation: UInt64) {
        lock.lock()
        defer { lock.unlock() }
        return (pending != nil, generationValue)
    }

    func pairedSessionID() -> String? {
        lock.lock()
        defer { lock.unlock() }
        return boundSessionID
    }

    func pendingMeasurementID() -> Int64? {
        lock.lock()
        defer { lock.unlock() }
        return pending?.command.measurementID
    }
}

struct ReplyAcknowledgement: Sendable {
    let sessionID: String
    let measurementID: Int64
    let eventID: String
    let accepted: Bool
    let reason: String

    init?(json: [String: Any]) {
        guard JSONWire.string(json, "type") == "reply_ack",
              let sessionID = JSONWire.string(json, "session_id"),
              let measurementID = JSONWire.int64(json, "measurement_id"),
              let eventID = JSONWire.string(json, "android_event_id"),
              let accepted = JSONWire.bool(json, "accepted")
        else { return nil }
        self.sessionID = sessionID
        self.measurementID = measurementID
        self.eventID = eventID
        self.accepted = accepted
        reason = JSONWire.string(json, "reason") ?? (accepted ? "accepted" : "rejected")
    }
}
