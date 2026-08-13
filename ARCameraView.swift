import ARKit
import SceneKit
import SwiftUI

struct ARCameraView: UIViewRepresentable {
    let resetID: UUID
    let onUpdate: (TrackingSample) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(resetID: resetID, onUpdate: onUpdate)
    }

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.automaticallyUpdatesLighting = true
        view.session.delegate = context.coordinator

        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity

        // 在支持 LiDAR 的设备上请求逐帧深度数据。
        // 位姿融合本身由 ARKit 完成，应用不需要手动融合 IMU。
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            configuration.frameSemantics.insert(.sceneDepth)
        }

        view.session.run(
            configuration,
            options: [.resetTracking, .removeExistingAnchors]
        )
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        context.coordinator.onUpdate = onUpdate
        if context.coordinator.resetID != resetID {
            context.coordinator.resetID = resetID
            context.coordinator.requestReset()
        }
    }

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: Coordinator) {
        uiView.session.pause()
    }

    final class Coordinator: NSObject, ARSessionDelegate {
        var resetID: UUID
        var onUpdate: (TrackingSample) -> Void

        private let lock = NSLock()
        private var origin: SIMD3<Float>?
        private var resetRequested = true
        private var lastPosition = SIMD3<Float>(repeating: 0)

        init(resetID: UUID, onUpdate: @escaping (TrackingSample) -> Void) {
            self.resetID = resetID
            self.onUpdate = onUpdate
        }

        func requestReset() {
            lock.lock()
            resetRequested = true
            lock.unlock()
        }

        func session(_ session: ARSession, didUpdate frame: ARFrame) {
            let tracking = trackingDescription(frame.camera.trackingState)

            guard case .normal = frame.camera.trackingState else {
                publish(position: lastPosition, status: tracking, isTracking: false)
                return
            }

            let translation = frame.camera.transform.columns.3
            let current = SIMD3<Float>(translation.x, translation.y, translation.z)

            lock.lock()
            if resetRequested || origin == nil {
                origin = current
                resetRequested = false
            }
            let relative = current - origin!

            // ARKit 的相机朝前方向是 -Z；对用户显示时将其翻转，
            // 使得从起始朝向向前移动时 Z 为正数。
            lastPosition = SIMD3<Float>(relative.x, relative.y, -relative.z)
            let output = lastPosition
            lock.unlock()

            publish(position: output, status: tracking, isTracking: true)
        }

        private func publish(position: SIMD3<Float>, status: String, isTracking: Bool) {
            let callback = onUpdate
            let sample = TrackingSample(
                x: position.x,
                y: position.y,
                z: position.z,
                status: status,
                isTracking: isTracking
            )
            DispatchQueue.main.async {
                callback(sample)
            }
        }

        private func trackingDescription(_ state: ARCamera.TrackingState) -> String {
            switch state {
            case .normal:
                return "跟踪正常"
            case .notAvailable:
                return "跟踪不可用"
            case .limited(let reason):
                switch reason {
                case .initializing:
                    return "正在建立坐标系…"
                case .excessiveMotion:
                    return "移动过快，请放慢"
                case .insufficientFeatures:
                    return "环境特征不足，请对准物体"
                case .relocalizing:
                    return "正在重新定位…"
                @unknown default:
                    return "跟踪受限"
                }
            }
        }
    }
}
