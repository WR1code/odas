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

enum CapturePointQuality: String, Sendable {
    case pending
    case passed
    case failed
}

struct CapturePoint: Identifiable, Sendable {
    let id: UUID
    let sequence: Int
    let sessionID: String
    let measurementID: Int64
    let coordinate: SIMD3<Double>
    let arWorldPosition: SIMD3<Float>
    var quality: CapturePointQuality
    var qualityDetail: String
}

final class PoseTracker: NSObject, ObservableObject, ARSessionDelegate, @unchecked Sendable {
    @Published private(set) var currentPose = DevicePose.unavailable
    @Published private(set) var statusText = "等待 ARKit 初始化"
    @Published private(set) var capturePoints: [CapturePoint] = []
    @Published private(set) var isSpatialScanning = false
    @Published private(set) var spatialScanPointCount = 0
    @Published private(set) var spatialCalibrationStatus = "尚未开始空间扫描"

    private let poseLock = NSLock()
    private var latestPose = DevicePose.unavailable
    private var origin: SIMD3<Float>?
    private var originBasis: simd_float3x3?
    private var latestARWorldPosition: SIMD3<Float>?
    private var captureSequence = 0
    private var qualityByMeasurement: [String: (CapturePointQuality, String)] = [:]
    private var resetRequested = true
    private let spatialCloud = SpatialPointCloudAccumulator()

    func snapshot() -> DevicePose {
        poseLock.lock()
        defer { poseLock.unlock() }
        return latestPose
    }

    func resetOrigin() {
        guard !spatialCloud.isScanning else {
            DispatchQueue.main.async { [weak self] in self?.spatialCalibrationStatus = "扫描期间不能重置原点" }
            return
        }
        poseLock.lock()
        resetRequested = true
        poseLock.unlock()
    }

    func startSpatialScan() {
        guard snapshot().trackingState == "tracking" else {
            spatialCalibrationStatus = "ARKit 未处于 tracking，不能开始"
            return
        }
        spatialCloud.start()
        isSpatialScanning = true
        spatialScanPointCount = 0
        spatialCalibrationStatus = "正在扫描：请缓慢绕场并覆盖墙角、桌面和非对称物体"
    }

    func stopSpatialScan() {
        spatialCloud.stop()
        isSpatialScanning = false
        spatialScanPointCount = spatialCloud.count
        spatialCalibrationStatus = "扫描已停止，共 \(spatialCloud.count) 个体素点"
    }

