import Darwin
import Foundation

enum LocalNetworkInfo {
    struct Snapshot: Equatable {
        let mode: String
        let addressLabel: String
        let localIPv4: String
        let interfaceName: String?
        let netmask: String?

        static let unavailable = Snapshot(
            mode: "未检测到局域网", addressLabel: "iPhone 可达 IPv4",
            localIPv4: "unavailable", interfaceName: nil, netmask: nil
        )

        func linuxTargetWarning(host: String) -> String? {
            let target = host.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !target.isEmpty else { return "请填写 Linux 当前使用的 IPv4 地址。" }
            guard LocalNetworkInfo.ipv4Value(target) != nil else {
                return "Linux 目标不是有效的 IPv4 地址。"
            }
            guard localIPv4 != "unavailable", let netmask else { return nil }
            guard LocalNetworkInfo.sameSubnet(localIPv4, target, netmask: netmask) else {
                return "Linux 目标与 iPhone 当前局域网不在同一网段；若未使用 VPN 或静态路由，请改填电脑当前的 Wi-Fi IPv4。"
            }
            return nil
        }
    }

    private struct InterfaceAddress {
        let name: String
        let address: String
        let netmask: String?
    }

    static func ipv4Addresses() -> [(name: String, address: String)] {
        interfaceAddresses().map { ($0.name, $0.address) }
    }

    static func snapshot() -> Snapshot {
        let addresses = interfaceAddresses()
        if let hotspot = addresses.first(where: { $0.name == "bridge100" }) {
            return Snapshot(
                mode: "个人热点", addressLabel: "iPhone 热点 IPv4",
                localIPv4: hotspot.address, interfaceName: hotspot.name, netmask: hotspot.netmask
            )
        }
        if let wifi = addresses.first(where: { $0.name == "en0" }) {
            return Snapshot(
                mode: "Wi-Fi", addressLabel: "iPhone 局域网 IPv4",
                localIPv4: wifi.address, interfaceName: wifi.name, netmask: wifi.netmask
            )
        }
        guard let fallback = addresses.first else { return .unavailable }
        return Snapshot(
            mode: "其它网络", addressLabel: "iPhone 可达 IPv4",
            localIPv4: fallback.address, interfaceName: fallback.name, netmask: fallback.netmask
        )
    }

    private static func interfaceAddresses() -> [InterfaceAddress] {
        var pointer: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&pointer) == 0, let first = pointer else { return [] }
        defer { freeifaddrs(pointer) }
        var output: [InterfaceAddress] = []
        var current: UnsafeMutablePointer<ifaddrs>? = first
        while let interface = current?.pointee {
            defer { current = interface.ifa_next }
            guard let address = interface.ifa_addr, Int32(address.pointee.sa_family) == AF_INET else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                let name = String(cString: interface.ifa_name), value = String(cString: host)
                var maskValue: String?
                if let netmask = interface.ifa_netmask {
                    var mask = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                    if getnameinfo(netmask, socklen_t(netmask.pointee.sa_len), &mask, socklen_t(mask.count), nil, 0, NI_NUMERICHOST) == 0 {
                        maskValue = String(cString: mask)
                    }
                }
                if value != "127.0.0.1" {
                    output.append(InterfaceAddress(name: name, address: value, netmask: maskValue))
                }
            }
        }
        return output
    }

    /// Address used by devices connected to this iPhone's Personal Hotspot.
    /// `pdp_ip0` is the carrier-facing cellular address and must never be
    /// presented as the hotspot/LAN address.
    static func hotspotIPv4() -> String {
        snapshot().mode == "个人热点" ? snapshot().localIPv4 : "unavailable"
    }

    static func display() -> String {
        ipv4Addresses().map { "\($0.name)=\($0.address)" }.joined(separator: ", ")
    }

    private static func ipv4Value(_ address: String) -> UInt32? {
        var parsed = in_addr()
        guard address.withCString({ inet_pton(AF_INET, $0, &parsed) }) == 1 else { return nil }
        return parsed.s_addr
    }

    private static func sameSubnet(_ lhs: String, _ rhs: String, netmask: String) -> Bool {
        guard let left = ipv4Value(lhs), let right = ipv4Value(rhs), let mask = ipv4Value(netmask) else {
            return true
        }
        return left & mask == right & mask
    }
}
