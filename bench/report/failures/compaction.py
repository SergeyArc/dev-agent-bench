#!/usr/bin/env python3
"""The commit rule before and after history compaction.

    python3 bench/report/failures/compaction.py

For every step of the G2 / G2b trials: was the history compacted, is there a final commit,
did the agent call `git commit` at all, the tail of its last message. For the steps with
compaction the traffic log is walked request by request, and a line is printed whenever
the shape of the history changes: number of messages, the first user message, whether the
task section "Finishing the step" is still in the request, whether the harness system
prompt carries "NEVER commit". For the last response — the sentences of its reasoning
that mention a commit. Written to `~/bench-runs/analysis/failures/compaction/summary.md`.
Read-only on the runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import OUT_ROOT, listed, scrub, turns

GIT_COMMIT = re.compile(r"\bgit\b[^|;&\n]*\bcommit\b")
COMMIT_SENTENCE = re.compile(r"[^.\n]*\bcommit[^.\n]*", re.IGNORECASE)
RULE = "Finishing the step"
HARNESS_BAN = "NEVER commit"


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


def history_shapes(traffic: Path) -> tuple[list[str], list[str]]:
    """(lines on every change of the history shape, commit sentences of the last reasoning)."""
    lines: list[str] = []
    previous: tuple[str, bool, bool, bool] | None = None
    last_reasoning = ""
    for record in sorted(traffic.glob("*.json")):
        try:
            data = json.loads(record.read_text())
        except ValueError:
            continue
        body = data.get("request_body") or {}
        messages = body.get("messages") or []
        if not messages:
            continue
        system = " ".join(
            text_of(m.get("content")) for m in messages if m.get("role") == "system"
        )
        users = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
        everything = " ".join(text_of(m.get("content")) for m in messages)
        first_user = " ".join(users[0][:70].split()) if users else ""
        shape = (
            first_user,
            RULE in everything,
            HARNESS_BAN in system,
            bool(body.get("tools")),
        )
        if shape != previous:
            lines.append(
                f"- {record.name}: messages={len(messages)}, tools={'yes' if shape[3] else 'no'}, "
                f"first user message «{scrub(first_user)}», «{RULE}» in request: {'yes' if shape[1] else 'no'}, "
                f"«{HARNESS_BAN}» in system: {'yes' if shape[2] else 'no'}"
            )
            previous = shape
        if body.get("tools"):
            last_reasoning = str(
                (data.get("response_assembled") or {}).get("reasoning") or ""
            )
    return lines, [
        scrub(m.group(0).strip())[:300]
        for m in COMMIT_SENTENCE.finditer(last_reasoning)
    ]


def main() -> None:
    lines: list[str] = []
    for item in listed(("G2", "G2b")):
        for number, step in sorted(item.trial.steps.items()):
            run = step.run
            commit_turns = [
                turn.index
                for turn in turns(step)
                for _, arguments in turn.calls
                if isinstance(arguments.get("command"), str)
                and GIT_COMMIT.search(arguments["command"])
            ]
            lines.append(
                f"\n## {item.variant} …{item.short} {item.cell} step {number}: "
                f"history_shrinks={run.get('history_shrinks')}, final_commit={(run.get('g2') or {}).get('final_commit')}, "
                f"`git commit` calls at turns {commit_turns or '—'}, last message is a question: "
                f"{run.get('last_message_is_question')}, length finishes: {run.get('length_finishes')}"
            )
            tail = " / ".join(
                scrub(str(run.get("last_message_tail") or "")[-260:]).splitlines()
            )
            lines.append(f"last message: …{tail}")
            if run.get("history_shrinks"):
                shapes, sentences = history_shapes(Path(str(run["traffic_log"])))
                lines.extend(shapes)
                lines.extend(
                    f"  last reasoning on commit: {sentence}" for sentence in sentences
                )
    out = OUT_ROOT / "compaction"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"{len(lines)} lines -> {out / 'summary.md'}")


if __name__ == "__main__":
    main()
