"""Second pass over the CJ1 evidence: tests that were renamed AND edited.

The verifier's collector (`benchverify/evidence.py`, not changed here) pairs a
test that disappeared with a test that appeared when the body is identical or
the function name is the same. A test that was renamed and edited in the same
step matches neither rule and stays in `missing_node_ids` — e.g. the reference
solution, step 5: `test_upgrade_head_creates_the_track_tables` became
`test_upgrade_head_creates_the_tables` and gained two assertions.

Same principle, one step further: the bodies are compared as sequences of
tokens (identifiers, literals, operators). A pair is reported as moved /
renamed when the bodies are nearly equal, or when both the bodies and the
function names are alike — a similar body alone is not enough, tests of one
module share their arrange / act lines. The similarity and the assertion
counts of both sides are given. The judge gets this list instead of the raw
one; a pair found here is marked `matched_by = "similar_body"`, so that it can
be checked against the diff.

Works on the test files of the previous and of the current snapshot. Agent
code is parsed, never executed.
"""

from __future__ import annotations

import ast
import difflib
import io
import sys
import tokenize
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from benchverify import evidence as collector  # the verifier's own AST analysis

__all__ = ["collector", "rematch"]

# Shares of matching tokens (difflib ratio). Bodies this close are one test whatever
# the names are …
BODY_ALONE = 0.9
# … and below that both the body and the name (its words, without `test`) must be alike.
# The reference case: body 0.94, name 0.91. Unrelated tests of one module reach 0.6–0.8 by
# body with no word of the name in common.
BODY_WITH_NAME = 0.6
NAME = 0.5
_SKIPPED_TOKENS = frozenset(
    {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT, tokenize.ENDMARKER}
)


def _functions(files: dict[str, str]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Test functions keyed like the collector keys them: `tests/x.py::Class::test`."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    def walk(node: ast.AST, rel: str, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, rel, [*stack, child.name])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                if child.name.startswith("test"):
                    found["::".join([rel, *stack, child.name])] = child
                else:
                    walk(child, rel, [*stack, child.name])

    for rel, text in sorted(files.items()):
        if not rel.endswith(".py"):
            continue
        try:
            walk(ast.parse(text, filename=rel), rel, [])
        except (SyntaxError, ValueError, RecursionError):
            continue
    return found


def body_tokens(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Tokens of the body without the name, decorators, docstring and layout."""
    body = node.body
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        body = body[1:]
    source = ast.unparse(ast.Module(body=body, type_ignores=[]))
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return [tok.string for tok in tokens if tok.type not in _SKIPPED_TOKENS]


def similarity(a: list[str], b: list[str]) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def name_words(key: str) -> list[str]:
    return [word for word in key.rsplit("::", 1)[-1].lower().split("_") if word and word != "test"]


def same_test(body: float, name: float) -> bool:
    return body >= BODY_ALONE or (body >= BODY_WITH_NAME and name >= NAME)


def rematch(evidence: dict[str, Any], before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    """The evidence with renamed-and-edited tests moved from `missing_node_ids` to `moved_or_renamed`.

    `before` / `after`: text of the files under tests/ at the previous and at the
    current step, keyed by path relative to the project root.
    """
    out = dict(evidence)
    missing = evidence.get("missing_node_ids")
    if not missing:
        out["rematch"] = {"status": "nothing_missing", "pairs": 0}
        return out
    old, new = _functions(before), _functions(after)
    taken = {m["to"] for m in evidence.get("moved_or_renamed", [])}
    gone = sorted({collector._base(n) for n in missing} & old.keys())
    appeared = sorted(new.keys() - old.keys() - taken)

    old_tokens = {key: body_tokens(old[key]) for key in gone}
    new_tokens = {key: body_tokens(new[key]) for key in appeared}
    candidates = []
    for a in gone:
        for b in appeared:
            if not old_tokens[a] or not new_tokens[b]:
                continue
            body = similarity(old_tokens[a], new_tokens[b])
            name = similarity(name_words(a), name_words(b))
            if same_test(body, name):
                candidates.append((-body, -name, a.split("::")[0] != b.split("::")[0], a, b))
    pairs: dict[str, tuple[str, float, float]] = {}
    used: set[str] = set()
    for body, name, _other_file, a, b in sorted(candidates):  # closest first; the same file first
        if a not in pairs and b not in used:
            pairs[a] = (b, round(-body, 3), round(-name, 3))
            used.add(b)

    counts_old, counts_new = collector.analyse(before).tests, collector.analyse(after).tests
    moved = list(evidence.get("moved_or_renamed", []))
    for key, (target, ratio, name_ratio) in sorted(pairs.items()):
        moved.append(
            {
                "from": key,
                "to": target,
                "matched_by": "similar_body",
                "similarity": ratio,
                "name_similarity": name_ratio,
                "assertions": {"before": counts_old.get(key), "after": counts_new.get(target)},
                "node_ids": [n for n in missing if collector._base(n) == key],
            }
        )
    out["moved_or_renamed"] = moved
    out["missing_node_ids"] = [n for n in missing if collector._base(n) not in pairs]
    out["rematch"] = {
        "status": "done",
        "pairs": len(pairs),
        "thresholds": {"body_alone": BODY_ALONE, "body_with_name": BODY_WITH_NAME, "name": NAME},
        "missing_before": len(missing),
        "missing_after": len(out["missing_node_ids"]),
    }
    return out
