import Foundation
import Observation

/// A computer we connected to before (address remembered for when Bonjour is slow or blocked).
struct SavedHelper: Codable, Equatable, Identifiable {
    var id: String
    var name: String
    var os: String
    var host: String
    var port: Int
    var lastUsed: Date
}

@MainActor
@Observable
final class HelperStore {
    private enum Key {
        static let saved = "savedHelpers"
        static let last = "lastHelperID"
        static let phoneMode = "phoneMode"
    }

    private let defaults: UserDefaults
    private(set) var saved: [SavedHelper] = []

    private(set) var lastHelperID: String?
    private(set) var phoneMode: Bool

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        lastHelperID = defaults.string(forKey: Key.last)
        phoneMode = defaults.object(forKey: Key.phoneMode) as? Bool ?? true
        if let data = defaults.data(forKey: Key.saved),
           let decoded = try? JSONDecoder().decode([SavedHelper].self, from: data) {
            saved = decoded.sorted { $0.lastUsed > $1.lastUsed }
        }
    }

    func helper(id: String) -> SavedHelper? { saved.first { $0.id == id } }

    func setPhoneMode(_ on: Bool) {
        phoneMode = on
        defaults.set(on, forKey: Key.phoneMode)
    }

    func remember(_ helper: SavedHelper) {
        saved.removeAll { $0.id == helper.id }
        saved.insert(helper, at: 0)
        lastHelperID = helper.id
        defaults.set(helper.id, forKey: Key.last)
        persist()
    }

    func forget(id: String) {
        saved.removeAll { $0.id == id }
        CredentialStore.delete(helperID: id)
        if lastHelperID == id {
            lastHelperID = nil
            defaults.removeObject(forKey: Key.last)
        }
        persist()
    }

    private func persist() {
        if let data = try? JSONEncoder().encode(saved) {
            defaults.set(data, forKey: Key.saved)
        }
    }
}
