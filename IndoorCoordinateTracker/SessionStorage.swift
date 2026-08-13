import Combine
import Foundation

final class FolderSelectionStore: ObservableObject {
    @Published private(set) var selectedURL: URL?
    @Published private(set) var status = "未选择，将保存到 App Documents/AVTwin"
    private let bookmarkKey = "resultFolderSecurityBookmark"

    init() {
        guard let bookmark = UserDefaults.standard.data(forKey: bookmarkKey) else { return }
        var stale = false
        if let url = try? URL(
            resolvingBookmarkData: bookmark,
            options: .withSecurityScope,
            relativeTo: nil,
            bookmarkDataIsStale: &stale
        ), !stale {
            selectedURL = url
            status = "结果目录：\(url.lastPathComponent)（已恢复授权）"
        }
    }

    func select(_ url: URL) throws {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        try Self.validate(url)
        let bookmark = try url.bookmarkData(options: .withSecurityScope, includingResourceValuesForKeys: nil, relativeTo: nil)
        UserDefaults.standard.set(bookmark, forKey: bookmarkKey)
        selectedURL = url
        status = "结果目录：\(url.lastPathComponent)，读写验证通过"
    }

    func clear() {
        UserDefaults.standard.removeObject(forKey: bookmarkKey)
        selectedURL = nil
        status = "未选择，将保存到 App Documents/AVTwin"
    }

    static func validate(_ root: URL) throws {
        let test = root.appendingPathComponent(".avtwin_write_test_\(UUID().uuidString).tmp")
        try Data([0x41]).write(to: test, options: .atomic)
        try FileManager.default.removeItem(at: test)
    }
}

final class SessionStorage: @unchecked Sendable {
    private let queue = DispatchQueue(label: "com.avtwin.ios.session-storage")
    private let rootURL: URL
    private let sessionURL: URL
    private let eventsURL: URL
    private let poseURL: URL
    private let logsURL: URL
    private let sessionJSONURL: URL
    private let audioURL: URL?
    private let securityAccessStarted: Bool
    private let saveDebugAudio: Bool

    init(root: URL?, sessionID: String, c1: ProbeDefinition, c2: ProbeDefinition, saveDebugAudio: Bool) throws {
        let selectedRoot: URL
        let accessStarted: Bool
        if let root {
            selectedRoot = root
            accessStarted = root.startAccessingSecurityScopedResource()
        } else {
            selectedRoot = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("AVTwin", isDirectory: true)
            accessStarted = false
        }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let safeSession = sessionID.replacingOccurrences(of: "[^A-Za-z0-9._-]", with: "_", options: .regularExpression)
        let selectedSession = selectedRoot.appendingPathComponent("\(formatter.string(from: Date()))_\(safeSession)", isDirectory: true)
        let probes = selectedSession.appendingPathComponent("probes", isDirectory: true)
        let debugDirectory = selectedSession.appendingPathComponent("audio", isDirectory: true)
        rootURL = selectedRoot
        securityAccessStarted = accessStarted
        sessionURL = selectedSession
        eventsURL = selectedSession.appendingPathComponent("events.jsonl")
        poseURL = selectedSession.appendingPathComponent("manual_pose_records.csv")
        logsURL = selectedSession.appendingPathComponent("logs.txt")
        sessionJSONURL = selectedSession.appendingPathComponent("session.json")
        audioURL = saveDebugAudio ? debugDirectory : nil
        self.saveDebugAudio = saveDebugAudio

        do {
            if root != nil { try FolderSelectionStore.validate(selectedRoot) }
            else { try FileManager.default.createDirectory(at: selectedRoot, withIntermediateDirectories: true) }
            try FileManager.default.createDirectory(at: probes, withIntermediateDirectories: true)
            if saveDebugAudio { try FileManager.default.createDirectory(at: debugDirectory, withIntermediateDirectories: true) }
            FileManager.default.createFile(atPath: eventsURL.path, contents: nil)
            FileManager.default.createFile(atPath: logsURL.path, contents: nil)
            try "session_id,measurement_id,t2_sample,pose_revision,source,frame_id,x_m,y_m,z_m,yaw_deg,pitch_deg,roll_deg,qx,qy,qz,qw\n"
                .write(to: poseURL, atomically: true, encoding: .utf8)
            try WavWriter.writeMonoPCM16(c1.samples, to: probes.appendingPathComponent("c1_used.wav"))
            try WavWriter.writeMonoPCM16(c2.samples, to: probes.appendingPathComponent("c2_used.wav"))
            try Self.writeJSON([
                "c1_name": c1.name, "c1_source_sha256": c1.sourceSHA256,
                "c1_internal_pcm_sha256": c1.internalPCMSHA256, "c1_source_channel": c1.sourceChannel,
                "c2_name": c2.name, "c2_source_sha256": c2.sourceSHA256,
                "c2_internal_pcm_sha256": c2.internalPCMSHA256, "c2_source_channel": c2.sourceChannel,
                "sample_rate": Int(ProbeDefaults.sampleRate),
                "note": "c1_used.wav/c2_used.wav are exact internal 48 kHz mono templates"
            ], to: probes.appendingPathComponent("probe_metadata.json"))
        } catch {
            if accessStarted { selectedRoot.stopAccessingSecurityScopedResource() }
            throw error
        }
    }

