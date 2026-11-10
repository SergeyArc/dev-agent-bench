#!/usr/bin/env python3
"""Steps that ended on a response cut by the output limit without a tool call.

    python3 bench/report/failures/no_action.py

For every step of the listed trials whose last response hit the output limit
(`last_response_length` in run.json): the last record of the traffic log — finish reason,
size of the reasoning, of the text and the number of tool calls — and the first and last
characters of that reasoning. Written to `~/bench-runs/analysis/failures/no_action/summary.md`.
Read-only on the runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from common import OUT_ROOT, listed, scrub, turns

EDGE = 700


def main() -> None:
    lines: list[str] = []
    for item in listed():
        for number, step in sorted(item.trial.steps.items()):
            if not step.run.get("last_response_length"):
                continue
            records = sorted(Path(str(step.run["traffic_log"])).glob("*.json"))
            last = json.loads(records[-1].read_text()) if records else {}
            answer = last.get("response_assembled") or {}
            if not answer:  # not a stream: the body is kept as the server sent it
                choice = ((last.get("response_body") or {}).get("choices") or [{}])[0]
                message = choice.get("message") or {}
                answer = {
                    "finish_reason": choice.get("finish_reason"),
                    "reasoning": message.get("reasoning")
                    or message.get("reasoning_content"),
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls"),
                }
            reasoning = str(answer.get("reasoning") or "")
            agent_turns = sum(1 for turn in turns(step) if turn.source == "agent")
            lines.append(
                f"\n## {item.variant} …{item.short} {item.cell} step {number}: end_reason={step.end_reason}, "
                f"agent turns={agent_turns}, reward={(step.run.get('reward') or {}).get('reward')}, "
                f"final commit={(step.run.get('g2') or {}).get('final_commit')}\n"
                f"last response ({records[-1].name if records else '—'}): finish_reason={answer.get('finish_reason')}, "
                f"reasoning {len(reasoning)} characters, text {len(str(answer.get('content') or ''))} characters, "
                f"tool calls {len(answer.get('tool_calls') or [])}, "
                f"max completion tokens of a response={step.run.get('max_response_completion_tokens')}"
            )
            lines.append("first: " + " / ".join(scrub(reasoning[:EDGE]).splitlines()))
            lines.append("last: " + " / ".join(scrub(reasoning[-EDGE:]).splitlines()))
    out = OUT_ROOT / "no_action"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(line[:420] for line in lines))


if __name__ == "__main__":
    main()
