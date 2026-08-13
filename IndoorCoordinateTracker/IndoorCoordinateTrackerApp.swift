import SwiftUI

@main
struct IndoorCoordinateTrackerApp: App {
    @StateObject private var poseTracker = PoseTracker()

    var body: some Scene {
        WindowGroup {
            ContentView(poseTracker: poseTracker)
                .preferredColorScheme(.dark)
        }
    }
}
