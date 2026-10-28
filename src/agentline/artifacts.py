"""Content-addressed artifact store: node results, transcripts, diffs.

An artifact is a JSON value (kind `json`) or bytes with a media type (kind `file`). Its hash is
sha256 of the canonical JSON or of the bytes, so writing the same content twice is a no-op.
Small payloads live inline in the ledger's `blobs` table (keyed by the same hash); larger ones
are files at `<root>/<hash[:2]>/<hash>`, written atomically (temp file + rename) *before* the
`artifacts` row is inserted, so a row always points at a complete file.

Large inputs are handed to handlers as a reference (see `ArtifactRef`) instead of the value;
templates render a reference as its summary plus a file path.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentline.ids import canonical_json, content_hash
from agentline.ledger.base import Artifact, LedgerTx

INLINE_MAX = 256 * 1024  # stored inline in the ledger up to this many bytes
HANDOFF_MAX = 64 * 1024  # inputs larger than this reach handlers as a reference
SUMMARY_MAX = 300
JSON_MEDIA = "application/json"
DIFF_MEDIA = "text/x-diff"
REF_KEY = "$artifact"

Summarizer = Callable[[Any, str], str]
"""(value, media_type) -> summary. `value` is a JSON value for kind json, else bytes."""


def default_root() -> Path:
    home = os.environ.get("AGENTLINE_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".agentline"
    return base / "artifacts"


# -- references ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class ArtifactRef:
    """What a handler receives instead of a large input. Plain JSON as a dict (`as_dict`)."""

    hash: str
    summary: str
    size: int
    media_type: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            REF_KEY: self.hash,
            "summary": self.summary,
            "size": self.size,
            "media_type": self.media_type,
            "path": self.path,
        }

    @classmethod
    def from_value(cls, value: Any) -> ArtifactRef:
        if isinstance(value, ArtifactRef):
            return value
        if not is_ref(value):
            raise TypeError(f"not an artifact reference: {value!r:.80}")
        return cls(
            hash=value[REF_KEY],
            summary=value.get("summary", ""),
            size=int(value.get("size", 0)),
            media_type=value.get("media_type", ""),
            path=value.get("path", ""),
        )

    def read_file(self) -> Any:
        """Load the referenced content from its file: JSON value or bytes."""
        data = Path(self.path).read_bytes()
        return json.loads(data) if self.media_type == JSON_MEDIA else data


def is_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get(REF_KEY), str)


def render_ref(value: Any) -> str:
    ref = ArtifactRef.from_value(value)
    return f"{ref.summary}\n(full content: {ref.path})"


# -- summaries ----------------------------------------------------------------------------------
def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: max(0, n - 1)] + "…"


def _short(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(_clip(value.splitlines()[0] if value else "", 40), ensure_ascii=False)
    if isinstance(value, list):
        return f"[{len(value)} item{'' if len(value) == 1 else 's'}]"
    if isinstance(value, dict):
        return f"{{{len(value)} key{'' if len(value) == 1 else 's'}}}"
    return json.dumps(value)


def summarize_json(value: Any) -> str:
    if isinstance(value, dict):
        parts = [f"{k}: {_short(v)}" for k, v in value.items()]
        return _clip("{" + ", ".join(parts) + "}", SUMMARY_MAX)
    if isinstance(value, list):
        kinds = sorted({type(v).__name__ for v in value})
        return _clip(f"[{len(value)} items: {', '.join(kinds)}]" if value else "[]", SUMMARY_MAX)
    if isinstance(value, str):
        return summarize_text(value)
    return _clip(json.dumps(value), SUMMARY_MAX)


def summarize_text(text: str, max_lines: int = 3) -> str:
    lines = text.splitlines()
    head = [_clip(ln.strip(), 100) for ln in lines if ln.strip()][:max_lines]
    n = len(lines)
    return _clip("\n".join([*head, f"({n} line{'' if n == 1 else 's'})"]), SUMMARY_MAX)


_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_PLUS_FILE = re.compile(r"^\+\+\+ (?:b/)?(\S+)")


def looks_like_diff(text: str) -> bool:
    return text.startswith(("diff --git ", "--- ")) or "\ndiff --git " in text


def summarize_diff(text: str) -> str:
    files: list[str] = []
    added = removed = 0
    for line in text.splitlines():
        if m := _DIFF_FILE.match(line):
            files.append(m.group(2))
        elif line.startswith("+++ "):
            m2 = _PLUS_FILE.match(line)
            if m2 and m2.group(1) != "/dev/null" and m2.group(1) not in files:
                files.append(m2.group(1))
        elif line.startswith("--- "):
            continue
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    n = len(files)
    head = f"{n} file{'' if n == 1 else 's'} changed, +{added} -{removed}"
    return _clip(f"{head}: {', '.join(files)}" if files else head, SUMMARY_MAX)


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{n} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def summarize(value: Any, media_type: str = JSON_MEDIA) -> str:
    """Deterministic summary (≤ ~300 chars) of a JSON value or of bytes with a media type."""
    if not isinstance(value, (bytes, bytearray)):
        return summarize_json(value)
    data = bytes(value)
    if media_type == JSON_MEDIA:
        try:
            return summarize_json(json.loads(data))
        except ValueError:
            pass
    if media_type.startswith("text/") or media_type == JSON_MEDIA:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"{media_type}, {human_size(len(data))}"
        if media_type in (DIFF_MEDIA, "text/x-patch") or looks_like_diff(text):
            return summarize_diff(text)
        return summarize_text(text)
    return f"{media_type or 'application/octet-stream'}, {human_size(len(data))}"


def guess_media_type(name: str, data: str | bytes) -> str:
    base = name.rsplit("/", 1)[-1]
    if base == "diff" or base.endswith((".diff", ".patch")):
        return DIFF_MEDIA
    if base.endswith(".json"):
        return JSON_MEDIA
    if isinstance(data, str):
        return "text/plain"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


# -- store --------------------------------------------------------------------------------------
class ArtifactStore:
    """Reads and writes artifacts through a ledger transaction plus a file directory.

    `summarizer` is the seam for a smarter (LLM) summarizer later; it must be deterministic
    enough to call inside a transaction (it runs once per new artifact)."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        inline_max: int = INLINE_MAX,
        summarizer: Summarizer = summarize,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self.inline_max = inline_max
        self.summarizer = summarizer

    @property
    def root(self) -> Path:
        if self._root is None:  # resolved lazily: nothing touches disk until a file is written
            self._root = default_root()
        return self._root

    def file_path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    # writes
    def put_json(self, tx: LedgerTx, value: Any) -> Artifact:
        data = canonical_json(value).encode()
        return self._put(tx, data, "json", JSON_MEDIA, lambda: self.summarizer(value, JSON_MEDIA))

    def put_bytes(self, tx: LedgerTx, data: bytes, media_type: str) -> Artifact:
        return self._put(tx, data, "file", media_type, lambda: self.summarizer(data, media_type))

    def _put(
        self, tx: LedgerTx, data: bytes, kind: str, media_type: str, summary: Callable[[], str]
    ) -> Artifact:
        digest = content_hash(data)
        if (existing := tx.get_artifact(digest)) is not None:
            return existing
        if len(data) > self.inline_max:
            path: str | None = str(self._write_file(digest, data))
            storage = "file"
        else:
            tx.put_blob(data)
            path, storage = None, "inline"
        art = Artifact(
            hash=digest,
            kind=kind,
            media_type=media_type,
            size=len(data),
            summary=_clip(summary(), SUMMARY_MAX),
            storage=storage,
            path=path,
            created_at=time.time(),
        )
        tx.put_artifact(art)
        return art

    def _write_file(self, digest: str, data: bytes) -> Path:
        dst = self.file_path(digest)
        if dst.exists() and dst.stat().st_size == len(data):
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=f".{digest[:8]}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dst)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return dst

    # reads
    def meta(self, tx: LedgerTx, digest: str) -> Artifact | None:
        return tx.get_artifact(digest)

    def read_bytes(self, tx: LedgerTx, digest: str) -> bytes | None:
        """Raw content. Falls back to a bare blob for refs written before Phase 5."""
        art = tx.get_artifact(digest)
        if art is None or art.storage == "inline":
            return tx.get_blob(digest)
        path = Path(art.path) if art.path else self.file_path(digest)
        if not path.exists():
            path = self.file_path(digest)
        return path.read_bytes()

    def load(self, tx: LedgerTx, digest: str) -> Any:
        """JSON value for kind json (and for pre-Phase-5 result blobs), else bytes."""
        art = tx.get_artifact(digest)
        data = self.read_bytes(tx, digest)
        if data is None:
            return None
        if art is None or art.kind == "json":
            return json.loads(data)
        return data

    def materialize(self, tx: LedgerTx, art: Artifact) -> Path:
        """A file holding the artifact's bytes (writes one for inline artifacts)."""
        if art.storage == "file" and art.path and Path(art.path).exists():
            return Path(art.path)
        data = self.read_bytes(tx, art.hash)
        if data is None:
            raise FileNotFoundError(f"artifact {art.hash} has no content")
        return self._write_file(art.hash, data)

    def ref(self, tx: LedgerTx, art: Artifact) -> ArtifactRef:
        return ArtifactRef(
            hash=art.hash,
            summary=art.summary,
            size=art.size,
            media_type=art.media_type,
            path=str(self.materialize(tx, art)),
        )


def artifact_dict(art: Artifact) -> dict[str, Any]:
    return asdict(art)
