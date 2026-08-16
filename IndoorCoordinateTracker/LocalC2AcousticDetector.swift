import Foundation

struct LocalC2Detection: Sendable {
    let t3Sample: Int64
    let score: Double
    let segmentOffsetSamples: Int
    let segmentLengthSamples: Int
}

enum LocalC2AcousticDetector {
    static func detect(
        audio: [Float],
        windowStartSample: Int64,
        fullTemplate: [Float],
        searchStartSample: Int64,
        threshold: Double = 0.25
    ) -> LocalC2Detection? {
        guard !audio.isEmpty, fullTemplate.count >= 512 else { return nil }
        let segmentOffset = max(0, Int(Double(fullTemplate.count) * 0.05))
        let requestedLength = max(512, Int(Double(fullTemplate.count) * 0.20))
        let segmentEnd = min(fullTemplate.count, segmentOffset + requestedLength)
        guard segmentEnd - segmentOffset >= 512 else { return nil }
        let segment = Array(fullTemplate[segmentOffset..<segmentEnd])
        let first = max(0, Int(searchStartSample - windowStartSample) + segmentOffset)
        let last = audio.count - segment.count
        guard last >= first else { return nil }

        var bestStart = first
        var bestScore = -1.0
        var start = first
        while start <= last {
            let score = normalizedScore(audio: audio, template: segment, start: start, decimation: 4)
            if score > bestScore { bestScore = score; bestStart = start }
            start += 4
        }
        for candidate in max(first, bestStart - 12)...min(last, bestStart + 12) {
            let score = normalizedScore(audio: audio, template: segment, start: candidate, decimation: 1)
            if score > bestScore { bestScore = score; bestStart = candidate }
        }
        guard bestScore >= threshold else { return nil }
        return LocalC2Detection(
            t3Sample: windowStartSample + Int64(bestStart - segmentOffset),
            score: bestScore,
            segmentOffsetSamples: segmentOffset,
            segmentLengthSamples: segment.count
        )
    }

    private static func normalizedScore(
        audio: [Float], template: [Float], start: Int, decimation: Int
    ) -> Double {
        guard start >= 0, start + template.count <= audio.count else { return 0 }
        var dot = 0.0, audioEnergy = 0.0, templateEnergy = 0.0
        var index = 0
        while index < template.count {
            let input = Double(audio[start + index])
            let reference = Double(template[index])
            dot += input * reference
            audioEnergy += input * input
            templateEnergy += reference * reference
            index += decimation
        }
        guard audioEnergy > 1e-12, templateEnergy > 1e-12 else { return 0 }
        return abs(dot) / sqrt(audioEnergy * templateEnergy)
    }
}