    func uploadSpatialScan(host: String, port: UInt16 = 5010) {
        spatialCloud.stop()
        isSpatialScanning = false
        spatialCalibrationStatus = "正在编码并上传手机点云…"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                let data = try self.spatialCloud.encodedAVPC()
                SpatialCalibrationUploader.upload(data: data, host: host, port: port) { result in
                    DispatchQueue.main.async {
                        switch result {
                        case .failure(let error):
                            self.spatialCalibrationStatus = "上传/标定失败：\(error.localizedDescription)"
                        case .success(let response):
                            let calibration = response["result"] as? [String: Any]
                            let quality = calibration?["quality"] as? [String: Any]
                            let accepted = quality?["accepted"] as? Bool ?? false
                            let rmse = quality?["rmse_m"] as? Double
                            let reason = quality?["reason"] as? String ?? (response["reason"] as? String ?? "等待雷达地图")
                            let rmseText = rmse.map { String(format: "，RMSE %.3fm", $0) } ?? ""
                            self.spatialCalibrationStatus = accepted
                                ? "坐标系标定通过\(rmseText)"
                                : "点云已上传，但标定未通过：\(reason)\(rmseText)"
                        }
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.spatialCalibrationStatus = "点云编码失败：\(error.localizedDescription)"
                }
            }
        }
    }

    /// Records one completed C1 -> C2 exchange at the phone's physical AR
    /// position. `pose` supplies the coordinate printed beside the marker,
    /// while the AR-world position keeps the marker fixed as the camera moves.
    func recordCapturePoint(sessionID: String, measurementID: Int64, pose: DevicePose) {
        poseLock.lock()
        let arWorldPosition: SIMD3<Float>?
        if pose.source == "ios_arkit_world_tracking", let origin, let originBasis {
            let relative = SIMD3<Float>(
                Float(pose.position.x), Float(pose.position.y), Float(pose.position.z)
            )
            arWorldPosition = origin + originBasis * relative
        } else {
            arWorldPosition = latestARWorldPosition
        }
        guard let arWorldPosition else {
            poseLock.unlock()
            return
        }
        captureSequence += 1
        let key = Self.captureKey(sessionID: sessionID, measurementID: measurementID)
        let quality = qualityByMeasurement[key] ?? (.pending, "等待 Linux 质量判定")
        let point = CapturePoint(
            id: UUID(), sequence: captureSequence, sessionID: sessionID, measurementID: measurementID,
            coordinate: pose.position, arWorldPosition: arWorldPosition,
            quality: quality.0, qualityDetail: quality.1
        )
        poseLock.unlock()
        DispatchQueue.main.async { [weak self] in self?.capturePoints.append(point) }
    }

    func updateCaptureQuality(sessionID: String, measurementID: Int64, passed: Bool, detail: String) {
        let quality: CapturePointQuality = passed ? .passed : .failed
        let key = Self.captureKey(sessionID: sessionID, measurementID: measurementID)
        poseLock.lock()
        qualityByMeasurement[key] = (quality, detail)
        poseLock.unlock()
        DispatchQueue.main.async { [weak self] in
            guard let self,
                  let index = self.capturePoints.lastIndex(where: {
                      $0.sessionID == sessionID && $0.measurementID == measurementID
                  })
            else { return }
            self.capturePoints[index].quality = quality
            self.capturePoints[index].qualityDetail = detail
        }
    }

    private static func captureKey(sessionID: String, measurementID: Int64) -> String {
        "\(sessionID)|\(measurementID)"
    }

    func clearCapturePoints() {
        poseLock.lock()
        captureSequence = 0
        qualityByMeasurement.removeAll()
        poseLock.unlock()
        DispatchQueue.main.async { [weak self] in self?.capturePoints.removeAll() }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let transform = frame.camera.transform
        let current = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
        let rawRotation = simd_float3x3(
            SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
            SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
            SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
        )
        let currentHorizontalBasis = Self.horizontalFLUBasis(cameraRotation: rawRotation)
        poseLock.lock()
        latestARWorldPosition = current
        if resetRequested || origin == nil {
            origin = current
            originBasis = currentHorizontalBasis
            resetRequested = false
        }
        let referenceOrigin = origin ?? current
        let relative = current - referenceOrigin
        let referenceBasis = originBasis ?? currentHorizontalBasis
        poseLock.unlock()
        // `referenceBasis` columns are the reset-time X-forward, Y-left and
        // gravity-aligned Z-up axes expressed in ARKit world coordinates.
        let translated = referenceBasis.transpose * relative
        if spatialCloud.isScanning {
            spatialCloud.add(frame: frame, origin: referenceOrigin, basis: referenceBasis)
            let count = spatialCloud.count
            DispatchQueue.main.async { [weak self] in self?.spatialScanPointCount = count }
        }
        let position = SIMD3<Double>(Double(translated.x), Double(translated.y), Double(translated.z))
        // ARKit exposes camera axes in its landscape camera convention. This
        // app is portrait-only, so map those axes to the physical phone body:
        // X forward, Y to the phone's left, Z toward the top edge. Without
        // this portrait compensation an upright phone reads roughly 90° roll.
        let cameraFLUInARWorld = simd_float3x3(
            -rawRotation.columns.2,
            -rawRotation.columns.1,
            -rawRotation.columns.0
        )
        let correctedRotation = referenceBasis.transpose * cameraFLUInARWorld
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
        let euler = Self.navigationAnglesDegrees(quaternion)
        let pose = DevicePose(
            source: "ios_arkit_world_tracking",
            frameID: "arkit_user_origin_x_forward_y_left_z_up",
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

    private static func horizontalFLUBasis(cameraRotation: simd_float3x3) -> simd_float3x3 {
        let worldUp = SIMD3<Float>(0, 1, 0)
        var forward = -cameraRotation.columns.2
        forward.y = 0
        if simd_length_squared(forward) < 1e-6 { forward = SIMD3<Float>(0, 0, -1) }
        forward = simd_normalize(forward)
        let left = simd_normalize(simd_cross(worldUp, forward))
        return simd_float3x3(forward, left, worldUp)
    }

    /// Navigation angles for X-forward, Y-left, Z-up coordinates.
    /// Yaw is positive toward +Y (left), pitch is nose-up, and roll is about +X.
    private static func navigationAnglesDegrees(_ quaternion: simd_quatd) -> (yaw: Double, pitch: Double, roll: Double) {
        let forward = quaternion.act(SIMD3<Double>(1, 0, 0))
        let left = quaternion.act(SIMD3<Double>(0, 1, 0))
        let up = quaternion.act(SIMD3<Double>(0, 0, 1))
        let yaw = atan2(forward.y, forward.x)
        let pitch = atan2(forward.z, hypot(forward.x, forward.y))
        let roll = atan2(left.z, up.z)
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
        let yawRotation = simd_quatd(angle: yaw, axis: SIMD3<Double>(0, 0, 1))
        let pitchRotation = simd_quatd(angle: -pitch, axis: SIMD3<Double>(0, 1, 0))
        let rollRotation = simd_quatd(angle: roll, axis: SIMD3<Double>(1, 0, 0))
        let quaternion = yawRotation * pitchRotation * rollRotation
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
