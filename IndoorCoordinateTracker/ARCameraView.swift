import ARKit
import SwiftUI

struct ARCameraView: UIViewRepresentable {
    @ObservedObject var poseTracker: PoseTracker
    let visualOriginRevision: Int

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session.delegate = poseTracker
        view.delegate = context.coordinator
        view.automaticallyUpdatesLighting = true
        view.rendersCameraGrain = true
        view.scene.rootNode.addChildNode(context.coordinator.originNode)
        view.scene.rootNode.addChildNode(context.coordinator.connectorNode)
        view.scene.rootNode.addChildNode(context.coordinator.distanceNode)
        context.coordinator.lastRevision = visualOriginRevision
        startTracking(on: view)
        context.coordinator.pendingInitialPlacement = true
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {
        guard context.coordinator.lastRevision != visualOriginRevision else { return }
        context.coordinator.lastRevision = visualOriginRevision
        context.coordinator.placeOriginAtCamera(in: uiView)
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

    final class Coordinator: NSObject, ARSCNViewDelegate {
        fileprivate let originNode = ARCoordinateOriginNode()
        let connectorNode: SCNNode = {
            let cylinder = SCNCylinder(radius: 0.004, height: 0.001)
            cylinder.firstMaterial?.diffuse.contents = UIColor.systemYellow
            cylinder.firstMaterial?.emission.contents = UIColor.systemYellow.withAlphaComponent(0.35)
            return SCNNode(geometry: cylinder)
        }()
        let distanceNode: SCNNode = {
            let geometry = SCNText(string: "0.000 m", extrusionDepth: 0.1)
            geometry.font = .boldSystemFont(ofSize: 8)
            geometry.firstMaterial?.diffuse.contents = UIColor.systemYellow
            let node = SCNNode(geometry: geometry)
            node.scale = SCNVector3(0.006, 0.006, 0.006)
            node.constraints = [SCNBillboardConstraint()]
            return node
        }()
        var lastRevision = 0
        var pendingInitialPlacement = false
        private var originPosition = SIMD3<Float>.zero
        private var hasOrigin = false

        func placeOriginAtCamera(in view: ARSCNView) {
            guard let transform = view.session.currentFrame?.camera.transform else {
                pendingInitialPlacement = true
                return
            }
            originPosition = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
            originNode.simdWorldPosition = originPosition
            let cameraRotation = simd_float3x3(
                SIMD3<Float>(transform.columns.0.x, transform.columns.0.y, transform.columns.0.z),
                SIMD3<Float>(transform.columns.1.x, transform.columns.1.y, transform.columns.1.z),
                SIMD3<Float>(transform.columns.2.x, transform.columns.2.y, transform.columns.2.z)
            )
            originNode.simdOrientation = simd_quatf(Self.horizontalFLUBasis(cameraRotation: cameraRotation))
            hasOrigin = true
            pendingInitialPlacement = false
        }

        func renderer(_ renderer: SCNSceneRenderer, updateAtTime time: TimeInterval) {
            guard let view = renderer as? ARSCNView, let transform = view.session.currentFrame?.camera.transform else { return }
            if pendingInitialPlacement || !hasOrigin { placeOriginAtCamera(in: view) }
            guard hasOrigin else { return }
            let cameraPosition = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
            updateConnector(from: originPosition, to: cameraPosition)
        }

        private func updateConnector(from origin: SIMD3<Float>, to current: SIMD3<Float>) {
            let vector = current - origin
            let distance = simd_length(vector)
            let visible = distance >= 0.025
            connectorNode.isHidden = !visible
            distanceNode.isHidden = !visible
            guard visible, let cylinder = connectorNode.geometry as? SCNCylinder else { return }

            cylinder.height = CGFloat(distance)
            connectorNode.simdWorldPosition = (origin + current) / 2
            connectorNode.simdOrientation = simd_quatf(from: SIMD3<Float>(0, 1, 0), to: simd_normalize(vector))
            distanceNode.simdWorldPosition = (origin + current) / 2 + SIMD3<Float>(0, 0.045, 0)
            if let text = distanceNode.geometry as? SCNText {
                text.string = String(format: "原点 ↔ 当前 %.3f m", distance)
                let bounds = text.boundingBox
                distanceNode.pivot = SCNMatrix4MakeTranslation((bounds.min.x + bounds.max.x) / 2, bounds.min.y, 0)
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
    }
}

/// A world-space origin marker placed at the camera's exact AR position when
/// reset. It is not attached to the camera, so it remains there as the user moves.
private final class ARCoordinateOriginNode: SCNNode {
    override init() {
        super.init()
        // Geometry is authored in local FLU axes. The node is rotated to the
        // reset-time horizontal phone heading when the origin is placed.
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
