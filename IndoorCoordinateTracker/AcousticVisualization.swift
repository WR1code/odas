import Combine
import Foundation
import SwiftUI

struct AcousticMonitorSnapshot: Sendable {
    var waveform: [Float]
    var spectrumDBFS: [Float]
    var spectrogramDBFS: [Float]
    var spectrogramColumns: Int
    var spectrogramRows: Int
    var rir: [Float]
    var rirPreArrivalSamples: Int
    var status: String

    static let empty = AcousticMonitorSnapshot(
        waveform: Array(repeating: 0, count: 240),
        spectrumDBFS: Array(repeating: -120, count: 121),
        spectrogramDBFS: Array(repeating: -120, count: 96 * 2),
        spectrogramColumns: 2,
        spectrogramRows: 96,
        rir: Array(repeating: 0, count: 480),
        rirPreArrivalSamples: 480,
        status: "等待会话音频"
    )
}

final class AcousticMonitorStore: ObservableObject {
    @Published private(set) var snapshot = AcousticMonitorSnapshot.empty

    func publish(_ value: AcousticMonitorSnapshot) { snapshot = value }
    func reset() { snapshot = .empty }
}

struct ProbeVisualization: Sendable {
    let spectrumDBFS: [Float]
    let spectrogramDBFS: [Float]
    let columns: Int
    let rows: Int
    let peak: Double
    let rms: Double
    let energySamples: Double
    let energySeconds: Double
    let crestDB: Double
    let sweepStartHz: Double?
    let sweepEndHz: Double?
    let sweepR2: Double?

    static func analyze(_ samples: [Float]) -> ProbeVisualization {
        let peak = samples.reduce(0.0) { max($0, Double(abs($1))) }
        let energy = samples.reduce(0.0) { $0 + Double($1) * Double($1) }
        let rms = samples.isEmpty ? 0 : sqrt(energy / Double(samples.count))
        let crest = rms > 0 ? 20 * log10(max(peak, 1e-12) / rms) : 0
        let spectrum = AcousticVisualizationDSP.spectrumDBFS(samples, outputBins: 121)
        let stft = AcousticVisualizationDSP.spectrogramDBFS(samples, rows: 96)
        let sweep = AcousticVisualizationDSP.estimateSweep(
            stft.values, columns: stft.columns, rows: stft.rows,
            duration: Double(samples.count) / ProbeDefaults.sampleRate
        )
        return ProbeVisualization(
            spectrumDBFS: spectrum, spectrogramDBFS: stft.values,
            columns: stft.columns, rows: stft.rows,
            peak: peak, rms: rms, energySamples: energy,
            energySeconds: energy / ProbeDefaults.sampleRate, crestDB: crest,
            sweepStartHz: sweep?.start, sweepEndHz: sweep?.end, sweepR2: sweep?.r2
        )
    }
}

final class AcousticMonitorProcessor {
    private let store: AcousticMonitorStore
    private var history = SampleRing(capacity: 96_000)
    private var spectrogramColumns: [[Float]] = []
    private var samplesToHop = 256
    private var samplesToPublish = 4_800
    private var rir: [Float] = Array(repeating: 0, count: 480)
    private var rirPreArrivalSamples = 480
    private var status = "等待会话音频"

    init(store: AcousticMonitorStore) { self.store = store }

    func reset() {
        history = SampleRing(capacity: 96_000)
        spectrogramColumns.removeAll(keepingCapacity: true)
        samplesToHop = 256
        samplesToPublish = 4_800
        rir = Array(repeating: 0, count: 480)
        rirPreArrivalSamples = 480
        status = "实时监听 iPhone 麦克风"
        DispatchQueue.main.async { self.store.reset() }
    }

