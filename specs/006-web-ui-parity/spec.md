# Feature Specification: Web UI Parity With the Mac App

**Feature Branch**: `claude/web-ui-macos-parity-d2045f` (to be merged as `phase-54`)

**Created**: 2026-11-02

**Status**: Draft

**Input**: User description: "Current UI only support native MacOS. Create a webUI, that is very identical to the MacOS UI. Let me know if there is any MacOS native only feature that might be blocker, and propose solution." Followed by: build all 12 gaps found, following the Spec Kit flow.

## Context

Agentline Studio, the web UI in `web/`, is already the Mac app's only window (phase 20), and
`agentline serve` already serves the same pages to any browser. The screens are therefore the same
by construction. What a plain browser lacks is what the Mac app adds around the page: twelve
features that today only the Mac app can answer through its native bridge, or that live in the
app's menus, banners, notifications and Dock. In a browser those controls are hidden or missing.
This feature closes those twelve gaps so that a person on Windows, Linux, or a Mac without the app
gets the same product.

## Clarifications

### Session 2026-11-02

- Q: Who may browse the engine machine's folders and change the pipelines folder from the browser? → A: A browser on the engine's machine, or one signed in with the API token (the same rule as the rest of the API).
- Q: Which run outcomes should raise a browser notification? → A: The same as the Mac app: every waiting gate once (including gates already waiting when Studio opens), and runs that become done, dead or needs-review after the first refresh; new problem reports.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Know when the engine is down, and restart it (Priority: P1)

A person using Studio in a browser sees a clear banner when the engine stops answering, the same as
the Mac app's "The engine isn't running" banner, with the command to start it again. While the
engine is up, they can restart it from Settings → General or the command palette, and the page
reconnects by itself.

**Why this priority**: Without it, a stopped engine looks like a broken page, and every other
feature depends on the engine answering.

**Independent Test**: Stop the engine with Studio open, confirm the banner appears within a few
seconds; start it again, confirm the banner clears and the page works without a reload. Press
Restart Engine, confirm the page shows "Restarting…" then reconnects.

**Acceptance Scenarios**:

1. **Given** Studio open in a browser, **When** the engine stops answering, **Then** within 10 seconds a banner says the engine isn't reachable, shows how to start it, and keeps retrying.
2. **Given** the banner is showing, **When** the engine answers again, **Then** the banner clears and the current page refreshes its data without a full reload.
3. **Given** a running engine, **When** the person chooses Restart Engine, **Then** the engine answers first, then restarts, and the page shows "Restarting the engine…" until it is back.
4. **Given** a browser that is not on the engine's own machine and has no valid API token, **When** it asks for a restart, **Then** the restart is refused.
5. **Given** the Mac app, **When** the person restarts the engine, **Then** the Mac app's own restart is used, unchanged.

---

### User Story 2 - Sign in with an API token (Priority: P1)

When the engine requires a token (it is reached from another machine), a person opening Studio sees
a sign-in screen in the Studio style that asks for the token, explains where to find it, and
remembers it in this browser. A wrong token says so. They can sign out from Settings → General.

**Why this priority**: Today a bare browser prompt asks for the token, which looks like a phishing
dialog and gives no help. A remote browser cannot use Studio at all without getting past it.

**Independent Test**: Start an engine that requires a token, open Studio, confirm the sign-in
screen appears, enter a wrong token (error shown), then the right one (Studio opens).

**Acceptance Scenarios**:

1. **Given** an engine that requires a token and no stored token, **When** Studio opens, **Then** a sign-in screen asks for the token and names where it is kept on the engine's machine.
2. **Given** a wrong token, **When** it is submitted, **Then** the screen says the token was refused and stores nothing.
3. **Given** a stored token the engine later refuses, **When** any request is refused, **Then** the stored token is forgotten and the sign-in screen returns.
4. **Given** a signed-in browser, **When** the person signs out, **Then** the token is forgotten and the sign-in screen shows.
5. **Given** an engine on the local machine with no token required, **When** Studio opens, **Then** no sign-in screen appears.

---

### User Story 3 - See app info and manage folders from the browser (Priority: P1)

