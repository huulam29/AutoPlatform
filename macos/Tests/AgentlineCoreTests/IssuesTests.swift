import XCTest

@testable import AgentlineCore

final class IssuesTests: XCTestCase {
    let json = """
        {"number": 3, "title": "KeyError: x", "status": "fixed",
         "source": {"kind": "run", "ref": "r1"}, "detail": "boom",
         "created_at": 1.5, "updated_at": 9, "last_seen": 2, "occurrences": 2,
         "notes": [{"at": 3, "text": "looked"}],
         "history": [{"at": 1.5, "from": null, "to": "open", "note": "created"},
                     {"at": 8, "from": "open", "to": "fixed", "note": null}]}
        """

    func testDecodesEngineRecord() throws {
        let i = try JSONDecoder().decode(Issue.self, from: Data(json.utf8))
        XCTAssertEqual(i.number, 3)
        XCTAssertTrue(i.isClosed)
        XCTAssertEqual(i.problemTag, "r:r1")
        XCTAssertEqual(i.history.last?.to, "fixed")
        XCTAssertEqual(i.notes.first?.text, "looked")
    }

    func testFilterCountsProblemsWithoutIssueAsOpen() throws {
        let fixed = try JSONDecoder().decode(Issue.self, from: Data(json.utf8))
        XCTAssertTrue(IssueFilter.active.shows(nil))
        XCTAssertFalse(IssueFilter.closed.shows(nil))
        XCTAssertFalse(IssueFilter.active.shows(fixed))
        XCTAssertTrue(IssueFilter.closed.shows(fixed))
        XCTAssertTrue(IssueFilter.all.shows(fixed))
    }

    func testFilesReaderSkipsJunk() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let dir = home.appendingPathComponent("issues")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data(json.utf8).write(to: dir.appendingPathComponent("3.json"))
        try Data("not json".utf8).write(to: dir.appendingPathComponent("4.json"))
        XCTAssertEqual(IssueFiles(home: home).list().map(\.number), [3])
    }
}

final class ChatStatusTests: XCTestCase {
    func testDecodesEveryPlatform() throws {
        let json = """
            {"platforms": ["telegram", "slack"],
             "telegram": {"title": "Telegram", "keys": [{"name": "TELEGRAM_BOT_TOKEN", "set": true}],
                          "token_env": "TELEGRAM_BOT_TOKEN", "token_set": true, "bot": "my_bot",
                          "linked": {"chat_id": 42, "user_id": 42, "username": "me", "linked_at": 1},
                          "pairing": null, "setup": "BotFather"},
             "slack": {"title": "Slack", "keys": [{"name": "SLACK_APP_TOKEN", "set": false},
                                                  {"name": "SLACK_BOT_TOKEN", "set": false}],
                       "token_env": "SLACK_APP_TOKEN", "token_set": false, "bot": null,
                       "linked": null, "pairing": null, "setup": "Socket Mode"},
             "notify": {"gates": true, "failures": true, "done": false, "questions": true},
             "code": "123456", "link": null}
            """
        let s = try JSONDecoder().decode(ChatStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.platforms, ["telegram", "slack"])
        XCTAssertEqual(s.byName["telegram"]?.linked?.chatId, "42")
        XCTAssertEqual(s.byName["slack"]?.keys.map(\.name), ["SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"])
        XCTAssertEqual(s.code, "123456")
        XCTAssertNil(s.link)
        let slackLinked = json.replacingOccurrences(of: "\"linked\": null, \"pairing\": null, \"setup\": \"Socket Mode\"",
                                                    with: "\"linked\": {\"chat_id\": \"D1\"}, \"pairing\": null, \"setup\": \"Socket Mode\"")
        XCTAssertEqual(try JSONDecoder().decode(ChatStatus.self, from: Data(slackLinked.utf8)).byName["slack"]?.linked?.chatId, "D1")
    }
}