    func append(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        var offset = 0
        while offset < samples.count {
            let count = min(samplesToHop, samples.count - offset)
            history.append(contentsOf: samples[offset..<(offset + count)])
            offset += count
            samplesToHop -= count
            if samplesToHop == 0 {
                if history.count >= 1_024 {
                    spectrogramColumns.append(
                        AcousticVisualizationDSP.spectrumDBFS(
                            history.suffix(1_024), fftSize: 1_024, outputBins: 96
                        )
                    )
                    if spectrogramColumns.count > 375 {
                        spectrogramColumns.removeFirst(spectrogramColumns.count - 375)
                    }
                }
                samplesToHop = 256
            }
        }
        samplesToPublish -= samples.count
        if samplesToPublish <= 0 {
            samplesToPublish += 4_800
            publish()
        }
    }

    func updateRIR(_ samples: [Float], preArrivalSamples: Int) {
        rir = samples.isEmpty ? Array(repeating: 0, count: 480) : samples
        rirPreArrivalSamples = preArrivalSamples
        status = "C1 remote RIR 已更新"
        publish()
    }

    private func publish() {
        let waveform = AcousticVisualizationDSP.downsample(history.suffix(2_400), to: 300)
        let spectrum = AcousticVisualizationDSP.spectrumDBFS(
            history.suffix(2_048), fftSize: 2_048, outputBins: 121
        )
        let columnStep = max(1, Int(ceil(Double(spectrogramColumns.count) / 125.0)))
        let visibleColumns = spectrogramColumns.enumerated().compactMap { index, value in
            index.isMultiple(of: columnStep) ? value : nil
        }
        var displayedColumns = visibleColumns
        if displayedColumns.count == 1, let only = displayedColumns.first { displayedColumns.append(only) }
        let flat = displayedColumns.flatMap { $0 }
        let snapshot = AcousticMonitorSnapshot(
            waveform: waveform,
            spectrumDBFS: spectrum,
            spectrogramDBFS: flat.isEmpty ? Array(repeating: -120, count: 192) : flat,
            spectrogramColumns: max(2, displayedColumns.count), spectrogramRows: 96,
            rir: rir, rirPreArrivalSamples: rirPreArrivalSamples, status: status
        )
        DispatchQueue.main.async { self.store.publish(snapshot) }
    }
}

enum AcousticVisualizationDSP {
    static func downsample(_ samples: [Float], to outputCount: Int) -> [Float] {
        guard !samples.isEmpty, outputCount > 0 else { return [] }
        if samples.count <= outputCount { return samples }
        return (0..<outputCount).map { index in
            let start = index * samples.count / outputCount
            let end = max(start + 1, (index + 1) * samples.count / outputCount)
            var selected = samples[start]
            for value in samples[start..<min(end, samples.count)] where abs(value) > abs(selected) {
                selected = value
            }
            return selected
        }
    }

    static func spectrumDBFS(
        _ samples: [Float], fftSize requestedSize: Int? = nil, outputBins: Int
    ) -> [Float] {
        guard !samples.isEmpty, outputBins > 1 else { return Array(repeating: -120, count: max(0, outputBins)) }
        let size = requestedSize ?? nextPowerOfTwo(max(1_024, samples.count))
        var real = [Double](repeating: 0, count: size)
        var imaginary = [Double](repeating: 0, count: size)
        let used = min(samples.count, size)
        let sourceStart = samples.count - used
        var windowSum = 0.0
        for index in 0..<used {
            let window = used > 1 ? 0.5 - 0.5 * cos(2 * .pi * Double(index) / Double(used - 1)) : 1
            real[index] = Double(samples[sourceStart + index]) * window
            windowSum += window
        }
        fft(real: &real, imaginary: &imaginary, inverse: false)
        let normalization = max(1e-12, windowSum / 2)
        return (0..<outputBins).map { outputIndex in
            let fftIndex = min(size / 2, Int((Double(outputIndex) * Double(size / 2) / Double(outputBins - 1)).rounded()))
            let amplitude = hypot(real[fftIndex], imaginary[fftIndex]) / normalization
            return Float(max(-120, min(3, 20 * log10(max(amplitude, 1e-12)))))
        }
    }

