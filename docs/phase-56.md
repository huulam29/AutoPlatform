# Phase 56: the engine and tests on native Windows

Approach: Direct (Lane 0, override 2: a bug fix with known failing behaviour; the diagnosis is
below instead of spec files).

## Diagnosis

On Windows 11 (Python 3.11, Git Bash) `import agentline` failed at once: `incidents.py`,
`issues.py` and `chatlink/state.py` import `fcntl`. With that stubbed, the earlier sample
(`specs/README.md`) had 54 failures, and a full run showed these causes:

| Cause | Where |
|---|---|
| `fcntl.flock` for the incidents, issues and chat-link lock files | 3 modules |
| `os.killpg` on timeout / cancel; `start_new_session` does nothing on Windows | `runners/common.py`, `actions/_util.py` |
| `create_subprocess_exec("claude")` only finds `claude.exe`, not npm's `claude.cmd` | same |
| Children got no `SYSTEMROOT`, `TEMP`, `USERPROFILE`, ... (the env allowlist was POSIX names) | `runners/common.py` `SAFE_ENV` |
| `read_text()` / `open()` without `encoding=` read UTF-8 files as cp1252 (`lessons.md`'s "·") | 89 calls in `src/` |
| `shutil.rmtree` can't delete git's read-only object files (probe clones stayed behind) | `probe.py`, `workspaces/git.py`, skills |
| `os.fchmod` (secrets file), `os.getuid` (container `--user`) don't exist | `secrets.py`, `workspaces/container.py` |
| `os.kill(pid, 0)` *terminates* a process on Windows (parent watchdog) | `cli/commands.py` |
| `core.autocrlf` checked everything out CRLF; the editor API returns files byte for byte | `tests/api/test_editor.py` |
| Tests: `#!/bin/sh` fake CLIs, `os.getuid`, mode bits, Windows paths inside YAML/TOML strings, `HOME` instead of `USERPROFILE` | `tests/` |

## What changed

- `filelock.py`: `locked_file(path)`, an exclusive cross-process lock (`fcntl.flock` on POSIX,
  `msvcrt.locking` with retries on Windows). Incidents, issues and the chat-link state use it.
- `proctree.py`: `kill_tree(pid)` (process group on POSIX, `taskkill /T /F` on Windows) and
  `resolve_argv(argv, env)` (Windows: a bare program name is looked up with PATHEXT, so
  `claude.cmd`, `gh.cmd` and other npm/pip shims start). Both the runner and the action command
  paths use them.
- `fsutil.py`: `rmtree` that clears the read-only flag and retries.
- Every text `open` / `read_text` / `write_text` / `os.fdopen` in `src/` says `encoding="utf-8"`;
  subprocess text pipes that carry file content (git, gh, MCP, skills) too.
- `SAFE_ENV` gains the Windows variables a child needs to start (they aren't set on POSIX).
- `secrets.py` sets 0600 only where `os.fchmod` exists; on Windows the file sits in the user's
  profile, whose ACL already keeps other users out.
- `.gitattributes`: `* text=auto eol=lf` (`*.cmd`/`*.bat` CRLF). An existing Windows clone gets
  LF files after `git rm -r --cached . && git reset --hard` (with nothing uncommitted).
- Tests: `tests/fakebin.py` `fake_bin(folder, name, script=|code=)` writes `name.cmd` on Windows
  and an executable `#!/bin/sh` wrapper elsewhere; shell fakes (`gh`, `claude auth status`) are
  Python now.

## What stays POSIX-only

| Feature | On Windows | Test |
|---|---|---|
| Container workspaces (`workspace: {type: container}`) | Refused with a permanent error naming WSL / `type: local`: they mount host paths as `path:path` and map the uid | `tests/workspaces/test_container.py` skipped |
| `shell.run`, `env.deploy`, `env.test` string commands | Run through `cmd.exe`, not `/bin/sh`: POSIX shell syntax in a pipeline doesn't work there. Argv lists work | 12 tests in `tests/actions/test_shell_env.py` skipped (`posix_shell`) |
| Parent-pid watchdog (`AGENTLINE_PARENT_PID`, Mac app only) | Off | `test_parent_watchdog.py` (slow) skipped |
| Secrets file mode 0600 | Profile ACL instead | mode asserts skipped |
| Chaos demos (`demo.chaos`, `demo.e2e`) kill workers with SIGKILL | Not tried on Windows | slow, not in the fast suite |

## Verification

- Windows 11, Git Bash, Python 3.11: `uv run pytest -m "not slow"` gives 975 passed, 54 skipped,
  0 failed. The skips are 35 Postgres contract tests (no `AGENTLINE_PG_DSN`), 2 smoke tests,
  12 POSIX-shell tests and 5 container tests.
- Ubuntu (WSL 2), Python 3.14, fresh clone of the branch: 992 passed, 37 skipped. `ruff check`
  and `ruff format --check` are clean.
- `agentline serve` on Windows (scratch `AGENTLINE_HOME`, db and config; `web/dist` built
  with `npm ci && npm run build`) serves Studio on `127.0.0.1`. `/api/pipelines` lists the
  example, and the editor opens `ticket-to-eod` and shows it as valid.
- Not tried on Windows: a real agent CLI (`claude.cmd`) running a node; the slow chaos and e2e
  tests; Docker Desktop.

## Known limits

- Fake CLIs are `.cmd` shims, so argv goes through `cmd.exe`. An argument containing a newline
  would be cut there. Real npm shims (`claude.cmd`) have the same limit. The runners pass
  prompts as arguments, so a multi-line prompt to a `.cmd`-installed CLI on Windows is untested.
