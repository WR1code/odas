import SwiftUI

struct TrackingSample: Equatable {
    var x: Float = 0
    var y: Float = 0
    var z: Float = 0
    var status = "正在初始化…"
    var isTracking = false

    var horizontalDistance: Float {
        sqrt(x * x + z * z)
    }
}

struct ContentView: View {
    @State private var sample = TrackingSample()
    @State private var resetID = UUID()

    var body: some View {
        ZStack {
            ARCameraView(resetID: resetID) { newSample in
                sample = newSample
            }
            .ignoresSafeArea()

            LinearGradient(
                colors: [.black.opacity(0.66), .clear, .black.opacity(0.50)],
                startPoint: .top,
                endPoint: .bottom
            )
            .ignoresSafeArea()
            .allowsHitTesting(false)

            VStack(spacing: 16) {
                statusCard
                Spacer()
                coordinateCard
                resetButton
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
        }
        .preferredColorScheme(.dark)
    }

    private var statusCard: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(sample.isTracking ? Color.green : Color.orange)
                .frame(width: 10, height: 10)
            Text(sample.status)
                .font(.subheadline.weight(.semibold))
            Spacer()
            Text("单位：米")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(14)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private var coordinateCard: some View {
        VStack(spacing: 14) {
            HStack(spacing: 8) {
                coordinateCell(title: "X 横向", value: sample.x, color: .red)
                coordinateCell(title: "Y 高度", value: sample.y, color: .green)
                coordinateCell(title: "Z 前进", value: sample.z, color: .blue)
            }

            Divider()

            HStack {
                Text("水平离原点")
                    .foregroundStyle(.secondary)
                Spacer()
                Text(String(format: "%.3f m", sample.horizontalDistance))
                    .font(.system(.title3, design: .monospaced, weight: .semibold))
            }

            Text("请保持后置摄像头朝向有纹理、光线充足的环境")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(16)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func coordinateCell(title: String, value: Float, color: Color) -> some View {
        VStack(spacing: 6) {
            HStack(spacing: 5) {
                Circle()
                    .fill(color)
                    .frame(width: 7, height: 7)
                Text(title)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Text(String(format: "%+.3f", value))
                .font(.system(.title3, design: .monospaced, weight: .bold))
                .minimumScaleFactor(0.7)
                .lineLimit(1)
        }
        .frame(maxWidth: .infinity)
    }

    private var resetButton: some View {
        Button {
            resetID = UUID()
        } label: {
            Label("将当前位置设为原点", systemImage: "scope")
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 15)
        }
        .buttonStyle(.borderedProminent)
        .tint(.blue)
        .disabled(!sample.isTracking)
    }
}

#Preview {
    ContentView()
}
