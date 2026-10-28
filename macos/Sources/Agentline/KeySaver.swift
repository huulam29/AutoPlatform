import AgentlineCore
import SwiftUI

/// Saves a key in the local secrets file (`<home>/secrets.env`, owner-only), the one key store
/// (decision 60). The running engine notices the file changed and uses the key (agentline.live),
/// so no restart is needed.
@MainActor
enum KeySaver {
    static func save(_ value: String, for name: String, model: AppModel) throws -> String? {
        let file = SecretsFile(home: model.backend.paths.home)
        do {
            try file.set(value, for: name)
        } catch {
            throw SaveError(message: "Couldn't save \(name) in \(file.url.path): \(error.localizedDescription)")
        }
        return nil
    }

    struct SaveError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
}
