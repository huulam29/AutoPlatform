# Phase 13: crash and failure reporting in the Mac app (as built)

The Mac app records its own crashes, unclean exits and engine deaths as **incidents**. These are the same
files the engine writes (`src/agentline/incidents.py`, branch `phase-13`), and the app shows them, with
failed runs, in a Problems screen. Reports stay local; the user exports them by hand. For use, see
[macos/README.md](../macos/README.md#crash-and-failure-reports).

## Design

```
<app support>/                       (= $AGENTLINE_HOME of the engine child)
├─ incidents/
│  ├─ .lock                          flock(LOCK_EX) by both writers
│  ├─ <id>.json                      one per fingerprint; count / last_seen merge
│  └─ <id>/Agentline-….ips           attachments (redacted copies)
├─ running.marker                    {pid, started_at, version, exception_incident?}
└─ crash-scan.json                   {last_scan, processed: [file names]}
```

All logic that can be tested lives in `AgentlineCore`:

| File | What |
|---|---|
| `Incidents.swift` | `Incident` (lenient decode, explicit-null `exception_type`), `IncidentDraft`, `Fingerprint`, `IncidentStore` (record/merge, attach, list, get, markSeen, delete, clear; flock + temp-file/`rename`), `IncidentNotifyLimiter` |
| `Redactor.swift` | exact Keychain values + API token, then token patterns |
| `CrashReports.swift` | `.ips` / `.crash` parser, `CrashScanner` (which files are new), `RunMarker`, `LaunchCheck.evaluate` (pure verdict), drafts for app exceptions, unclean exits and engine exits |
| `ReportExport.swift` | `SystemInfo`, the export's entry list (pure), `summary.md`, write + `ditto -c -k --keepParent` |
| `APIClient` | `/api/incidents` list/get/seen/delete/clear; a 500's `incident` id stays readable via `APIError.incidentId` |
| `BackendManager` | `onUnexpectedExit` (only for exits the app didn't ask for), `redactor`, 500-line log buffer |

The app target (`CrashCapture.swift`, `AppModel+Problems.swift`, `ProblemsScreen.swift`) holds:
- the AppKit glue;
- `NSSetUncaughtExceptionHandler`;
- the launch check;
- the Problems UI, export panel, banner and notifications.

### Capture paths

1. **Uncaught NSException.** The handler runs on the dying thread and does three things:
   - writes the `app_exception` record synchronously (name, reason, `callStackSymbols`);
   - stores the incident id in `running.marker`;
   - leaves the process to abort as usual.

   At the next launch, the `.ips` that macOS wrote for that pid is **attached to the exception incident**, not recorded as a second crash.
2. **Other crashes** (`fatalError`, signals). At launch, the app scans `~/Library/Logs/DiagnosticReports` and `/Library/Logs/DiagnosticReports` for `Agentline[-_.]*.ips|.crash` files.
   - **Which files count:** those newer than the last scan (minus 120 s of slack) and not already in the processed list. The first scan looks back 7 days.
   - **Title:** comes from the exception type and the first frame in agentline's own binary.
   - **Message:** the application-specific info or the termination indicator.
   - **Fingerprint frames:** the app's own frames, because every `fatalError` starts in libswiftCore.
3. **Unclean exit.** `running.marker` is written at launch and removed in `applicationShouldTerminate`. At the next launch, `LaunchCheck.evaluate` decides what the marker means:
   - **explained:** a crash report has the marker's pid, a pid-less report is newer than the marker's start, or the exception handler set `exception_incident`;
   - **another live instance:** the pid is running and is an `Agentline` binary;
   - **unclean exit:** nothing explains it. The check runs again after 3 s and after 5 more seconds, because `.ips` files arrive late. If still unexplained, it records `app_unclean_exit`.

   Every unclean exit has the same fingerprint, so they count up in one file.
4. **Engine exits.** An exit the app didn't cause gets an `engine_exit` record 0.5 s later, which lets the last output arrive. The record has:
   - exit code or signal name;
   - the last 200 log lines;
   - the last Python traceback in the log, if there is one; its `file:function` frames go into the fingerprint, as the engine does it.

   A crash loop increments `count`. `exception_type` is `SIGKILL` / `exit 1`, so different causes get different files.

### Problems screen

- **Sections:** Crashes and errors (newest first: source, kind, count, last seen) and Failed runs (`GET /api/runs?status=dead` and `needs-review`, with Open Run Detail).
- **Detail:** title, tags, seen and first/last times, version, platform, id, message, traceback (monospaced, selectable), context, the engine log tail, and attachments with Show in Finder.
- **Actions:** Mark Seen, Copy Summary, Export This Report…, Delete. Above the list: Scan for Crash Reports, Copy Summary, Export Report…, Clear All (with confirmation).
- **Data source:** the engine API when it is up (the list is reloaded about every 10 s, and on actions). When the engine is down, or the API fails, the app reads and writes the directory directly. The two are the same files.
- **Launch banner:** "agentline quit unexpectedly last time", with View Report and one notification.
- **Engine-exit notifications:** at most one per incident per 10 minutes.
- **API 500s:** an action that hits a 500 shows "View Report" for the recorded incident.

## Decisions (proposals for the decision log)

1. **Fingerprint = the engine's rule exactly.**
   - **Rule:** first line, trimmed, 300 characters; `0x…` -> `<hex>`, long hex -> `<id>`, `\d+` -> `N`; frames joined by `,`; up to 3 frames.
   - **App-side frames:** the app also strips the frame index, the address and `+ offset` before hashing.
   - **Test:** pinned against two hashes computed with `agentline.incidents.fingerprint`.
2. **Writes take `flock` on `<incidents>/.lock`**, as the engine does.
   - **Temp files:** dot-prefixed in the same directory, then `rename(2)`. Readers only take `*.json` files that don't start with a dot.
   - **Limits:** title 300 characters, message 20 000, the last 20 000 characters of a traceback, as in the engine.
3. **Redaction happens inside the store**, before the fingerprint is computed, so an id never depends on a secret. The store redacts:
   - every Keychain key value and the API token;
   - the token patterns;
   - copied text attachments (`.ips`, `.crash`, `.log`, …).

   For the token-after-`key=` pattern, the value must be at least 12 characters and contain a digit, so env var names such as `api_key_env = "OPENROUTER_API_KEY"` stay readable. The app reads the Keychain separately only when the engine failed to start before loading keys.
4. **Expected exits are tracked per `Process` object**, not with the old `stopping` flag. This fixes a race where the termination handler of a stopped engine could run after `restart()` had cleared `process` and been taken as a crash.
5. **AppKit-caught exceptions are not captured.** `NSApplicationCrashOnExceptions` is left off, so an exception that AppKit catches during event handling is still only logged, as before. Turning it on would capture these exceptions but would also crash the app on each one. That is the user's call.
6. **The export's file name has the time too** (`agentline-report-YYYY-MM-DD-HHmm.zip`), so two exports in one day don't collide.

## Verified (built app, `AGENTLINE_APP_SUPPORT_OVERRIDE` on scratch dirs)

- **Tests:** `swift test`: 46 XCTests, all passing (28 before plus 18 new). They cover:
  - merge, count, seen, delete and clear;
  - attachments and redaction;
  - lenient decoding;
  - fingerprints, including the hashes the engine produces;
  - `.ips` parsing (fixture) and the legacy `.crash` format;
  - scanner selection;
  - the marker verdict;
  - engine-exit drafts;
  - export entries, and a real write + `ditto` zip;
  - the notify limiter;
  - the 500 incident id.
- **App `kill -9`:** launched, then `kill -9` on the app (the engine stopped by itself through the watchdog). On relaunch:
  - `app_unclean_exit` was recorded;
  - the banner and the Problems badge showed (`main.png`, `problems.png`);
  - a second `kill -9` and relaunch raised `count` to 2 in the same file.
- **Engine `kill -9`** while the app ran:
  - `engine_exit` was recorded with `signal: SIGKILL` and the log tail;
  - a second engine kill made it `count: 2`;
  - the log tail shows the app reading `GET /api/incidents` and `GET /api/incidents/{id}` from the engine;
  - `agentline incidents list` (the engine CLI) lists the app's records.
- **`AGENTLINE_TEST_CRASH=exception`:**
  - the handler wrote `app_exception` and the marker's `exception_incident`;
  - macOS wrote `Agentline-…ips`;
  - the relaunch attached it to the same incident (no extra crash or unclean exit) and showed the banner.
- **`AGENTLINE_TEST_CRASH=trap`:** on relaunch, an `app_crash` was recorded, titled "Crash: EXC_BREAKPOINT (SIGTRAP) in closure #1 in static CrashCapture.scheduleTestCrashIfRequested(after:)", with the `.ips` attached and no unclean-exit record.
- **Not done by hand:** clicking Export / Copy Summary / Delete in the UI (no input automation), and notification delivery.

## Limits

- **The first scan of a fresh data folder imports the last 7 days** of Agentline crash reports, because DiagnosticReports is shared by every copy of the app, including dev builds.
- **A report that arrives later than the ~8 s launch window** is still recorded, by the 30 s rescan or by "Scan for Crash Reports". The `app_unclean_exit` for that run has already been counted by then.
- **In-process exception call stacks are mangled Swift symbols** (`$s9Agentline…`). The `.ips` attached at the next launch has the demangled ones.
- **The engine log tail is from this app session only.** The engine's output isn't stored in a file.
- **Notifications need the app bundle.** `swift run` gets none.

> Phase 30 note: these verification crashes were once run on the real data folder and showed up
> later as two real-looking app reports. The hook now fires only when
> `AGENTLINE_APP_SUPPORT_OVERRIDE` points at a throwaway folder (`TestCrash.mode`).
