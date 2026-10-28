import Darwin
import Foundation

/// One app per data folder: an exclusive `flock` on `<home>/app.lock`, held for the life of the
/// process (the kernel drops it when the process dies, so a crash never leaves it stuck). A
/// second copy of the app (another build, `open -n`) finds it held and hands over to the first
/// instead of starting a second engine on the same ledger.
public final class InstanceLock {
    public enum Outcome: Equatable {
        case acquired
        /// Someone else holds it; `pid` is what they wrote into the file, if it could be read.
        case heldBy(pid: pid_t?)
    }

    public let url: URL
    private var fd: Int32 = -1

    public init(home: URL) {
        url = home.appending(path: "app.lock")
    }

    deinit { release() }

    public func acquire() -> Outcome {
        if fd >= 0 { return .acquired }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let f = open(url.path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        guard f >= 0 else { return .acquired }  // can't lock (odd file system): don't block the app
        guard flock(f, LOCK_EX | LOCK_NB) == 0 else {
            let pid = Self.readPid(f)
            close(f)
            return .heldBy(pid: pid)
        }
        fd = f
        let text = "\(getpid())\n"
        ftruncate(f, 0)
        _ = text.withCString { pwrite(f, $0, strlen($0), 0) }
        return .acquired
    }

    public func release() {
        guard fd >= 0 else { return }
        flock(fd, LOCK_UN)
        close(fd)
        fd = -1
    }

    private static func readPid(_ f: Int32) -> pid_t? {
        var buf = [UInt8](repeating: 0, count: 32)
        let n = pread(f, &buf, buf.count, 0)
        guard n > 0 else { return nil }
        let s = String(decoding: buf[0..<n], as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return pid_t(s)
    }
}
