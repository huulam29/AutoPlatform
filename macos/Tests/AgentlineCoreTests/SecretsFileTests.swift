import Foundation
import XCTest

@testable import AgentlineCore

final class SecretsFileTests: XCTestCase {
    func testKeychainMigrationCopiesIntoTheFileAndKeychainWins() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "secrets-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = SecretsFile(home: dir)
        try file.set("old-file-value", for: "OPENROUTER_API_KEY")
        try file.set("keep-me", for: "JIRA_EMAIL")
        let copied = try KeyMigration.merge(
            ["OPENROUTER_API_KEY": "from-keychain", "TELEGRAM_BOT_TOKEN": "tg-1", "bad name": "x", "EMPTY_KEY": ""],
            into: file)
        XCTAssertEqual(copied, ["OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN"])
        XCTAssertEqual(
            file.read(),
            ["OPENROUTER_API_KEY": "from-keychain", "TELEGRAM_BOT_TOKEN": "tg-1", "JIRA_EMAIL": "keep-me"])
        // running it again changes nothing
        XCTAssertEqual(try KeyMigration.merge(["TELEGRAM_BOT_TOKEN": "tg-1"], into: file), [])
    }

    func testRoundTripPermissionsAndEngineFormat() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "secrets-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = SecretsFile(home: dir)
        try file.set("sk-or-abc123", for: "OPENROUTER_API_KEY")
        try file.set("tok-2", for: "JIRA_API_TOKEN")
        XCTAssertEqual(file.read(), ["OPENROUTER_API_KEY": "sk-or-abc123", "JIRA_API_TOKEN": "tok-2"])
        let mode = try FileManager.default.attributesOfItem(atPath: file.url.path)[.posixPermissions] as? Int
        XCTAssertEqual(mode, 0o600)
        // the same NAME=value format the engine's agentline.secrets reads
        let text = try String(contentsOf: file.url, encoding: .utf8)
        XCTAssertTrue(text.contains("OPENROUTER_API_KEY=sk-or-abc123\n"))
        XCTAssertTrue(try file.delete("JIRA_API_TOKEN"))
        XCTAssertFalse(try file.delete("JIRA_API_TOKEN"))
        XCTAssertEqual(file.names(), ["OPENROUTER_API_KEY"])
        XCTAssertThrowsError(try file.set("x", for: "bad name"))
        XCTAssertThrowsError(try file.set("two\nlines", for: "GOOD_NAME"))
    }
}
