# Phase 31: GitHub setup in Settings → General

Where failure-triage reports go (phase 28/29), set up in the app instead of agentline.toml. The
user asked for GitHub SSO or SSH support. Their choices (decision 65):

- **Sign-in:** the GitHub CLI's browser login, with a pasted token as the fallback.
- **SSH:** the repo is detected from git remotes, SSH or HTTPS. An SSH key can't authenticate
  the issues API (GitHub takes tokens only), so it isn't used for sign-in.

## Engine: `src/agentline/github_setup.py`

- **Sign-in.** `Login.start()` runs `gh auth login --hostname github.com --git-protocol https
  --web --skip-ssh-key` with no terminal and presses Enter for it. It reads gh's "one-time code
  (XXXX-XXXX)" and the device URL, and returns them within a few seconds. The process keeps
  waiting until the user approves in the browser (SSO orgs then ask to authorise it); its exit
  code makes the state `done` or `failed`. `cancel()` kills it. One sign-in runs at a time.
- **Token.** `token()` returns `(token, source)`:
  - `GITHUB_TOKEN` from the environment when it differs from the saved one (`env`),
  - else the secrets file (`secrets`),
  - else `gh auth token` (`gh`, cached for 30 s).
  `triage.github_token` uses it, so a CLI sign-in is enough to report. `gh_path()` also looks
  in the Homebrew folders, because the Mac app's PATH doesn't include them.
- **Repo.** `parse_remote` turns `git@github.com:o/r.git`, `ssh://git@github.com[:22]/o/r`
  and `https://[user@]github.com/o/r[.git]` into `o/r`. `detect_repos` reads `git remote -v`
  of agentline's own checkout and of the pipelines folder.
- **Save.** `save()` writes `[report] github_repo` and `github_auto` with
  `tomlwrite.set_plain_table`. That's new: a writer for a top-level table that refuses when
  anything else in the file would change. A pasted URL is reduced to `owner/name`.
- **Test.** `test()` calls `GET /repos/{repo}` with the token and gives a plain reason on
  401, 403 and 404. `GitHubClient.get_repo` was added for it.

API (`api/github.py`):
- `GET /api/github`: the status, never a token.
- `PUT /api/github {repo?, auto?}`: saves, and reloads the config.
- `POST|GET|DELETE /api/github/login`: start, progress and cancel the sign-in.
- `POST /api/github/test`.

Chat parity: a `github` action with `op` = `save` (with `repo` and/or `auto`), `login` or
`test`. LIVE STATE gets a `GITHUB` line (the repo, where the token comes from, the CLI login,
auto-report) and the `DETECTED` remotes.

## UI: `web/src/components/GitHubSetup.tsx` (in Settings → General)

- A status chip: Ready / Sign in needed / Repo needed / Not set up.
- **Repository:** an input (owner/name or a remote URL), Save, and "Use me/agentline
  (agentline, SSH)" buttons for the detected remotes.
- **Sign-in:** "Using: …" shows where the token comes from.
  - With gh installed, "Sign in with GitHub" shows the one-time code, an Open GitHub link and
    Cancel, and polls every 2 s until it's done.
  - Without gh, a hint: `brew install gh`.
  - Either way there's a password field to paste a token, saved through `/api/secrets` as
    `GITHUB_TOKEN`.
- **Reports:** "Create issues automatically" (`github_auto`) and Test.
- Helpers live in `lib/github.ts` (tests in `github.test.ts`).

## Tests

`tests/ai/test_github_setup.py`:
- remote parsing and detection on a real git repo
- `[report]` writes that keep the rest of the file (and refuse dotted keys)
- token precedence
- the browser sign-in through a fake `gh` script (code, URL, approval, cancel, gh missing)
- the API end to end with a fake GitHub server (save, sign in, test ok and 404)

`tests/ai/conftest.py` makes every AI test see no GitHub CLI, so the machine's real login is
never used. Chat parity cases are in `test_assistant_parity.py` and `assistantApply.test.ts`.

Checked in the built-in browser with a fake `gh` on PATH: the detected SSH remote was offered
and saved, sign-in showed the code, approval moved the page to "Ready (octo)", and Test showed
a readable error. The real `gh` and GitHub weren't used.

## Not done

- No sign-out button. Run `gh auth logout` yourself, or delete the pasted key in Settings → Keys.
- The real GitHub sign-in and issue creation haven't run yet: there's no repo on GitHub.