    static func spectrogramDBFS(_ samples: [Float], rows: Int) -> (values: [Float], columns: Int, rows: Int) {
        let fftSize = 1_024, hop = 256
        let padded = samples.count < fftSize ? Array(repeating: Float(0), count: fftSize - samples.count) + samples : samples
        let starts = stride(from: 0, through: max(0, padded.count - fftSize), by: hop).map { $0 }
        let columns = max(1, starts.count)
        var values: [Float] = []
        values.reserveCapacity(columns * rows)
        for start in starts {
            values.append(contentsOf: spectrumDBFS(
                Array(padded[start..<(start + fftSize)]), fftSize: fftSize, outputBins: rows
            ))
        }
        return (values, columns, rows)
    }

    static func estimateSweep(
        _ values: [Float], columns: Int, rows: Int, duration: Double
    ) -> (start: Double, end: Double, r2: Double)? {
        guard columns >= 2, rows >= 2, values.count >= columns * rows else { return nil }
        var times: [Double] = [], frequencies: [Double] = []
        for column in 0..<columns {
            let base = column * rows
            guard let peakRow = (0..<rows).max(by: { values[base + $0] < values[base + $1] }),
                  values[base + peakRow] > -65 else { continue }
            times.append(Double(column) / Double(max(1, columns - 1)) * duration)
            frequencies.append(Double(peakRow) / Double(rows - 1) * ProbeDefaults.sampleRate / 2)
        }
        guard times.count >= 2 else { return nil }
        let meanT = times.reduce(0, +) / Double(times.count)
        let meanF = frequencies.reduce(0, +) / Double(frequencies.count)
        let covariance = zip(times, frequencies).reduce(0.0) { $0 + ($1.0 - meanT) * ($1.1 - meanF) }
        let timeVariance = times.reduce(0.0) { $0 + ($1 - meanT) * ($1 - meanT) }
        guard timeVariance > 0 else { return nil }
        let slope = covariance / timeVariance
        let intercept = meanF - slope * meanT
        let residual = zip(times, frequencies).reduce(0.0) {
            let error = $1.1 - (intercept + slope * $1.0)
            return $0 + error * error
        }
        let total = frequencies.reduce(0.0) { $0 + ($1 - meanF) * ($1 - meanF) }
        let r2 = total > 0 ? max(0, min(1, 1 - residual / total)) : 1
        return (max(0, intercept), max(0, intercept + slope * duration), r2)
    }

    static func matchedFilterRIR(
        audio: [Float], probe: [Float], preArrivalSamples: Int, outputSamples: Int
    ) -> [Float] {
        guard !audio.isEmpty, !probe.isEmpty, outputSamples > 0 else { return [] }
        let convolutionCount = audio.count + probe.count - 1
        let size = nextPowerOfTwo(convolutionCount)
        var ar = [Double](repeating: 0, count: size), ai = [Double](repeating: 0, count: size)
        var br = [Double](repeating: 0, count: size), bi = [Double](repeating: 0, count: size)
        for index in audio.indices { ar[index] = Double(audio[index]) }
        for index in probe.indices { br[index] = Double(probe[probe.count - 1 - index]) }
        fft(real: &ar, imaginary: &ai, inverse: false)
        fft(real: &br, imaginary: &bi, inverse: false)
        for index in 0..<size {
            let real = ar[index] * br[index] - ai[index] * bi[index]
            let imaginary = ar[index] * bi[index] + ai[index] * br[index]
            ar[index] = real; ai[index] = imaginary
        }
        fft(real: &ar, imaginary: &ai, inverse: true)
        let energy = max(1e-12, probe.reduce(0.0) { $0 + Double($1) * Double($1) })
        let first = probe.count - 1
        var output = (0..<outputSamples).map { index -> Float in
            let source = first + index
            return source < ar.count ? Float(ar[source] / energy) : 0
        }
        let scale = max(1e-9, output.map { abs($0) }.max() ?? 1)
        output = output.map { $0 / scale }
        _ = preArrivalSamples
        return output
    }

