import ARKit
import SwiftUI

struct ARCameraView: UIViewRepresentable {
    @ObservedObject var poseTracker: PoseTracker

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView(frame: .zero)
        view.session.delegate = poseTracker
        view.automaticallyUpdatesLighting = true
        view.rendersCameraGrain = true
        startTracking(on: view)
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: ()) {
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
}
