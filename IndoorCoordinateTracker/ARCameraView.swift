import ARKit
import SwiftUI

struct ARCameraView: UIViewRepresentable {
    @ObservedObject var poseTracker: PoseTracker
    let visualOriginRevision: Int

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session.delegate = poseTracker
        view.automaticallyUpdatesLighting = true
        view.rendersCameraGrain = true
        view.scene.rootNode.addChildNode(context.coordinator.originNode)
        context.coordinator.placeOrigin(in: view)
        context.coordinator.lastRevision = visualOriginRevision
        startTracking(on: view)
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        guard context.coordinator.lastRevision != visualOriginRevision else { return }
        context.coordinator.lastRevision = visualOriginRevision
        context.coordinator.placeOrigin(in: uiView)
    }

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: Coordinator) {
        uiView.session.pause()
    }

    private func startTracking(on view: ARSCNView) {
        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity
        configuration.environmentTexturing = .automatic
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            configuration.frameSemantics.insert(.sceneDepth)
        }
        view.session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
    }

    final class Coordinator {
        fileprivate let originNode = ARCoordinateOriginNode()
        var lastRevision = 0

        func placeOrigin(in view: ARSCNView) {
            if let transform = view.session.currentFrame?.camera.transform {
                let cameraPosition = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
                let cameraForward = -SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
                originNode.simdWorldPosition = cameraPosition + cameraForward * 0.9 + SIMD3<Float>(0, -0.18, 0)
            } else {
                // ARKit starts with the camera at the world origin looking down -Z.
                originNode.simdWorldPosition = SIMD3<Float>(0, -0.18, -0.9)
            }
            originNode.simdOrientation = simd_quatf(angle: 0, axis: SIMD3<Float>(0, 1, 0))
        }
    }
}

/// A world-space origin marker. It is added once and deliberately not attached
/// to the camera, so it remains at the placed point while the camera moves.
private final class ARCoordinateOriginNode: SCNNode {
    override init() {
        super.init()
        addChildNode(Self.axis(length: 0.22, radius: 0.006, color: .systemRed, axis: .x, label: "X"))
        addChildNode(Self.axis(length: 0.22, radius: 0.006, color: .systemGreen, axis: .y, label: "Y"))
        addChildNode(Self.axis(length: 0.22, radius: 0.006, color: .systemBlue, axis: .z, label: "Z"))
        let center = SCNSphere(radius: 0.014)
        center.firstMaterial?.diffuse.contents = UIColor.white
        addChildNode(SCNNode(geometry: center))
    }

    required init?(coder: NSCoder) { super.init(coder: coder) }

    private enum Axis { case x, y, z }

    private static func axis(length: CGFloat, radius: CGFloat, color: UIColor, axis: Axis, label: String) -> SCNNode {
        let root = SCNNode()
        let shaft = SCNNode(geometry: SCNCylinder(radius: radius, height: length))
        let arrow = SCNNode(geometry: SCNCone(topRadius: 0, bottomRadius: radius * 2.6, height: 0.045))
        shaft.geometry?.firstMaterial?.diffuse.contents = color
        arrow.geometry?.firstMaterial?.diffuse.contents = color

        switch axis {
        case .x:
            shaft.eulerAngles.z = -.pi / 2
            arrow.eulerAngles.z = -.pi / 2
            shaft.position.x = Float(length / 2)
            arrow.position.x = Float(length + 0.0225)
        case .y:
            shaft.position.y = Float(length / 2)
            arrow.position.y = Float(length + 0.0225)
        case .z:
            shaft.eulerAngles.x = .pi / 2
            arrow.eulerAngles.x = .pi / 2
            shaft.position.z = Float(length / 2)
            arrow.position.z = Float(length + 0.0225)
        }
        root.addChildNode(shaft)
        root.addChildNode(arrow)

        let textGeometry = SCNText(string: label, extrusionDepth: 0.2)
        textGeometry.font = .boldSystemFont(ofSize: 8)
        textGeometry.firstMaterial?.diffuse.contents = color
        let text = SCNNode(geometry: textGeometry)
        text.scale = SCNVector3(0.006, 0.006, 0.006)
        text.constraints = [SCNBillboardConstraint()]
        switch axis {
        case .x: text.position = SCNVector3(Float(length + 0.06), 0, 0)
        case .y: text.position = SCNVector3(0, Float(length + 0.06), 0)
        case .z: text.position = SCNVector3(0, 0, Float(length + 0.06))
        }
        root.addChildNode(text)
        return root
    }
}
