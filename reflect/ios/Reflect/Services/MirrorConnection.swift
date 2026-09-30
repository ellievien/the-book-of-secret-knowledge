import Foundation
import Network
import Observation
import UIKit

/// Where to connect: a Bonjour service (re-resolved on every attempt) and/or known addresses.
struct ConnectTarget: Equatable {
    var name: String
    var os: String = ""
    var helperID: String?
    var endpoint: NWEndpoint?
    var hosts: [String]
    var port: Int
}

/// One live session with a desktop helper: pairing/auth, frames, input, stats and auto-reconnect.
@MainActor
@Observable
final class MirrorConnection: Identifiable {
    enum Phase: Equatable {
        case connecting
        case needsPairing
        case streaming
        case reconnecting(attempt: Int)
        case stopped
    }

    nonisolated var id: ObjectIdentifier { ObjectIdentifier(self) }

    private(set) var target: ConnectTarget
    private(set) var phase: Phase = .connecting
    private(set) var image: UIImage?
    private(set) var status: Wire.Status?
    private(set) var fps = 0
    private(set) var latencyMs: Int?
    private(set) var helperName: String
    private(set) var pairingOpen = true
    private(set) var pairingError: String?
    private(set) var pairingBusy = false
    private(set) var lastProblem: String?
    private(set) var phoneMode: Bool

    private let store: HelperStore
    private let session: URLSession
    private var socket: URLSessionWebSocketTask?
    private var generation = 0
    private var attempt = 0
    private var helperID: String?
    private var nonce: Data?
    private var connectedHost: EndpointResolver.HostPort?
    private var framesThisSecond = 0
    private var lastMessageAt = Date()
    private var tickTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var sentViewport: CGSize = .zero
    private var viewport: CGSize = .zero
    private var suspended = false

    init(target: ConnectTarget, store: HelperStore) {
        self.target = target
        self.store = store
        self.helperName = target.name
        self.helperID = target.helperID
        self.phoneMode = store.phoneMode
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 10
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
    }

    var isStreaming: Bool { phase == .streaming }

    // MARK: Lifecycle

    func start() {
        suspended = false
        openSocket()
        startTicking()
    }

    func stop() {
        phase = .stopped
        teardownSocket()
        tickTask?.cancel()
        tickTask = nil
        reconnectTask?.cancel()
        reconnectTask = nil
        session.invalidateAndCancel()
    }

    /// App went to the background: drop the socket (iOS would suspend it anyway).
    func suspend() {
        guard phase != .stopped else { return }
        suspended = true
        teardownSocket()
        reconnectTask?.cancel()
    }

    /// App came back: reconnect straight away.
    func resume() {
        guard suspended, phase != .stopped else { return }
        suspended = false
        attempt = 0
        openSocket()
    }

    private func teardownSocket() {
        generation += 1
        socket?.cancel(with: .goingAway, reason: nil)
        socket = nil
    }

    private func openSocket() {
        teardownSocket()
        let generation = self.generation
        if phase != .needsPairing {
            phase = attempt == 0 ? .connecting : .reconnecting(attempt: attempt)
        }
        Task { [weak self] in
            guard let self else { return }
            let address = await self.chooseAddress()
            guard self.generation == generation, !self.suspended, self.phase != .stopped else { return }
            guard let address else {
                self.connectionLost("Can't find \(self.helperName) on the network")
                return
            }
            self.connect(to: address, generation: generation)
        }
    }

    /// Bonjour resolution first (the address may have changed), then TXT/remembered addresses in rotation.
    private func chooseAddress() async -> EndpointResolver.HostPort? {
        var candidates: [EndpointResolver.HostPort] = []
        if let endpoint = target.endpoint, let resolved = await EndpointResolver.resolve(endpoint) {
            candidates.append(resolved)
        }
        for host in target.hosts where !candidates.contains(where: { $0.host == host }) {
            candidates.append(.init(host: host, port: target.port))
        }
        guard !candidates.isEmpty else { return nil }
        return candidates[attempt % candidates.count]
    }

    private func connect(to address: EndpointResolver.HostPort, generation: Int) {
        guard let url = URL(string: "ws://\(address.host):\(address.port)/") else {
            connectionLost("Bad address \(address.host)")
            return
        }
        connectedHost = address
        var request = URLRequest(url: url)
        request.timeoutInterval = 6
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 32 << 20
        self.socket = socket
        lastMessageAt = Date()
        socket.resume()
        receive(on: socket, generation: generation)
    }

    private func receive(on socket: URLSessionWebSocketTask, generation: Int) {
        Task { [weak self] in
            while true {
                let message: URLSessionWebSocketTask.Message
                do {
                    message = try await socket.receive()
                } catch {
                    guard let self, self.generation == generation else { return }
                    self.connectionLost(error.localizedDescription)
                    return
                }
                guard let self, self.generation == generation else { return }
                self.lastMessageAt = Date()
                switch message {
                case .string(let text):
                    self.handle(text)
                case .data(let data):
                    await self.handleFrame(data, generation: generation)
                @unknown default:
                    break
                }
            }
        }
    }

