import Foundation
import Observation
import SwiftUI

@MainActor
@Observable
final class AppModel {
    let browser = HelperBrowser()
    let store = HelperStore()
    var connection: MirrorConnection?

    /// Auto-connect happens once per launch, and never right after the user pressed Disconnect.
    private var autoConnectUsed = false
    private var started = false

    func start() {
        guard !started else { return }
        started = true
        browser.onChange = { [weak self] in self?.autoConnect() }
        browser.start()
        // If Bonjour is blocked on this network, fall back to the remembered address.
        Task { [weak self] in
            try? await Task.sleep(for: .seconds(3))
            self?.autoConnect(allowRemembered: true)
        }
    }

    private func autoConnect(allowRemembered: Bool = false) {
        guard !autoConnectUsed, connection == nil,
              let lastID = store.lastHelperID, CredentialStore.has(helperID: lastID) else { return }
        if let helper = browser.helpers.first(where: { $0.id == lastID }) {
            autoConnectUsed = true
            connect(to: helper)
        } else if allowRemembered, let saved = store.helper(id: lastID) {
            autoConnectUsed = true
            connect(saved: saved)
        }
    }

    func connect(to helper: DiscoveredHelper) {
        var hosts = helper.addresses
        if let saved = store.helper(id: helper.id), !hosts.contains(saved.host) { hosts.append(saved.host) }
        open(ConnectTarget(name: helper.name, os: helper.os, helperID: helper.id, endpoint: helper.endpoint,
                           hosts: hosts, port: helper.port ?? Wire.defaultPort))
    }

    func connect(saved: SavedHelper) {
        if let live = browser.helpers.first(where: { $0.id == saved.id }) {
            connect(to: live)
            return
        }
        open(ConnectTarget(name: saved.name, os: saved.os, helperID: saved.id, endpoint: nil,
                           hosts: [saved.host], port: saved.port))
    }

    func connect(host: String, port: Int) {
        open(ConnectTarget(name: host, helperID: nil, endpoint: nil, hosts: [host], port: port))
    }

    private func open(_ target: ConnectTarget) {
        autoConnectUsed = true
        connection?.stop()
        let connection = MirrorConnection(target: target, store: store)
        self.connection = connection
        connection.start()
    }

    func disconnect() {
        connection?.stop()
        connection = nil
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        switch phase {
        case .active:
            connection?.resume()
            browser.start()
        case .background:
            connection?.suspend()
        default:
            break
        }
    }
}