In Settings → General a person sees the engine's version, data folder, settings file and pipelines
folder, as the Mac app shows them. They can change the pipelines folder by typing a path or
browsing the engine machine's folders, and the new folder takes effect without a restart. Each path
has a Copy button. On the engine's own machine, each path also has "Show in folder", which opens it
in the system file manager (Finder, Explorer, or the Linux file manager).

**Why this priority**: These are the settings a person needs to point Studio at their own work.

**Independent Test**: Open Settings → General in a browser, read the four values, copy a path,
change the pipelines folder through the folder browser, confirm the Pipelines page lists the new
folder's pipelines.

**Acceptance Scenarios**:

1. **Given** Studio in a browser, **When** Settings → General opens, **Then** it shows version, data folder, settings file, pipelines folder and engine address.
2. **Given** the pipelines folder field, **When** the person enters a folder that exists, **Then** it is saved to the settings file and the Pipelines page shows that folder's pipelines without a restart.
3. **Given** a path that does not exist or is not a folder, **When** it is entered, **Then** it is refused with a reason and nothing changes.
4. **Given** the folder browser, **When** it opens, **Then** it lists only folders (not files) under the chosen place, with a way up and to the home folder.
5. **Given** a browser on the engine's own machine, **When** the person presses "Show in folder", **Then** the system file manager opens at that path.
6. **Given** a browser on another machine, **When** a path is shown, **Then** only Copy is offered, never "Show in folder".

---

### User Story 4 - Approvals reach me while Studio is open (Priority: P2)

A person with Studio open in a background tab gets a system notification when a gate waits for
approval, a run finishes or fails, or a new problem report appears, like the Mac app's
notifications. Where the browser supports it, the approval notification has Approve and Reject
buttons; elsewhere, clicking it opens the approval. The tab title and icon show the number of
waiting approvals, and an installed Studio shows it on its app icon.

**Why this priority**: Approvals block runs. The Mac app's notifications and Dock badge are how its
users notice them quickly.

**Independent Test**: Allow notifications, start a run that reaches a gate, switch tabs, confirm a
notification appears, the title reads "(1) Agentline Studio", and Approve from the notification
(or clicking it and approving) moves the run on.

**Acceptance Scenarios**:

1. **Given** notifications are allowed and Studio is open, **When** a gate starts waiting, **Then** one notification names the pipeline, run and gate.
2. **Given** a browser that supports notification buttons, **When** the person presses Approve, **Then** the gate is approved, with the audit row naming the person as the web UI does today.
3. **Given** Reject is pressed, **When** the notification is handled, **Then** Studio opens the approval with a comment field, as the Mac app's Reject… does, and nothing is rejected until the person confirms.
4. **Given** a browser without notification buttons, **When** the notification is clicked, **Then** Studio comes to the front on that approval.
5. **Given** N approvals waiting, **When** the count changes, **Then** the tab title, tab icon and installed-app badge show N within one refresh, and nothing when N is 0.
6. **Given** a run that becomes done, dead or needs-review after Studio's first refresh, or a new problem report, **When** it is seen for the first time, **Then** one notification is shown, and clicking it opens it. Runs that had already finished when Studio opened are not announced.
7. **Given** several Studio tabs open, **When** an event happens, **Then** the person gets one notification, not one per tab.
8. **Given** notifications not yet allowed, **When** the person opens Settings → General, **Then** a control explains the feature and asks the browser for permission only when pressed.
9. **Given** the Mac app, **When** an event happens, **Then** only the Mac app's own notification is shown, not a second web one.

---

### User Story 5 - Problem reports: export and "quit unexpectedly" (Priority: P2)

A person in a browser can export all problem reports as one .zip download from the App reports
page, as the Mac app's Export does. When the engine stopped without shutting down cleanly last time
(killed, crashed, machine lost power), the next start records an App report, and Studio shows the
same "agentline quit unexpectedly last time" banner with View report and Dismiss.

**Why this priority**: These reports are how problems get fixed, and today the browser can neither
export them nor learn of a crash.

**Independent Test**: Kill the engine hard, start it again, open Studio: banner shown, App reports
has an unclean-exit report; press Export, a .zip downloads holding every report.

**Acceptance Scenarios**:

