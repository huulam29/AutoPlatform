"""Issues: one tracked item per failed run, with a status (Problems).

Stored locally in `$AGENTLINE_HOME/issues/<number>.json`, next to `incidents/`. Nothing is
uploaded. `sync()` makes sure every failed run has an issue: a dead / needs-review run gets one
(status `open`), and a problem that comes back after its issue was marked `fixed` reopens it
(the history says so).

Problems shows only what can be fixed in the app (pipelines, settings, config, keys). agentline's
own problems are **app problems** and stay off it (`is_app`): crash / error reports (incidents,
which no longer get issues; older incident issues are hidden) and failed runs whose failure
triage says `infrastructure`. They are triaged and reported from App reports (agentline.triage).
Issues outlive their problem: retrying a run leaves the issue and its notes in place.

Statuses: open -> in_progress -> fixed | wont_fix (any status can move to any other).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError
from agentline.filelock import locked_file

STATUSES = ("open", "in_progress", "fixed", "wont_fix")
CLOSED = ("fixed", "wont_fix")
FAILED_RUN_STATUSES = ("dead", "needs-review")
MAX_NOTE = 4000


def is_app(issue: dict[str, Any]) -> bool:
    """An agentline problem (not fixable in the app): an incident's issue, or a run whose
    triage verdict is infrastructure."""
    if (issue.get("source") or {}).get("kind") == "incident":
        return True
    return ((issue.get("triage") or {}).get("verdict")) == "infrastructure"


def issues_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "issues"


def source_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"


class IssueStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else issues_dir()

    # -- files ------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with locked_file(self.root / ".lock"):
            yield

    def _path(self, number: int) -> Path:
        return self.root / f"{number}.json"

    def _write(self, rec: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".issue.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, default=str)
        os.replace(tmp, self._path(int(rec["number"])))

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        out = [self._read(p) for p in self.root.glob("*.json")]
        return [r for r in out if isinstance(r.get("number"), int)]

    # -- reading ----------------------------------------------------------------------------
    def list(self, status: str | None = None, *, app: bool = False) -> list[dict[str, Any]]:
        """Newest activity first. `status` may be one status, `active` (open + in progress)
        or `closed`. App problems (`is_app`) are left out unless `app`."""
        items = [i for i in self._all() if app or not is_app(i)]
        if status == "active":
            items = [i for i in items if i.get("status") not in CLOSED]
        elif status == "closed":
            items = [i for i in items if i.get("status") in CLOSED]
        elif status:
            items = [i for i in items if i.get("status") == status]
        items.sort(key=lambda i: i.get("updated_at", 0), reverse=True)
        return items

    def get(self, number: int) -> dict[str, Any] | None:
        rec = self._read(self._path(number))
        return rec or None

    def by_source(self) -> dict[str, dict[str, Any]]:
        return {source_key(i["source"]["kind"], i["source"]["ref"]): i for i in self._all()}

    # -- writing ----------------------------------------------------------------------------
    def _new_number(self) -> int:
        return max((i["number"] for i in self._all()), default=0) + 1

    def ensure(
        self, kind: str, ref: str, *, title: str, seen_at: float, detail: str = ""
    ) -> tuple[dict[str, Any], str]:
        """The issue for this problem, created if missing and reopened if it was fixed before
        `seen_at`. Returns (issue, what happened: created | reopened | unchanged)."""
        with self._locked():
            key = source_key(kind, ref)
            rec = self.by_source().get(key)
            now = time.time()
            if rec is None:
                rec = {
                    "number": self._new_number(),
                    "title": title[:300],
                    "status": "open",
                    "source": {"kind": kind, "ref": ref},
                    "detail": detail[:MAX_NOTE],
                    "created_at": now,
                    "updated_at": now,
                    "last_seen": seen_at,
                    "occurrences": 1,
                    "notes": [],
                    "history": [{"at": now, "from": None, "to": "open", "note": "created"}],
                }
                self._write(rec)
                return rec, "created"
            changed = "unchanged"
            if seen_at > float(rec.get("last_seen") or 0) + 1e-6:
                rec["last_seen"] = seen_at
                rec["occurrences"] = int(rec.get("occurrences") or 1) + 1
                rec["title"] = title[:300] or rec["title"]
                if detail:
                    rec["detail"] = detail[:MAX_NOTE]
                if rec.get("status") == "fixed":
                    rec["history"].append(
                        {"at": now, "from": "fixed", "to": "open", "note": "happened again"}
                    )
                    rec["status"] = "open"
                    changed = "reopened"
                rec["updated_at"] = now
                self._write(rec)
            return rec, changed

    def update(
        self,
        number: int,
        *,
        status: str | None = None,
        note: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in STATUSES:
            raise ValidationError(f"unknown status {status!r} ({', '.join(STATUSES)})")
        note = (note or "").strip()[:MAX_NOTE]
        with self._locked():
            rec = self.get(number)
            if rec is None:
                raise KeyError(number)
            now = time.time()
            if status is not None and status != rec.get("status"):
                rec["history"].append(
                    {"at": now, "from": rec.get("status"), "to": status, "note": note or None}
                )
                rec["status"] = status
            elif note:
                rec["notes"].append({"at": now, "text": note})
            if title is not None and title.strip():
                rec["title"] = title.strip()[:300]
            rec["updated_at"] = now
            self._write(rec)
            return rec

    def set_triage(self, number: int, triage: dict[str, Any], note: str = "") -> dict[str, Any]:
        """Attach a failure triage's verdict (agentline.triage) and add its note."""
        with self._locked():
            rec = self.get(number)
            if rec is None:
                raise KeyError(number)
            now = time.time()
            rec["triage"] = triage
            if note.strip():
                rec["notes"].append({"at": now, "text": note.strip()[:MAX_NOTE]})
            rec["updated_at"] = now
            self._write(rec)
            return rec

    def delete(self, number: int) -> bool:
        with self._locked():
            path = self._path(number)
            if not path.exists():
                return False
            path.unlink()
            return True

    # -- sync -------------------------------------------------------------------------------
    def sync(
        self, incidents: Iterable[dict[str, Any]], failed_runs: Iterable[dict[str, Any]]
    ) -> dict[str, int]:
        """An issue for every failed run (dicts as the APIs return them). Incidents are app
        problems: they go to App reports, not here (the argument stays for callers). Returns
        counts of created and reopened issues."""
        counts = {"created": 0, "reopened": 0}
        for r in failed_runs:
            if r.get("status") not in FAILED_RUN_STATUSES:
                continue
            _, what = self.ensure(
                "run",
                str(r["id"]),
                title=f"Run {r['id']} of {r.get('flow', '?')}: {r['status']}",
                seen_at=float(r.get("updated_at") or 0),
                detail=str(r.get("error") or ""),
            )
            counts[what] = counts.get(what, 0) + 1
        return {"created": counts["created"], "reopened": counts["reopened"]}
