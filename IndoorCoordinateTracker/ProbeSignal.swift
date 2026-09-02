import CryptoKit
import Combine
import Foundation

struct ProbeDefinition: Sendable {
    let samples: [Float]
    let spectrumBins: [Float]
    let visualization: ProbeVisualization
    let name: String
    let isBuiltIn: Bool
    let originalSampleRate: Int
    let originalChannels: Int
    let sourceChannel: String
    let leftPeak: Double
    let rightPeak: Double
    let sourceSHA256: String
    let internalPCMSHA256: String
    let sourceURLDescription: String?

    var durationMilliseconds: Double { Double(samples.count) * 1_000 / ProbeDefaults.sampleRate }
    var internalPeak: Double { samples.reduce(0) { max($0, Double(abs($1))) } }

    var summary: String {
        let origin = isBuiltIn ? "内置" : "WAV"
        let conversion = originalSampleRate == Int(ProbeDefaults.sampleRate) && originalChannels == 1
            ? "" : " | source=\(originalSampleRate)Hz/\(originalChannels)ch → 48k mono"
        return String(format: "%@ | %.1f ms | %@%@ | source channel=%@", name, durationMilliseconds, origin, conversion, sourceChannel)
    }

    var diagnostics: String {
        String(
            format: "source channel=%@ | L peak=%.6f | R peak=%.6f | internal peak=%.6f\nsource SHA256=%@\ninternal PCM SHA256=%@",
            sourceChannel, leftPeak, rightPeak, internalPeak, sourceSHA256, internalPCMSHA256
        )
    }
}

enum ProbeDefaults {
    static let sampleRate = 48_000.0

    static func c1() -> ProbeDefinition {
        builtIn(name: "Default C1 11-19 kHz chirp", samples: chirp(duration: 0.200, start: 11_000, end: 19_000))
    }

    static func c2() -> ProbeDefinition {
        builtIn(name: "Default C2 50 Hz-9 kHz chirp", samples: chirp(duration: 0.200, start: 50, end: 9_000))
    }

    private static func builtIn(name: String, samples: [Float]) -> ProbeDefinition {
        let pcm = PCM16.encode(samples)
        let hash = SHA256.hex(pcm)
        return ProbeDefinition(
            samples: samples, spectrumBins: ProbeSpectrum.normalizedBins(samples),
            visualization: ProbeVisualization.analyze(samples), name: name, isBuiltIn: true,
            originalSampleRate: Int(sampleRate), originalChannels: 1,
            sourceChannel: "BUILT_IN_MONO", leftPeak: 0, rightPeak: 0,
            sourceSHA256: hash, internalPCMSHA256: hash, sourceURLDescription: nil
        )
    }

    private static func chirp(duration: Double, start: Double, end: Double) -> [Float] {
        let count = Int(sampleRate * duration)
        let sweep = (end - start) / duration
        let fadeCount = max(1, Int(sampleRate * 0.005))
        let pcm: [Int16] = (0..<count).map { index in
            let time = Double(index) / sampleRate
            let phase = 2 * Double.pi * (start * time + 0.5 * sweep * time * time)
            var envelope = 1.0
            if index < fadeCount {
                envelope = 0.5 - 0.5 * cos(Double.pi * Double(index) / Double(fadeCount))
            } else if index >= count - fadeCount {
                let reverse = count - 1 - index
                envelope = min(envelope, 0.5 - 0.5 * cos(Double.pi * Double(reverse) / Double(fadeCount)))
            }
            return Int16(clamping: Int(0.65 * envelope * sin(phase) * Double(Int16.max)))
        }
        return PCM16.floatSamples(pcm)
    }
}

