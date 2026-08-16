import ARKit
import Combine
import Foundation
import simd

struct DevicePose: Sendable {
    let source: String
    let frameID: String
    let revision: Int64
    let position: SIMD3<Double>
    let orientation: simd_quatd
    let yawDegrees: Double
    let pitchDegrees: Double
    let rollDegrees: Double
    let frameTimestamp: TimeInterval
    let capturedUptimeMilliseconds: Int64
    let trackingState: String

    static let unavailable = DevicePose(
        source: "unavailable", frameID: "unavailable", revision: 0,
        position: .zero,
        orientation: simd_quatd(angle: 0, axis: SIMD3<Double>(0, 1, 0)),
        yawDegrees: 0,
        pitchDegrees: 0,
        rollDegrees: 0,
        frameTimestamp: 0,
        capturedUptimeMilliseconds: 0,
        trackingState: "unavailable"
    )

    var wireFields: [String: Any] {
        let common: [String: Any] = [
            "ios_pose_source": source,
            "ios_pose_frame_id": frameID,
            "ios_pose_frame_timestamp_s": frameTimestamp,
            "ios_pose_captured_uptime_ms": capturedUptimeMilliseconds,
            "ios_pose_tracking_state": trackingState,
            "ios_position_x_m": position.x,
            "ios_position_y_m": position.y,
            "ios_position_z_m": position.z,
            "ios_orientation_yaw_deg": yawDegrees,
            "ios_orientation_pitch_deg": pitchDegrees,
            "ios_orientation_roll_deg": rollDegrees,
            "ios_orientation_qx": orientation.imag.x,
            "ios_orientation_qy": orientation.imag.y,
            "ios_orientation_qz": orientation.imag.z,
            "ios_orientation_qw": orientation.real
        ]

        // Keep the Android v0.9 field names as compatibility aliases. Existing Linux
        // collection code can ingest the iPhone responder without a schema migration.
        var fields = common
        fields.merge([
            "android_pose_source": source,
            "android_pose_frame_id": frameID,
            "android_pose_revision": revision,
            "android_pose_updated_elapsed_realtime_ms": capturedUptimeMilliseconds,
            "android_position_x_m": position.x,
            "android_position_y_m": position.y,
            "android_position_z_m": position.z,
            "android_orientation_yaw_deg": yawDegrees,
            "android_orientation_pitch_deg": pitchDegrees,
            "android_orientation_roll_deg": rollDegrees,
            "android_orientation_qx": orientation.imag.x,
            "android_orientation_qy": orientation.imag.y,
            "android_orientation_qz": orientation.imag.z,
            "android_orientation_qw": orientation.real
        ]) { _, new in new }
        return fields
    }
}

final class PoseTracker: NSObject, ObservableObject, ARSessionDelegate, @unchecked Sendable {
    @Published private(set) var currentPose = DevicePose.unavailable
    @Published private(set) var statusText = "等待 ARKit 初始化"

    private let poseLock = NSLock()
    private var latestPose = DevicePose.unavailable
    private var origin: SIMD3<Float>?
    private var resetRequested = true

    func snapshot() -> DevicePose {
        poseLock.lock()
        defer { poseLock.unlock() }
        return latestPose
    }

    func resetOrigin() {
        poseLock.lock()
        resetRequested = true
        poseLock.unlock()
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let transform = frame.camera.transform
        let current = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
        poseLock.lock()
        if resetRequested || origin == nil {
            origin = current
            resetRequested = false
        }
        let relative = current - (origin ?? current)
        poseLock.unlock()
        // Match the original coordinate tracker: ARKit camera-forward is -Z, while the
        // user-facing/exported frame uses +Z forward.
        let position = SIMD3<Double>(Double(relative.x), Double(relative.y), Double(-relative.z))
        let basisFlip = simd_float3x3(diagonal: SIMD3<Float>(1, 1, -1))
        let rawRotation = simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let correctedRotation = basisFlip * rawRotation * basisFlip
        let doubleRotation = simd_double3x3(
            SIMD3<Double>(
                Double(correctedRotation.columns.0.x),
                Double(correctedRotation.columns.0.y),
                Double(correctedRotation.columns.0.z)
            ),
            SIMD3<Double>(
                Double(correctedRotation.columns.1.x),
                Double(correctedRotation.columns.1.y),
                Double(correctedRotation.columns.1.z)
            ),
            SIMD3<Double>(
                Double(correctedRotation.columns.2.x),
                Double(correctedRotation.columns.2.y),
                Double(correctedRotation.columns.2.z)
            )
        )
        let quaternion = simd_quatd(doubleRotation)
        let euler = Self.zyxEulerDegrees(quaternion)
        let pose = DevicePose(
            source: "ios_arkit_world_tracking",
            frameID: "arkit_user_origin_x_right_y_up_z_forward",
            revision: Int64(frame.timestamp * 1_000),
            position: position,
            orientation: quaternion,
            yawDegrees: euler.yaw,
            pitchDegrees: euler.pitch,
            rollDegrees: euler.roll,
            frameTimestamp: frame.timestamp,
            capturedUptimeMilliseconds: Int64(ProcessInfo.processInfo.systemUptime * 1_000),
            trackingState: Self.trackingDescription(frame.camera.trackingState)
        )
        poseLock.lock()
        latestPose = pose
        poseLock.unlock()

        DispatchQueue.main.async { [weak self] in
            self?.currentPose = pose
            self?.statusText = pose.trackingState
        }
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        DispatchQueue.main.async { [weak self] in self?.statusText = "ARKit 错误：\(error.localizedDescription)" }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        DispatchQueue.main.async { [weak self] in self?.statusText = "ARKit 已中断" }
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        DispatchQueue.main.async { [weak self] in self?.statusText = "ARKit 正在恢复" }
    }

