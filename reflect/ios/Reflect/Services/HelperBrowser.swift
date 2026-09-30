import Foundation
import Network
import Observation

/// A Reflect helper announced on the local network.
struct DiscoveredHelper: Identifiable, Hashable {
    let id: String
    let name: String
    let os: String
    let endpoint: NWEndpoint
    /// Addresses and port from the TXT record, used if Bonjour resolution fails.
    let addresses: [String]
    let port: Int?
}

/// Browses for `_reflect._tcp` services with NWBrowser.
@MainActor
@Observable
final class HelperBrowser {
    private(set) var helpers: [DiscoveredHelper] = []
    private(set) var problem: String?
    var onChange: (@MainActor () -> Void)?

    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let parameters = NWParameters()
        parameters.includePeerToPeer = false
        let browser = NWBrowser(for: .bonjourWithTXTRecord(type: Wire.serviceType, domain: "local."), using: parameters)
        browser.stateUpdateHandler = { [weak self] state in
            MainActor.assumeIsolated { self?.handle(state) }
        }
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            MainActor.assumeIsolated { self?.update(results) }
        }
        self.browser = browser
        browser.start(queue: .main)
    }

    func restart() {
        browser?.cancel()
        browser = nil
        start()
    }

    private func handle(_ state: NWBrowser.State) {
        switch state {
        case .ready:
            problem = nil
        case .waiting(let error), .failed(let error):
            problem = Self.describe(error)
            if case .failed = state {
                browser?.cancel()
                browser = nil
                Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(2))
                    self?.start()
                }
            }
        default:
            break
        }
    }

    private static func describe(_ error: NWError) -> String {
        if case .dns(let code) = error, code == -65570 {  // kDNSServiceErr_PolicyDenied
            return "Reflect needs Local Network access: open Settings \u{2192} Privacy & Security \u{2192} Local Network and turn on Reflect."
        }
        return "Can't search the network (\(error.localizedDescription)). Check that Wi-Fi is on."
    }

    private func update(_ results: Set<NWBrowser.Result>) {
        var found: [DiscoveredHelper] = []
        for result in results {
            guard case .service(let serviceName, _, _, _) = result.endpoint else { continue }
            var txt: [String: String] = [:]
            if case .bonjour(let record) = result.metadata {
                txt = record.dictionary
            }
            let addresses = (txt["ips"] ?? "").split(separator: ",").map(String.init).filter { !$0.isEmpty }
            found.append(DiscoveredHelper(
                id: txt["id"] ?? serviceName,
                name: txt["name"] ?? serviceName,
                os: txt["os"] ?? "",
                endpoint: result.endpoint,
                addresses: addresses,
                port: txt["port"].flatMap(Int.init)
            ))
        }
        helpers = found.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        onChange?()
    }
}
