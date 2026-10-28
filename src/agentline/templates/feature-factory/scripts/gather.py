#!/usr/bin/env python3
"""Join the four reviews: PASS only when every reviewer passed; else CHANGES with the list."""

import json
import os

with open(os.environ["AGENTLINE_INPUTS"]) as f:
    inputs = json.load(f)
reviews = {
    k: inputs.get(k) or {} for k in ("security_review", "ux_review", "ui_review", "code_review")
}
changes = []
for name, r in reviews.items():
    if str(r.get("verdict", "")).upper() != "PASS":
        for f in r.get("findings") or [r.get("summary") or "changes requested"]:
            changes.append(f"[{name.removesuffix('_review')}] {f}")
print(
    json.dumps(
        {
            "result": "CHANGES" if changes else "PASS",
            "changes": changes,
            "reviews": {k: str(v.get("verdict", "")).upper() for k, v in reviews.items()},
        }
    )
)