    private static func nextPowerOfTwo(_ value: Int) -> Int {
        var result = 1
        while result < value { result <<= 1 }
        return result
    }

    private static func fft(real: inout [Double], imaginary: inout [Double], inverse: Bool) {
        let count = real.count
        guard count > 1, count == imaginary.count else { return }
        var j = 0
        for i in 1..<count {
            var bit = count >> 1
            while j & bit != 0 { j ^= bit; bit >>= 1 }
            j ^= bit
            if i < j { real.swapAt(i, j); imaginary.swapAt(i, j) }
        }
        var length = 2
        while length <= count {
            let angle = (inverse ? 2.0 : -2.0) * .pi / Double(length)
            let wLengthReal = cos(angle), wLengthImaginary = sin(angle)
            for start in stride(from: 0, to: count, by: length) {
                var wr = 1.0, wi = 0.0
                for offset in 0..<(length / 2) {
                    let even = start + offset, odd = even + length / 2
                    let vr = real[odd] * wr - imaginary[odd] * wi
                    let vi = real[odd] * wi + imaginary[odd] * wr
                    let ur = real[even], ui = imaginary[even]
                    real[even] = ur + vr; imaginary[even] = ui + vi
                    real[odd] = ur - vr; imaginary[odd] = ui - vi
                    let nextWR = wr * wLengthReal - wi * wLengthImaginary
                    wi = wr * wLengthImaginary + wi * wLengthReal; wr = nextWR
                }
            }
            length <<= 1
        }
        if inverse {
            let scale = Double(count)
            for index in 0..<count { real[index] /= scale; imaginary[index] /= scale }
        }
    }
}

private struct SampleRing {
    private var storage: [Float]
    private var writeIndex = 0
    private(set) var count = 0

    init(capacity: Int) { storage = Array(repeating: 0, count: capacity) }

    mutating func append(contentsOf samples: ArraySlice<Float>) {
        for sample in samples {
            storage[writeIndex] = sample
            writeIndex = (writeIndex + 1) % storage.count
            count = min(storage.count, count + 1)
        }
    }

    func suffix(_ requested: Int) -> [Float] {
        let amount = min(count, requested)
        let start = (writeIndex - amount + storage.count) % storage.count
        return (0..<amount).map { storage[(start + $0) % storage.count] }
    }
}

struct AcousticMonitorCard: View {
    @ObservedObject var monitor: AcousticMonitorStore
    let isRunning: Bool

    var body: some View {
        let snapshot = monitor.snapshot
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("实时声学四图", systemImage: "waveform.path.ecg.rectangle")
                    .font(.subheadline.bold())
                Spacer()
                Text(isRunning ? snapshot.status : "启动会话后刷新")
                    .font(.caption2).foregroundStyle(isRunning ? .green : .secondary)
            }
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 290), spacing: 8)], spacing: 8) {
                MonitorPanel(title: "实时输入波形", subtitle: "iPhone Mic · 最近 50 ms") {
                    WaveformPlot(samples: snapshot.waveform).frame(height: 155)
                }
                MonitorPanel(title: "C1 remote RIR", subtitle: "匹配滤波 · 到达点对齐") {
                    RIRPlot(samples: snapshot.rir, preArrivalSamples: snapshot.rirPreArrivalSamples)
                        .frame(height: 155)
                }
                MonitorPanel(title: "实时 Chirp 时频图", subtitle: "最近 2 s · Hann 1024 · hop 256 · dBFS") {
                    HeatmapPlot(
                        values: snapshot.spectrogramDBFS, columns: snapshot.spectrogramColumns,
                        rows: snapshot.spectrogramRows, xLeft: "−2 s", xRight: "现在",
                        yBottom: "0", yTop: "24 kHz"
                    ).frame(height: 155)
                }
                MonitorPanel(title: "实时接收声波频谱", subtitle: "0–24 kHz · 幅度 dBFS") {
                    SpectrumPlot(values: snapshot.spectrumDBFS).frame(height: 155)
                }
            }
            Text("Chirp 图读法：横轴是时间，纵轴是频率，颜色越亮表示该时频点越强。C1 默认应看到约 11→19 kHz 的上升斜线；C2 默认应看到约 0.05→9 kHz 的上升斜线。弯曲、断裂或额外亮带通常表示失真、遮挡、回声或环境干扰。")
                .font(.caption2).foregroundStyle(.secondary)
        }
        .padding(13)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
        .overlay { RoundedRectangle(cornerRadius: 16).stroke(.white.opacity(0.14)) }
    }
}

