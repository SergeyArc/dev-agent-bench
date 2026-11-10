"""Read-only access to the infrastructure snapshot of a bench step.

`bench-snapshot` exports `/app` after every step as a git bundle
(`steps/step-N/artifacts/logs/artifacts/snapshot-step-N.bundle`) and records the
snapshot commit in `snapshot-step-N.json`. The bundle of step N carries the whole
history, so the previous snapshot and the initial commit are in it as well.

Nothing is written next to the runs: the bundle is fetched into a temporary bare
repository. Only `git` is needed — no Docker, no checkout of the agent's code.
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import tempfile
from pathlib import Path
from types import TracebackType
from typing import Self


class SnapshotError(RuntimeError):
    """The snapshot of a step is missing or unreadable."""


def artifact_paths(step_dir: Path, step: int) -> tuple[Path, Path]:
    base = step_dir / "artifacts" / "logs" / "artifacts"
    return base / f"snapshot-step-{step}.json", base / f"snapshot-step-{step}.bundle"


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Glob match where `**/` also matches no directory at all."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**/", "")):
            return True
    return False


class Snapshot:
    """The working copy of `/app` as committed after one step."""

    def __init__(self, step_dir: Path, step: int) -> None:
        meta_path, self.bundle = artifact_paths(step_dir, step)
        if not meta_path.is_file():
            raise SnapshotError(f"no snapshot metadata: {meta_path}")
        self.meta = json.loads(meta_path.read_text())
        self.commit: str = self.meta.get("commit") or ""
        if not self.bundle.is_file():
            reason = self.meta.get("error") or "bundle file is missing"
            raise SnapshotError(f"no snapshot bundle for step {step}: {reason}")
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Self:
        self._tmp = tempfile.TemporaryDirectory(prefix="bench-snapshot-")
        self._git("init", "-q", "--bare", ".")
        self._git("fetch", "-q", str(self.bundle), "HEAD")
        fetched = self._git("rev-parse", "FETCH_HEAD").strip()
        if self.commit and fetched != self.commit:
            raise SnapshotError(f"bundle HEAD {fetched} != recorded snapshot commit {self.commit}")
        self.commit = fetched
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()

    def _git_bytes(self, *args: str) -> bytes:
        if self._tmp is None:
            raise SnapshotError("snapshot is not open")
        done = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self._tmp.name,
            capture_output=True,
            check=False,
        )
        if done.returncode != 0:
            raise SnapshotError(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip()}")
        return done.stdout

    def _git(self, *args: str) -> str:
        return self._git_bytes(*args).decode(errors="replace")

    def initial_commit(self) -> str:
        """The commit `/app` was initialised with (the root of the history)."""
        return self._git("rev-list", "--max-parents=0", self.commit).split()[-1]

    def files(self, rev: str | None = None) -> list[str]:
        return self._git("ls-tree", "-r", "--name-only", rev or self.commit).splitlines()

    def read(self, path: str, rev: str | None = None) -> str:
        return self._git("show", f"{rev or self.commit}:{path}")

    def extract(self, dest: Path, prefix: str) -> list[str]:
        """Write the files of the snapshot under `prefix` into `dest` as plain files.

        Nothing is checked out or executed; a symlink is written as a file holding its target.
        """
        written = [p for p in self.files() if p.startswith(prefix)]
        for path in written:
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._git_bytes("show", f"{self.commit}:{path}"))
        return written

    def diff(self, base: str, patterns: tuple[str, ...] | list[str]) -> str:
        """Unified diff base..snapshot limited to the files matching `patterns`."""
        changed = self._git("diff", "--name-only", base, self.commit).splitlines()
        paths = [p for p in changed if matches(p, patterns)]
        if not paths:
            return ""
        return self._git("diff", "--no-color", "-M", base, self.commit, "--", *paths)
