import ARKit
import CoreVideo
import Foundation
import Network
import SceneKit
import SwiftUI
import simd

struct SpatialPointCloudSnapshot: Identifiable, Sendable {
    let id = UUID()
    let points: [SIMD3<Float>]
    let frameID: String
    let source: String
}

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

    func snapshot(maximumPreviewPoints: Int = 60_000) -> SpatialPointCloudSnapshot? {
        lock.lock()
        let values = Array(voxels.values)
        lock.unlock()
        guard values.count >= 3 else { return nil }
        let points: [SIMD3<Float>]
        if values.count <= maximumPreviewPoints {
            points = values
        } else {
            let step = max(1, values.count / maximumPreviewPoints)
            points = stride(from: 0, to: values.count, by: step).prefix(maximumPreviewPoints).map {
                values[$0]
            }
        }
        return SpatialPointCloudSnapshot(
            points: points,
            frameID: "arkit_user_origin_x_forward_y_left_z_up",
            source: "iphone_arkit_scene_depth_live"
        )
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

enum SpatialPointCloudDownloader {
    static func download(
        host: String,
        port: UInt16,
        completion: @escaping @Sendable (Result<SpatialPointCloudSnapshot, Error>) -> Void
    ) {
        guard let networkPort = NWEndpoint.Port(rawValue: port) else {
            completion(.failure(URLError(.badURL))); return
        }
        let connection = NWConnection(host: NWEndpoint.Host(host), port: networkPort, using: .tcp)
        let queue = DispatchQueue(label: "com.avtwin.ios.lidar-map-download", qos: .userInitiated)
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                let request = "GET /v1/lidar-map HTTP/1.0\r\nHost: \(host):\(port)\r\nAccept: application/vnd.avtwin.point-cloud\r\nConnection: close\r\n\r\n"
                connection.send(content: Data(request.utf8), completion: .contentProcessed { error in
                    if let error {
                        connection.cancel(); completion(.failure(error)); return
                    }
                    receive(connection: connection, accumulated: Data(), completion: completion)
                })
            case .failed(let error):
                connection.cancel(); completion(.failure(error))
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    private static func receive(
        connection: NWConnection,
        accumulated: Data,
        completion: @escaping @Sendable (Result<SpatialPointCloudSnapshot, Error>) -> Void
    ) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1024 * 1024) {
            chunk, _, complete, error in
            var received = accumulated
            if let chunk { received.append(chunk) }
            if let error {
                connection.cancel(); completion(.failure(error)); return
            }
            if !complete {
                receive(connection: connection, accumulated: received, completion: completion)
                return
            }
            connection.cancel()
            do {
                let separator = Data("\r\n\r\n".utf8)
                guard let boundary = received.range(of: separator),
                      let statusLine = String(
                        data: received[..<boundary.lowerBound], encoding: .utf8
                      )?.components(separatedBy: "\r\n").first,
                      statusLine.contains(" 200 ")
                else { throw URLError(.badServerResponse) }
                completion(.success(try decodeAVPC(Data(received[boundary.upperBound...]))))
            } catch {
                completion(.failure(error))
            }
        }
    }

    static func decodeAVPC(_ data: Data) throws -> SpatialPointCloudSnapshot {
        let bytes = [UInt8](data)
        guard bytes.count >= 12, Data(bytes[0..<8]) == Data("AVPC0001".utf8) else {
            throw NSError(domain: "AVTwinPointCloud", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Linux 返回的不是 AVTWIN 点云"
            ])
        }
        let headerLength = Int(UInt32(bytes[8])
            | UInt32(bytes[9]) << 8
            | UInt32(bytes[10]) << 16
            | UInt32(bytes[11]) << 24)
        let payloadOffset = 12 + headerLength
        guard headerLength > 0, headerLength <= 1_048_576, payloadOffset <= bytes.count,
              let header = try JSONSerialization.jsonObject(
                with: Data(bytes[12..<payloadOffset])
              ) as? [String: Any],
              header["format"] as? String == "AVTWIN_POINT_CLOUD_V1",
              let pointCount = (header["point_count"] as? NSNumber)?.intValue,
              pointCount >= 3, pointCount <= 2_000_000,
              bytes.count == payloadOffset + pointCount * 12
        else {
            throw NSError(domain: "AVTwinPointCloud", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Linux 点云头或数据长度无效"
            ])
        }
        var points: [SIMD3<Float>] = []
        points.reserveCapacity(pointCount)
        func float(at offset: Int) -> Float {
            let bits = UInt32(bytes[offset])
                | UInt32(bytes[offset + 1]) << 8
                | UInt32(bytes[offset + 2]) << 16
                | UInt32(bytes[offset + 3]) << 24
            return Float(bitPattern: bits)
        }
        for index in 0..<pointCount {
            let offset = payloadOffset + index * 12
            let point = SIMD3<Float>(
                float(at: offset), float(at: offset + 4), float(at: offset + 8)
            )
            if point.x.isFinite, point.y.isFinite, point.z.isFinite { points.append(point) }
        }
        guard points.count >= 3 else {
            throw NSError(domain: "AVTwinPointCloud", code: 3, userInfo: [
                NSLocalizedDescriptionKey: "Linux 点云没有足够的有限坐标"
            ])
        }
        return SpatialPointCloudSnapshot(
            points: points,
            frameID: header["frame_id"] as? String ?? "unknown",
            source: header["source"] as? String ?? "linux_lidar_map"
        )
    }
}