struct ProbeDetailVisualization: View {
    let title: String
    let probe: ProbeDefinition
    let tint: Color

    var body: some View {
        let detail = probe.visualization
        VStack(alignment: .leading, spacing: 7) {
            Text(chirpDescription(detail)).font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.secondary).textSelection(.enabled)
            SpectrumPlot(values: detail.spectrumDBFS, tint: tint)
                .frame(height: 88)
                .overlay(alignment: .topLeading) { Text("\(title) WAV 频谱").font(.caption2.bold()).padding(6) }
            HeatmapPlot(
                values: detail.spectrogramDBFS, columns: detail.columns, rows: detail.rows,
                xLeft: "0 s", xRight: String(format: "%.3f s", probe.durationMilliseconds / 1_000),
                yBottom: "0", yTop: "24 kHz"
            )
            .frame(height: 118)
            .overlay(alignment: .topLeading) { Text("\(title) WAV 时频图").font(.caption2.bold()).padding(6) }
        }
    }

    private func chirpDescription(_ detail: ProbeVisualization) -> String {
        let sweep: String
        if let start = detail.sweepStartHz, let end = detail.sweepEndHz, let r2 = detail.sweepR2 {
            sweep = String(format: "估计 %.2f→%.2f kHz · 线性度 R²=%.4f", start / 1_000, end / 1_000, r2)
        } else { sweep = "未能稳定估计线性扫频范围" }
        return String(
            format: "%@: 48.0 kHz · %.1f ms · %d samples · %@\nPeak=%.5f FS · RMS=%.5f FS (%.2f dBFS) · 峰均比=%.2f dB · Σx²=%.2f FS²·sample · Σx²/fs=%.6f FS²·s",
            title, probe.durationMilliseconds, probe.samples.count, sweep,
            detail.peak, detail.rms, 20 * log10(max(detail.rms, 1e-12)), detail.crestDB,
            detail.energySamples, detail.energySeconds
        )
    }
}

private struct MonitorPanel<Content: View>: View {
    let title: String
    let subtitle: String
    private let content: Content

    init(title: String, subtitle: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption.bold())
            Text(subtitle).font(.system(size: 8)).foregroundStyle(.secondary)
            content
        }
        .padding(8)
        .background(.black.opacity(0.16), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct WaveformPlot: View {
    let samples: [Float]
    var body: some View {
        HStack(spacing: 2) {
            VerticalAxisLabel(text: "PCM 幅度 (FS)")
            Canvas { context, size in
                plotGrid(&context, size: size, horizontal: 4, vertical: 4)
                guard samples.count > 1 else { return }
                var path = Path()
                for (index, value) in samples.enumerated() {
                    let point = CGPoint(
                        x: CGFloat(index) / CGFloat(samples.count - 1) * size.width,
                        y: size.height / 2 - CGFloat(max(-1, min(1, value))) * size.height * 0.46
                    )
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
                context.stroke(path, with: .color(.cyan), style: StrokeStyle(lineWidth: 1.1, lineJoin: .round))
                context.draw(Text("+1").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: 2), anchor: .topLeading)
                context.draw(Text("0").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height / 2), anchor: .leading)
                context.draw(Text("−1").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height - 2), anchor: .bottomLeading)
                context.draw(Text("0 ms").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 18, y: size.height - 2), anchor: .bottomLeading)
                context.draw(Text("50 ms").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: size.width - 2, y: size.height - 2), anchor: .bottomTrailing)
            }
            .background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 6))
        }
    }
}

