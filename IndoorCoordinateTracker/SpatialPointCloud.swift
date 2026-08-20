import ARKit
import CoreVideo
import Foundation
import Network
import simd

private struct SpatialVoxelKey: Hashable {
    let x: Int32
    let y: Int32
    let z: Int32
}

final class SpatialPointCloudAccumulator: @unchecked Sendable {
    private let lock = NSLock()
    private let voxelSize: Float
    private var voxels: [SpatialVoxelKey: SIMD3<Float>] = [:]
    private var scanning = false
    private var lastFrameTimestamp: TimeInterval = -.infinity
    private let minimumFrameInterval: TimeInterval = 0.18
    private let maximumPoints = 180_000

    init(voxelSize: Float = 0.08) {
        self.voxelSize = voxelSize
    }

    func start() {
        lock.lock()
        voxels.removeAll(keepingCapacity: true)
        scanning = true
        lastFrameTimestamp = -.infinity
        lock.unlock()
    }

    func stop() {
        lock.lock(); scanning = false; lock.unlock()
    }

    var isScanning: Bool {
        lock.lock(); defer { lock.unlock() }
        return scanning
    }

    var count: Int {
        lock.lock(); defer { lock.unlock() }
        return voxels.count
    }

    func add(frame: ARFrame, origin: SIMD3<Float>, basis: simd_float3x3) {
        lock.lock()
        guard scanning, frame.timestamp - lastFrameTimestamp >= minimumFrameInterval,
              voxels.count < maximumPoints
        else { lock.unlock(); return }
        lastFrameTimestamp = frame.timestamp
        lock.unlock()

        guard let depth = frame.sceneDepth else { return }
        let depthMap = depth.depthMap
        let confidenceMap = depth.confidenceMap
        CVPixelBufferLockBaseAddress(depthMap, .readOnly)
        if let confidenceMap { CVPixelBufferLockBaseAddress(confidenceMap, .readOnly) }
        defer {
            if let confidenceMap { CVPixelBufferUnlockBaseAddress(confidenceMap, .readOnly) }
            CVPixelBufferUnlockBaseAddress(depthMap, .readOnly)
        }
        guard let depthBase = CVPixelBufferGetBaseAddress(depthMap) else { return }
        let width = CVPixelBufferGetWidth(depthMap)
        let height = CVPixelBufferGetHeight(depthMap)
        let depthStride = CVPixelBufferGetBytesPerRow(depthMap) / MemoryLayout<Float32>.stride
        let depthValues = depthBase.assumingMemoryBound(to: Float32.self)
        let confidenceBase: UnsafeMutablePointer<UInt8>?
        if let confidenceMap, let base = CVPixelBufferGetBaseAddress(confidenceMap) {
            confidenceBase = base.assumingMemoryBound(to: UInt8.self)
        } else {
            confidenceBase = nil
        }
        let confidenceStride = confidenceMap.map { CVPixelBufferGetBytesPerRow($0) } ?? 0

        let imageSize = frame.camera.imageResolution
        let scaleX = Float(width) / Float(imageSize.width)
        let scaleY = Float(height) / Float(imageSize.height)
        let intrinsics = frame.camera.intrinsics
        let fx = intrinsics.columns.0.x * scaleX
        let fy = intrinsics.columns.1.y * scaleY
        let cx = intrinsics.columns.2.x * scaleX
        let cy = intrinsics.columns.2.y * scaleY
        guard fx > 0, fy > 0 else { return }
        let cameraToWorld = frame.camera.transform
        var additions: [(SpatialVoxelKey, SIMD3<Float>)] = []
        additions.reserveCapacity((width / 4) * (height / 4))
        for row in stride(from: 0, to: height, by: 4) {
            for column in stride(from: 0, to: width, by: 4) {
                let distance = depthValues[row * depthStride + column]
                guard distance.isFinite, distance >= 0.25, distance <= 8.0 else { continue }
                if let confidenceBase, confidenceBase[row * confidenceStride + column] < 1 { continue }
                let cameraPoint = SIMD4<Float>(
                    (Float(column) - cx) * distance / fx,
                    -(Float(row) - cy) * distance / fy,
                    -distance,
                    1
                )
                let world4 = cameraToWorld * cameraPoint
                let relative = basis.transpose * (SIMD3<Float>(world4.x, world4.y, world4.z) - origin)
                let key = SpatialVoxelKey(
                    x: Int32(floor(relative.x / voxelSize)),
                    y: Int32(floor(relative.y / voxelSize)),
                    z: Int32(floor(relative.z / voxelSize))
                )
                additions.append((key, relative))
            }
        }
        lock.lock()
        if scanning {
            for (key, point) in additions where voxels.count < maximumPoints {
                if voxels[key] == nil { voxels[key] = point }
            }
        }
        lock.unlock()
    }

