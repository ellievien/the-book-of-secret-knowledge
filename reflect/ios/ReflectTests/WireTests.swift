import XCTest
@testable import Reflect

final class WireTests: XCTestCase {
    /// Same vector as helper/tests/test_units.py, so both sides compute identical proofs.
    func testAuthProofMatchesHelper() {
        #if canImport(CryptoKit)
        let token = Data(0..<32)
        let nonce = Data(32..<64)
        XCTAssertEqual(token.base64EncodedString(), "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
        XCTAssertEqual(Wire.authProof(token: token, nonce: nonce),
                       "62215de7bddcea7e2c4047ff6bb94f8d18262fc8b3f3648134bb7d44158ff84d")
        #endif
    }

    func testFrameHeader() throws {
        var data = Data([0, 0, 1, 0xB8, 0, 0, 3, 0xBC])
        data.append(contentsOf: [0xFF, 0xD8, 0xFF])
        let frame = try XCTUnwrap(Wire.parseFrame(data))
        XCTAssertEqual(frame.width, 440)
        XCTAssertEqual(frame.height, 956)
        XCTAssertEqual(frame.jpeg, Data([0xFF, 0xD8, 0xFF]))
        XCTAssertNil(Wire.parseFrame(Data([0, 0, 1])))
    }

    func testFrameHeaderOnSlicedData() throws {
        let backing = Data([9, 9, 0, 0, 0, 2, 0, 0, 0, 3, 0xAA])
        let slice = backing.subdata(in: 2..<backing.count)[...]
        let frame = try XCTUnwrap(Wire.parseFrame(Data(slice)))
        XCTAssertEqual(frame.width, 2)
        XCTAssertEqual(frame.height, 3)
        XCTAssertEqual(frame.jpeg, Data([0xAA]))
    }

    func testParseHelloAndStatus() throws {
        let hello = #"{"type":"hello","version":1,"id":"abc","name":"PC","os":"windows","nonce":"AAECAw==","pairing_open":true}"#
        guard case .hello(let h)? = Wire.In.parse(hello) else { return XCTFail("not hello") }
        XCTAssertEqual(h.id, "abc")
        XCTAssertEqual(h.nonce, Data([0, 1, 2, 3]))
        XCTAssertTrue(h.pairingOpen)

        let status = #"{"type":"status","claude_running":false,"window_found":false,"streaming":true,"phone_mode":true,"phone_mode_active":false,"window":null,"fps":15,"message":"Claude desktop not running"}"#
        guard case .status(let s)? = Wire.In.parse(status) else { return XCTFail("not status") }
        XCTAssertFalse(s.claudeRunning)
        XCTAssertEqual(s.message, "Claude desktop not running")

        let paired = #"{"type":"pair","ok":true,"device_id":"d1","token":"AAEC"}"#
        XCTAssertEqual(Wire.In.parse(paired), .pair(ok: true, deviceID: "d1", token: "AAEC", message: nil))
        XCTAssertNil(Wire.In.parse("not json"))
    }

    func testOutgoingMessages() throws {
        func object(_ text: String) throws -> NSDictionary {
            try XCTUnwrap(JSONSerialization.jsonObject(with: Data(text.utf8)) as? NSDictionary)
        }
        XCTAssertEqual(try object(Wire.Out.tap(CGPoint(x: 0.25, y: 0.123456))),
                       ["type": "input", "kind": "tap", "x": 0.25, "y": 0.1235] as NSDictionary)
        XCTAssertEqual(try object(Wire.Out.key("esc")), ["type": "input", "kind": "key", "key": "esc"] as NSDictionary)
        XCTAssertEqual(try object(Wire.Out.scroll(at: CGPoint(x: 0.5, y: 0.5), by: CGVector(dx: 0, dy: -0.012345))),
                       ["type": "input", "kind": "scroll", "x": 0.5, "y": 0.5, "dx": 0, "dy": -0.01235] as NSDictionary)
        XCTAssertEqual(try object(Wire.Out.settings(phoneMode: true, viewport: CGSize(width: 402, height: 780.4))),
                       ["type": "settings", "phone_mode": true, "viewport": ["w": 402, "h": 780]] as NSDictionary)
        XCTAssertEqual(try object(Wire.Out.settings(phoneMode: nil, viewport: nil)), ["type": "settings"] as NSDictionary)
    }

    func testGeometry() {
        let fit = MirrorGeometry.aspectFit(CGSize(width: 440, height: 956), in: CGRect(x: 0, y: 0, width: 402, height: 700))
        XCTAssertEqual(fit.height, 700, accuracy: 0.001)
        XCTAssertEqual(fit.width, 700 * 440 / 956, accuracy: 0.001)
        XCTAssertEqual(fit.midX, 201, accuracy: 0.001)
        XCTAssertEqual(MirrorGeometry.normalized(CGPoint(x: 100, y: 50), in: CGSize(width: 400, height: 200)),
                       CGPoint(x: 0.25, y: 0.25))
        XCTAssertNil(MirrorGeometry.normalized(CGPoint(x: -1, y: 50), in: CGSize(width: 400, height: 200)))
        XCTAssertEqual(MirrorGeometry.normalizedDelta(CGPoint(x: 40, y: -20), in: CGSize(width: 400, height: 200)),
                       CGVector(dx: 0.1, dy: -0.1))
    }
}