private struct SpectrumPlot: View {
    let values: [Float]
    var tint: Color = .orange
    var body: some View {
        HStack(spacing: 2) {
            VerticalAxisLabel(text: "幅度 (dBFS)")
            Canvas { context, size in
                plotGrid(&context, size: size, horizontal: 4, vertical: 4)
                guard values.count > 1 else { return }
                var path = Path()
                for (index, value) in values.enumerated() {
                    let normalized = CGFloat((max(-120, min(0, value)) + 120) / 120)
                    let point = CGPoint(x: CGFloat(index) / CGFloat(values.count - 1) * size.width, y: size.height * (1 - normalized))
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
                context.stroke(path, with: .color(tint), lineWidth: 1.1)
                context.draw(Text("0").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: 2), anchor: .topLeading)
                context.draw(Text("−60").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height / 2), anchor: .leading)
                context.draw(Text("−120").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height - 2), anchor: .bottomLeading)
                context.draw(Text("0 kHz").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 24, y: size.height - 2), anchor: .bottomLeading)
                context.draw(Text("24 kHz").font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: size.width - 2, y: size.height - 2), anchor: .bottomTrailing)
            }
            .background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 6))
        }
    }
}

private struct RIRPlot: View {
    let samples: [Float]
    let preArrivalSamples: Int
    var body: some View {
        HStack(spacing: 2) {
            VerticalAxisLabel(text: "RIR 相对幅度")
            VStack(spacing: 4) {
                line(samples, range: 0..<samples.count, label: "完整 · −10…490 ms")
                line(samples, range: 0..<min(samples.count, preArrivalSamples + 2_400), label: "到达点附近 · −10…50 ms")
            }
        }
    }

    private func line(_ values: [Float], range: Range<Int>, label: String) -> some View {
        Canvas { context, size in
            plotGrid(&context, size: size, horizontal: 2, vertical: 3)
            guard range.count > 1, range.upperBound <= values.count else { return }
            var path = Path()
            for (offset, index) in range.enumerated() {
                let x = CGFloat(offset) / CGFloat(range.count - 1) * size.width
                let y = size.height / 2 - CGFloat(max(-1, min(1, values[index]))) * size.height * 0.44
                if offset == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
            }
            context.stroke(path, with: .color(.purple), lineWidth: 1)
            let arrivalX = CGFloat(preArrivalSamples - range.lowerBound) / CGFloat(max(1, range.count - 1)) * size.width
            if arrivalX >= 0 && arrivalX <= size.width {
                var marker = Path(); marker.move(to: CGPoint(x: arrivalX, y: 0)); marker.addLine(to: CGPoint(x: arrivalX, y: size.height))
                context.stroke(marker, with: .color(.yellow.opacity(0.8)), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
            }
            context.draw(Text(label).font(.system(size: 7)).foregroundStyle(.secondary), at: CGPoint(x: 4, y: 3), anchor: .topLeading)
            context.draw(Text("+1").font(.system(size: 6)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: 13), anchor: .topLeading)
            context.draw(Text("0").font(.system(size: 6)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height / 2), anchor: .leading)
            context.draw(Text("−1").font(.system(size: 6)).foregroundStyle(.secondary), at: CGPoint(x: 2, y: size.height - 2), anchor: .bottomLeading)
        }
        .background(Color.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 5))
    }
}

private struct HeatmapPlot: View {
    let values: [Float]
    let columns: Int
    let rows: Int
    let xLeft: String
    let xRight: String
    let yBottom: String
    let yTop: String