    func encodedAVPC() throws -> Data {
        lock.lock()
        let points = Array(voxels.values)
        lock.unlock()
        guard points.count >= 80 else {
            throw NSError(domain: "AVTwinSpatialScan", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "有效深度点不足 80 个，请继续扫描有结构的静态表面"
            ])
        }
        let header: [String: Any] = [
            "format": "AVTWIN_POINT_CLOUD_V1",
            "point_count": points.count,
            "fields": ["x", "y", "z"],
            "dtype": "float32_le",
            "unit": "metre",
            "frame_id": "arkit_user_origin_x_forward_y_left_z_up",
            "source": "iphone_arkit_scene_depth",
            "gravity_aligned": true,
            "voxel_size_m": voxelSize
        ]
        let headerData = try JSONSerialization.data(withJSONObject: header, options: [.sortedKeys])
        var data = Data("AVPC0001".utf8)
        var headerLength = UInt32(headerData.count).littleEndian
        withUnsafeBytes(of: &headerLength) { data.append(contentsOf: $0) }
        data.append(headerData)
        data.reserveCapacity(data.count + points.count * 12)
        for point in points {
            for value in [point.x, point.y, point.z] {
                var bits = value.bitPattern.littleEndian
                withUnsafeBytes(of: &bits) { data.append(contentsOf: $0) }
            }
        }
        return data
    }
}

enum SpatialCalibrationUploader {
    static func upload(
        data: Data,
        host: String,
        port: UInt16,
        completion: @escaping @Sendable (Result<[String: Any], Error>) -> Void
    ) {
        guard let networkPort = NWEndpoint.Port(rawValue: port) else {
            completion(.failure(URLError(.badURL))); return
        }
        let connection = NWConnection(host: NWEndpoint.Host(host), port: networkPort, using: .tcp)
        let queue = DispatchQueue(label: "com.avtwin.ios.spatial-upload", qos: .userInitiated)
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                let header = "POST /v1/phone-map HTTP/1.0\r\nHost: \(host):\(port)\r\nContent-Type: application/vnd.avtwin.point-cloud\r\nContent-Length: \(data.count)\r\nConnection: close\r\n\r\n"
                var request = Data(header.utf8)
                request.append(data)
                connection.send(content: request, completion: .contentProcessed { error in
                    if let error { connection.cancel(); completion(.failure(error)); return }
                    Self.receiveResponse(connection: connection, data: Data(), completion: completion)
                })
            case .failed(let error):
                connection.cancel(); completion(.failure(error))
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    private static func receiveResponse(
        connection: NWConnection,
        data: Data,
        completion: @escaping @Sendable (Result<[String: Any], Error>) -> Void
    ) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) {
            chunk, _, complete, error in
            var received = data
            if let chunk { received.append(chunk) }
            if let error {
                connection.cancel(); completion(.failure(error)); return
            }
            if !complete {
                Self.receiveResponse(connection: connection, data: received, completion: completion)
                return
            }
            connection.cancel()
            let separator = Data("\r\n\r\n".utf8)
            guard let boundary = received.range(of: separator),
                  let statusLine = String(
                    data: received[..<boundary.lowerBound], encoding: .utf8
                  )?.components(separatedBy: "\r\n").first,
                  statusLine.contains(" 200 "),
                  let object = try? JSONSerialization.jsonObject(
                    with: Data(received[boundary.upperBound...])
                  ) as? [String: Any]
            else {
                completion(.failure(URLError(.badServerResponse))); return
            }
            completion(.success(object))
        }
    }
}