struct SpatialPointCloudPreview: UIViewRepresentable {
    let cloud: SpatialPointCloudSnapshot

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> SCNView {
        let view = SCNView(frame: .zero)
        view.backgroundColor = .black
        view.allowsCameraControl = true
        view.autoenablesDefaultLighting = false
        view.antialiasingMode = .multisampling4X
        context.coordinator.render(cloud, in: view)
        return view
    }

    func updateUIView(_ view: SCNView, context: Context) {
        guard context.coordinator.renderedID != cloud.id else { return }
        context.coordinator.render(cloud, in: view)
    }

    final class Coordinator {
        var renderedID: UUID?

        func render(_ cloud: SpatialPointCloudSnapshot, in view: SCNView) {
            renderedID = cloud.id
            let scene = SCNScene()
            let converted = cloud.points.map { SIMD3<Float>(-$0.y, $0.z, -$0.x) }
            let minimumZ = converted.map(\.y).min() ?? 0
            let maximumZ = converted.map(\.y).max() ?? 1
            let heightSpan = max(maximumZ - minimumZ, 0.01)
            let colors = converted.map { point -> SIMD4<Float> in
                let normalized = max(0, min(1, (point.y - minimumZ) / heightSpan))
                return Self.turboColor(normalized)
            }
            let indices = (0..<converted.count).map(UInt32.init)
            let vertexData = converted.withUnsafeBytes { Data($0) }
            let colorData = colors.withUnsafeBytes { Data($0) }
            let indexData = indices.withUnsafeBytes { Data($0) }
            let vertices = SCNGeometrySource(
                data: vertexData, semantic: .vertex, vectorCount: converted.count,
                usesFloatComponents: true, componentsPerVector: 3,
                bytesPerComponent: MemoryLayout<Float>.size,
                dataOffset: 0, dataStride: MemoryLayout<SIMD3<Float>>.stride
            )
            let colorSource = SCNGeometrySource(
                data: colorData, semantic: .color, vectorCount: colors.count,
                usesFloatComponents: true, componentsPerVector: 4,
                bytesPerComponent: MemoryLayout<Float>.size,
                dataOffset: 0, dataStride: MemoryLayout<SIMD4<Float>>.stride
            )
            let element = SCNGeometryElement(
                data: indexData, primitiveType: .point,
                primitiveCount: indices.count, bytesPerIndex: MemoryLayout<UInt32>.size
            )
            element.pointSize = 2.0
            element.minimumPointScreenSpaceRadius = 1.0
            element.maximumPointScreenSpaceRadius = 3.0
            let geometry = SCNGeometry(sources: [vertices, colorSource], elements: [element])
            let material = SCNMaterial()
            material.lightingModel = .constant
            material.isDoubleSided = true
            geometry.materials = [material]
            scene.rootNode.addChildNode(SCNNode(geometry: geometry))

            let sum = converted.reduce(SIMD3<Float>.zero, +)
            let center = sum / Float(converted.count)
            let radius = max(converted.map { simd_length($0 - center) }.max() ?? 1, 0.5)
            let camera = SCNCamera()
            camera.zNear = 0.01
            camera.zFar = Double(max(radius * 8, 20))
            let cameraNode = SCNNode()
            cameraNode.camera = camera
            cameraNode.simdPosition = center + SIMD3<Float>(radius * 0.9, radius * 0.7, radius * 1.4)
            cameraNode.look(at: SCNVector3(center))
            scene.rootNode.addChildNode(cameraNode)
            view.scene = scene
            view.pointOfView = cameraNode
        }

        private static func turboColor(_ value: Float) -> SIMD4<Float> {
            // Compact blue-cyan-green-yellow-red height ramp.
            let red = max(0, min(1, 1.5 - abs(4 * value - 3)))
            let green = max(0, min(1, 1.5 - abs(4 * value - 2)))
            let blue = max(0, min(1, 1.5 - abs(4 * value - 1)))
            return SIMD4<Float>(red, green, blue, 1)
        }
    }
}