enum SHA256 {
    static func hex(_ data: Data) -> String {
        CryptoKit.SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

enum PCM16 {
    static func integers(_ samples: [Float]) -> [Int16] {
        samples.map { sample in
            let scaled = Int((Double(sample.clamped(to: -1...1)) * 32_768).rounded())
            return Int16(clamping: scaled)
        }
    }

    static func floatSamples(_ samples: [Int16]) -> [Float] {
        samples.map { Float($0) / 32_768 }
    }

    static func encode(_ samples: [Float]) -> Data {
        var data = Data(capacity: samples.count * 2)
        for value in integers(samples) {
            var littleEndian = value.littleEndian
            withUnsafeBytes(of: &littleEndian) { data.append(contentsOf: $0) }
        }
        return data
    }
}

enum WavProbeLoader {
    static func load(_ url: URL) throws -> ProbeDefinition {
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        let data = try Data(contentsOf: url, options: .mappedIfSafe)
        guard data.count <= 16 * 1_024 * 1_024 else { throw error("WAV 超过 16 MB") }
        return try decode(name: url.lastPathComponent, data: data, source: url.absoluteString)
    }

    static func decode(name: String, data: Data, source: String? = nil) throws -> ProbeDefinition {
        let bytes = [UInt8](data)
        guard bytes.count >= 44, ascii(bytes, 0, 4) == "RIFF", ascii(bytes, 8, 4) == "WAVE" else {
            throw error("只支持 RIFF/WAVE 文件")
        }
        var format = -1, channels = -1, sampleRate = -1, bits = -1, dataOffset = -1, dataSize = -1
        var position = 12
        while position + 8 <= bytes.count {
            let identifier = ascii(bytes, position, 4)
            let size = Int(u32(bytes, position + 4))
            let start = position + 8
            guard size >= 0, start <= bytes.count, start + size <= bytes.count else { throw error("WAV chunk 截断：\(identifier)") }
            if identifier == "fmt " {
                guard size >= 16 else { throw error("WAV fmt chunk 无效") }
                format = u16(bytes, start)
                channels = u16(bytes, start + 2)
                sampleRate = Int(u32(bytes, start + 4))
                bits = u16(bytes, start + 14)
            } else if identifier == "data" {
                dataOffset = start
                dataSize = size
            }
            position = start + size + (size & 1)
        }
        guard dataOffset >= 0, dataSize > 0 else { throw error("WAV 没有 data chunk") }
        guard (1...8).contains(channels), (8_000...192_000).contains(sampleRate) else { throw error("WAV 采样率或声道数不支持") }
        let bytesPerSample: Int
        if format == 1, [8, 16, 24, 32].contains(bits) { bytesPerSample = bits / 8 }
        else if format == 3, bits == 32 { bytesPerSample = 4 }
        else { throw error("仅支持 PCM 8/16/24/32-bit 或 IEEE float32 WAV") }

        let frameBytes = bytesPerSample * channels
        let frameCount = dataSize / frameBytes
        guard frameCount > 0 else { throw error("WAV 音频为空") }
        let selectedChannel = channels >= 2 ? 1 : 0
        var selected = [Int16](repeating: 0, count: frameCount)
        var leftPeak = 0.0, rightPeak = 0.0
        for frame in 0..<frameCount {
            let base = dataOffset + frame * frameBytes
            for channel in 0..<channels {
                let offset = base + channel * bytesPerSample
                let unit = format == 3 ? float32(bytes, offset) : pcm(bytes, offset, bits)
                if channel == 0 { leftPeak = max(leftPeak, abs(unit)) }
                if channel == 1 { rightPeak = max(rightPeak, abs(unit)) }
                if channel == selectedChannel {
                    selected[frame] = Int16(clamping: Int(floor(unit.clamped(to: -1...1) * 32_767 + 0.5)))
                }
            }
        }
        if channels == 1 { rightPeak = 0 }
        let internalPCM = sampleRate == Int(ProbeDefaults.sampleRate)
            ? selected : linearResample(selected, from: sampleRate, to: Int(ProbeDefaults.sampleRate))
        let internalSamples = PCM16.floatSamples(internalPCM)
        let duration = Double(internalPCM.count) / ProbeDefaults.sampleRate
        guard duration >= 0.020 else { throw error("探针短于 20 ms") }
        guard duration <= 2.0 else { throw error("探针长于 2 s") }
        return ProbeDefinition(
            samples: internalSamples, spectrumBins: ProbeSpectrum.normalizedBins(internalSamples),
            visualization: ProbeVisualization.analyze(internalSamples), name: name, isBuiltIn: false,
            originalSampleRate: sampleRate, originalChannels: channels,
            sourceChannel: channels >= 2 ? "RIGHT" : "MONO",
            leftPeak: leftPeak, rightPeak: rightPeak,
            sourceSHA256: SHA256.hex(data), internalPCMSHA256: SHA256.hex(PCM16.encode(internalSamples)),
            sourceURLDescription: source
        )
    }

    private static func linearResample(_ input: [Int16], from sourceRate: Int, to targetRate: Int) -> [Int16] {
        let outputCount = max(1, Int((Double(input.count) * Double(targetRate) / Double(sourceRate)).rounded()))
        let ratio = Double(sourceRate) / Double(targetRate)
        return (0..<outputCount).map { index in
            let source = Double(index) * ratio
            let lower = min(input.count - 1, Int(floor(source)))
            let upper = min(input.count - 1, lower + 1)
            let fraction = source - Double(lower)
            let value = Double(input[lower]) * (1 - fraction) + Double(input[upper]) * fraction
            return Int16(clamping: Int(floor(value + 0.5)))
        }
    }

    private static func pcm(_ bytes: [UInt8], _ offset: Int, _ bits: Int) -> Double {
        switch bits {
        case 8: return Double(Int(bytes[offset]) - 128) / 128
        case 16: return Double(Int16(bitPattern: UInt16(u16(bytes, offset)))) / 32_768
        case 24:
            var value = Int(bytes[offset]) | Int(bytes[offset + 1]) << 8 | Int(bytes[offset + 2]) << 16
            if value & 0x800000 != 0 { value |= -0x1000000 }
            return Double(value) / 8_388_608
        case 32: return Double(Int32(bitPattern: u32(bytes, offset))) / 2_147_483_648
        default: return 0
        }
    }

    private static func float32(_ bytes: [UInt8], _ offset: Int) -> Double {
        let value = Float(bitPattern: u32(bytes, offset))
        return value.isFinite ? Double(value).clamped(to: -1...1) : 0
    }

    private static func ascii(_ bytes: [UInt8], _ offset: Int, _ count: Int) -> String {
        String(bytes: bytes[offset..<(offset + count)], encoding: .ascii) ?? ""
    }
    private static func u16(_ bytes: [UInt8], _ offset: Int) -> Int {
        Int(bytes[offset]) | Int(bytes[offset + 1]) << 8
    }
    private static func u32(_ bytes: [UInt8], _ offset: Int) -> UInt32 {
        UInt32(bytes[offset]) | UInt32(bytes[offset + 1]) << 8 | UInt32(bytes[offset + 2]) << 16 | UInt32(bytes[offset + 3]) << 24
    }
    private static func error(_ message: String) -> NSError {
        NSError(domain: "AVTwin.WAV", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

/// Small, precomputed frequency overview for the probe picker. Keeping this on
/// ProbeDefinition avoids recalculating an FFT-like preview for every AR frame.
enum ProbeSpectrum {
    static func normalizedBins(_ samples: [Float], count: Int = 72) -> [Float] {
        guard !samples.isEmpty, count > 0 else { return [] }
        let analysisCount = min(samples.count, 8_192)
        let window = samples.prefix(analysisCount).enumerated().map { index, sample in
            let hann = analysisCount > 1 ? 0.5 - 0.5 * cos(2 * .pi * Double(index) / Double(analysisCount - 1)) : 1
            return Double(sample) * hann
        }
        let values = (0..<count).map { bin -> Double in
            // Cover 0...24 kHz uniformly. A direct DFT is sufficient for this
            // deliberately tiny preview and runs only when a probe is loaded.
            let frequencyIndex = Double(bin + 1) * Double(analysisCount / 2) / Double(count)
            var real = 0.0, imaginary = 0.0
            for (sampleIndex, sample) in window.enumerated() {
                let phase = 2 * Double.pi * frequencyIndex * Double(sampleIndex) / Double(analysisCount)
                real += sample * cos(phase)
                imaginary -= sample * sin(phase)
            }
            return log10(max(1e-9, hypot(real, imaginary)))
        }
        guard let high = values.max() else {
            return Array(repeating: 0, count: count)
        }
        // `values` are log10 amplitudes; four decades gives an 80 dB display
        // range and prevents numerical noise from filling the whole graph.
        return values.map { Float((($0 - (high - 4)) / 4).clamped(to: 0...1)) }
    }
}

enum WavWriter {
    static func writeMonoPCM16(_ samples: [Float], to url: URL, sampleRate: Int = 48_000) throws {
        let pcm = PCM16.encode(samples)
        var output = Data()
        output.append("RIFF".data(using: .ascii)!)
        appendUInt32(UInt32(36 + pcm.count), to: &output)
        output.append("WAVEfmt ".data(using: .ascii)!)
        appendUInt32(16, to: &output)
        appendUInt16(1, to: &output)
        appendUInt16(1, to: &output)
        appendUInt32(UInt32(sampleRate), to: &output)
        appendUInt32(UInt32(sampleRate * 2), to: &output)
        appendUInt16(2, to: &output)
        appendUInt16(16, to: &output)
        output.append("data".data(using: .ascii)!)
        appendUInt32(UInt32(pcm.count), to: &output)
        output.append(pcm)
        try output.write(to: url, options: .atomic)
    }

    private static func appendUInt16(_ value: UInt16, to data: inout Data) {
        var little = value.littleEndian
        withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
    }
    private static func appendUInt32(_ value: UInt32, to data: inout Data) {
        var little = value.littleEndian
        withUnsafeBytes(of: &little) { data.append(contentsOf: $0) }
    }
}

enum ProbeSlot: String { case c1, c2 }

final class ProbeSelectionStore: ObservableObject {
    @Published private(set) var c1 = ProbeDefaults.c1()
    @Published private(set) var c2 = ProbeDefaults.c2()
    @Published private(set) var lastError: String?

    init() {
        restore(.c1)
        restore(.c2)
    }

    func select(_ url: URL, for slot: ProbeSlot) {
        do {
            let accessed = url.startAccessingSecurityScopedResource()
            defer { if accessed { url.stopAccessingSecurityScopedResource() } }
            let probe = try WavProbeLoader.load(url)
            let bookmark = try url.bookmarkData(options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
            UserDefaults.standard.set(bookmark, forKey: key(slot))
            if slot == .c1 { c1 = probe } else { c2 = probe }
            lastError = nil
        } catch { lastError = error.localizedDescription }
    }

    func useDefault(_ slot: ProbeSlot) {
        UserDefaults.standard.removeObject(forKey: key(slot))
        if slot == .c1 { c1 = ProbeDefaults.c1() } else { c2 = ProbeDefaults.c2() }
        lastError = nil
    }

    private func restore(_ slot: ProbeSlot) {
        guard let bookmark = UserDefaults.standard.data(forKey: key(slot)) else { return }
        var stale = false
        guard let url = try? URL(resolvingBookmarkData: bookmark, options: .withoutUI, relativeTo: nil, bookmarkDataIsStale: &stale),
              !stale else {
            UserDefaults.standard.removeObject(forKey: key(slot)); return
        }
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }
        guard let probe = try? WavProbeLoader.load(url) else {
            UserDefaults.standard.removeObject(forKey: key(slot)); return
        }
        if slot == .c1 { c1 = probe } else { c2 = probe }
    }

    private func key(_ slot: ProbeSlot) -> String { slot == .c1 ? "c1ProbeBookmark" : "c2ProbeBookmark" }
}

private extension Comparable {
    func clamped(to range: ClosedRange<Self>) -> Self { min(max(self, range.lowerBound), range.upperBound) }
}