1. **Given** the engine was stopped by a clean shutdown, **When** it starts, **Then** no report is created.
2. **Given** the engine stopped without a clean shutdown, **When** it starts, **Then** one App report of kind unclean exit is created, with the previous start time, version and platform, redacted.
3. **Given** an unclean-exit report not yet seen, **When** Studio opens, **Then** the banner shows; View report opens it and marks it seen; Dismiss hides it and marks it seen.
4. **Given** App reports, **When** the person presses Export, **Then** a .zip downloads with every report, its attachments, system info and the end of the engine log, all redacted.
5. **Given** the Mac app, **When** the engine runs inside it, **Then** the Mac app's own crash checks keep working and the same unclean exit is not reported twice.
6. **Given** a browser, **When** App reports opens, **Then** "Scan for crash reports" (macOS crash logs) is not offered.

---

### User Story 6 - Keyboard and command palette (Priority: P3)

A person presses Cmd+K (Ctrl+K on Windows and Linux) to open a command palette listing every
action in the Mac app's menus (Start Run…, Pipelines, Runs, Approvals, Problems, Settings, Refresh,
Restart Engine, Show Logs) and the other top-bar pages, filters it by typing, and runs one with
Enter. Two-key shortcuts that do not clash with the browser (`g` then `p` for Pipelines, and so on)
work outside text fields.

**Why this priority**: The Mac app's menu shortcuts mostly clash with browser shortcuts (Cmd+N new
window, Cmd+R reload, Cmd+1–4 tabs), so they cannot be copied; this gives the same speed.

**Independent Test**: Press Cmd/Ctrl+K, type "appr", press Enter, confirm the Approvals page opens.
Press `g` `r` outside a text field, confirm the Runs page opens.

**Acceptance Scenarios**:

1. **Given** any page, **When** Cmd/Ctrl+K is pressed, **Then** the palette opens with the search field focused; Escape closes it.
2. **Given** the palette, **When** the person types, **Then** the list narrows to matching actions; arrow keys move; Enter runs the selected one.
3. **Given** focus in a text field or the YAML editor, **When** a two-key shortcut is typed, **Then** it is typed as text, not run.
4. **Given** the palette, **When** it is open, **Then** each action shows its shortcut, if it has one.
5. **Given** the Mac app, **When** its menu shortcuts are used, **Then** they keep working as today.

---

### User Story 7 - One engine per data folder (Priority: P2)

A person who starts a second engine on a data folder that already has one running gets a clear
message naming the running one, and the second one exits, instead of two engines sharing one
ledger and data folder.

**Why this priority**: Two engines on one folder would both run the background loops (triage,
reviews, schedules, chat links) and could double the chat replies and model costs.

**Independent Test**: Start `agentline serve` twice with the same data folder, confirm the second
exits with the message and the first is unaffected.

**Acceptance Scenarios**:

1. **Given** a running engine on a data folder, **When** a second starts on the same folder, **Then** it exits with an error naming the first one's address and process.
2. **Given** an engine that was killed, **When** a new one starts, **Then** the stale lock does not block it.
3. **Given** two engines on different data folders, **When** both start, **Then** both run.
4. **Given** the Mac app, **When** it starts its engine, **Then** the lock does not stop it, including after the app restarts its engine.

---

### User Story 8 - Logs page wording (Priority: P3)

In a browser the Logs page says it shows the engine log, without offering the Mac app's
"app and engine output" or its Finder button; Copy path is offered instead.

**Why this priority**: Small, but the page should not suggest controls that are not there.

**Independent Test**: Open Logs in a browser and in the app; compare the wording.

**Acceptance Scenarios**:

