import Foundation
import Network

/// Turns a Bonjour service into an address URLSession can use.
enum EndpointResolver {
    struct HostPort: Equatable {
        let host: String  // already formatted for a URL (IPv6 in brackets)
        let port: Int
    }

    /// Opens a throwaway TCP connection to the service and reads the address it connected to.
    static func resolve(_ endpoint: NWEndpoint, timeout: TimeInterval = 4) async -> HostPort? {
        await withCheckedContinuation { (continuation: CheckedContinuation<HostPort?, Never>) in
            let parameters = NWParameters.tcp
            if let ip = parameters.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options {
                ip.version = .v4  // the helper advertises IPv4 addresses
            }
            let connection = NWConnection(to: endpoint, using: parameters)
            let queue = DispatchQueue(label: "com.neverheard.reflect.resolve")
            let once = Once()
            let finish: (HostPort?) -> Void = { value in
                guard once.claim() else { return }
                connection.cancel()
                continuation.resume(returning: value)
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if case .hostPort(let host, let port)? = connection.currentPath?.remoteEndpoint {
                        finish(HostPort(host: urlHost(host), port: Int(port.rawValue)))
                    } else {
                        finish(nil)
                    }
                case .failed, .cancelled:
                    finish(nil)
                default:
                    break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { finish(nil) }
        }
    }

    static func urlHost(_ host: NWEndpoint.Host) -> String {
        switch host {
        case .ipv4(let address):
            return address.rawValue.map { String($0) }.joined(separator: ".")
        case .ipv6(let address):
            var text = ipv6String(address.rawValue)
            if let interface = address.interface, text.lowercased().hasPrefix("fe80") {
                text += "%25" + interface.name
            }
            return "[\(text)]"
        case .name(let name, _):
            return name
        @unknown default:
            return "\(host)"
        }
    }

    private static func ipv6String(_ raw: Data) -> String {
        var address = in6_addr()
        withUnsafeMutableBytes(of: &address) { $0.copyBytes(from: raw.prefix(16)) }
        var buffer = [CChar](repeating: 0, count: Int(INET6_ADDRSTRLEN))
        return inet_ntop(AF_INET6, &address, &buffer, socklen_t(buffer.count)).map { String(cString: $0) } ?? "::"
    }
}

/// Lets exactly one of several racing callbacks win.
final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if done { return false }
        done = true
        return true
    }
}
