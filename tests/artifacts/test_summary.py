"""Deterministic auto summaries for each artifact kind."""

from __future__ import annotations

from agentline.artifacts import SUMMARY_MAX, guess_media_type, summarize

DIFF = """\
diff --git a/src/login.py b/src/login.py
index 111..222 100644
--- a/src/login.py
+++ b/src/login.py
@@ -1,3 +1,4 @@
-old = 1
+new = 1
+extra = 2
 same
diff --git a/tests/test_login.py b/tests/test_login.py
--- /dev/null
+++ b/tests/test_login.py
@@ -0,0 +1 @@
+def test(): pass
"""


def test_json_object() -> None:
    value = {"summary": "Fix login", "steps": [1, 2, 3], "files": ["a", "b"], "ok": True}
    assert summarize(value) == (
        '{summary: "Fix login", steps: [3 items], files: [2 items], ok: true}'
    )


def test_json_nested_and_long_values() -> None:
    s = summarize({"meta": {"a": 1}, "one": [1], "text": "y" * 100 + "\nsecond line"})
    assert s.startswith('{meta: {1 key}, one: [1 item], text: "yyy')
    assert "…" in s and "second line" not in s


def test_json_list_and_scalars() -> None:
    assert summarize([1, "a", 2]) == "[3 items: int, str]"
    assert summarize([]) == "[]"
    assert summarize(None) == "null"
    assert summarize("line one\nline two") == "line one\nline two\n(2 lines)"


def test_text_first_non_empty_lines_and_count() -> None:
    text = "\n\nFirst\n\nSecond\nThird\nFourth\n"
    assert summarize(text.encode(), "text/plain") == "First\nSecond\nThird\n(7 lines)"


def test_diff_files_and_counts() -> None:
    assert summarize(DIFF.encode(), "text/x-diff") == (
        "2 files changed, +3 -1: src/login.py, tests/test_login.py"
    )
    assert summarize(DIFF.encode(), "text/plain").startswith("2 files changed")  # sniffed


def test_binary_and_undecodable() -> None:
    assert summarize(b"\x89PNG" + bytes(2044), "image/png") == "image/png, 2.0 KiB"
    assert summarize(b"\xff\xfe", "text/plain") == "text/plain, 2 B"


def test_json_bytes_and_length_cap() -> None:
    assert summarize(b'{"a": [1, 2]}', "application/json") == "{a: [2 items]}"
    big = {f"key{i}": "v" * 30 for i in range(50)}
    assert len(summarize(big)) <= SUMMARY_MAX


def test_media_type_guess() -> None:
    assert guess_media_type("diff", "x") == "text/x-diff"
    assert guess_media_type("transcript.first", "x") == "text/plain"
    assert guess_media_type("blob", b"\xff\x00") == "application/octet-stream"
