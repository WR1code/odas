import Darwin
import Foundation

enum LocalNetworkInfo {
    static func ipv4Addresses() -> [(name: String, address: String)] {
        var pointer: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&pointer) == 0, let first = pointer else { return [] }
        defer { freeifaddrs(pointer) }
        var output: [(String, String)] = []
        var current: UnsafeMutablePointer<ifaddrs>? = first
        while let interface = current?.pointee {
            defer { current = interface.ifa_next }
            guard let address = interface.ifa_addr, Int32(address.pointee.sa_family) == AF_INET else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(address, socklen_t(address.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
                let name = String(cString: interface.ifa_name), value = String(cString: host)
                if value != "127.0.0.1" { output.append((name, value)) }
            }
        }
        return output
    }

    static func preferredIPv4() -> String {
        let addresses = ipv4Addresses()
        return addresses.first(where: { $0.name == "en0" })?.address ?? addresses.first?.address ?? "unavailable"
    }

    static func display() -> String {
        ipv4Addresses().map { "\($0.name)=\($0.address)" }.joined(separator: ", ")
    }
}
