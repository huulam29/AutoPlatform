import Foundation
import Security

/// API keys as generic passwords in the login Keychain: service `dev.agentline.keys`, account =
/// the env var name. Values are read only to hand them to the engine's environment.
public struct KeychainStore: Sendable {
    public static let defaultService = "dev.agentline.keys"
    public let service: String

    public init(service: String = KeychainStore.defaultService) { self.service = service }

    public enum Failure: Error, LocalizedError, Equatable {
        case status(OSStatus)
        case badName(String)

        public var errorDescription: String? {
            switch self {
            case .status(let s):
                let msg = SecCopyErrorMessageString(s, nil) as String? ?? "error"
                return "Keychain: \(msg) (\(s))"
            case .badName(let n):
                return "\(n.isEmpty ? "An empty name" : "'\(n)'") isn't an env var name (letters, digits, _; not starting with a digit)"
            }
        }
    }

    /// Env var names: `[A-Za-z_][A-Za-z0-9_]*`.
    public static func isValidName(_ name: String) -> Bool {
        guard let first = name.unicodeScalars.first else { return false }
        let head = CharacterSet.letters.union(CharacterSet(charactersIn: "_"))
        let body = head.union(CharacterSet(charactersIn: "0123456789"))
        return name.allSatisfy(\.isASCII) && head.contains(first)
            && name.unicodeScalars.allSatisfy { body.contains($0) }
    }

    private func base(_ account: String? = nil) -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
        ]
        if let account { q[kSecAttrAccount as String] = account }
        return q
    }

    public func set(_ value: String, for name: String) throws {
        guard Self.isValidName(name) else { throw Failure.badName(name) }
        let data = Data(value.utf8)
        let update = SecItemUpdate(base(name) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else { throw Failure.status(update) }
        var add = base(name)
        add[kSecValueData as String] = data
        add[kSecAttrLabel as String] = "agentline: \(name)"
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        let status = SecItemAdd(add as CFDictionary, nil)
        guard status == errSecSuccess else { throw Failure.status(status) }
    }

    public func get(_ name: String) throws -> String? {
        var q = base(name)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = out as? Data else { throw Failure.status(status) }
        return String(decoding: data, as: UTF8.self)
    }

    /// Returns false when there was nothing to delete.
    @discardableResult
    public func delete(_ name: String) throws -> Bool {
        let status = SecItemDelete(base(name) as CFDictionary)
        if status == errSecItemNotFound { return false }
        guard status == errSecSuccess else { throw Failure.status(status) }
        return true
    }

    /// Stored names, sorted (no values).
    public func names() throws -> [String] {
        var q = base()
        q[kSecReturnAttributes as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitAll
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        if status == errSecItemNotFound { return [] }
        guard status == errSecSuccess, let items = out as? [[String: Any]] else { throw Failure.status(status) }
        return items.compactMap { $0[kSecAttrAccount as String] as? String }.sorted()
    }

    /// Every stored key with its value, for the engine's environment.
    public func all() throws -> [String: String] {
        var out: [String: String] = [:]
        for n in try names() {
            if let v = try get(n) { out[n] = v }
        }
        return out
    }

    /// Delete everything under this service (tests clean up with this).
    public func removeAll() throws {
        let status = SecItemDelete(base() as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw Failure.status(status) }
    }
}