1. **Given** a browser, **When** Logs opens, **Then** it says "engine log" and offers Copy path of the data folder (and Show in folder on the engine's own machine).

---

### Edge Cases

- The engine restarts on a new port: a browser cannot follow it, so the engine keeps its port on a restart from the web.
- Restart pressed twice quickly: the second press is refused while a restart is under way.
- The engine is behind a proxy, so the request looks local but the person is not: "on the engine's own machine" is judged by the browser's own address of the engine and the request's network source together; anything forwarded counts as not local.
- Notifications blocked by the browser or the operating system: the control in Settings says so and how to change it; nothing else breaks.
- Several tabs with notifications: one tab speaks for all, chosen without a server round trip; if it closes, another takes over.
- An approval decided elsewhere before the notification button is pressed: the decision is refused as today and the notification says it was already decided.
- A folder browser path the engine's user cannot read: shown as not readable, not an error page.
- The data folder lock on a network drive where locking is not supported: the engine starts with a warning rather than refusing.
- An unclean exit while the engine was never fully started: still reported once.
- The sign-in screen and a token typed with spaces or a newline: surrounding white space is trimmed.
- The palette while a dialog is open: the dialog's keys win; the palette does not open over a dialog.

## Requirements *(mandatory)*

### Functional Requirements

**Engine lifecycle**

- **FR-001**: The web UI MUST show an "engine not reachable" banner within 10 seconds of the engine failing to answer, with the command to start it, and MUST clear it and refresh data when the engine answers again.
- **FR-002**: The engine MUST offer a restart that answers the caller before restarting and keeps the same address.
- **FR-003**: Restart MUST be allowed only to a caller on the engine's own machine or holding a valid API token, MUST be refused while a restart is under way, and MUST write an audit-style log line naming who asked.
- **FR-004**: In the Mac app, restart MUST keep using the app's own restart.

**Sign-in**

- **FR-005**: When the engine requires a token and the browser has none, or its token is refused, the web UI MUST show a sign-in screen in the Studio style instead of a browser prompt.
- **FR-006**: The sign-in screen MUST check the token with the engine before storing it, say when it is refused, and store it only in this browser.
- **FR-007**: Settings → General MUST offer Sign out when a token is stored.

**App info and folders**

- **FR-008**: The engine MUST report its version, data folder, settings file, pipelines folder, address and whether it runs inside the Mac app.
- **FR-009**: Settings → General in a browser MUST show those values with a Copy button per path.
- **FR-010**: A person MUST be able to set the pipelines folder by path, and by a folder browser that lists only folders on the engine's machine. Folders that do not exist are refused. The change MUST be saved to the settings file and reach the running engine without a restart. Both are allowed to a caller on the engine's own machine or holding a valid API token, as for the rest of the API.
- **FR-011**: "Show in folder" MUST be offered, and accepted by the engine, only when the caller is on the engine's own machine. It MUST open only paths inside the data folder, the pipelines folder or the incidents folder.
- **FR-012**: Every "Show in Finder" control the Mac app has (data folder, pipelines folder, a problem report, Logs) MUST have the browser equivalent.

**Problem reports**

- **FR-013**: App reports MUST offer Export in a browser, downloading one .zip with every report, attachments, system info and the engine log tail, redacted.
- **FR-014**: The engine MUST record a start marker and clear it on a clean shutdown; a marker still present at the next start MUST create one redacted App report of kind unclean exit.
- **FR-015**: The web UI MUST show the "quit unexpectedly last time" banner for an unseen unclean-exit report, with View report and Dismiss, both of which mark it seen.
- **FR-016**: When the engine runs inside the Mac app, the engine MUST NOT create unclean-exit reports, so the app's own checks stay the only source.
- **FR-017**: Scanning macOS crash logs MUST stay Mac-app only.

**Notifications and badges**

- **FR-018**: With the person's permission, the web UI MUST show one system notification, across all open Studio tabs in that browser, following the Mac app's rules: each waiting gate once (including gates already waiting when Studio opens); each run that becomes done, dead or needs-review after the first refresh (the first refresh is only a baseline); each new problem report.
- **FR-019**: Where the browser supports notification buttons, gate notifications MUST offer Approve and Reject. Approve MUST decide the gate through the same path as the Approvals page, with the comment "approved from a notification" as the Mac app uses, and the same audit row. Reject MUST open the approval with a comment field and not reject until confirmed.
- **FR-020**: Clicking any notification MUST bring Studio to the front on the related page.
- **FR-021**: The tab title, the tab icon and, when installed, the app icon MUST show the number of waiting approvals, and nothing at zero.
- **FR-022**: Permission MUST be asked only after the person presses a control in Settings → General, which also shows the current permission and how to change it when blocked.
- **FR-023**: In the Mac app, web notifications and badges MUST stay off.
- **FR-024**: Studio MUST be installable as an app from the browser (name, icon, standalone window), which is what lets the app-icon badge and notification buttons work in browsers that need it.

**Keyboard**

- **FR-025**: Cmd+K on macOS and Ctrl+K elsewhere MUST open a command palette with every Mac app menu action and every top-bar page; it MUST filter by typing and run with Enter.
- **FR-026**: Two-key `g` shortcuts MUST open Pipelines (`g p`), Runs (`g r`), Approvals (`g a`), Problems (`g x`), Settings (`g s`), Logs (`g l`), and `n` MUST start a run, never while typing in a field, the YAML editor, or with a dialog open.
- **FR-027**: The palette MUST be usable by keyboard alone and announce itself to screen readers as a dialog with a list.

**One engine per data folder**

- **FR-028**: `agentline serve` MUST hold a lock on its data folder for its lifetime; a second engine on the same folder MUST exit with an error naming the first's address and process id; a lock left by a dead process MUST NOT block.
- **FR-029**: The Mac app's engine MUST be able to take the lock, including on its own restarts.

**Logs**

- **FR-030**: In a browser, Logs MUST describe its content as the engine log and offer Copy path (and Show in folder on the engine's own machine) instead of the Finder button.

**Chat parity and the Mac app**

- **FR-031**: Every new GUI action (restart engine, set pipelines folder, show in folder, export reports, sign out, notification permission, dismiss crash banner) MUST have a Chat action checked against live state, or be listed in the parity table with the reason it has none.
- **FR-032**: The Mac app MUST keep working unchanged: every control that uses the native bridge today MUST keep using it inside the app.

### Key Entities

- **App info**: version, data folder, settings file, pipelines folder, engine address, whether inside the Mac app, whether the caller is local.
- **Start marker**: engine process id, start time, version, platform, kept in the data folder while the engine runs.
- **Unclean-exit report**: an App report (existing incident kind) built from a leftover start marker.
- **Data folder lock**: held by one engine process per data folder, names its address and process id.
- **Stored token**: kept in the browser only, per engine address.
- **Notification leader**: the one Studio tab in a browser that shows notifications for all.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Each of the 12 Mac-only features listed in the Context has a working browser equivalent, or a recorded reason it stays Mac-only (macOS crash-log scanning only).
- **SC-002**: A person in a browser notices a stopped engine within 10 seconds and gets back to work without reloading the page once the engine is up.
- **SC-003**: A person on another machine can sign in with a token on the first try, given the token, with no browser prompt dialog.
- **SC-004**: A waiting approval reaches a person with Studio in a background tab within one refresh cycle (5 seconds), once notifications are allowed.
- **SC-005**: A person can reach any Mac app menu action from the keyboard in at most 3 keystrokes after opening the palette.
- **SC-006**: Starting a second engine on the same data folder never results in two running engines.
- **SC-007**: The Mac app passes its existing tests and behaves the same in a manual check of every native-bridge control.

## Assumptions

- Notifications only fire while at least one Studio tab is open in that browser. Notifications with the browser fully closed would need a push service, which is out of scope.
- Notification buttons work only where the browser supports them (installed or service-worker notifications in Chromium browsers); Safari and Firefox fall back to click-to-open. That is acceptable.
- "On the engine's own machine" means the request comes from a loopback address and was not forwarded by a proxy. A person running Studio through a reverse proxy gets the remote behaviour.
- The engine restart from the web restarts the engine process in place; it is not a supervisor. If the engine was started by a supervisor (systemd, launchd, Docker), the supervisor's own restart remains the recommended way, and the in-place restart keeps working alongside it.
- The two-key shortcut letters are a reasonable default and can change later without a spec change.
- The secrets file and key status already work in a browser through the Settings API (phase 22 moved keys out of the Keychain), so keys are not one of the 12 gaps; the stale Keychain wording in the bridge is tidied as part of this work.
- Mobile layout is out of scope; Studio is used on desktop browsers.
- Tests use fakes for the file manager launcher and for browser notification APIs; real notifications and a real file manager are checked by hand.
