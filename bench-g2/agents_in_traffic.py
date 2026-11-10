#!/usr/bin/env python3
"""Where the text of /app/AGENTS.md and the "Finishing the step" section are in the
requests of a trial, before and after the harness compacted the history.

    python3 bench-g2/agents_in_traffic.py <job or batch directory>...

Reads the traffic log of the proxy (``<job>/bench-run/traffic``), nothing else; works on
a trial that is still running. One row per trial, step and phase:

  phase         ``pre`` - the first user message is the instruction; ``post`` - the history
                was compacted (opencode: the first user message is "What did we do so far?")
  requests      main requests of the phase (auxiliary ones - titles, summaries - are skipped)
  AGENTS in system / rule in system / rule in user / rule in other
                requests whose system message carries a line of AGENTS.md; whose system
                message carries the section heading; the same for user messages and for
                assistant and tool messages
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

AGENTS_LINE = "Do not silence the tools: no `# noqa`"
RULE = "## Finishing the step"
COMPACTED = "What did we do so far?"
COLUMNS = ("requests", "agents_system", "rule_system", "rule_user", "rule_other")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def scan(traffic: Path) -> dict[tuple[str, str], dict[str, int]]:
    """Counters per (step, phase) over the main chat requests of one trial."""
    rows: dict[tuple[str, str], dict[str, int]] = {}
    for line in (traffic / "index.jsonl").read_text().splitlines():
        try:
            entry = json.loads(line)
            if entry.get("method") != "POST" or entry.get("auxiliary") or not entry.get("file"):
                continue
            messages = json.loads((traffic / entry["file"]).read_text())["request_body"]["messages"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        by_role: dict[str, list[str]] = {"system": [], "user": [], "other": []}
        for message in messages:
            role = message.get("role")
            by_role[role if role in ("system", "user") else "other"].append(_text(message.get("content")))
        system, user, other = ("\n".join(by_role[key]) for key in ("system", "user", "other"))
        phase = "post" if by_role["user"] and COMPACTED in by_role["user"][0][:200] else "pre"
        row = rows.setdefault((str(entry.get("step")), phase), dict.fromkeys(COLUMNS, 0))
        row["requests"] += 1
        row["agents_system"] += AGENTS_LINE in system
        row["rule_system"] += RULE in system
        row["rule_user"] += RULE in user
        row["rule_other"] += RULE in other
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    print("| trial | step | phase | requests | AGENTS in system | rule in system | rule in user | rule in other |")
    print("|---|---|---|---|---|---|---|---|")
    for root in map(Path, sys.argv[1:]):
        for traffic in sorted(p.parent for p in root.glob("**/bench-run/traffic/index.jsonl")):
            for (step, phase), row in sorted(scan(traffic).items()):
                print(f"| {traffic.parent.parent.name} | {step} | {phase} | " + " | ".join(str(row[c]) for c in COLUMNS) + " |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