    private static func trackingDescription(_ state: ARCamera.TrackingState) -> String {
        switch state {
        case .normal:
            return "tracking"
        case .notAvailable:
            return "not_available"
        case let .limited(reason):
            switch reason {
            case .initializing: return "limited_initializing"
            case .excessiveMotion: return "limited_excessive_motion"
            case .insufficientFeatures: return "limited_insufficient_features"
            case .relocalizing: return "limited_relocalizing"
            @unknown default: return "limited_unknown"
            }
        }
    }

    private static func zyxEulerDegrees(_ quaternion: simd_quatd) -> (yaw: Double, pitch: Double, roll: Double) {
        let x = quaternion.imag.x, y = quaternion.imag.y, z = quaternion.imag.z, w = quaternion.real
        let roll = atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        let pitchTerm = max(-1.0, min(1.0, 2 * (w * y - z * x)))
        let pitch = asin(pitchTerm)
        let yaw = atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return (yaw * 180 / .pi, pitch * 180 / .pi, roll * 180 / .pi)
    }
}

enum PoseSourceMode: String, CaseIterable, Identifiable {
    case arkit = "ARKit 自动"
    case manual = "手动输入"
    var id: String { rawValue }
}

final class PoseSelectionStore: ObservableObject, @unchecked Sendable {
    @Published var mode: PoseSourceMode = .arkit
    @Published private(set) var manualPose = DevicePose.unavailable
    @Published private(set) var manualSummary = "尚未应用手动位姿"

    private let lock = NSLock()
    private let arkitSnapshot: @Sendable () -> DevicePose
    private var revision: Int64

    init(arkitSnapshot: @escaping @Sendable () -> DevicePose) {
        self.arkitSnapshot = arkitSnapshot
        revision = Int64(UserDefaults.standard.integer(forKey: "manualPoseRevision"))
        let defaults = UserDefaults.standard
        let values = ["x", "y", "z", "yaw", "pitch", "roll"].map { defaults.double(forKey: "manualPose.\($0)") }
        manualPose = Self.makeManual(values, revision: revision)
        manualSummary = Self.summary(manualPose)
        if defaults.string(forKey: "poseSourceMode") == PoseSourceMode.manual.rawValue { mode = .manual }
    }

    func snapshot() -> DevicePose {
        lock.lock()
        let selectedMode = mode
        let selectedManual = manualPose
        lock.unlock()
        return selectedMode == .manual ? selectedManual : arkitSnapshot()
    }

    @discardableResult
    func applyManual(_ textValues: [String]) -> Bool {
        guard textValues.count == 6 else { return false }
        let parsed = textValues.compactMap { Double($0.trimmingCharacters(in: .whitespacesAndNewlines)) }
        guard parsed.count == 6, parsed.allSatisfy(\.isFinite) else {
            DispatchQueue.main.async { self.manualSummary = "输入无效：六项必须是有限数字，仍使用上一次位姿" }
            return false
        }
        lock.lock()
        revision += 1
        let pose = Self.makeManual(parsed, revision: revision)
        manualPose = pose
        lock.unlock()
        let defaults = UserDefaults.standard
        for (key, value) in zip(["x", "y", "z", "yaw", "pitch", "roll"], parsed) {
            defaults.set(value, forKey: "manualPose.\(key)")
        }
        defaults.set(revision, forKey: "manualPoseRevision")
        DispatchQueue.main.async { self.manualSummary = Self.summary(pose) }
        return true
    }

    func setMode(_ newMode: PoseSourceMode) {
        lock.lock(); mode = newMode; lock.unlock()
        UserDefaults.standard.set(newMode.rawValue, forKey: "poseSourceMode")
    }

    private static func makeManual(_ values: [Double], revision: Int64) -> DevicePose {
        let yaw = values[3] * .pi / 180, pitch = values[4] * .pi / 180, roll = values[5] * .pi / 180
        let cy = cos(yaw / 2), sy = sin(yaw / 2), cp = cos(pitch / 2), sp = sin(pitch / 2), cr = cos(roll / 2), sr = sin(roll / 2)
        let quaternion = simd_quatd(
            ix: sr * cp * cy - cr * sp * sy,
            iy: cr * sp * cy + sr * cp * sy,
            iz: cr * cp * sy - sr * sp * cy,
            r: cr * cp * cy + sr * sp * sy
        )
        return DevicePose(
            source: "ios_manual_input", frameID: "manual_map", revision: revision,
            position: SIMD3<Double>(values[0], values[1], values[2]), orientation: quaternion,
            yawDegrees: values[3], pitchDegrees: values[4], rollDegrees: values[5],
            frameTimestamp: 0, capturedUptimeMilliseconds: Int64(ProcessInfo.processInfo.systemUptime * 1_000),
            trackingState: "manual"
        )
    }

    private static func summary(_ pose: DevicePose) -> String {
        String(format: "rev=%lld pos=(%.3f, %.3f, %.3f)m yaw/pitch/roll=(%.1f, %.1f, %.1f)°", pose.revision, pose.position.x, pose.position.y, pose.position.z, pose.yawDegrees, pose.pitchDegrees, pose.rollDegrees)
    }
}
