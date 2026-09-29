import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

/// The WebSocket protocol spoken with the desktop helper (see PROTOCOL.md).
enum Wire {
    static let version = 1
    static let serviceType = "_reflect._tcp"
    static let defaultPort = 47800

    // MARK: Binary frames

    struct Frame {
        let width: Int
        let height: Int
        let jpeg: Data
    }

    /// Binary message = uint32 width, uint32 height (big-endian), then one JPEG.
    static func parseFrame(_ data: Data) -> Frame? {
        guard data.count > 8 else { return nil }
        let bytes = [UInt8](data.prefix(8))
        func uint32(_ offset: Int) -> Int {
            Int(bytes[offset]) << 24 | Int(bytes[offset + 1]) << 16 | Int(bytes[offset + 2]) << 8 | Int(bytes[offset + 3])
        }
        let width = uint32(0), height = uint32(4)
        guard width > 0, height > 0 else { return nil }
        return Frame(width: width, height: height, jpeg: data.subdata(in: data.startIndex + 8 ..< data.endIndex))
    }

    // MARK: Authentication

    #if canImport(CryptoKit)
    /// Lowercase hex HMAC-SHA256(token, nonce): proves we hold the token without sending it.
    static func authProof(token: Data, nonce: Data) -> String {
        let code = HMAC<SHA256>.authenticationCode(for: nonce, using: SymmetricKey(data: token))
        return Data(code).map { String(format: "%02x", $0) }.joined()
    }
    #endif

    // MARK: Phone -> helper

    enum Out {
        static func pair(code: String, device: String) -> String {
            encode(["type": "pair", "code": code, "device": device])
        }

        static func auth(deviceID: String, proof: String) -> String {
            encode(["type": "auth", "device_id": deviceID, "proof": proof])
        }

        static func tap(_ point: CGPoint) -> String {
            encode(["type": "input", "kind": "tap", "x": round4(point.x), "y": round4(point.y)])
        }

        static func longPress(_ point: CGPoint) -> String {
            encode(["type": "input", "kind": "long_press", "x": round4(point.x), "y": round4(point.y)])
        }

        static func scroll(at point: CGPoint, by delta: CGVector) -> String {
            encode(["type": "input", "kind": "scroll", "x": round4(point.x), "y": round4(point.y),
                    "dx": round5(delta.dx), "dy": round5(delta.dy)])
        }

        static func type(_ text: String) -> String {
            encode(["type": "input", "kind": "type", "text": text])
        }

        static func key(_ key: String) -> String {
            encode(["type": "input", "kind": "key", "key": key])
        }

        static func settings(phoneMode: Bool?, viewport: CGSize?) -> String {
            var object: [String: Any] = ["type": "settings"]
            if let phoneMode { object["phone_mode"] = phoneMode }
            if let viewport, viewport.width > 0, viewport.height > 0 {
                object["viewport"] = ["w": Double(viewport.width.rounded()), "h": Double(viewport.height.rounded())]
            }
            return encode(object)
        }

        static func ping(_ t: Double) -> String { encode(["type": "ping", "t": t]) }
        static func pong(_ t: Any?) -> String { encode(["type": "pong", "t": t ?? NSNull()]) }

        private static func round4(_ value: CGFloat) -> Double { (Double(value) * 10_000).rounded() / 10_000 }
        private static func round5(_ value: CGFloat) -> Double { (Double(value) * 100_000).rounded() / 100_000 }

        static func encode(_ object: [String: Any]) -> String {
            guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]),
                  let text = String(data: data, encoding: .utf8) else { return "{}" }
            return text
        }
    }

    // MARK: Helper -> phone

    struct Hello: Equatable {
        let id: String
        let name: String
        let os: String
        let nonce: Data
        let pairingOpen: Bool
        let version: Int
    }

    struct Status: Equatable {
        var claudeRunning = true
        var windowFound = false
        var streaming = true
        var phoneMode = true
        var phoneModeActive = false
        var windowWidth = 0
        var windowHeight = 0
        var fps = 15
        var message = ""
    }

    enum In: Equatable {
        case hello(Hello)
        case pair(ok: Bool, deviceID: String?, token: String?, message: String?)
        case auth(ok: Bool, message: String?)
        case status(Status)
        case ping(Double?)
        case pong(Double?)
        case error(code: String, message: String)
        case other(String)

        static func parse(_ text: String) -> In? {
            guard let data = text.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let type = object["type"] as? String else { return nil }
            func string(_ key: String) -> String? { object[key] as? String }
            func bool(_ key: String) -> Bool? { object[key] as? Bool }
            func double(_ key: String) -> Double? { (object[key] as? NSNumber)?.doubleValue }
            switch type {
            case "hello":
                guard let id = string("id"), let nonceText = string("nonce"),
                      let nonce = Data(base64Encoded: nonceText) else { return nil }
                return .hello(Hello(id: id, name: string("name") ?? "Computer", os: string("os") ?? "",
                                    nonce: nonce, pairingOpen: bool("pairing_open") ?? false,
                                    version: Int(double("version") ?? 1)))
            case "pair":
                return .pair(ok: bool("ok") ?? false, deviceID: string("device_id"), token: string("token"),
                             message: string("message"))
            case "auth":
                return .auth(ok: bool("ok") ?? false, message: string("message"))
            case "status":
                var status = Status()
                status.claudeRunning = bool("claude_running") ?? true
                status.windowFound = bool("window_found") ?? false
                status.streaming = bool("streaming") ?? true
                status.phoneMode = bool("phone_mode") ?? true
                status.phoneModeActive = bool("phone_mode_active") ?? false
                if let window = object["window"] as? [String: Any] {
                    status.windowWidth = (window["w"] as? NSNumber)?.intValue ?? 0
                    status.windowHeight = (window["h"] as? NSNumber)?.intValue ?? 0
                }
                status.fps = Int(double("fps") ?? 15)
                status.message = string("message") ?? ""
                return .status(status)
            case "ping":
                return .ping(double("t"))
            case "pong":
                return .pong(double("t"))
            case "error":
                return .error(code: string("code") ?? "error", message: string("message") ?? "")
            default:
                return .other(type)
            }
        }
    }
}

/// Maps between the on-screen mirror and the 0...1 coordinates the helper expects.
enum MirrorGeometry {
    /// The rectangle an image of `imageSize` occupies when aspect-fitted (letterboxed) into `bounds`.
    static func aspectFit(_ imageSize: CGSize, in bounds: CGRect) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0, bounds.width > 0, bounds.height > 0 else { return .zero }
        let scale = min(bounds.width / imageSize.width, bounds.height / imageSize.height)
        let size = CGSize(width: imageSize.width * scale, height: imageSize.height * scale)
        return CGRect(x: bounds.minX + (bounds.width - size.width) / 2,
                      y: bounds.minY + (bounds.height - size.height) / 2,
                      width: size.width, height: size.height)
    }

    /// A point in the image view's own coordinates as 0...1 fractions, or nil if it is outside the image.
    static func normalized(_ point: CGPoint, in size: CGSize) -> CGPoint? {
        guard size.width > 0, size.height > 0 else { return nil }
        let x = point.x / size.width, y = point.y / size.height
        guard (0...1).contains(x), (0...1).contains(y) else { return nil }
        return CGPoint(x: x, y: y)
    }

    /// A finger movement in the image view's coordinates as a fraction of the image.
    static func normalizedDelta(_ delta: CGPoint, in size: CGSize) -> CGVector {
        guard size.width > 0, size.height > 0 else { return .zero }
        return CGVector(dx: delta.x / size.width, dy: delta.y / size.height)
    }
}
