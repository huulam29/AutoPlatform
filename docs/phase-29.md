# Phase 29: app problems leave Problems and go to App reports

The user saw two crash reports from the Mac app on Problems (`app_crash` from
`CrashCapture.scheduleTestCrashIfRequested`, the phase 13 test crashes) and asked:

1. App issues should not appear on Problems: it shows only what can be fixed in the app
   (pipeline nodes, settings, config, keys).
2. App issues are triaged and reported on GitHub with the failure-triage procedure (phase 28).
3. The Chat gets the rule: report an app/engine problem on GitHub that way, then remove it from
   the Problems page.

Decision 64.

## What changed

- **Problems is for user-fixable problems.** `issues.sync` no longer creates issues for
  incidents. `issues.is_app(issue)` marks app problems: an incident's issue (older ones stay on
  disk, hidden) or a run whose triage verdict is `infrastructure`. `IssueStore.list` leaves them
  out unless `app=True`. `GET /api/issues` also drops runs with an infrastructure triage record
  (even when the issue missed the link), so counts and the nav badge only count fixable problems.
- **Crash reports are triaged as infrastructure, without a model.** `triage.triage_incidents`
  makes one record per incident (`incident_id` = sha1 of `incident:<id>`), with a redacted
  report built from the incident (`incident_markdown`: source, kind, count, version, platform,
  message, traceback, context), the `report_issue` card when GitHub is set up, and an automatic
  issue with `github_auto = true`. A report seen again updates its record. A record whose
  incident was deleted goes too. The triage loop, the App reports API and the Chat's LIVE STATE
  all run it, so it's cheap and always current.
- **App reports** (`GET /api/app-reports`, `DELETE /api/app-reports/{id}`; `triage.app_reports`)
  list crash reports and infrastructure verdicts on runs. Removing a crash report deletes the
  incident too.
- **UI:** `web/src/components/AppReports.tsx` at `#/app-reports`, linked from the Problems
  toolbar, the Logs toolbar and Settings → General. Each card shows Reported / Not reported, the
  source ("Crash report (app_crash), seen 2×" or the run), the report, Report on GitHub (when set
  up), Copy as GitHub issue (with the hand-copy fallback), Open run, Show in Finder (Mac) and
  Remove. The Mac crash buttons (Scan for crash reports, Export reports…, Clear) moved here from
  Problems, and Problems lost its incident detail section.
- **Chat:**
  - The default Chat instructions gain the rule (`chat_instructions.APP_PROBLEMS`). Existing
    files get it once through `UPGRADES` (recorded in `chat-instructions.upgrades.json`, so
    deleting it sticks). It goes with the general rules, before the lessons section.
  - The guide's APP PROBLEMS paragraph explains it.
  - LIVE STATE has an `APP REPORTS` list with triage ids, so the Chat can propose `report_issue`
    and then an `incident` delete. There are no new action types, so the parity table is
    unchanged.

## Tests

`tests/ai/test_triage.py`:
- crash reports become redacted infrastructure records with no model call (repeat count, pruning)
- the GitHub card and the create-once behaviour for crash reports
- the App reports API and that Problems keeps only the user-fixable run
- the instruction rule in the defaults and as a one-time upgrade that stays deleted
- LIVE STATE

`tests/ai/test_issues.py`: incidents don't become issues. `web/src/lib/triage.test.ts`:
`appReportSource`.

Checked in the built-in browser with a scratch copy of the user's two real crash reports and
issues: Problems showed only the run, App reports showed both crashes and the infrastructure
run, and Remove deleted a crash report.

## Not done

- No GitHub repo is configured yet, so App reports offer "Copy as GitHub issue". Once
  `[report] github_repo` and `GITHUB_TOKEN` are set, each report gets "Report on GitHub".
- Settings and config errors (a bad agentline.toml, a model source that fails its test) aren't
  issues on Problems yet. They show where they happen (Settings, the editor).
