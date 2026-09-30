import Foundation
import Security

/// The pairing token for each helper, kept in the iOS Keychain (this device only).
struct Credentials: Codable, Equatable {
    let deviceID: String
    let token: String  // base64 of 32 random bytes, issued by the helper

    var tokenData: Data? { Data(base64Encoded: token) }
}

enum CredentialStore {
    private static let service = "com.neverheard.reflect.pairing"

    private static func query(_ helperID: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: helperID,
        ]
    }

    static func load(helperID: String) -> Credentials? {
        var request = query(helperID)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Credentials.self, from: data)
    }

    static func has(helperID: String) -> Bool { load(helperID: helperID) != nil }

    @discardableResult
    static func save(_ credentials: Credentials, helperID: String) -> Bool {
        guard let data = try? JSONEncoder().encode(credentials) else { return false }
        SecItemDelete(query(helperID) as CFDictionary)
        var item = query(helperID)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func delete(helperID: String) {
        SecItemDelete(query(helperID) as CFDictionary)
    }
}
