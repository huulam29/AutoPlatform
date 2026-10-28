import Foundation

/// The engine's local secrets file, `<home>/secrets.env` (`NAME=value` lines, owner-only 0600 in a
/// 0700 folder), the same file `agentline keys set` and Settings > Keys write. It is the Mac
/// app's only key store (decision 60): the engine loads it at startup, so the app never reads
/// the Keychain and macOS never prompts after a rebuild.
public struct SecretsFile: Sendable {
    public let url: URL

    public init(home: URL) { url = home.appending(path: "secrets.env") }

    public func read() -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            let name = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            if KeychainStore.isValidName(name) { out[name] = String(line[line.index(after: eq)...]) }
        }
        return out
    }

    public func names() -> [String] { read().keys.sorted() }

    public func set(_ value: String, for name: String) throws {
        guard KeychainStore.isValidName(name) else { throw KeychainStore.Failure.badName(name) }
        guard !value.contains("\n"), !value.contains("\r") else {
            throw CocoaError(.fileWriteInvalidFileName, userInfo: [NSLocalizedDescriptionKey: "A key can't contain line breaks"])
        }
        var values = read()
        values[name] = value
        try write(values)
    }

    @discardableResult
    public func delete(_ name: String) throws -> Bool {
        var values = read()
        guard values.removeValue(forKey: name) != nil else { return false }
        try write(values)
        return true
    }

    private func write(_ values: [String: String]) throws {
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var text = "# agentline secrets: written by the GUI/CLI; keep this file private\n"
        for k in values.keys.sorted() { text += "\(k)=\(values[k]!)\n" }
        let tmp = dir.appending(path: ".secrets.\(UUID().uuidString).tmp")
        guard FileManager.default.createFile(atPath: tmp.path, contents: Data(text.utf8), attributes: [.posixPermissions: 0o600]) else {
            throw CocoaError(.fileWriteUnknown)
        }
        if FileManager.default.fileExists(atPath: url.path) {
            _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
        } else {
            try FileManager.default.moveItem(at: tmp, to: url)
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}

/// The one-time move from the Keychain (the store before decision 60) into the secrets file.
public enum KeyMigration {
    /// Copy `keychain` into `file`. The Keychain values were the ones the engine used (they won
    /// over the file at launch), so they overwrite a file value of the same name. Returns the
    /// names copied.
    @discardableResult
    public static func merge(_ keychain: [String: String], into file: SecretsFile) throws -> [String] {
        let current = file.read()
        var copied: [String] = []
        for (name, value) in keychain.sorted(by: { $0.key < $1.key })
        where KeychainStore.isValidName(name) && !value.isEmpty && current[name] != value {
            try file.set(value, for: name)
            copied.append(name)
        }
        return copied
    }
}
