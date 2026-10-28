import Foundation
import XCTest

@testable import AgentlineCore

final class InstanceLockTests: XCTestCase {
    func testSecondInstanceSeesTheFirstAndCanTakeOverAfterRelease() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "lock-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let first = InstanceLock(home: dir)
        XCTAssertEqual(first.acquire(), .acquired)
        XCTAssertEqual(first.acquire(), .acquired)  // idempotent
        // flock is per open file, so a second open in the same process is a fair stand-in
        // for a second app.
        let second = InstanceLock(home: dir)
        XCTAssertEqual(second.acquire(), .heldBy(pid: getpid()))
        first.release()
        XCTAssertEqual(second.acquire(), .acquired)
    }
}