    private func connectionLost(_ reason: String) {
        guard phase != .stopped, !suspended else { return }
        teardownSocket()
        lastProblem = reason
        attempt += 1
        if phase != .needsPairing {
            phase = .reconnecting(attempt: attempt)
        }
        let delay = min(8.0, 0.5 * pow(2.0, Double(attempt - 1))) * Double.random(in: 0.8...1.2)
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled, let self, self.phase != .stopped, !self.suspended else { return }
            self.openSocket()
        }
    }

    // MARK: Incoming

    private func handle(_ text: String) {
        guard let message = Wire.In.parse(text) else { return }
        switch message {
        case .hello(let hello):
            helperID = hello.id
            helperName = hello.name
            nonce = hello.nonce
            pairingOpen = hello.pairingOpen
            if let credentials = CredentialStore.load(helperID: hello.id), let token = credentials.tokenData {
                send(Wire.Out.auth(deviceID: credentials.deviceID, proof: Wire.authProof(token: token, nonce: hello.nonce)))
            } else {
                phase = .needsPairing
            }
        case .pair(let ok, let deviceID, let token, let message):
            pairingBusy = false
            if ok, let deviceID, let token, let helperID {
                CredentialStore.save(Credentials(deviceID: deviceID, token: token), helperID: helperID)
                pairingError = nil
                didAuthenticate()
            } else {
                pairingError = message ?? "Pairing failed. Check the code and try again."
            }
        case .auth(let ok, let message):
            if ok {
                didAuthenticate()
            } else {
                if let helperID { CredentialStore.delete(helperID: helperID) }
                pairingError = message
                phase = .needsPairing
            }
        case .status(let status):
            self.status = status
            phoneMode = status.phoneMode
        case .ping(let t):
            send(Wire.Out.pong(t))
        case .pong(let t):
            if let t {
                latencyMs = max(0, Int((Date().timeIntervalSince1970 * 1000 - t).rounded()))
            }
        case .error(_, let message):
            lastProblem = message
        case .other:
            break
        }
    }

    private func didAuthenticate() {
        phase = .streaming
        attempt = 0
        lastProblem = nil
        if let helperID, let host = connectedHost {
            store.remember(SavedHelper(id: helperID, name: helperName, os: target.os,
                                       host: host.host, port: host.port, lastUsed: Date()))
            if !target.hosts.contains(host.host) { target.hosts.append(host.host) }
        }
        sentViewport = .zero
        sendSettings(includePhoneMode: true)
    }

    private func handleFrame(_ data: Data, generation: Int) async {
        guard let frame = Wire.parseFrame(data) else { return }
        let decoded = await Task.detached(priority: .userInitiated) { () -> UIImage? in
            UIImage(data: frame.jpeg)?.preparingForDisplay()
        }.value
        guard self.generation == generation, let decoded else { return }
        image = decoded
        framesThisSecond += 1
    }

    // MARK: Timers

    private func startTicking() {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            var second = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self else { return }
                second += 1
                self.fps = self.framesThisSecond
                self.framesThisSecond = 0
                guard self.socket != nil else { continue }
                if second % 2 == 0 {
                    self.send(Wire.Out.ping(Date().timeIntervalSince1970 * 1000))
                }
                if Date().timeIntervalSince(self.lastMessageAt) > 7 {
                    self.connectionLost("The computer stopped answering")
                }
            }
        }
    }

    // MARK: Outgoing

    private func send(_ text: String) {
        socket?.send(.string(text)) { _ in }
    }

    func pair(code: String) {
        let digits = code.filter(\.isNumber)
        guard digits.count == 6 else {
            pairingError = "The code has 6 digits."
            return
        }
        pairingBusy = true
        pairingError = nil
        send(Wire.Out.pair(code: digits, device: UIDevice.current.name))
    }

    func tap(_ point: CGPoint) { if isStreaming { send(Wire.Out.tap(point)) } }
    func longPress(_ point: CGPoint) { if isStreaming { send(Wire.Out.longPress(point)) } }
    func scroll(at point: CGPoint, by delta: CGVector) { if isStreaming { send(Wire.Out.scroll(at: point, by: delta)) } }
    func type(_ text: String) { if isStreaming, !text.isEmpty { send(Wire.Out.type(text)) } }
    func key(_ key: String) { if isStreaming { send(Wire.Out.key(key)) } }

    func setPhoneMode(_ on: Bool) {
        phoneMode = on
        store.setPhoneMode(on)
        if isStreaming { send(Wire.Out.settings(phoneMode: on, viewport: nil)) }
    }

    /// The mirror area's size in points; phone mode gives the desktop window this shape.
    func updateViewport(_ size: CGSize) {
        guard size.width > 50, size.height > 50 else { return }
        viewport = size
        sendSettings(includePhoneMode: false)
    }

    private func sendSettings(includePhoneMode: Bool) {
        guard isStreaming else { return }
        let changed = abs(viewport.width - sentViewport.width) > 1 || abs(viewport.height - sentViewport.height) > 1
        guard includePhoneMode || changed else { return }
        send(Wire.Out.settings(phoneMode: includePhoneMode ? phoneMode : nil, viewport: changed ? viewport : nil))
        if changed { sentViewport = viewport }
    }
}