    var body: some View {
        HStack(spacing: 2) {
            VerticalAxisLabel(text: "频率 (kHz)")
            Canvas { context, size in
                guard columns > 0, rows > 0, values.count >= columns * rows else { return }
                let cellWidth = size.width / CGFloat(columns), cellHeight = size.height / CGFloat(rows)
                for column in 0..<columns {
                    for row in 0..<rows {
                        let value = values[column * rows + row]
                        let rect = CGRect(
                            x: CGFloat(column) * cellWidth,
                            y: size.height - CGFloat(row + 1) * cellHeight,
                            width: cellWidth + 0.5, height: cellHeight + 0.5
                        )
                        context.fill(Path(rect), with: .color(heatColor(value)))
                    }
                }
                context.draw(Text(xLeft).font(.system(size: 7)).foregroundStyle(.white.opacity(0.75)), at: CGPoint(x: 18, y: size.height - 2), anchor: .bottomLeading)
                context.draw(Text(xRight).font(.system(size: 7)).foregroundStyle(.white.opacity(0.75)), at: CGPoint(x: size.width - 20, y: size.height - 2), anchor: .bottomTrailing)
                context.draw(Text(yTop).font(.system(size: 7)).foregroundStyle(.white.opacity(0.85)), at: CGPoint(x: 3, y: 2), anchor: .topLeading)
                context.draw(Text("12").font(.system(size: 7)).foregroundStyle(.white.opacity(0.85)), at: CGPoint(x: 3, y: size.height / 2), anchor: .leading)
                context.draw(Text(yBottom).font(.system(size: 7)).foregroundStyle(.white.opacity(0.85)), at: CGPoint(x: 3, y: size.height - 13), anchor: .bottomLeading)
            }
            .background(Color.black, in: RoundedRectangle(cornerRadius: 6))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(alignment: .trailing) {
                HStack(spacing: 2) {
                    Rectangle()
                        .fill(LinearGradient(
                            colors: [.white, .yellow, .orange, .purple, .black],
                            startPoint: .top, endPoint: .bottom
                        ))
                        .frame(width: 7)
                    VStack(spacing: 0) {
                        Text("0 dBFS")
                        Spacer()
                        Text("−100")
                    }
                    .font(.system(size: 6, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.8))
                }
                .padding(.vertical, 8).padding(.trailing, 3)
            }
        }
    }

    private func heatColor(_ db: Float) -> Color {
        let unit = Double(max(0, min(1, (db + 100) / 100)))
        if unit < 0.20 { return Color(red: unit * 0.35, green: 0, blue: unit * 0.55) }
        if unit < 0.65 {
            let value = (unit - 0.20) / 0.45
            return Color(red: 0.08 + value * 0.78, green: value * 0.16, blue: 0.18 + value * 0.20)
        }
        let value = (unit - 0.65) / 0.35
        return Color(red: 0.86 + value * 0.14, green: 0.16 + value * 0.84, blue: 0.38 + value * 0.35)
    }
}

private struct VerticalAxisLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: 7, weight: .medium))
            .foregroundStyle(.secondary)
            .fixedSize()
            .rotationEffect(.degrees(-90))
            .frame(width: 11)
    }
}

private func plotGrid(_ context: inout GraphicsContext, size: CGSize, horizontal: Int, vertical: Int) {
    var path = Path()
    for index in 0...horizontal {
        let y = CGFloat(index) / CGFloat(max(1, horizontal)) * size.height
        path.move(to: CGPoint(x: 0, y: y)); path.addLine(to: CGPoint(x: size.width, y: y))
    }
    for index in 0...vertical {
        let x = CGFloat(index) / CGFloat(max(1, vertical)) * size.width
        path.move(to: CGPoint(x: x, y: 0)); path.addLine(to: CGPoint(x: x, y: size.height))
    }
    context.stroke(path, with: .color(.white.opacity(0.12)), lineWidth: 0.5)
}
