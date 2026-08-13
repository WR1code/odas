import Foundation

struct C1Detection: Sendable {
    let detected: Bool
    let t2Sample: Int64?
    let score: Double
    let candidateSample: Int64
    let rejectionReason: String?
    let bandRatio: Double?
    let detectionCompletedAtSample: Int64
}

final class StreamingC1Detector {
    private let template: [Float]
    private let activeStart: Int
    private let segment: [Float]
    private let capacity = 24_000
    private var samples: [Float] = []
    private var baseSample: Int64 = 0
    private var lastRejectedAt = Int64.min
    private var seenGeneration: UInt64 = 0

    init(template: [Float] = ProbeDefaults.c1().samples, detectionMilliseconds: Int = 60) {
        self.template = template
        let peak = template.map { abs($0) }.max() ?? 0
        let threshold = max(0.0005, peak * 0.02)
        activeStart = template.firstIndex(where: { abs($0) >= threshold }) ?? 0
        let requested = max(512, Int(ProbeDefaults.sampleRate) * detectionMilliseconds / 1_000)
        let length = max(1, min(template.count - activeStart, requested))
        segment = Array(template[activeStart..<(activeStart + length)])
        samples.reserveCapacity(capacity)
    }

    func reset(nextSample: Int64, generation: UInt64) {
        samples.removeAll(keepingCapacity: true)
        baseSample = nextSample
        lastRejectedAt = .min
        seenGeneration = generation
    }

    func process(
        _ incoming: [Float],
        absoluteStartSample: Int64,
        armed: Bool,
        generation: UInt64
    ) -> C1Detection? {
        guard armed else {
            reset(nextSample: absoluteStartSample + Int64(incoming.count), generation: generation)
            return nil
        }
        if generation != seenGeneration {
            reset(nextSample: absoluteStartSample, generation: generation)
        }
        append(incoming, absoluteStartSample: absoluteStartSample)
        guard samples.count >= segment.count else { return nil }

        let newestStart = samples.count - segment.count
        let first = max(0, newestStart - max(incoming.count * 2, 128))
        var bestStart = first
        var bestScore = -1.0
        var start = first
        while start <= newestStart {
            let score = normalizedScore(start: start, decimation: 4)
            if score > bestScore {
                bestScore = score
                bestStart = start
            }
            start += 4
        }
        for fineStart in max(first, bestStart - 8)...min(newestStart, bestStart + 8) {
            let score = normalizedScore(start: fineStart, decimation: 1)
            if score > bestScore {
                bestScore = score
                bestStart = fineStart
            }
        }

        let c1Start = max(0, baseSample + Int64(bestStart - activeStart))
        let completedAt = absoluteStartSample + Int64(incoming.count)
        let bandRatio = bestScore >= 0.18 ? highToLowBandRatio(start: bestStart) : nil
        let gateOK = (bandRatio ?? 0) >= 0.80
        if bestScore >= 0.28, gateOK {
            return .init(
                detected: true,
                t2Sample: c1Start,
                score: bestScore,
                candidateSample: c1Start,
                rejectionReason: nil,
                bandRatio: bandRatio,
                detectionCompletedAtSample: completedAt
            )
        }
        if bestScore >= 0.18,
           lastRejectedAt == .min || completedAt - lastRejectedAt >= Int64(ProbeDefaults.sampleRate / 10) {
            lastRejectedAt = completedAt
            return .init(
                detected: false,
                t2Sample: nil,
                score: bestScore,
                candidateSample: c1Start,
                rejectionReason: gateOK ? "score_below_threshold" : "high_frequency_gate_failed",
                bandRatio: bandRatio,
                detectionCompletedAtSample: completedAt
            )
        }
        return nil
    }

    func appendOnly(_ incoming: [Float], absoluteStartSample: Int64) {
        append(incoming, absoluteStartSample: absoluteStartSample)
    }

    func window(centerSample: Int64, before: Int, after: Int) -> [Float]? {
        let requestedStart = centerSample - Int64(before)
        let requestedEnd = centerSample + Int64(after)
        let availableEnd = baseSample + Int64(samples.count)
        let start = max(baseSample, requestedStart)
        let end = min(availableEnd, requestedEnd)
        guard end > start else { return nil }
        return Array(samples[Int(start - baseSample)..<Int(end - baseSample)])
    }

    private func append(_ incoming: [Float], absoluteStartSample: Int64) {
        if samples.isEmpty { baseSample = absoluteStartSample }
        samples.append(contentsOf: incoming)
        if samples.count > capacity {
            let removed = samples.count - capacity
            samples.removeFirst(removed)
            baseSample += Int64(removed)
        }
    }

    private func normalizedScore(start: Int, decimation: Int) -> Double {
        guard start >= 0, start + segment.count <= samples.count else { return 0 }
        var dot = 0.0
        var energyInput = 0.0
        var energyTemplate = 0.0
        var index = 0
        while index < segment.count {
            let input = Double(samples[start + index])
            let reference = Double(segment[index])
            dot += input * reference
            energyInput += input * input
            energyTemplate += reference * reference
            index += decimation
        }
        guard energyInput > 1e-12, energyTemplate > 1e-12 else { return 0 }
        return abs(dot) / sqrt(energyInput * energyTemplate)
    }

    private func highToLowBandRatio(start: Int) -> Double {
        let high = [12_000.0, 14_000.0, 16_000.0, 18_000.0]
        let low = [2_000.0, 4_000.0, 6_000.0, 8_000.0]
        let highPower = high.reduce(0) { $0 + goertzelPower(start: start, frequency: $1) }
        let lowPower = low.reduce(0) { $0 + goertzelPower(start: start, frequency: $1) }
        return highPower / (lowPower + 1e-12)
    }

    private func goertzelPower(start: Int, frequency: Double) -> Double {
        guard start >= 0, start + segment.count <= samples.count else { return 0 }
        let omega = 2 * Double.pi * frequency / ProbeDefaults.sampleRate
        let coefficient = 2 * cos(omega)
        var first = 0.0
        var second = 0.0
        for index in 0..<segment.count {
            let current = Double(samples[start + index]) + coefficient * first - second
            second = first
            first = current
        }
        return first * first + second * second - coefficient * first * second
    }
}
