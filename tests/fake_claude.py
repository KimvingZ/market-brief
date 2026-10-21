"""Stand-in for Claude's part of a run in offline tests: fill claude.json with
valid text, keeping the code's leans, caps and suggested invalidations."""

from __future__ import annotations

import json
from pathlib import Path


def fill(work: Path, run_type: str) -> dict:
    path = Path(work) / "claude.json"
    c = json.loads(path.read_text())
    c["headline"] = f"Test headline for the {run_type} run"
    c["overview"] = "Synthetic overview text that is long enough to pass the checks."
    for aid, a in c["assets"].items():
        if run_type == "full":
            a["summary"] = f"{aid} synthetic summary sentence for testing the pipeline."
            a["what_would_change"] = "A close beyond the invalidation level."
        else:
            a["changed"] = f"{aid} moved a little since the morning report."
        a["drivers"] = [{"text": f"{aid} test driver headline", "url": "https://example.com/news", "source": "Example"}]
        for hz, call in a["calls"].items():
            call["reason"] = f"Test reason for {aid} {hz}, follows the code lean."
    path.write_text(json.dumps(c, indent=1))
    return c