    var path: String { sessionURL.path }
    var shareURL: URL { sessionURL }

    func appendEvent(_ object: [String: Any]) {
        queue.async { [eventsURL] in Self.appendJSONLine(object, to: eventsURL) }
    }

    func appendLog(_ line: String) {
        queue.async { [logsURL] in
            let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
            Self.appendText("\(formatter.string(from: Date())) \(line)\n", to: logsURL)
        }
    }

    func appendPose(sessionID: String, measurementID: Int64, t2: Int64, pose: DevicePose) {
        let q = pose.orientation
        let escapedSource = pose.source.replacingOccurrences(of: ",", with: "_")
        let escapedFrame = pose.frameID.replacingOccurrences(of: ",", with: "_")
        let line = [
            sessionID, String(measurementID), String(t2), String(pose.revision), escapedSource, escapedFrame,
            String(pose.position.x), String(pose.position.y), String(pose.position.z),
            String(pose.yawDegrees), String(pose.pitchDegrees), String(pose.rollDegrees),
            String(q.imag.x), String(q.imag.y), String(q.imag.z), String(q.real)
        ].joined(separator: ",") + "\n"
        queue.async { [poseURL] in Self.appendText(line, to: poseURL) }
    }

    func updateSession(_ object: [String: Any]) {
        queue.async { [sessionJSONURL] in try? Self.writeJSON(object, to: sessionJSONURL) }
    }

    func saveDebugWindow(name: String, samples: [Float]) {
        guard saveDebugAudio, let audioURL, !samples.isEmpty else { return }
        let safe = name.replacingOccurrences(of: "[\\/:*?\"<>|]", with: "_", options: .regularExpression)
        queue.async { try? WavWriter.writeMonoPCM16(samples, to: audioURL.appendingPathComponent(safe)) }
    }

    func close() {
        queue.sync {}
        if securityAccessStarted { rootURL.stopAccessingSecurityScopedResource() }
    }

    private static func appendJSONLine(_ object: [String: Any], to url: URL) {
        guard let data = try? JSONWire.encode(object), var text = String(data: data, encoding: .utf8) else { return }
        text.append("\n")
        appendText(text, to: url)
    }

    private static func appendText(_ text: String, to url: URL) {
        guard let data = text.data(using: .utf8), let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        do { try handle.seekToEnd(); try handle.write(contentsOf: data) } catch {}
    }

    private static func writeJSON(_ object: [String: Any], to url: URL) throws {
        try JSONWire.encode(object).write(to: url, options: .atomic)
    }
}
